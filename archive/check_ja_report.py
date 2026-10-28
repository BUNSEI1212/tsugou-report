#!/usr/bin/env python
"""日文报告文案独立核查（Researcher 用，不依赖 final_check.py）。

为什么需要这个：
    final_check.py 的 F4 只扫 5 个中文表头词，查不到三件事：
      1. 表头换了、但横幅/说明句/图例还是中文（漏层）
      2. 客户原始日文数据被翻译改写（动了不该动的）
      3. 判定状态 / 建议文案那两层没换全

判中日文的正确方法（重要）：
    不能按"是不是汉字"判 —— 日文本身就是汉字+假名混排，
    「確信度」「契約単価と一致しません」里的字都是日文汉字，不是中文。
    本脚本的判据是：
      - 含假名（平假名/片假名）→ 日文
      - 含日文专用汉字字形（実/値/単/経/録/変/様/異/違…）→ 日文
      - 否则若整句有汉字且无假名 → 疑似中文，报出来

用法：
    python check_ja_report.py out/演示用_突合レポート_2026-10.xlsx
退出码 0 = 通过；非 0 = 有问题。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from openpyxl import load_workbook

# 日文专用字形：简体中文里不会出现，出现即判日文
JP_ONLY_KANJI = set("実値単経録変様異違対図実営収読経関発伝転選択続")

# 假名
KANA = set("ぁあぃいぅうぇえぉおかがきぎくぐけげこごさざしじすずせぜそぞただちぢっつづてでとどなにぬねのはばぱひびぴふぶぷへべぺほぼぽまみむめもゃやゅゆょよらりるれろゎわゐゑをんゔゕゖァアィイゥウェエォオカガキギクグケゲコゴサザシジスズセゼソゾタダチヂッツヅテデトドナニヌネノハバパヒビピフブプヘベペホボポマミムメモャヤュユョヨラリルレロヮワヰヱヲンヴ")

# 中文界面残留（这些词中文里用、日文不用或写不同）
CN_RESIDUE = [
    "差异类型", "定位", "原文", "建议修正值", "置信度", "判定状态",
    "人工确认", "备注", "合计", "图例", "本次不可用", "需确认",
    "已确认过", "不在本月计划内", "合同未约定",
    # 横幅类（本次实际抓到的那条）
    "模拟数据", "本报告使用", "不对应任何真实", "虚构", "真实客户",
    "本月", "处不一致", "需要人工确认",
]


def has_kana(s: str) -> bool:
    return any(c in KANA for c in s)


def has_jp_kanji(s: str) -> bool:
    return any(c in JP_ONLY_KANJI for c in s)


def is_japanese(s: str) -> bool:
    return has_kana(s) or has_jp_kanji(s)


def has_cjk(s: str) -> bool:
    return any("\u4e00" <= c <= "\u9fff" for c in s)


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else Path("out/演示用_突合レポート_2026-10.xlsx")
    if not path.exists():
        print(f"✗ 文件不存在：{path}")
        return 2

    wb = load_workbook(path)
    ws = wb.worksheets[0]
    text_cells = [
        (r, c, str(ws.cell(row=r, column=c).value))
        for r in range(1, ws.max_row + 1)
        for c in range(1, ws.max_column + 1)
        if isinstance(ws.cell(row=r, column=c).value, str)
    ]

    problems: list[str] = []

    # 1) 明确的界面中文残留
    for word in CN_RESIDUE:
        hits = [(r, c, v) for r, c, v in text_cells if word in v]
        if hits:
            r, c, v = hits[0]
            problems.append(f"[中文残留] 「{word}」{len(hits)} 处，例 R{r}C{c}: {v[:44]}")

    # 2) 整句中文（有汉字、无假名、无日文专用字形）——按长度阈值判，短的可能是编号
    susp = [
        (r, c, v) for r, c, v in text_cells
        if len(v) >= 6 and has_cjk(v) and not is_japanese(v)
    ]
    for r, c, v in susp[:6]:
        problems.append(f"[疑似中文整句] R{r}C{c}: {v[:50]}")

    # 2b) 定位列 / 原文列的简体字标签
    SIMPLIFIED_UI = [
        (r'第\s*\d+\s*行', '「第 N 行」应为「N 行目」'),
        ('数量', '「数量」应为「数量」（数 为简体字形）'),
        ('样品', '「样品」应为「サンプル」/「見本」'),
    ]
    for pat, why in SIMPLIFIED_UI:
        hits = [(r, c, v) for r, c, v in text_cells if re.search(pat, v)]
        if hits:
            r, c, v = hits[0]
            problems.append(f'[简体标签] {len(hits)} 处 {why}，例 R{r}C{c}: {v[:40]}')

    # 3) 客户原始日文数据必须还在（证明没把不该动的也翻译了）
    jp_data = [v for _, _, v in text_cells if has_kana(v)]
    if not jp_data:
        problems.append("[数据被翻译] 报告里没有任何假名文本，客户原始日文数据疑似被改写")

    print(f"检查：{path}")
    print(f"  含文本单元格 {len(text_cells)} 个 | 含假名的日文文本 {len(jp_data)} 个")

    # 三层覆盖统计
    for layer, keys in (
        ("L1 表头", ["差異種別", "差異類型", "位置", "該当行"]),
        ("L2 状態", ["要確認", "確認済み", "再発"]),
        ("L3 建议", ["契約単価", "一致しません", "計画"]),
    ):
        hit = [k for k in keys if any(k in v for _, _, v in text_cells)]
        print(f"  {layer}：命中 {len(hit)}/{len(keys)} {hit}")

    print("=" * 50)
    if problems:
        print(f"✗ {len(problems)} 处问题：")
        for p in problems:
            print("  -", p)
        return 1
    print("✓ 文案为日文，客户原始数据保留")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
