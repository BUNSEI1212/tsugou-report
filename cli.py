"""命令行入口：跑一次对账，输出 xlsx 报告。

用法：
    python cli.py --period 2026-10 \
        --items sample/利用実績_2026-10.csv \
        --contract sample/契約単価表_分段.csv \
        --plan sample/計画明細_2026-10.csv \
        --customer 介護グループA
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Windows 控制台默认 GBK，输出日文/中文会乱码，统一切到 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from engine import (
    STATUS_CONFLICT,
    ContractDataError,
    build_rate_index,
    load_contract_rates,
    load_line_items,
    load_plan_with_display,
    reconcile,
)
from report import LABELS_JA, write_report_ja
from rulebook import Rulebook

BASE = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser(description="対賬差異報告生成")
    ap.add_argument("--period", required=True, help="対象期間 YYYY-MM")
    ap.add_argument("--items", default="sample/利用実績_2026-10.csv")
    ap.add_argument("--contract", default="sample/契約単価表_分段.csv")
    ap.add_argument(
        "--plan",
        default="sample/計画明細_2026-10.csv",
        help="本月计划应出账项；省略或传 none 则缺失行检查不可用",
    )
    ap.add_argument("--customer", default="介護グループA")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    items = load_line_items(BASE / args.items)
    try:
        rates = load_contract_rates(BASE / args.contract)
    except ContractDataError as e:
        print(f"合同数据有误，已中止：{e}", file=sys.stderr)
        return 2
    rate_index = build_rate_index(rates)

    plan = None
    plan_display = None
    if args.plan and args.plan.lower() != "none":
        plan, plan_display = load_plan_with_display(BASE / args.plan)

    book = Rulebook.load(BASE / "rules" / f"{args.customer}.json")

    diffs, warnings = reconcile(
        items,
        rate_index,
        args.period,
        rulebook=book,
        plan=plan,
        plan_display=plan_display,
    )

    out = Path(args.out) if args.out else BASE / "out" / f"突合レポート_{args.period}.xlsx"
    write_report_ja(
        diffs,
        out,
        args.customer,
        args.period,
        LABELS_JA,
        warnings=warnings,
        unavailable=[
            "消費税額の誤り（税率の適用区分が未確定のため、今回の出力には含めていません）"
        ],
    )

    need = sum(1 for d in diffs if d.needs_human)
    print(f"今月 {len(diffs)} 件の不一致、うち {need} 件は要確認")
    for w in warnings:
        print(f"  ⚠ {w}")
    for d in diffs:
        print(f"  [{d.status.value}] {d.diff_type:<10} {d.locator}")
        print(f"        {d.original} -> {d.suggested}")
    print(f"报告已生成: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
