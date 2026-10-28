"""把演示报告 xlsx 渲染成 PNG，方便代理店不装 Excel 也能看。

不引入新依赖：用 openpyxl 读 xlsx + Pillow 画图，字体用系统自带的
Yu Gothic（日文）和微软雅黑（中文兜底）。

用法：
    cd recon-report && python render_demo.py
产物：
    out/演示用_突合レポート_2026-10.png
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# 报告界面目前是中文为主体、夹日文数据，所以字体必须能同时覆盖
# 简体中文 + 日文假名 + 日文汉字。Yu Gothic 缺部分简体字形（会显示成方块），
# 微软雅黑覆盖更全，作为主力；日文假名在雅黑里也有。
FONT_JP = Path(r"C:\Windows\Fonts\msyh.ttc")
FONT_JP_BOLD = Path(r"C:\Windows\Fonts\msyhbd.ttc")
FONT_ZH = FONT_JP
FONT_ZH_BOLD = FONT_JP_BOLD

# 列宽（像素），与报告的逻辑列一一对应
COL_W = [40, 170, 300, 190, 330, 80, 190, 90, 110]
ROW_H = 34
PAD = 24
HEADER_H = 40


def load_font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


def text_width(draw: ImageDraw.ImageDraw, s: str, font) -> int:
    box = draw.textbbox((0, 0), s, font=font)
    return box[2] - box[0]


def wrap_to_width(
    draw: ImageDraw.ImageDraw, s: str, font, max_w: int
) -> list[str]:
    """按像素宽度折行。中日文没有空格，只能逐字符累加。"""
    lines: list[str] = []
    cur = ""
    for ch in str(s):
        if text_width(draw, cur + ch, font) <= max_w:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines or [""]


def hexcolor(v) -> str | None:
    """openpyxl 的 rgb 是 8 位 ARGB，转成 #RRGGBB。"""
    if not isinstance(v, str) or len(v) != 8:
        return None
    if v.upper().endswith("000000"):
        return None
    body = v[2:]
    if body == "000000":
        return None
    return "#" + body


def build_merged_map(ws) -> dict[tuple[int, int], tuple[int, int]]:
    """返回 {(row, col): (col_start, col_end)}，标记合并单元格的归属。

    报告里标题、样品横幅、摘要句都是整行合并（A:I），
    渲染时必须按整行宽度折行，不能塞进 A 列的 40 像素里。
    """
    out: dict[tuple[int, int], tuple[int, int]] = {}
    for rng in ws.merged_cells.ranges:
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                out[(r, c)] = (rng.min_col, rng.max_col)
    return out


def merge_horizontally_adjacent(cells: list[dict]) -> list[dict]:
    """把同一行里横向相邻、背景色相同且连续的单元格视觉上合并。

    只用于报告里那些「语义上是一组」的行（合计行、图例行）：
    它们在 xlsx 里没有真合并，按原列宽各自渲染会把文字挤成一列一字。
    数据行绝不能用这个（同底色相邻会被误并）。
    """
    out: list[dict] = []
    for cell in cells:
        prev = out[-1] if out else None
        if prev is not None and prev["fill"] == cell["fill"]:
            prev["width"] += cell["width"]
            prev["text"] = (prev["text"] + "  " + cell["text"]).strip()
            prev["_merged"] = True
        else:
            out.append(dict(cell))
    return out


def main() -> int:
    from openpyxl import load_workbook

    base = Path(__file__).resolve().parent
    src = base / "out" / "演示用_突合レポート_2026-10.xlsx"
    dst = base / "out" / "演示用_突合レポート_2026-10.png"

    ws = load_workbook(src).active
    merged = build_merged_map(ws)

    font = load_font(FONT_JP, 17)
    font_bold = load_font(FONT_JP_BOLD, 17)
    font_title = load_font(FONT_JP_BOLD, 26)
    font_banner = load_font(FONT_JP_BOLD, 17)

    probe = Image.new("RGB", (10, 10))
    pdraw = ImageDraw.Draw(probe)

    rows: list[dict] = []
    for r in range(1, ws.max_row + 1):
        cells = []
        c = 1
        while c <= len(COL_W):
            cell = ws.cell(row=r, column=c)
            span = merged.get((r, c))
            if span:
                # 合并单元格：宽度 = 跨的几列之和，只渲染起点那格
                c_from, c_to = span
                w = sum(COL_W[c_from - 1 : c_to])
                if cell.value is not None:
                    fill = hexcolor(cell.fill.fgColor.rgb)
                    is_title = r == 1
                    f = font_title if is_title else (
                        font_banner if fill and fill.lower() == "#c00000" else font
                    )
                    lines = wrap_to_width(pdraw, cell.value, f, w - 16)
                    cells.append({"text": str(cell.value), "lines": lines,
                                  "fill": fill, "width": w, "font": f})
                c = c_to + 1
                continue
            if cell.value is not None:
                fill = hexcolor(cell.fill.fgColor.rgb)
                is_header = fill and fill.lower() == "#1f4e78"
                f = font_bold if is_header else font
                lines = wrap_to_width(pdraw, cell.value, f, COL_W[c - 1] - 12)
                cells.append({"text": str(cell.value), "lines": lines,
                              "fill": fill, "width": COL_W[c - 1], "font": f})
            else:
                # 占位，保证合并/相邻判断能对齐列
                cells.append({"text": "", "lines": [""], "fill": None,
                              "width": COL_W[c - 1], "font": font})
            c += 1

        # 合计行、图例行这类「语义成组」的行，视觉上合并同底色相邻单元格
        first_text = cells[0]["text"] if cells else ""
        if first_text.startswith("合計") or first_text.startswith("合计"):
            cells = merge_horizontally_adjacent([x for x in cells if x["text"]])
            for x in cells:
                x["lines"] = wrap_to_width(pdraw, x["text"], x["font"], x["width"] - 16)
            # 「合計」这一格本身要给够宽度，否则会被挤成竖排
            if cells:
                cells[0]["width"] = max(cells[0]["width"], 80)
                cells[0]["lines"] = wrap_to_width(
                    pdraw, cells[0]["text"], cells[0]["font"], cells[0]["width"] - 16
                )
        elif first_text.startswith("図例") or first_text.startswith("图例") or first_text.startswith("凡例"):
            # 图例是「标签 + 四个色块」，色块底色各不相同，不能合并；
            # 色块要够宽，否则长标签会被挤成一列一字
            cells = [x for x in cells if x["text"]]
            if cells:
                cells[0]["width"] = 90
                cells[0]["lines"] = [first_text]
                for x in cells[1:]:
                    x["width"] = 230
                    x["lines"] = wrap_to_width(
                        pdraw, x["text"], x["font"], x["width"] - 16
                    )

        if cells:
            height = max(len(x["lines"]) for x in cells) * (ROW_H - 8) + 14
            rows.append({"cells": cells, "height": max(ROW_H, height)})

    total_h = PAD * 2 + sum(r["height"] for r in rows) + 20
    total_w = PAD * 2 + sum(COL_W)

    img = Image.new("RGB", (total_w, total_h), "#FFFFFF")
    draw = ImageDraw.Draw(img)

    y = PAD
    for row in rows:
        x = PAD
        h = row["height"]
        for cell in row["cells"]:
            w = cell["width"]
            if cell["fill"]:
                draw.rectangle([x, y, x + w - 2, y + h], fill=cell["fill"])
            # 深色底用白字
            dark = cell["fill"] and cell["fill"].lower() in ("#1f4e78", "#c00000")
            color = "#FFFFFF" if dark else "#222222"
            for i, line in enumerate(cell["lines"]):
                draw.text((x + 8, y + 7 + i * (ROW_H - 8)), line,
                          fill=color, font=cell["font"])
            x += w
        draw.line([PAD, y + h, total_w - PAD, y + h], fill="#DDDDDD")
        y += h

    img.save(dst)
    print(f"渲染完成: {dst}  ({img.width}x{img.height})")
    print("说明: 这是预览图，正式交流请以 xlsx 原件为准")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
