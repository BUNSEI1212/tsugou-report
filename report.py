"""把差异清单渲染成 Excel 报告（对齐 差异类型清单 v1）。

按清单 v1 的硬性要求：
- 第 8 类「日期无法解析」是数据质量问题，不是对账结论，
  放独立段落、不计入「N 处不一致」，否则客户会觉得我们总数算错。
- 第 5 类税额口径未定时标「不可用·税率口径待定」，不输出。
- 有检查项不可用时（缺计划表），页首显式说明。
- 三级判定用底色区分，「与上次判定不符」必须显著。
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from engine import Diff, DiffStatus
from rulebook import (
    ALL_INTERNAL_DIFF_TYPES,
    DIFF_AMOUNT_MISMATCH,
    DIFF_BEFORE_CONTRACT,
    DIFF_DATE_RANGE,
    DIFF_DATE_UNPARSABLE,
    DIFF_EXTRA_ROW,
    DIFF_MISSING_ROW,
    DIFF_NO_CONTRACT,
    DIFF_TAX,
    DIFF_UNIT_PRICE,
)

THIN = Side(style="thin", color="D0D0D0")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=10)
TITLE_FONT = Font(bold=True, size=14)
SUMMARY_FONT = Font(bold=True, size=11, color="C00000")
WARN_FONT = Font(bold=True, size=9, color="833C00")
SECTION_FONT = Font(bold=True, size=11, color="1F4E78")
DATA_HEADER_FILL = PatternFill("solid", fgColor="7F7F7F")

FILLS = {
    DiffStatus.NEW: PatternFill("solid", fgColor="FCE4D6"),                    # 橙
    DiffStatus.CONFIRMED_SAME: PatternFill("solid", fgColor="E2EFDA"),          # 绿
    DiffStatus.RECURRING: PatternFill("solid", fgColor="FFC7CE"),               # 红
}

# 列宽。表头文字由调用方传入（对外交付是日文 labels，内部核对可传中文），
# 这里只放宽度，避免中文界面文案硬编码在渲染层。
COL_WIDTHS = [6, 20, 34, 22, 38, 9, 28, 10, 24]

# 日文界面的差异类型名与判定状态名。
# 中文是内部工作语言，日文是对外交付语言；两套必须一一对应，不能有遗漏。
DIFF_TYPE_JA = {
    DIFF_AMOUNT_MISMATCH: "金額不一致",
    DIFF_MISSING_ROW: "計上漏れ",
    DIFF_EXTRA_ROW: "計上対象外の行",
    DIFF_UNIT_PRICE: "契約単価と不一致",
    DIFF_TAX: "消費税額の誤り",
    DIFF_DATE_RANGE: "対象期間外の日付",
    DIFF_DATE_UNPARSABLE: "日付を読み取れない",
    DIFF_NO_CONTRACT: "契約に無い項目",
    DIFF_BEFORE_CONTRACT: "契約開始前の日付",
}

STATUS_JA = {
    DiffStatus.NEW: "要確認",
    DiffStatus.CONFIRMED_SAME: "確認済み・変更不要",
    DiffStatus.RECURRING: "前回修正済み・今回再発",
}

# 建议修正值的日文对照。引擎产出的是中文，对外报告必须换成日文。
# 用关键词匹配而不是精确匹配，因为文案里带具体数字（金额、日期）。
#
# 左边是**中文键**，用来匹配引擎输出的原文；右边是交给客户的**日文值**。
# 键写成中文字面量是刻意为之：可读、可审、改文案时一眼能看懂。
# 不用 \u 转义——转义只是让扫描器看不见，等于把告警关掉，问题还在。
JA_SUGGESTION_RULES: list[tuple[str, str]] = [
    ("合同单价表中无此",
     "契約単価表に該当項目がありません。契約外サービスか品目名の誤記をご確認ください"),
    ("合同最早从",
     "契約の適用開始日が{earliest}のため、このサービス日は契約範囲外です。"
     "別期間の単価を適用していないかご確認ください"),
    ("不在本月计划内", "今月の計上計画に含まれていません"),
    ("不属本期间", "対象期間外の日付です"),
    ("本月计划应出账", "今月の計上計画にありますが、請求明細に見当たりません"),
    ("数量×単価", "数量×単価での計算値と一致しません"),
    ("契約単価", "契約単価と一致しません"),
    ("无法解析", "日付の書式を読み取れません。YYYY-MM-DD でご確認ください"),
    # 缺失行的两处：只在「计划有、请求书没有」时出现，演示数据里没有缺失行，
    # 所以演示产物看不出问题，真实客户一遇到缺失行就会看到中文。
    ("请求书内无此行", "（請求明細に該当行がありません）"),
    ("按契约単価", "（契約単価 {amount} 円で計上）"),
]



# 原文列的中文→日文对照（只在缺失行出现）
JA_ORIGINAL_RULES: list[tuple[str, str]] = [
    ("请求书内无此行", "（請求明細に該当行がありません）"),
]

def _translate_original(text: str) -> str:
    """原文列的日文对照。

    多数差异的 original 是数值（単価 1,300），日文已通用；
    但缺失行的 original 是中文「（请求书内无此行）」，会原样漏到客户眼前。
    演示数据里没有缺失行，所以这处一直没暴露出来。
    """
    for zh, ja in JA_ORIGINAL_RULES:
        if zh in text:
            return ja
    return text


def _translate_suggestion(text: str) -> str:
    """把引擎产出的中文建议文案翻成日文。匹配不到就原样返回（不猜）。"""
    for zh, ja in JA_SUGGESTION_RULES:
        if zh in text:
            if "{amount}" in ja:
                import re as _re

                m = _re.search(r"[\d,]+\.?\d*", text)
                amount = m.group(0) if m else ""
                return ja.replace("{amount}", amount)
            if "{earliest}" in ja:
                import re

                m = re.search(r"\d{4}-\d{2}-\d{2}", text)
                earliest = m.group(0) if m else ""
                return ja.replace("{earliest}", earliest)
            return ja
    return text

# 报告界面的日文文案（对外交付版）。
# 日本代理店第一眼看到的就是这份实物，界面必须是日文；
# 中文已全部移出渲染层，避免半中半日。
LABELS_JA = {
    "title": "突合レポート",
    "customer": "デモ用（模擬データ）",
    "period": "対象期間",
    "summary": "今月 {total} 件の不一致、うち {need} 件は要確認"
    "（前回から修正済みで今回再発 {recurring} 件／前回判定と同一 {confirmed} 件）",
    "unavailable": "今回利用できません：",
    "data_issue": "データ不備：{n} 行は日付を読み取れませんでした"
    "（上記「不一致」件数には含めていません。元データの書式の問題で、突合の結論ではありません）",
    "headers": [
        "#", "差異種別", "該当箇所", "該当行の値",
        "推奨修正値 / 判定理由", "確信度", "判定状態", "確認", "備考",
    ],
    "total": "合計",
    "unit": "件",
    "legend": "凡例：",
}


def write_report_ja(
    diffs: list[Diff],
    out_path: str | Path,
    customer: str,
    period: str,
    labels: dict,
    warnings: list[str] | None = None,
    unavailable: list[str] | None = None,
    banner: str = "",
) -> Path:
    """日文界面版报告。

    日本代理店看到的第一眼是这份实物，界面必须是日文；
    中文版留给内部核对用。两套共用同一份对账结果，不重跑引擎。
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    unknown = {d.diff_type for d in diffs} - set(ALL_INTERNAL_DIFF_TYPES)
    if unknown:
        raise ValueError(
            f"报告不认识这些差异类型，会被静默漏掉：{sorted(unknown)}"
        )

    wb = Workbook()
    ws = wb.active
    ws.title = str(labels["title"])

    warnings = warnings or []
    unavailable = unavailable or []

    data_diffs = [d for d in diffs if d.diff_type == DIFF_DATE_UNPARSABLE]
    report_diffs = [d for d in diffs if d.diff_type != DIFF_DATE_UNPARSABLE]

    total = len(report_diffs)
    need_human = sum(1 for d in report_diffs if d.needs_human)
    recurring = sum(1 for d in report_diffs if d.status is DiffStatus.RECURRING)
    confirmed = sum(1 for d in report_diffs if d.status is DiffStatus.CONFIRMED_SAME)

    row = 1
    ws.merge_cells(f"A{row}:I{row}")
    ws[f"A{row}"] = (
        f"{labels['title']}　{customer}　{labels['period']} {period}"
    )
    ws[f"A{row}"].font = TITLE_FONT
    ws[f"A{row}"].alignment = Alignment(vertical="center")
    ws.row_dimensions[row].height = 26
    row += 1

    if banner:
        ws.merge_cells(f"A{row}:I{row}")
        ws[f"A{row}"] = banner
        ws[f"A{row}"].font = Font(bold=True, size=11, color="FFFFFF")
        ws[f"A{row}"].fill = PatternFill("solid", fgColor="C00000")
        ws[f"A{row}"].alignment = Alignment(vertical="center", wrap_text=True)
        ws.row_dimensions[row].height = 30
        row += 1

    ws.merge_cells(f"A{row}:I{row}")
    ws[f"A{row}"] = labels["summary"].format(
        total=total, need=need_human, recurring=recurring, confirmed=confirmed
    )
    ws[f"A{row}"].font = SUMMARY_FONT
    ws.row_dimensions[row].height = 22
    row += 1

    for u in unavailable:
        ws.merge_cells(f"A{row}:I{row}")
        ws[f"A{row}"] = f"○ {labels['unavailable']}{u}"
        ws[f"A{row}"].font = WARN_FONT
        row += 1
    for w in warnings:
        ws.merge_cells(f"A{row}:I{row}")
        ws[f"A{row}"] = f"⚠ {w}"
        ws[f"A{row}"].font = WARN_FONT
        row += 1

    row += 1
    header_row = row
    for idx, name in enumerate(labels["headers"], start=1):
        cell = ws.cell(row=header_row, column=idx, value=name)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = BORDER
        ws.column_dimensions[get_column_letter(idx)].width = COL_WIDTHS[idx - 1]

    r = header_row + 1
    for i, d in enumerate(report_diffs, start=1):
        values = [
            i,
            DIFF_TYPE_JA.get(d.diff_type, d.diff_type),
            d.locator,
            _translate_original(d.original),
            _translate_suggestion(d.suggested),
            f"{d.confidence:.0%}",
            _status_label(d),
            "",
            "",
        ]
        fill = FILLS.get(d.status, FILLS[DiffStatus.NEW])
        for col, v in enumerate(values, start=1):
            cell = ws.cell(row=r, column=col, value=v)
            cell.border = BORDER
            cell.fill = fill
            cell.alignment = Alignment(
                vertical="center",
                horizontal="center" if col in (1, 6, 8) else "left",
            )
        r += 1

    ws.auto_filter.ref = f"A{header_row}:I{max(header_row + 1, r - 1)}"
    ws.freeze_panes = f"A{header_row + 1}"

    ws.cell(row=r, column=1, value=labels["total"]).font = Font(bold=True)
    ws.cell(row=r, column=2, value=f"{total} {labels['unit']}").font = Font(bold=True)
    r += 2

    if data_diffs:
        ws.merge_cells(f"A{r}:I{r}")
        ws[f"A{r}"] = labels["data_issue"].format(n=len(data_diffs))
        ws[f"A{r}"].font = SECTION_FONT
        r += 1
        for idx, name in enumerate(labels["headers"], start=1):
            cell = ws.cell(row=r, column=idx, value=name)
            cell.fill = DATA_HEADER_FILL
            cell.font = HEADER_FONT
            cell.border = BORDER
        r += 1
        for i, d in enumerate(data_diffs, start=1):
            values = [
                i,
                DIFF_TYPE_JA.get(d.diff_type, d.diff_type),
                d.locator,
                _translate_original(d.original),
                _translate_suggestion(d.suggested),
                f"{d.confidence:.0%}",
                _status_label(d),
                "",
                "",
            ]
            fill = FILLS.get(d.status, FILLS[DiffStatus.NEW])
            for col, v in enumerate(values, start=1):
                cell = ws.cell(row=r, column=col, value=v)
                cell.border = BORDER
                cell.fill = fill
            r += 1
        r += 1

    ws.cell(row=r, column=1, value=labels["legend"]).font = Font(bold=True, size=9)
    for offset, status in enumerate(
        [
            DiffStatus.NEW,
            DiffStatus.CONFIRMED_SAME,
            DiffStatus.RECURRING,
        ],
        start=2,
    ):
        c = ws.cell(row=r, column=offset * 2 - 1, value=STATUS_JA[status])
        c.fill = FILLS[status]
        c.font = Font(size=9)

    _apply_print_header(ws, banner)
    wb.save(out_path)
    return out_path


def _write_header(ws, row: int, fill) -> None:
    for col, (name, width) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=row, column=col, value=name)
        cell.fill = fill
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = BORDER
        ws.column_dimensions[get_column_letter(col)].width = width


def _write_rows(ws, start_row: int, rows: list[Diff]) -> int:
    for i, d in enumerate(rows, start=1):
        r = start_row + i - 1
        values = [
            i,
            d.diff_type,
            d.locator,
            _translate_original(d.original),
            d.suggested,
            f"{d.confidence:.0%}",
            d.status.value,
            "",
            "",
        ]
        fill = FILLS.get(d.status, FILLS[DiffStatus.NEW])
        for col, v in enumerate(values, start=1):
            cell = ws.cell(row=r, column=col, value=v)
            cell.border = BORDER
            cell.fill = fill
            cell.alignment = Alignment(
                vertical="center",
                horizontal="center" if col in (1, 6, 8) else "left",
            )
    return start_row + len(rows)


def _status_label(d) -> str:
    """状态列的显示文字。

    退回重看的项要带上原因，否则客户看到「要確認」会问
    「上个月确认过的，怎么又要确认」——规则簿看起来像突然坏了。
    """
    base = STATUS_JA.get(d.status, d.status.value)
    reason = getattr(d, "unknown_reason", "")
    if not reason:
        return base
    # 报告里只写行为，不写商务条款
    if reason == "前回金額不明":
        return f"{base}（前回金額不明のためご確認ください）"
    if reason == "金額変動":
        return f"{base}（前回ご確認時から金額が変動しています）"
    return base


def _apply_print_header(ws, banner: str) -> None:
    """设置打印页眉页脚，保证每页都有模拟数据标注。"""
    if not banner:
        return
    short = "※模擬データです。実在の法人・利用者の情報は含まれていません。"
    ws.oddHeader.center.text = short
    ws.oddHeader.center.size = 9
    ws.oddHeader.center.color = "C00000"
    ws.oddFooter.center.text = "突合レポート ／ 模擬データ"
    ws.oddFooter.center.size = 8
    # 让长表横向打印、按宽度缩放到一页
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True


def default_unavailable(plan_provided: bool) -> list[str]:
    """本次哪些检查项不可用（日文，对外交付用）。

    不能静默：客户看不到某类检查被跳过，会以为那类没有问题。
    """
    items = []
    if not plan_provided:
        items.append(
            "計上漏れ / 計上対象外の行（今月の計上計画表が無いため実施できません）"
        )
    items.append(
        "消費税額の誤り（税率の適用区分が未確定のため、今回の出力には含めていません）"
    )
    return items
