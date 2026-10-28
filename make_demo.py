"""生成给代理店看的演示报告（样品）。

三个硬要求（Chief 定的）：
1. **断网跑**：整个流程不碰网络。脚本开头就把 socket 掐掉，
   真有网络调用会直接抛异常而不是悄悄成功。
2. **全程从 CSV 输入到 xlsx 输出**，走的和正式使用完全相同的代码路径。
3. **显式标注模拟数据**：数据全为虚构，报告里必须写清楚，不能让人误以为是真的客户数据。

用法：
    cd recon-report && python make_demo.py
产物：
    out/演示用_突合レポート_2026-10.xlsx
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def block_network() -> None:
    """把网络出口全部掐掉。任何联网尝试都会抛异常，而不是静默成功。"""

    def _blocked(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError(
            "演示报告要求全程断网，但代码尝试建立网络连接 —— 这是 bug，请排查。"
        )

    socket.socket = _blocked  # type: ignore[assignment]
    socket.create_connection = _blocked  # type: ignore[assignment]
    socket.getaddrinfo = _blocked  # type: ignore[assignment]


block_network()

from engine import (  # noqa: E402
    build_rate_index,
    load_contract_rates,
    load_line_items,
    load_plan_with_display,
    reconcile,
)
from report import LABELS_JA, write_report_ja  # noqa: E402
from rulebook import Rulebook  # noqa: E402

BASE = Path(__file__).resolve().parent
DEMO = BASE / "sample" / "demo"
OUT = BASE / "out" / "演示用_突合レポート_2026-10.xlsx"

BANNER = (
    "※模擬データです。実在の法人・利用者・会計データは一切含まれていません。"
)



def main() -> int:
    items = load_line_items(DEMO / "利用実績_2026-10.csv")
    rates = load_contract_rates(DEMO / "契約単価表.csv")
    rate_index = build_rate_index(rates)
    plan, plan_display = load_plan_with_display(DEMO / "計画明細_2026-10.csv")

    # 演示不加载任何历史规则簿：让代理店看到「第一次跑」的真实样子
    book = Rulebook(customer="演示用")

    diffs, warnings = reconcile(
        items,
        rate_index,
        "2026-10",
        rulebook=book,
        plan=plan,
        plan_display=plan_display,
    )

    # 清掉 out/ 里除样品外的其它 xlsx。
    # 之前 out/ 堆了 6 份旧测试报告，有人误把旧的当成样品打开，
    # 报出的标题和数字都对不上。样品目录必须只有一份样品。
    for stale in OUT.parent.glob("*.xlsx"):
        if stale.name != OUT.name:
            stale.unlink()
            print(f"  清理旧文件: {stale.name}")

    write_report_ja(
        diffs,
        OUT,
        LABELS_JA["customer"],
        "2026-10",
        LABELS_JA,
        unavailable=[
            "消費税額の誤り（税率の適用区分が未確定のため、今回の出力には含めていません）"
        ],
        banner=BANNER,
    )

    need = sum(1 for d in diffs if d.needs_human)
    print(BANNER)
    print(f"输入: {len(items)} 行利用実績 → 差异 {len(diffs)} 条，其中 {need} 条需人看")
    for w in warnings:
        print(f"  ⚠ {w}")
    for d in diffs:
        print(f"  [{d.status.value}] {d.diff_type}: {d.locator}")
    print(f"\n演示报告已生成: {OUT}")
    print("（全程断网，无任何网络连接）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
