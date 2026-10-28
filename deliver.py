"""一键交付检查（DevBuilder 维护）。

Chief 的要求：跑一次就能全部通过，不必来回催。
这个脚本按顺序做五件事，任何一步失败立即退出非 0：

  1. 清空 out/ 后从头重跑，确认产物是新建的、不是旧的
  2. 跑 review_gate.py（四条阻断项）
  3. 跑 final_check.py（完整验收，含阈值 / 状态 / 界面 / 图例一致性）
  4. 列出所有产出的判定状态，证明四态已改成三态
  5. 输出日文文案表 + 语义层核对指标

为什么不把检查写在本文件里：检查标准和实现如果由同一个人写，
很容易变成"自己批改自己的作业"。review_gate.py 和 final_check.py 是
CodeReviewer 写的验收标准，这里只负责**按顺序调用**并汇总结果。

用法：
    cd recon-report && python deliver.py
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
OUT = BASE / "out"

# 中文特有词组。用来抓「该换的没换」。
# 为什么不用单字表：汉字不是语言标签。
#   - 按「有汉字、无假名」判 -> 误报「差異種別」「金額」「数量」等日文正常汉字词
#   - 按「简体专用字」判     -> 连「不」「修正」「判定」这些中日共用字也误伤
# 这些词组连写成中文的样子，日文里不会这样写，所以既能抓到又不误伤。
# 反向验证：拿旧的中文横幅「【样品·模拟数据】本报告使用的全部数据均为虚构...」
# 去测，能命中「样品」「模拟数据」「本报告」「不对应」「任何真实」「客户」。
ZH_PHRASES = [
    "样品", "模拟数据", "本报告", "不对应", "任何真实", "客户",
    "差异类型", "建议修正", "判定状态", "人工确认", "置信度",
    "需要人工", "处不一致", "本月", "条数据", "请客户", "请确认",
    "无法解析", "计划内", "合同单价表", "该品目",
]

failures: list[str] = []


def step(title: str) -> None:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def run_script(name: str) -> bool:
    """跑一个检查脚本，返回是否退出码 0。"""
    r = subprocess.run(
        [sys.executable, name], cwd=BASE, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    tail = [ln for ln in (r.stdout or "").strip().splitlines() if ln.strip()][-4:]
    for ln in tail:
        print(f"  {ln}")
    if r.returncode != 0:
        failures.append(f"{name} 退出码 {r.returncode}")
        err = (r.stderr or "").strip()
        if err:
            print(f"  [stderr] {err[-300:]}")
    return r.returncode == 0


def main() -> int:
    # ── 1. 清空并重跑，确保产物是新的
    step("1/5  清空 out/ 后从头重跑产物")
    for p in OUT.glob("*"):
        p.unlink()
        print(f"  删除旧文件: {p.name}")
    if not run_script("make_demo.py"):
        failures.append("make_demo.py 失败")
    if not run_script("render_demo.py"):
        failures.append("render_demo.py 失败")

    # ── 2. 阻断项
    step("2/5  review_gate.py（四条阻断项）")
    run_script("review_gate.py")

    # ── 3. 完整验收
    step("3/5  final_check.py（完整验收）")
    run_script("final_check.py")

    # ── 4. 产出的判定状态
    step("4/5  实际产出的判定状态（证明死文案已删）")
    from engine import DiffStatus
    from report import STATUS_JA
    from openpyxl import load_workbook

    xlsx = OUT / "演示用_突合レポート_2026-10.xlsx"
    if not xlsx.exists():
        failures.append("产物 xlsx 不存在")
        ws = None
    else:
        ws = load_workbook(xlsx).active

    enum_states = list(DiffStatus)
    print(f"  枚举共 {len(enum_states)} 态：")
    for s in enum_states:
        print(f"    {s.name:16} = {s.value:8} -> {STATUS_JA[s]}")

    if ws is not None:
        # 图例项数必须与枚举一致，否则客户会看到永远不出现的状态
        legend_n = None
        for r in range(1, ws.max_row + 1):
            if str(ws.cell(row=r, column=1).value or "").startswith("凡例"):
                legend_n = sum(
                    1 for c in range(2, 10) if ws.cell(row=r, column=c).value
                )
        print(f"  图例项数 = {legend_n} | 枚举态数 = {len(enum_states)}", end="  ")
        if legend_n == len(enum_states):
            print("✓")
        else:
            print("✗")
            failures.append(f"图例项数({legend_n}) ≠ 枚举态数({len(enum_states)})")

    # ── 5. 语义层核对指标
    step("5/5  交付指标")
    if ws is not None:
        blob = "\n".join(
            str(c.value) for row in ws.iter_rows() for c in row
            if c.value is not None
        )
        zh_status = sum(blob.count(s.value) for s in enum_states)
        data_rows = sum(
            1 for r in range(1, ws.max_row + 1)
            if ws.cell(row=r, column=7).value in STATUS_JA.values()
        )
        metrics = {
            "MD5": hashlib.md5(xlsx.read_bytes()).hexdigest().upper(),
            "产物时间": datetime.fromtimestamp(
                xlsx.stat().st_mtime
            ).strftime("%H:%M:%S"),
            "中文「第N行」": blob.count("第 "),
            "日文「N行目」": blob.count("行目"),
            "中文判定状态词": zh_status,
            "数据行数": data_rows,
        }
        for k, v in metrics.items():
            print(f"  {k:16} = {v}")
        # 语义层硬约束
        if metrics["中文「第N行」"] != 0:
            failures.append("产物仍有中文「第N行」")
        if metrics["中文判定状态词"] != 0:
            failures.append("产物仍有中文判定状态词")
        if metrics["日文「N行目」"] < 6:
            failures.append("日文「N行目」少于 6 处")

        # ── 第四层：整句语言检查
        #
        # 前三层（表头 / 状态值 / 建议文案）是 F4 覆盖的名词，但横幅、摘要句、
        # 图例标签这些位置 F4 扫不到。曾经有人报过「第 2 行横幅还是中文」，
        # 当时实物已经是日文，但那类问题确实需要一道闸。
        #
        # 判据不用「中文字符表」——汉字不是语言标签：
        # 第一版按「有汉字、无假名」判，误报「差異種別」「金額」等日文正常汉字词；
        # 第二版按「简体专用字」判，连「不」「修正」「判定」这些中日共用字都误伤。
        # 改用「中文特有词组」：这些连写组合日文里绝不会出现。
        print("\n  第四层 整句语言检查（中文特有词组）：")
        zh_hits = []
        for row in ws.iter_rows():
            for c in row:
                v = c.value
                if not isinstance(v, str):
                    continue
                for ph in ZH_PHRASES:
                    if ph in v:
                        zh_hits.append((c.coordinate, v[:48], ph))
                        break
        if zh_hits:
            for coord, v, ph in zh_hits:
                print(f"    {coord}: {v}  <- 「{ph}」")
                failures.append(f"{coord} 出现中文词组「{ph}」")
        else:
            print("    0 处中文词组 ✓")

    # ── 汇总
    step("汇总")
    if failures:
        print(f"✗ 交付检查未通过，{len(failures)} 项失败：")
        for f in failures:
            print(f"    - {f}")
        print("\n不能交付。")
        return 1
    print("✓ 全部检查通过。可以交付。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
