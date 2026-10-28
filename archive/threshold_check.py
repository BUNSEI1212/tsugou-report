"""±20% 阈值验证（CodeReviewer 维护）。

Chief 定的规矩：所有「去掉金额、只按客户+品目判断」的指纹，都必须有变动阈值，
超过阈值要退回「需确认」。这个脚本验的就是这件事。

    cd recon-report && python threshold_check.py

退出码 0 = 阈值生效；非 0 = 还有指纹在无条件放过。
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import engine  # noqa: E402
from rulebook import (  # noqa: E402
    DIFF_NO_CONTRACT,
    VERDICT_KEEP_ORIGINAL,
    Rulebook,
)

RESULTS: list[tuple[str, bool, str]] = []


def scenario(prev_amount: float, now_amount: float, verdict: str = VERDICT_KEEP_ORIGINAL):
    """上个月按 prev_amount 确认过一次，这个月金额变成 now_amount，返回本月这条差异。"""
    index = engine.build_rate_index([])  # 合同里没有这个品目 -> 第 7 类

    def one(amount: float, period: str):
        items = [
            engine.LineItem(
                row_no=1,
                customer_name="佐藤 花子",
                item_name="訪問介護（身体介護1)",
                quantity=1,
                unit_price=amount,
                amount=amount,
                service_date=f"{period}-05",
            )
        ]
        diffs, _ = engine.reconcile(items, index, period, plan=None, rulebook=book)
        got = [d for d in diffs if d.diff_type == DIFF_NO_CONTRACT]
        assert got, "第 7 类差异没有报出来"
        return got[0]

    book = Rulebook(customer="X")
    first = engine.reconcile(
        [
            engine.LineItem(
                row_no=1,
                customer_name="佐藤 花子",
                item_name="訪問介護（身体介護1)",
                quantity=1,
                unit_price=prev_amount,
                amount=prev_amount,
                service_date="2026-10-05",
            )
        ],
        index,
        "2026-10",
        plan=None,
    )[0][0]
    book.confirm(first.diff_type, first.fingerprint, verdict, "2026-10", note="确认过")
    return one(now_amount, "2026-11")


def check(name: str, expect_needs_human: bool, prev: float, now: float) -> None:
    try:
        d = scenario(prev, now)
        actual = d.needs_human
        ok = actual == expect_needs_human
        RESULTS.append(
            (
                name,
                ok,
                ""
                if ok
                else f"{prev:,.0f} -> {now:,.0f} 判定为 {d.status.value}，"
                f"needs_human={actual}，应为 {expect_needs_human}",
            )
        )
    except Exception as e:  # noqa: BLE001
        RESULTS.append((name, False, f"{type(e).__name__}: {e}"))


# Chief 点名要的场景：涨到 100 倍必须退回人工
check("1,830 -> 183,000（+9900%，必须退回需确认）", True, 1830, 183000)
# 阈值内的正常波动：不该打扰客户
check("1,830 -> 1,900（+3.8%，阈值内，不必重看）", False, 1830, 1900)
check("1,830 -> 2,100（+14.8%，阈值内）", False, 1830, 2100)
# 边界：刚好超 20%
check("1,830 -> 2,196（+20.0%，边界不超）", False, 1830, 2196)
check("1,830 -> 2,200（+20.2%，刚过线，要重看）", True, 1830, 2200)
# 金额大幅下降同样危险（漏收钱）
check("1,830 -> 500（-72.7%，大幅下降，要重看）", True, 1830, 500)


def main() -> int:
    for name, ok, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            print(f"      {detail}")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} 阈值场景通过")
    if passed != len(RESULTS):
        print("\n仍有指纹无条件放过金额变化，对外承诺不成立。")
        return 1
    print("\n±20% 阈值生效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
