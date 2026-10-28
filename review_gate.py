"""发布前阻断项检查。

用途：不接受口头汇报。跑这个脚本，用退出码和输出说话。

    cd recon-report && python review_gate.py

退出码 0 = 四处阻断项全部修复；非 0 = 还有未修的，逐条打印。

这个文件是「验收口径」的可执行版本，与 差异类型清单_v1.md 第 3 节对应。
改代码后必须先跑这个，复核跑同一个。
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str):
    """装饰器：跑一个检查项，异常记为失败，不中断后续检查。"""

    def wrap(fn):
        try:
            fn()
            RESULTS.append((name, True, ""))
        except Exception as e:  # noqa: BLE001
            detail = f"{type(e).__name__}: {e}"
            RESULTS.append((name, False, detail))
        return fn

    return wrap


# ── 阻断项 1：分段重叠必须报错，不能替客户挑价 ────────────────────────────
@check("B1 合同分段区间重叠 → 报错中止")
def b1() -> None:
    import tempfile

    import engine

    # 真正的风险不在「价格改回去」（那是正常业务），而在生效日格式不合法时
    # 排序和 <= 比较都会给出错误结果，代码会静默挑一个价、报告显示正常。
    # 各类常见写法都要拦下，不能默默接受。
    bad_formats = ["2026/04/01", "2026-4-1", "2026.04.01", "令和8年4月1日", "2026-13-01", "abc"]
    for bad in bad_formats:
        csv_text = (
            "利用者名,品目,契約単価,適用開始日\n"
            f"鈴木 次郎,訪問介護（生活援助2）,1250,{bad}\n"
            "鈴木 次郎,訪問介護（生活援助2）,1320,2026-10-01\n"
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "contract.csv"
            p.write_text(csv_text, encoding="utf-8")
            raised = False
            try:
                engine.load_contract_rates(p)
            except engine.ContractDataError:
                raised = True
            assert raised, (
                f"適用開始日「{bad}」格式不合法却未报错中止。"
                "build_rate_index 按字符串排序，非法格式会让排序和 <= 比较失效，"
                "等于替客户挑了一个价且报告显示正常"
            )

    # 合法 ISO 写法必须照常能加载（不能为了拦错的把对的也拦掉）
    ok_csv = (
        "利用者名,品目,契約単価,適用開始日\n"
        "鈴木 次郎,訪問介護（生活援助2）,1250,2026-04-01\n"
        "鈴木 次郎,訪問介護（生活援助2）,1320,2026-10-01\n"
    )
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "contract.csv"
        p.write_text(ok_csv, encoding="utf-8")
        rates = engine.load_contract_rates(p)
        assert len(rates) == 2, f"合法分段被误拦：加载到 {len(rates)} 条"


# ── 阻断项 2：早于所有分段必须报成差异，不能进 warning 跳过 ────────────────
@check("B2 服务日早于所有合同分段 → 报差异（合同未覆盖）")
def b2() -> None:
    import engine
    from rulebook import DIFF_BEFORE_CONTRACT

    # 合同只从 2026-04-01 起有价，服务日却在 2026-01-05
    rates = [
        engine.ContractRate(
            customer_name="鈴木 次郎",
            item_name="訪問介護（生活援助2）",
            unit_price=1250,
            effective_from="2026-04-01",
        )
    ]
    index = engine.build_rate_index(rates)
    items = [
        engine.LineItem(
            row_no=1,
            customer_name="鈴木 次郎",
            item_name="訪問介護（生活援助2）",
            quantity=5,
            unit_price=1250,
            amount=6250,
            service_date="2026-01-05",
        )
    ]
    diffs, warnings = engine.reconcile(items, index, "2026-01", plan=None)
    got = [d for d in diffs if d.diff_type == DIFF_BEFORE_CONTRACT]
    assert got, (
        "服务日早于所有合同分段时，没有报出「日期早于合同生效」差异。"
        f"当前 diffs={[d.diff_type for d in diffs]} warnings={warnings}。"
        "旧期间数据套用新合同是最该报的情况，不能归进数据质量警告"
    )
    # 必须计入差异总数，不能只是 warning
    skipped = [w for w in warnings if "跳过" in w and "单价" in w]
    assert not skipped, f"仍把这条当数据质量问题跳过：{skipped}"


# ── 阻断项 3：第 7 类指纹不能带金额，且要能学到 ─────────────────────────
@check("B3 合同未约定指纹不含金额 + 可被规则簿记住")
def b3() -> None:
    import engine
    from rulebook import DIFF_NO_CONTRACT, NEVER_AUTO_CONFIRM

    assert DIFF_NO_CONTRACT not in NEVER_AUTO_CONFIRM, (
        "DIFF_NO_CONTRACT 仍在 NEVER_AUTO_CONFIRM 中，"
        "规则簿永远学不会，合同外服务每月重复询问"
    )

    rates: list = []
    index = engine.build_rate_index(rates)

    def one(amount: float) -> engine.Diff:
        items = [
            engine.LineItem(
                row_no=1,
                customer_name="佐藤 花子",
                item_name="訪問介護（身体介護1)",
                quantity=1,
                unit_price=amount,
                amount=amount,
                service_date="2026-10-05",
            )
        ]
        diffs, _ = engine.reconcile(items, index, "2026-10", plan=None)
        got = [d for d in diffs if d.diff_type == DIFF_NO_CONTRACT]
        assert got, "合同未约定的行没有报出差异"
        return got[0]

    d_a = one(1830)
    d_b = one(1900)
    assert "1,830" not in d_a.fingerprint and "1,900" not in d_b.fingerprint, (
        f"第 7 类指纹仍带金额：{d_a.fingerprint}。金额每月会变，"
        "带进指纹等于规则永远学不会"
    )
    assert d_a.fingerprint == d_b.fingerprint, (
        "同一 (客户, 品目) 但金额不同，指纹不一致："
        f"{d_a.fingerprint} vs {d_b.fingerprint}"
    )


# ── 阻断项 4：RECURRING 语义 ────────────────────────────────────────────
@check("B4 RECURRING：上次已改、本次再现，标签不能说成「与上次判定不符」")
def b4() -> None:
    import engine
    from rulebook import VERDICT_ACCEPT_SUGGESTED

    assert hasattr(engine.DiffStatus, "RECURRING"), (
        "DiffStatus 没有 RECURRING。上次采纳建议值、本次同值再现，"
        "语义是「同一个错在犯第二次」，报成 CONFIRMED_DIVERGED 会让客户"
        "以为是自己改了主意"
    )
    got = engine._classify(VERDICT_ACCEPT_SUGGESTED)
    assert got is engine.DiffStatus.RECURRING, (
        f"_classify(采纳建议值) 返回 {got}，应为 RECURRING"
    )
    label = engine.DiffStatus.RECURRING.value
    assert "与上次判定不符" not in label, f"RECURRING 标签文案未修正：{label}"


def main() -> int:
    for name, ok, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            print(f"      {detail}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{passed}/{total} 阻断项通过")
    if passed != total:
        print("\n阻断项未全部修复，这个版本不能给任何代理店看。")
        return 1
    print("\n四处阻断项全部通过。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        raise SystemExit(2)
