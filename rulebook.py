"""客户核对规则的持久化规则簿。

设计要点：
- 规则不是写进代码的分支，而是每个客户一份的 JSON 数据。
- 每条规则由「人工确认」动作追加产生，只增不改，可回溯。
- 规则键 = (差异类型, 特征指纹)，其中特征指纹由差异本身推导，
  这样第二个月出现同一类差异时能命中并给出「已确认过」的提示。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# 差异类型清单（原型阶段先落地其中稳定部分）
DIFF_AMOUNT_MISMATCH = "金额不符"
DIFF_MISSING_ROW = "缺失行"
DIFF_EXTRA_ROW = "多余行"
DIFF_UNIT_PRICE = "单价与合同不一致"
DIFF_TAX = "税额计算错误"
DIFF_DATE_RANGE = "日期不在期间内"
DIFF_DATE_UNPARSABLE = "日期无法解析"
DIFF_NO_CONTRACT = "合同未约定"
# 「日期早于合同生效」是第 7 类的一种子原因，但保留独立常量名：
# 报告层要能区分「可能正常（合同外服务）」和「一定是错（旧单套新合同）」，
# 对外口径仍是 8 类，两类共用 DIFF_NO_CONTRACT 作为 diff_type。
DIFF_BEFORE_CONTRACT = "日期早于合同生效"
# 子原因标记：diff_type 相同、判定理由不同
REASON_BEFORE_CONTRACT = "合同未覆盖该日期"

ALL_DIFF_TYPES = [
    DIFF_AMOUNT_MISMATCH,
    DIFF_MISSING_ROW,
    DIFF_EXTRA_ROW,
    DIFF_UNIT_PRICE,
    DIFF_TAX,
    DIFF_DATE_RANGE,
    DIFF_DATE_UNPARSABLE,
    DIFF_NO_CONTRACT,
]

# 代码内部必须能处理的全集（含子原因类型）。
# 任何按类型遍历/校验的地方都应该用这个，而不是 ALL_DIFF_TYPES，
# 否则「日期早于合同生效」会被漏掉——它对外算在第 7 类里，但 diff_type 是独立值。
ALL_INTERNAL_DIFF_TYPES = [*ALL_DIFF_TYPES, DIFF_BEFORE_CONTRACT]

# 不参与三级判定、永远 NEW 的类型（清单 v1 第 94 行）
#
# 注意：DIFF_NO_CONTRACT 曾经在这里，是错的。
# 第 7 类「合同未约定」的指纹只含 (客户, 品目)，不带金额，本来就是为了让它能被记住——
# 一旦被判为「长期合作的合同外服务」，下个月不该再问一遍。
# 放进 NEVER_AUTO_CONFIRM 会让它永远学不会，等于自相矛盾。
# 真正的约束是：它首次必须人工确认（NEW），确认后才可能变成已知。
# 日期无法解析仍保持永远 NEW：那是数据格式问题，格式没修就不该被记住。
NEVER_AUTO_CONFIRM = {DIFF_DATE_UNPARSABLE}

# 人工对一条差异可以做的判定
VERDICT_ACCEPT_SUGGESTED = "采纳建议值"
VERDICT_KEEP_ORIGINAL = "保留原值"


@dataclass
class RuleRecord:
    """一条规则记录：由一次人工确认产生。

    关键约束：fingerprint 必须带够情境维度（客户/品目/具体数值），
    否则一次「保留原值」会泛化到该客户该品目下任意金额，
    把后续真正的错单标成「已确认过」而漏掉。
    """

    diff_type: str
    fingerprint: str
    verdict: str
    # 该规则生效的月份，格式 YYYY-MM
    effective_from: str
    # 客户当时填写的说明，便于下个月回看为什么这么判
    note: str = ""
    # 产生该规则的月份，用于时间线展示
    created_in: str = ""
    # 确认当时这笔的金额。指纹刻意不含金额（否则规则永远学不会），
    # 但金额必须单独存下来做阈值判断，否则 1,830 被认可后 183,000 也会被放过。
    amount: float | None = None


# 金额变动阈值（三档口径）：
#   1. 变动金额 > 10,000 日元          -> 退回重看，不看百分比（挡大额漏收）
#   2. 否则，幅度 > 20% 且变动 > 1,000 -> 退回重看（挡小额被比例放大）
#   3. 其余                            -> 不打扰（小额日常波动）
# 只有一条比例阈值是不够的：500->620 涨 24% 但只差 120 日元，不该打扰客户；
# 而 183,000->150,000 只跌 18%，却漏收 33,000 日元，必须退回。
AMOUNT_THRESHOLD_ABS = 10_000
AMOUNT_THRESHOLD_MIN_DELTA = 1_000
RATIO_THRESHOLD = 0.2


def needs_review(prev: float, now: float) -> bool:
    """金额变动是否要退回重看（True = 不沿用上次判定）。"""
    delta = abs(now - prev)
    if delta > AMOUNT_THRESHOLD_ABS:
        return True
    base = abs(prev)
    if base == 0:
        return delta > 0
    if delta > AMOUNT_THRESHOLD_MIN_DELTA and delta / base > RATIO_THRESHOLD:
        return True
    return False


# 退回重看的原因。客户看到的报告要说明「为什么这次又要确认」，
# 否则会觉得规则簿突然失效了。
REASON_AMOUNT_UNKNOWN = "前回金額不明"  # 升级前的旧记录没有金额基准
REASON_AMOUNT_CHANGED = "金額変動"      # 金额变动超过约定阈值


@dataclass
class Rulebook:
    """一个客户一份的规则簿。"""

    customer: str
    records: list[RuleRecord] = field(default_factory=list)
    # last_verdict 最近一次返回 None 的原因（供报告层显示）
    last_unknown_reason: str = ""

    def _match(self, diff_type: str, fingerprint: str) -> list[RuleRecord]:
        return [
            r
            for r in self.records
            if r.diff_type == diff_type and r.fingerprint == fingerprint
        ]

    def is_known(self, diff_type: str, fingerprint: str) -> bool:
        """这一类差异是否已被人工确认过。"""
        return bool(self._match(diff_type, fingerprint))

    def last_verdict(
        self,
        diff_type: str,
        fingerprint: str,
        amount: float | None = None,
    ) -> str | None:
        """返回可沿用的上次判定；金额超出阈值时返回 None（等于重新问）。

        指纹不含金额是为了让规则能跨月命中，但命中之后必须再比一次金额：
        「上次 1,830 被认可」不等于「这次 183,000 也可以」。
        这一层是必须防住的假正常场景，不能省。

        金额缺失时不静默沿用，而是报错：
        以前这里 return last.verdict，调用方漏传参数也不会有任何提示，
        threshold_check.py 就是这么把 3 个用例测成"碰巧通过"的。
        让错误暴露，比让错误沉默好。
        """
        matches = self._match(diff_type, fingerprint)
        self.last_unknown_reason = ""
        if not matches:
            return None
        last = matches[-1]
        if amount is None:
            raise ValueError(
                f"比较规则命中时必须传 amount，否则无法判断金额是否大幅变动。"
                f"diff_type={diff_type!r} fingerprint={fingerprint!r}。"
                f"（漏传参数会让大额变动被静默放过，这正是要防的假正常）"
            )
        if last.amount is None:
            # 旧版规则簿没有金额基准（升级前的记录）。
            # 口径：一律当作「上次金额不明」退回需确认，
            # 绝不跳过阈值检查 —— 放过一次 183,000 的代价，和多问一次不对等。
            # 也不能把默认值填 0：那会让所有旧规则集体退回，看起来像规则簿坏了。
            self.last_unknown_reason = REASON_AMOUNT_UNKNOWN
            return None
        if needs_review(last.amount, amount):
            self.last_unknown_reason = REASON_AMOUNT_CHANGED
            return None
        return last.verdict

    def confirm(
        self,
        diff_type: str,
        fingerprint: str,
        verdict: str,
        period: str,
        note: str = "",
        amount: float | None = None,
    ) -> RuleRecord:
        """记录一次人工确认，返回产生的规则。

        这里刻意不做去重：同一指纹被确认多次是有信息量的
        （说明客户改主意了），保留时间线才能回溯。

        **amount 必须是「行金额」，不是单价。**
        曾经出过这个 bug：确认时调用方传单价 1,300，对账时引擎传的是行金额
        6,500（数量 5 × 单价 1,300），两个数不同量纲，差距 400%，
        被阈值判成「金额大幅变动」，导致客户确认过的单价差异次月又变回要确认。
        调用方请优先用 `confirm_diff(diff, verdict, period)`，它会自动取对金额。
        """
        record = RuleRecord(
            diff_type=diff_type,
            fingerprint=fingerprint,
            verdict=verdict,
            effective_from=period,
            note=note,
            created_in=period,
            amount=amount,
        )
        self.records.append(record)
        return record

    def confirm_diff(self, diff, verdict: str, period: str, note: str = "") -> RuleRecord:
        """按一条 Diff 确认，自动带上正确的金额基准。

        优先用这个，别用 confirm() —— 手动传 amount 很容易传错量纲。

        存的是 `compare_amount` 而不是 `amount`：
        单价差异的 amount 是行合计（数量×单价），而它要和下次的单价比较，
        两个不同量纲的数字放一起比，会导致这条路径永远命中不了。
        """
        compare = getattr(diff, "compare_amount", None)
        return self.confirm(
            diff.diff_type,
            diff.fingerprint,
            verdict,
            period,
            note=note,
            amount=compare,
        )

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "customer": self.customer,
            "records": [
                {
                    "diff_type": r.diff_type,
                    "fingerprint": r.fingerprint,
                    "verdict": r.verdict,
                    "effective_from": r.effective_from,
                    "note": r.note,
                    "created_in": r.created_in,
                    "amount": r.amount,
                }
                for r in self.records
            ],
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Rulebook":
        path = Path(path)
        if not path.exists():
            return cls(customer=path.stem)
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            customer=raw.get("customer", path.stem),
            records=[RuleRecord(**r) for r in raw.get("records", [])],
        )
