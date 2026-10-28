"""对账差异引擎：输入请求书明细 + 合同单价表 + 本月计划表，输出带定位的差异清单。

设计约束（来自群评审）：
- 合同单价表重复键必须报错，绝不静默覆盖（按月分段、按生效日调整很常见）。
- 「缺失行」不能拿合同表全量判定 —— 合同表是全量清单，不是本月应出账清单。
  没有计划表时明确标注该项不可用，不产出虚假缺失行。
- 日期解析失败要报「日期无法解析」，不能默默放过。
"""

from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path

from rulebook import (
    DIFF_AMOUNT_MISMATCH,
    DIFF_BEFORE_CONTRACT,
    DIFF_DATE_RANGE,
    DIFF_DATE_UNPARSABLE,
    DIFF_EXTRA_ROW,
    DIFF_MISSING_ROW,
    DIFF_NO_CONTRACT,
    DIFF_TAX,
    DIFF_UNIT_PRICE,
    NEVER_AUTO_CONFIRM,
    REASON_BEFORE_CONTRACT,
    Rulebook,
)

# 容忍的舍入误差（日元取整）
EPSILON = 1


class ContractDataError(Exception):
    """合同数据本身有歧义，必须让客户修正，不能猜。"""


@dataclass
class LineItem:
    """请求书里的一行明细。row_no 是原始的 1 起算行号，用于定位。"""

    row_no: int
    customer_name: str
    item_name: str
    quantity: float
    unit_price: float
    amount: float
    service_date: str  # ISO 格式，可能是空串


@dataclass
class ContractRate:
    """合同单价表里的一行：某客户某项目的约定单价。"""

    customer_name: str
    item_name: str
    unit_price: float
    effective_from: str = ""  # 可选，ISO 日期；空串表示不限


class DiffStatus(str, Enum):
    """差异判定状态（三态）。

    NEW              没见过，或金额大幅变动 -> 要人看
    CONFIRMED_SAME   确认过且金额在约定阈值内 -> 不打扰
    RECURRING        上次采纳了修改，本次同样的错又原样出现 -> 重复出错，要人看

    曾经有第四态 CONFIRMED_DIVERGED（「已确认过·但与上次判定不符」），
    在阈值方案落地后被证明**永远产不出来**：
    - 金额超阈值 -> last_verdict 返回 None -> NEW
    - 金额在阈值内 -> 数值没变，谈不上「与上次判定不符」
    两条路径都到不了它。对外挂一个永远不出现的状态，等于卖不存在的东西，
    所以删除。这是 Researcher 推出来的，Chief 定为硬性要求：跑不出来就改三态。
    """

    NEW = "需确认"
    CONFIRMED_SAME = "已确认过·建议不变"
    RECURRING = "上次已改，本次再现"

    @property
    def label(self) -> str:
        return self.value


@dataclass
class Diff:
    """一条差异。

    fingerprint 由「情境」决定，必须包含能区分同一种情况的具体数值，
    否则一次确认会泛化到该客户该品目的所有同类差异，掩盖后续真错单。
    """

    diff_type: str
    row_no: int
    locator: str
    original: str
    suggested: str
    confidence: float
    fingerprint: str
    # 三级判定
    status: DiffStatus = DiffStatus.NEW
    known_verdict: str | None = None
    # 退回重看的原因（仅在 status=NEW 且命中过规则时有值）：
    # 「前回金額不明」= 旧记录没有金额基准；「金額変動」= 超过约定阈值
    unknown_reason: str = ""
    # 该差异涉及的行金额（不参与指纹）
    amount: float | None = None
    # 阈值比较用的数值。**不同差异类型语义不同，这是刻意的**：
    #   单价差异 -> 单价（1,300），因为要看的是"价变没变"
    #   其余类型 -> 行金额（数量 × 单价）
    # 曾经只有一个 amount 字段，确认时存单价、对账时传合计，
    # 两个数不同量纲，导致单价差异这条最核心的路径规则簿永远命中不了。
    # 字段名说清楚它是什么，就不会再有人填错。
    compare_amount: float | None = None

    @property
    def needs_human(self) -> bool:
        return self.status is not DiffStatus.CONFIRMED_SAME


# 兼容别名，供报告层按名引用
STATUS_NEED_HUMAN = DiffStatus.NEW
STATUS_CONFIRMED_SAME = DiffStatus.CONFIRMED_SAME


def _fmt(v: float) -> str:
    """金额按日元的习惯写法，整数不带小数。"""
    if float(v).is_integer():
        return f"{int(v):,}"
    return f"{v:,.2f}"


def _fp(*parts: object) -> str:
    """由差异的情境特征推导指纹。

    关键：必须带上具体数值（单价、金额），否则「保留原值」这类判定
    会泛化到该客户该品目的任意金额，把真正的错单标成已确认。
    """
    return "|".join(str(p) for p in parts)


# 品目名 / 利用者名的归一化。
# 不补这条，第一单就会被虚假的缺失行刷屏：
#   「訪問介護（身体介護1)」vs「訪問介護（身体介護１)」
#   「訪問介護（身体介護1）」vs「訪問介護 （身体介護1)」
#   半角括号 ) vs 全角 ）
#   「鈴木 次郎」vs「鈴木　次郎」(全角空格)
# NFKC 会把全角数字/括号/空格统一成半角，但不能处理「介護」和「介护」
# 这类繁简差异，也不能处理中间的空格，所以还要额外收口。

# 这些写法差异在业务上是同一个东西，归一化后应当相等
_TRAILING_HONORIFIC = re.compile(r"(様|さま|さん|殿)$")


def norm_name(s: str) -> str:
    """利用者名归一化：全半角统一、去所有空白、去敬称。"""
    if not s:
        return ""
    t = unicodedata.normalize("NFKC", s)
    t = re.sub(r"\s+", "", t)
    t = _TRAILING_HONORIFIC.sub("", t)
    return t


def norm_item(s: str) -> str:
    """品目名归一化：全半角统一、去所有空白。

    只做「写法」归一，不做语义映射——「身体介護1」和「身体介護2」
    是不同的品目，绝不能合并。
    """
    if not s:
        return ""
    t = unicodedata.normalize("NFKC", s)
    t = re.sub(r"\s+", "", t)
    return t


def item_key(customer_name: str, item_name: str) -> tuple[str, str]:
    """归一化后的 (利用者, 品目) 键，所有比对都用它。"""
    return (norm_name(customer_name), norm_item(item_name))


def load_line_items(path: str | Path) -> list[LineItem]:
    items: list[LineItem] = []
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader, start=1):
            items.append(
                LineItem(
                    row_no=idx,
                    customer_name=row["利用者名"].strip(),
                    item_name=row["品目"].strip(),
                    quantity=float(row["数量"] or 0),
                    unit_price=float(row["単価"] or 0),
                    amount=float(row["金額"] or 0),
                    service_date=(row.get("サービス日") or "").strip(),
                )
            )
    return items


def load_contract_rates(path: str | Path) -> list[ContractRate]:
    """加载合同单价表。

    同一 (利用者名, 品目) 出现多行时必须报错：
    - 真实合同常按生效日分段（改单价很常见），带 effective_from 且生效日互不相同是合法的，
      此时保留所有分段，由 resolve_rate 按服务日取对应单价。
    - 完全没有 effective_from 时出现重复，说明数据有歧义，报错让客户补生效日，
      绝不静默覆盖。
    - 生效日完全相同，说明同一时点有两个价，同样报错。

    关于「分段重叠」：按生效日分段的语义本来就是「从该日起生效，直到下一段」，
    所以区间天然是首尾相接而非互相独立，「重叠」本身是正常业务（涨价、后来又改回去）。
    真正会替客户选价而不报错的情况只有两种，都已拦下：
    ① 同一生效日出现两次（无法确定谁先谁后）
    ② 完全没有生效日却有多行（无法排序）
    """
    rates: list[ContractRate] = []
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for lineno, row in enumerate(reader, start=2):
            eff = (row.get("適用開始日") or "").strip()
            if eff:
                # 生效日必须是可比较的 ISO 日期，否则排序结果不可信
                try:
                    date.fromisoformat(eff)
                except ValueError:
                    raise ContractDataError(
                        f"合同单价表第 {lineno} 行的「適用開始日」不是合法日期：{eff!r}。"
                        f"请统一为 YYYY-MM-DD 后再跑。"
                    ) from None
            rates.append(
                ContractRate(
                    customer_name=row["利用者名"].strip(),
                    item_name=row["品目"].strip(),
                    unit_price=float(row["契約単価"] or 0),
                    effective_from=eff,
                )
            )

    # 重复键检查（用归一化键，否则「身体介護1」和「身体介護１」会被当成两个品目）
    seen: dict[tuple[str, str], list[ContractRate]] = {}
    for r in rates:
        seen.setdefault(item_key(r.customer_name, r.item_name), []).append(r)

    for key, rows in seen.items():
        if len(rows) <= 1:
            continue
        cust, item = rows[0].customer_name, rows[0].item_name
        # 多行只允许「都带生效日」并且生效日互不相同
        effs = [r.effective_from for r in rows]
        if any(not e for e in effs):
            raise ContractDataError(
                f"合同单价表存在重复键：{cust} / {item} 出现 {len(rows)} 行，"
                f"且缺少「適用開始日」无法区分先后。"
                f"请补生效日后再跑，不能替客户猜该用哪一行。"
            )
        if len(set(effs)) != len(effs):
            raise ContractDataError(
                f"合同单价表存在重复键：{cust} / {item} 有相同的適用開始日：{effs}。"
                f"无法确定以哪一行为准，请客户修正。"
            )
    return rates


class RateStatus(str, Enum):
    """resolve_rate 的四种结果。"""

    FOUND = "取到单价"
    NO_CONTRACT = "合同无此品目"
    BEFORE_ALL = "服务日早于所有分段"
    AMBIGUOUS = "无法唯一确定分段"


@dataclass
class RateLookup:
    """查价结果。price 仅在 FOUND 时有意义。"""

    status: RateStatus
    price: float | None = None
    earliest_from: str = ""


def build_rate_index(rates: list[ContractRate]) -> dict[tuple[str, str], list[ContractRate]]:
    """按归一化后的 (客户, 品目) 建索引，值是按生效日排好序的分段列表。"""
    idx: dict[tuple[str, str], list[ContractRate]] = {}
    for r in rates:
        idx.setdefault(item_key(r.customer_name, r.item_name), []).append(r)
    for item_list in idx.values():
        item_list.sort(key=lambda r: r.effective_from or "")
    return idx


def resolve_rate(
    index: dict[tuple[str, str], list[ContractRate]],
    customer: str,
    item: str,
    on_date: str,
) -> RateLookup:
    """查某个服务日对应的合同单价。

    四种结果必须区分开，否则会静默跳过该最该报的错：
    - FOUND        取到单价
    - NO_CONTRACT  合同里根本没这个 (利用者, 品目)
    - BEFORE_ALL   服务日早于该品目所有分段（客户拿旧期间数据套了新合同）-> 要报
    - AMBIGUOUS    无日期或有日期但无法唯一确定分段 -> 交给警告
    """
    segs = index.get(item_key(customer, item))
    if not segs:
        return RateLookup(RateStatus.NO_CONTRACT)
    if not on_date:
        # 无日期时，只有单一分段才无歧义
        if len(segs) == 1:
            return RateLookup(RateStatus.FOUND, segs[0].unit_price)
        return RateLookup(RateStatus.AMBIGUOUS)

    chosen = None
    for seg in segs:
        if not seg.effective_from or seg.effective_from <= on_date:
            chosen = seg
        else:
            break
    if chosen is None:
        # 服务日早于所有分段的生效日 —— 这是错，不是数据质量问题
        earliest = min((s.effective_from for s in segs if s.effective_from), default="")
        return RateLookup(RateStatus.BEFORE_ALL, earliest_from=earliest)
    return RateLookup(RateStatus.FOUND, chosen.unit_price)


def load_plan(path: str | Path) -> set[tuple[str, str]]:
    """加载本月计划应出账项（归一化键）。没有这个文件就不能判缺失行。"""
    plan, _ = load_plan_with_display(path)
    return plan


def load_plan_with_display(
    path: str | Path,
) -> tuple[set[tuple[str, str]], dict[tuple[str, str], tuple[str, str]]]:
    """加载计划表。

    返回 (归一化键集合, 归一化键 -> 原始写法)。
    比对用归一化键，展示用客户自己的写法 —— 客户看到「身体介護１」
    被改写成「身体介護1」会觉得我们在动他的数据。
    """
    plan: set[tuple[str, str]] = set()
    display: dict[tuple[str, str], tuple[str, str]] = {}
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            c, i = row["利用者名"].strip(), row["品目"].strip()
            key = item_key(c, i)
            plan.add(key)
            display.setdefault(key, (c, i))
    return plan, display


def _period_of(iso_date: str) -> str | None:
    """返回日期所属 YYYY-MM；空串或无法解析返回 None。"""
    if not iso_date:
        return None
    try:
        return date.fromisoformat(iso_date).strftime("%Y-%m")
    except ValueError:
        return None


def reconcile(
    items: list[LineItem],
    rate_index: dict[tuple[str, str], list[ContractRate]],
    period: str,
    rulebook: Rulebook | None = None,
    plan: set[tuple[str, str]] | None = None,
    plan_display: dict[tuple[str, str], tuple[str, str]] | None = None,
) -> tuple[list[Diff], list[str]]:
    """执行对账。

    返回 (差异列表, 警告列表)。plan 为 None 时不判缺失行，并在 warnings 里说明，
    绝不拿合同全量造出假的缺失行。

    注意：所有键比对都走 item_key() 归一化（全半角、空白、敬称），
    否则「身体介護1」vs「身体介護１」会被误判成缺失行 + 多余行两条。
    """
    diffs: list[Diff] = []
    warnings: list[str] = []

    for it in items:
        # 定位串直接产出日文：报告层不做正则回翻。
        # 之前是引擎产中文、报告层转日文，只要漏一处调用就退回原样。
        locator = f"{it.row_no} 行目 {it.customer_name} / {it.item_name}"
        lookup = resolve_rate(rate_index, it.customer_name, it.item_name, it.service_date)
        rate = lookup.price if lookup.status is RateStatus.FOUND else None

        # 0) 合同未约定（清单 v1 第 7 类）
        #    只在「合同里确实没有这个 (客户, 品目)」时报。
        #    指纹只保留 (客户, 品目)，不带金额 —— 第 7 类是被判为正常的长期合同外服务，
        #    金额每月都会变，带进指纹会让规则永远学不会。
        if lookup.status is RateStatus.NO_CONTRACT:
            fp = _fp("no-contract", *item_key(it.customer_name, it.item_name))
            diffs.append(
                Diff(
                    diff_type=DIFF_NO_CONTRACT,
                    row_no=it.row_no,
                    locator=locator,
                    original=f"単価 {_fmt(it.unit_price)} / 金額 {_fmt(it.amount)}",
                    suggested="合同单价表中无此（利用者, 品目），请确认是合同外服务还是品目名写错",
                    confidence=0.6,
                    fingerprint=fp,
                    amount=it.amount,
                    compare_amount=it.amount,
                )
            )
        elif lookup.status is RateStatus.BEFORE_ALL:
            # 第 7 类的一种子原因：合同里**有这个品目**，但服务日早于所有分段的生效日，
            # 典型情形是客户拿旧期间数据套了新合同。与「合同里根本没这个品目」区分开：
            # 前者一定是错，后者可能正常（合同外服务）。
            # 对外口径仍是 8 类（见 rulebook.ALL_DIFF_TYPES），此处 diff_type 用独立常量
            # 是为了让报告和 gate 能区分两种子原因。
            #
            # 指纹前缀刻意与第 7 类区分：两者语义相反。
            # 甲「合同没这品目」= 可能正常，确认后该学会不再问；
            # 乙「日期早于起点」= 一定是错，永远不该被记住成「已确认正常」。
            # 共用前缀现在靠 diff_type 才没串，一旦有人统一类型就会把乙永久放过。
            fp = _fp("before-contract", *item_key(it.customer_name, it.item_name))
            diffs.append(
                Diff(
                    diff_type=DIFF_BEFORE_CONTRACT,
                    row_no=it.row_no,
                    locator=locator,
                    original=f"サービス日 {it.service_date} / 単価 {_fmt(it.unit_price)}",
                    suggested=(
                        f"【{REASON_BEFORE_CONTRACT}】合同最早从 {lookup.earliest_from} 生效，"
                        f"该服务日早于合同起点；请确认是否套用了其他期间的单价"
                    ),
                    confidence=0.9,
                    fingerprint=fp,
                    amount=it.amount,
                    compare_amount=it.amount,
                )
            )
        elif lookup.status is RateStatus.AMBIGUOUS:
            # 合同有此项，但这个日期取不到唯一单价（分段歧义或日期缺失/异常）
            warnings.append(
                f"{it.row_no} 行目 {it.customer_name} / {it.item_name}："
                f"日期「{it.service_date or '空'}」无法确定对应哪段合同单价，本次跳过单价核对"
            )

        # 1) 单价与合同不一致 —— 指纹带上原单价和合同单价
        if rate is not None and abs(it.unit_price - rate) > EPSILON:
            fp = _fp("unitprice", it.customer_name, it.item_name, _fmt(it.unit_price), _fmt(rate))
            diffs.append(
                Diff(
                    diff_type=DIFF_UNIT_PRICE,
                    row_no=it.row_no,
                    locator=locator,
                    original=f"単価 {_fmt(it.unit_price)}",
                    suggested=f"契約単価 {_fmt(rate)}（金額 {_fmt(it.quantity * rate)}）",
                    confidence=0.95,
                    fingerprint=fp,
                    amount=it.amount,
                    compare_amount=it.unit_price,
                )
            )

        # 2) 金额不符：数量 × 单价 ≠ 金额 —— 指纹带上数量与两个金额
        computed = it.quantity * it.unit_price
        if abs(computed - it.amount) > EPSILON:
            fp = _fp(
                "amount",
                it.customer_name,
                it.item_name,
                f"{it.quantity:g}",
                _fmt(it.amount),
                _fmt(computed),
            )
            diffs.append(
                Diff(
                    diff_type=DIFF_AMOUNT_MISMATCH,
                    row_no=it.row_no,
                    locator=locator,
                    original=f"金額 {_fmt(it.amount)}",
                    suggested=f"数量×単価 = {_fmt(computed)}（差 {_fmt(computed - it.amount)}）",
                    confidence=0.99,
                    fingerprint=fp,
                    amount=it.amount,
                    compare_amount=it.amount,
                )
            )

        # 3) 日期：区分「无法解析」和「不在期间内」，两类都不放过
        #    若该行已经报了「日期早于合同生效」，不再重复报「不在期间内」——
        #    两者说的是同一件事（这个日期不该在这里），报两遍客户会以为数多了。
        already_reported_before_contract = lookup.status is RateStatus.BEFORE_ALL
        actual_period = _period_of(it.service_date) if it.service_date else None
        if it.service_date and actual_period is None:
            fp = _fp("date-unparsable", it.customer_name, it.item_name, it.service_date)
            diffs.append(
                Diff(
                    diff_type=DIFF_DATE_UNPARSABLE,
                    row_no=it.row_no,
                    locator=locator,
                    original=f"サービス日 {it.service_date}",
                    suggested="无法解析，请客户确认为 YYYY-MM-DD",
                    confidence=1.0,
                    fingerprint=fp,
                )
            )
        elif (
            actual_period is not None
            and actual_period != period
            and not already_reported_before_contract
        ):
            fp = _fp("date-range", it.customer_name, it.item_name, actual_period)
            diffs.append(
                Diff(
                    diff_type=DIFF_DATE_RANGE,
                    row_no=it.row_no,
                    locator=locator,
                    original=f"サービス日 {it.service_date}",
                    suggested=f"対象期間 {period} 内の日付",
                    confidence=0.9,
                    fingerprint=fp,
                )
            )

    # 4) 缺失行：只按「本月计划应出账项」判定，不用合同全量
    if plan is None:
        warnings.append(
            "未提供本月计划表（計画明細），缺失行（漏记）与多余行检查本次不可用。"
            "合同单价表是全量清单，不能用来推断本月应出账项。"
        )
    else:
        seen_keys = {item_key(it.customer_name, it.item_name) for it in items}
        # 缺失行展示用原始写法：归一化键只用于比对，客户看的是自己的字
        display = plan_display or {}

        for key in sorted(plan - seen_keys):
            cust, item = display.get(key, key)
            fp = _fp("missing", *key)
            # 缺失行也要给金额，否则它不参与阈值判断：
            # 客户确认过一次「这行本来就该缺」，之后金额怎么变都不再重问，
            # 和第 7 类原来那个洞一模一样。
            # 计划表本身没有金额，用该品目当期合同单价推算（取期间首日查价）。
            period_start = f"{period}-01"
            unit = resolve_rate(rate_index, cust, item, period_start)
            planned_amount = unit.price if unit.status is RateStatus.FOUND else None
            diffs.append(
                Diff(
                    diff_type=DIFF_MISSING_ROW,
                    row_no=0,
                    locator=f"未計上 {cust} / {item}",
                    original="（请求书内无此行）",
                    suggested=(
                        f"本月计划应出账，但请求书中未出现"
                        + (f"（按契约単価 {_fmt(planned_amount)} 计）"
                           if planned_amount is not None else "")
                    ),
                    confidence=0.7,
                    fingerprint=fp,
                    amount=planned_amount,
                    compare_amount=planned_amount,
                )
            )

        # 5) 多余行（清单 v1 第 4 类）：请求书里有、计划表里没有。
        #    必须给出具体理由 —— 「这家伙多收钱」和「这项本月不用出」
        #    客户反应完全不同，混淆会直接得罪客户。
        for it in items:
            key = item_key(it.customer_name, it.item_name)
            if key in plan:
                continue
            # 合同里也没有的，已经在第 7 类「合同未约定」报过，
            # 这里再报一次会让客户以为是两个问题。
            if key not in rate_index:
                continue
            reasons: list[str] = ["不在本月计划内"]
            if it.service_date and _period_of(it.service_date) not in (None, period):
                reasons.append(f"日期属 {_period_of(it.service_date)}，不属本期间")
            fp = _fp("extra", *key, _fmt(it.quantity), _fmt(it.amount))
            diffs.append(
                Diff(
                    diff_type=DIFF_EXTRA_ROW,
                    row_no=it.row_no,
                    locator=f"{it.row_no} 行目 {it.customer_name} / {it.item_name}",
                    original=f"数量 {it.quantity:g} / 金額 {_fmt(it.amount)}",
                    suggested="；".join(reasons),
                    confidence=0.75,
                    fingerprint=fp,
                    amount=it.amount,
                    compare_amount=it.amount,
                )
            )

    # 三级判定：不直接 needs_human=False，要和上次判定比对
    if rulebook is not None:
        for d in diffs:
            # 第 7、8 类不参与三级判定，永远 NEW（清单 v1 第 94 行）
            if d.diff_type in NEVER_AUTO_CONFIRM:
                d.status = STATUS_NEED_HUMAN
                continue
            # 必须带上金额：指纹靠不含金额才能跨月命中，但命中后还要比一次金额，
            # 否则 1,830 被认可后 183,000 也会被判成「已确认·建议不变」——假正常。
            # 用 compare_amount 而不是 amount：单价差异要比的是单价，
            # 用行金额会因数量变化误判（曾经就是这样让这条核心路径永远命中不了）。
            last = rulebook.last_verdict(
                d.diff_type, d.fingerprint, amount=d.compare_amount
            )
            if last is None:
                d.status = STATUS_NEED_HUMAN
                # 退回原因要让客户在报告上看到，否则会觉得规则簿突然失效
                d.unknown_reason = getattr(rulebook, "last_unknown_reason", "")
                continue
            d.known_verdict = last
            d.status = _classify(last)

    diffs.sort(key=lambda d: (d.row_no, d.diff_type))
    # 去重：同一原因可能逐行触发，摘要里只留一条
    seen_w: set[str] = set()
    uniq_warnings: list[str] = []
    for w in warnings:
        if w not in seen_w:
            seen_w.add(w)
            uniq_warnings.append(w)
    return diffs, uniq_warnings


def _classify(last_verdict: str) -> DiffStatus:
    """命中历史规则时的判定。

    指纹已经带了具体数值，所以能命中就意味着「同一个点、数值也一样」。
    - 上次「采纳建议值」→ 上个月这个错被改掉了，本次同样的错原样又出现，
      这是「同一个错误在犯第二次」，标 RECURRING（上次已改，本次再现）。
      不能说成「与上次判定不符」—— 客户会以为是自己改了主意。
    - 上次「保留原值」→ 客户明确认可这个值，本次同样值出现属正常。
    """
    from rulebook import VERDICT_ACCEPT_SUGGESTED

    if last_verdict == VERDICT_ACCEPT_SUGGESTED:
        return DiffStatus.RECURRING
    return DiffStatus.CONFIRMED_SAME
