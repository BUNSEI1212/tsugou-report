"""交付前总检查。

    cd recon-report && python final_check.py

退出码 0 = 可以交付；非 0 = 不可以。

这是本项目唯一的交付判定。任何"做完了"的结论都以它的退出码为准。

检查项分四组：
  F1 阈值口径      —— 三条分支，每类都必须被证明
  F2 阻断项        —— 调用 review_gate.py
  F3 第四态存亡    —— 跑完整流程，枚举实际产出的状态
  F4 界面与产物    —— 界面日文化 + out/ 只留最终一份 + 文件名合规
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

RESULTS: list[tuple[str, str, bool, str]] = []


def record(group: str, name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((group, name, ok, detail))


# ══════════════════════════════════════════════════════════════════════
# F1 阈值口径
#
# 阈值口径（三档）：
#   - 变动金额 > 10,000 日元            -> 退回重看（不看百分比）
#   - 否则，若幅度 > 20% 且金额 > 1,000 -> 退回重看
#   - 其余                              -> 不打扰
#
# 每个用例都记录了"为什么它必须这样判"，避免碰巧变绿。
# ══════════════════════════════════════════════════════════════════════
THRESHOLD_CASES = [
    # (上次确认金额, 本月金额, 期望是否退回, 理由)
    (1830, 183000, True, "差 181,170 超 10,000 绝对档 —— 直接退回，不看百分比"),
    (183000, 150000, True, "差 33,000 超 10,000 绝对档 —— 这是必须挡住的大额漏收"),
    (500, 620, False, "差 120，未超 1,000 —— 小额日常波动，不许打扰客户"),
    (100000, 109000, False, "差 9,000 ≤ 10,000、幅度 9% —— 落在约定的缝隙里，必须明确断言不打扰"),
    (1830, 2200, False, "差 370 ≤ 1,000 —— 相对档的 1,000 门槛未达到，不打扰"),
    (1830, 2196, False, "差 366 > 1,000 但幅度恰好 20.0% —— 边界不超，不打扰"),
    (1830, 1900, False, "差 70，未超 1,000 —— 不打扰"),
    (10000, 10900, False, "差 900 ≤ 1,000，且未超 10,000 —— 不打扰"),
    (10000, 12000, False, "差 2,000 ≤ 10,000、幅度恰好 20.0% —— 两条都不触发，不打扰"),
    (10000, 12500, True, "差 2,500 ≤ 10,000，但幅度 25% > 20% 且金额 > 1,000 —— 相对档触发"),
]


def f1_threshold() -> None:
    """阈值口径。

    期望值不在这里另写一份判断逻辑 —— 直接调 rulebook.needs_review()，
    保证「验收」和「产品」用的是同一份实现（阈值只保留一处定义）。
    这里只负责给出输入、以及断言这条用例为什么该这样判。
    """
    from rulebook import needs_review

    for prev, now, expect, reason in THRESHOLD_CASES:
        label = f"{prev:,.0f} -> {now:,.0f} {'退回' if expect else '不打扰'}"
        try:
            got = needs_review(prev, now)
            ok = got == expect
            detail = (
                ""
                if ok
                else f"期望 {'退回' if expect else '不打扰'}，"
                f"needs_review 返回 {'退回' if got else '不打扰'}（{reason}）"
            )
        except Exception as e:  # noqa: BLE001
            ok = False
            detail = f"{type(e).__name__}: {e}"
        record("F1 阈值", label, ok, detail)

    # 端到端：阈值必须真的作用在规则簿判命中上，不能只在函数层面成立
    for prev, now, expect, reason in [
        (1830, 183000, True, "100 倍变化必须重新问"),
        (1830, 1900, False, "70 日元波动不许打扰"),
    ]:
        try:
            got = _run_threshold_scenario(prev, now)
            ok = got == expect
            detail = (
                ""
                if ok
                else f"完整流程里期望 {'退回' if expect else '不打扰'}，"
                f"实际 {'退回' if got else '不打扰'}（{reason}）"
            )
        except Exception as e:  # noqa: BLE001
            ok = False
            detail = f"{type(e).__name__}: {e}"
        record("F1 阈值", f"[端到端] {prev:,.0f} -> {now:,.0f}", ok, detail)


def _run_threshold_scenario(prev: float, now: float) -> bool:
    """上个月按 prev 确认过一次（判定=保留原值），本月金额变 now，返回是否需人工。"""
    import engine
    from rulebook import DIFF_NO_CONTRACT, VERDICT_KEEP_ORIGINAL, Rulebook

    book = Rulebook(customer="T")
    index = engine.build_rate_index([])  # 合同里没有该品目 -> 走第 7 类

    def line(amount: float, period: str) -> engine.LineItem:
        return engine.LineItem(
            row_no=1,
            customer_name="佐藤 花子",
            item_name="訪問介護（身体介護1)",
            quantity=1,
            unit_price=amount,
            amount=amount,
            service_date=f"{period}-05",
        )

    first, _ = engine.reconcile([line(prev, "2026-10")], index, "2026-10", plan=None)
    got = [d for d in first if d.diff_type == DIFF_NO_CONTRACT]
    assert got, "第 7 类差异未报出"
    # 确认时需带上当时金额，供阈值比较
    try:
        book.confirm(got[0].diff_type, got[0].fingerprint, VERDICT_KEEP_ORIGINAL, "2026-10", amount=prev)
    except TypeError:
        book.confirm(got[0].diff_type, got[0].fingerprint, VERDICT_KEEP_ORIGINAL, "2026-10")

    second, _ = engine.reconcile([line(now, "2026-11")], index, "2026-11", plan=None, rulebook=book)
    d = [x for x in second if x.diff_type == DIFF_NO_CONTRACT]
    assert d, "第二个月第 7 类差异未报出"
    return d[0].needs_human


# ══════════════════════════════════════════════════════════════════════
# F2 阻断项：直接调 review_gate.py，不复制用例
# ══════════════════════════════════════════════════════════════════════
def f2_review_gate() -> None:
    gate = BASE / "review_gate.py"
    if not gate.exists():
        record("F2 阻断项", "review_gate.py 存在", False, "文件不存在")
        return
    r = subprocess.run(
        [sys.executable, str(gate)], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    record("F2 阻断项", "review_gate.py 退出码 0", r.returncode == 0, (r.stdout or "").strip()[-400:])


# ══════════════════════════════════════════════════════════════════════
# F3 第四态存亡
#
# 跑一份覆盖多类型的完整数据，枚举所有实际产出的 DiffStatus。
# 产不出来的状态 = 不存在，对外必须删掉。
# ══════════════════════════════════════════════════════════════════════
def f3_fourth_state() -> None:
    """每条状态用独立规则簿，互不污染 —— 否则前一条的规则会吃掉后一条的状态。"""
    import engine
    from rulebook import VERDICT_ACCEPT_SUGGESTED, VERDICT_KEEP_ORIGINAL, Rulebook

    rates = [engine.ContractRate("鈴木 次郎", "訪問介護（生活援助2)", 1250, "2026-04-01")]
    index = engine.build_rate_index(rates)

    def line(price: float, period: str) -> engine.LineItem:
        return engine.LineItem(1, "鈴木 次郎", "訪問介護（生活援助2)", 5, price, price * 5, f"{period}-05")

    seen: set[str] = set()

    # 路径 A：无规则簿 -> NEW
    a, _ = engine.reconcile([line(1300, "2026-10")], index, "2026-10", plan=None)
    seen |= {d.status.name for d in a}

    # 路径 B：确认「保留原值」后同值再现 -> CONFIRMED_SAME（独立规则簿）
    book_b = Rulebook(customer="B")
    b1, _ = engine.reconcile([line(1300, "2026-10")], index, "2026-10", plan=None)
    for d in b1:
        book_b.confirm(d.diff_type, d.fingerprint, VERDICT_KEEP_ORIGINAL, "2026-10", amount=1300)
    b2, _ = engine.reconcile([line(1300, "2026-11")], index, "2026-11", plan=None, rulebook=book_b)
    seen |= {d.status.name for d in b2}

    # 路径 C：确认「采纳建议值」后同值再现 -> RECURRING（独立规则簿）
    book_c = Rulebook(customer="C")
    c1, _ = engine.reconcile([line(1300, "2026-10")], index, "2026-10", plan=None)
    for d in c1:
        book_c.confirm(d.diff_type, d.fingerprint, VERDICT_ACCEPT_SUGGESTED, "2026-10", amount=1300)
    c2, _ = engine.reconcile([line(1300, "2026-11")], index, "2026-11", plan=None, rulebook=book_c)
    seen |= {d.status.name for d in c2}

    record("F3 状态", f"实际产出的状态：{sorted(seen)}", True, "")

    all_states = {s.name for s in engine.DiffStatus}

    # 判断「孤儿状态」：每个状态名必须在代码里真的被构造出来过。
    # 不是看我这几条用例覆盖了几个 —— 那取决于用例设计，不取决于产品。
    src = (BASE / "engine.py").read_text(encoding="utf-8")
    orphan = [s for s in all_states if f"DiffStatus.{s}" not in src and f"STATUS_{s}" not in src]
    record(
        "F3 状态",
        f"无孤儿状态（枚举 {len(all_states)} 个，本组用例覆盖 {len(seen)} 个）",
        not orphan,
        (
            f"这些状态在 engine.py 里没有任何代码路径能产出，对外必须删除：{sorted(orphan)}\n"
            f"      >>> 连带动作：对外材料里的状态表应改成实际存在的状态数"
            if orphan
            else ""
        ),
    )


# ══════════════════════════════════════════════════════════════════════
# F4 界面与产物
# ══════════════════════════════════════════════════════════════════════
CJK_ONLY = re.compile(r"[\u4e00-\u9fff]")


# 日文汉字与简体中文共用 Unicode 段（\u4e00-\u9fff），不能按「是不是汉字」判断。
# 判据改成：含假名 = 日文；无假名且含简体专用字 = 中文。
KANA = re.compile(r"[\u3040-\u30ff]")
# 日文里不用或极少用的简体字形
SIMPLIFIED_ONLY = set("这个说时报单实据样们开关问题产设备务应处组织领导动没为与东专业业")


def _is_chinese(s: str) -> bool:
    if KANA.search(s):
        return False  # 有假名，是日文
    return any(ch in SIMPLIFIED_ONLY for ch in s)


def f4_ui_language() -> None:
    """report.py 里客户可见的文案不应是中文。

    排除三类「中文合法存在」的地方：
      1. 内部异常提示（raise / ValueError），客户正常跑不会见到
      2. 中文→日文映射表的键，那本来就是翻译用的桥梁
      3. docstring
    """
    src = (BASE / "report.py").read_text(encoding="utf-8")
    lines = src.splitlines()
    bad: list[str] = []
    in_doc = False
    for idx, raw in enumerate(lines):
        lineno = idx + 1
        line = raw.strip()
        if line.startswith(('"""', "'''")):
            in_doc = not in_doc
            continue
        if in_doc or line.startswith("#"):
            continue
        if any(k in line for k in ("raise ", "ValueError(", "RuntimeError(", "assert ")):
            continue
        code = raw.split("#", 1)[0]
        strs = [m.group(1) if m.group(1) is not None else m.group(2)
                for m in re.finditer(r'"([^"]*)"|\'([^\']*)\'', code)]
        # 中文→日文映射表：键/值可能在同一行，也可能被拆成两行。
        # 判据：本行是纯中文 → 看下一行是否紧跟着日文（那就是映射值）。
        if len(strs) == 1 and _is_chinese(strs[0]):
            nxt = " ".join(lines[idx + 1: idx + 3])
            if re.search(r"[\u3040-\u30ff]", nxt):
                continue  # 这是映射表的键，值是下一行的日文
        # 键值同行：("中文", "日本語")
        if len(strs) >= 2 and _is_chinese(strs[0]) and re.search(r"[\u3040-\u30ff]", strs[1]):
            continue
        for s in strs:
            if s and _is_chinese(s):
                bad.append(f"L{lineno}: {s[:40]}")
    record(
        "F4 界面",
        "report.py 客户可见文案已无中文",
        not bad,
        ("残留：" + " | ".join(bad[:5])) if bad else "",
    )


def f4_outputs() -> None:
    """out/ 里只应留最终一份，且文件名用「突合レポート」。"""
    out = BASE / "out"
    if not out.exists():
        record("F4 产物", "out/ 存在", False, "目录不存在")
        return
    files = [p for p in out.iterdir() if p.is_file()]
    names = [p.name for p in files]

    record("F4 产物", f"out/ 只留最终产物（上限 3：xlsx / png / pdf，现有 {len(files)} 个）", len(files) <= 3,
           f"文件过多，可能混入旧版：{names}" if len(files) > 3 else "")

    stale = [n for n in names if "差異報告" in n]
    record("F4 产物", "文件名统一为「突合レポート」", not stale,
           f"仍用旧名：{stale}" if stale else "")

    xlsx = [p for p in files if p.suffix == ".xlsx"]
    if not xlsx:
        record("F4 产物", "至少有一份 xlsx 样品", False, "out/ 里没有 xlsx")
        return

    # 抽第一条，检查页首标题和模拟数据标注
    try:
        from openpyxl import load_workbook

        wb = load_workbook(xlsx[0])
        ws = wb.active
        head = " ".join(str(c.value) for row in ws.iter_rows(max_row=4) for c in row if c.value)
        record("F4 产物", "页首标题含「突合レポート」", "突合レポート" in head,
               f"实际页首：{head[:80]}")
        record("F4 产物", "标注为模拟数据", "模擬" in head or "模拟" in head,
               f"页首未标注模拟数据：{head[:80]}")
    except Exception as e:  # noqa: BLE001
        record("F4 产物", "xlsx 可读", False, f"{type(e).__name__}: {e}")


def f4_xlsx_layers() -> None:
    """日文化三层：表头 / 状态值 / 客户原始数据保留。

    只扫中文字符查不出「该换的没换」，这里逐层点名。
    """
    out = BASE / "out"
    xlsx = [p for p in out.iterdir() if p.is_file() and p.suffix == ".xlsx"] if out.exists() else []
    if not xlsx:
        record("F4 三层", "有 xlsx 可检", False, "out/ 里没有 xlsx")
        return

    from openpyxl import load_workbook

    ws = load_workbook(xlsx[0]).active
    all_cells = [str(c.value) for row in ws.iter_rows() for c in row if c.value is not None]
    joined = "\n".join(all_cells)

    # 层1 表头
    bad_headers = [
        h for h in ("差异类型", "定位", "原文", "建议修正值", "置信度", "判定状态", "人工确认")
        if h in joined
    ]
    record("F4 三层", "表头已日文化", not bad_headers,
           f"仍在报告里：{bad_headers}" if bad_headers else "")

    # 层2 状态值
    ja_states = ["要確認", "確認済み", "再発"]
    missing_ja = [s for s in ja_states if s not in joined]
    zh_states = [s for s in ("需确认", "已确认过", "上次已改") if s in joined]
    record("F4 三层", "状态值已日文化", not missing_ja and not zh_states,
           f"缺日文状态{missing_ja}；残留中文状态{zh_states}" if (missing_ja or zh_states) else "")

    # 层3 客户原始数据必须保留
    kana = re.compile(r"[\u3040-\u30ff]")
    kana_cells = [c for c in all_cells if kana.search(c)]
    record("F4 三层", "客户原始日文数据仍保留（没被翻译掉）", len(kana_cells) >= 5,
           f"只剩 {len(kana_cells)} 个含假名的内容，可能把客户数据也翻了")

    # 层5 定位/原文列的标签不能是中文写法
    #     「第 6 行」-> 日文「6 行目」（语序不同）
    #     注意：「数量」中日同码位同字形，不是中文残留，不纳入判定。
    #           （曾误判为简体字形，实测 '数量'=='数量' 为 True，收回）
    zh_labels = []
    if re.search(r"第\s*\d+\s*行", joined):
        zh_labels.append("「第 N 行」（日文应为「N 行目」）")
    if "样品" in joined:
        zh_labels.append("「样品」（日文用「サンプル」「見本」）")
    record("F4 三层", "定位/原文列的表签已日文化", not zh_labels,
           f"残留中文写法：{zh_labels}" if zh_labels else "")

    # 层6 图例不能列出「客户永远看不到」的状态。
    #     客户看到图例里有某个状态，会问「这个我什么时候能看到」，答不上来就是承诺过头。
    #     判据：图例里的每个状态，必须能被引擎真实产出（枚举里存在且有产出路径）。
    legend_states: list[str] = []
    for row in ws.iter_rows():
        cells = [str(c.value).strip() for c in row if c.value is not None]
        if not cells:
            continue
        joined_row = " ".join(cells)
        if "凡例" not in joined_row:
            continue
        for c in cells:
            v = c.replace("凡例：", "").replace("凡例", "").strip(" ：:")
            if v and v != "凡例":
                legend_states.append(v)
    if legend_states:
        try:
            import engine

            enum_count = len(list(engine.DiffStatus))
        except Exception:  # noqa: BLE001
            enum_count = 0
        # 报告层会把枚举的中文值翻成日文，所以不能按字面比对。
        # 真正要防的是「图例比枚举多」——多出来的分类客户永远看不到。
        record(
            "F4 三层",
            f"图例项数与状态数一致（图例 {len(legend_states)} 项 / 枚举 {enum_count} 个）",
            len(legend_states) == enum_count,
            (
                f"图例有 {len(legend_states)} 项但枚举只有 {enum_count} 个状态，"
                f"多出来的客户永远看不到：{legend_states}\n"
                f"      图例：{legend_states}"
            )
            if len(legend_states) != enum_count
            else "",
        )
    else:
        record("F4 三层", "图例只列实际存在的状态", False,
               "报告里没找到图例行，无法核对（图例可能被删了，或格式变了）")
    head_text = " ".join(
        str(c.value) for row in ws.iter_rows(max_row=3) for c in row if c.value is not None
    )
    zh_banner = [w for w in ("样品", "模拟数据", "本报告使用", "真实客户") if w in head_text]
    record("F4 三层", "页首横幅已日文化（不留中文/双语）", not zh_banner,
           f"横幅残留中文：{zh_banner} —— 代理店第一眼就是这行：{head_text[:90]}")


def f3_rulebook_roundtrip() -> None:
    """规则簿端到端：确认一次后，同样的数据重跑必须从 NEW 变成已确认。

    这条专门堵 review_gate.B4 的漏洞：B4 直接调 _classify() 测函数，
    函数本身没问题，但上游可能根本没调用到它。必须走完整 reconcile 流程才算数。
    """
    import engine
    from rulebook import VERDICT_KEEP_ORIGINAL, Rulebook

    rates = [engine.ContractRate("鈴木 次郎", "訪問介護（生活援助2)", 1250, "2026-04-01")]
    index = engine.build_rate_index(rates)

    def line(period: str) -> engine.LineItem:
        return engine.LineItem(1, "鈴木 次郎", "訪問介護（生活援助2)", 5, 1300, 6500, f"{period}-05")

    book = Rulebook(customer="RT")
    first, _ = engine.reconcile([line("2026-10")], index, "2026-10", plan=None)
    target = [d for d in first if d.diff_type == "单价与合同不一致"]
    assert target, "单价差异未产出，无法测试规则簿往返"
    assert target[0].status.name == "NEW", f"首次应为 NEW，实际 {target[0].status.name}"

    book.confirm(
        target[0].diff_type,
        target[0].fingerprint,
        VERDICT_KEEP_ORIGINAL,
        "2026-10",
        amount=getattr(target[0], "compare_amount", target[0].amount),
    )

    second, _ = engine.reconcile([line("2026-11")], index, "2026-11", plan=None, rulebook=book)
    again = [d for d in second if d.diff_type == "单价与合同不一致"]
    assert again, "第二个月单价差异未产出"
    got = again[0].status.name

    record(
        "F3 往返",
        "确认过的单价差异，次月不再要求重看",
        got != "NEW",
        f"指纹完全相同、规则簿里有记录，重跑仍是 {got}。"
        "通常是 confirm 存的金额和 reconcile 回填时传的金额不是同一个数"
        "（一个单价、一个合计）",
    )


def f3_negative_threshold() -> None:
    """反向验证：金额超过阈值时必须不命中。

    关键：规则簿「命中了」不能算证据，
    除非能看到「该不命中的也没命中」。只有正向没有反向，
    可能是金额比较根本没生效（空比较永远命中）。
    """
    import engine
    from rulebook import VERDICT_KEEP_ORIGINAL, Rulebook

    rates = [engine.ContractRate("鈴木 次郎", "訪問介護（生活援助2)", 1250, "2026-04-01")]
    index = engine.build_rate_index(rates)

    def line(price: float, period: str) -> engine.LineItem:
        return engine.LineItem(1, "鈴木 次郎", "訪問介護（生活援助2)", 1, price, price, f"{period}-05")

    book = Rulebook(customer="NEG")
    first, _ = engine.reconcile([line(1300, "2026-10")], index, "2026-10", plan=None)
    target = [d for d in first if d.diff_type == "单价与合同不一致"]
    assert target, "单价差异未产出"
    book.confirm(
        target[0].diff_type,
        target[0].fingerprint,
        VERDICT_KEEP_ORIGINAL,
        "2026-10",
        amount=getattr(target[0], "compare_amount", target[0].amount),
    )

    # 金额从 1,300 涨到 13,000（差 11,700 > 10,000 绝对档），必须重新要求确认
    far, _ = engine.reconcile([line(13000, "2026-11")], index, "2026-11", plan=None, rulebook=book)
    far_d = [d for d in far if d.diff_type == "单价与合同不一致"]
    assert far_d, "第二个月单价差异未产出"
    got = far_d[0].status.name
    record(
        "F3 反向",
        "金额超阈值时必须重新要求确认（不能靠旧规则放行）",
        got == "NEW",
        f"1,300 确认过一次后，13,000 仍被判为 {got}。"
        "说明阈值没有真正参与判断，规则簿只是「有记录就放行」",
    )


def f4_no_escaped_cjk() -> None:
    """禁止用 \\uXXXX 转义藏中文。

    红线：用转义让检查通过，而不是让问题消失，一律视为没修。
    输出一模一样还是中文，只是扫描器看不见字面量 —— 这让「绿」变成假的。

    注意：中文→日文映射表的「键」本来就必须是中文（引擎输出中文，靠键匹配），
    所以键里有中文是对的。转义的问题不是内容，而是让审查者看不见。
    还原成正常字符后，本项应通过；如果还原后其他项报红，那是检查恢复感知。
    """
    src = (BASE / "report.py").read_text(encoding="utf-8")
    hits = [
        f"L{n}"
        for n, line in enumerate(src.splitlines(), 1)
        if re.search(r"\\u[0-9a-fA-F]{4}", line)
    ]
    record(
        "F4 界面",
        "report.py 无 Unicode 转义藏中文",
        not hits,
        f"这些行用 \\u 转义写字面量：{hits[:8]}。还原成正常字符，"
        "让审查者能直接看见内容" if hits else "",
    )


def f4_mapping_values_are_ja() -> None:
    """映射表的值必须是日文。

    键是中文（引擎原文），值必须是日文 —— 这才叫翻译表。
    如果值也是中文，那这张表等于没起作用。
    """
    src = (BASE / "report.py").read_text(encoding="utf-8")
    # 还原转义后再判断内容
    try:
        decoded = src.encode("utf-8", "surrogateescape").decode("unicode_escape", "surrogateescape")
    except Exception:  # noqa: BLE001
        decoded = src
    bad = []
    for lineno, line in enumerate(decoded.splitlines(), 1):
        m = re.match(r'^\s*\(\s*"([^"]*)"\s*,\s*\n?\s*"([^"]*)"', line)
        if not m:
            continue
        _, value = m.group(1), m.group(2)
        if not value:
            continue
        if not re.search(r"[\u3040-\u30ff]", value):
            bad.append(f"L{lineno}: 值里没有假名，可能没翻译：{value[:34]}")
    record(
        "F4 界面",
        "中文→日文映射表的值确为日文",
        not bad,
        f"这些映射值不像日文：{bad[:5]}" if bad else "",
    )


def f4_engine_has_no_ui_text() -> None:
    """引擎不许产出面向客户的中文文案。

    locator / original 是引擎拼的，报告只是原样写入单元格。
    只查报告层永远查不到 —— 这就是 23/23 全绿但实物仍有 8 处中文的原因。
    """
    src = (BASE / "engine.py").read_text(encoding="utf-8")
    bad: list[str] = []
    in_doc = False
    # 中日写法必然不同的固定模式 —— 不能靠「是不是简体字」判断，
    # 「第」「行」中日通用，字表法会漏（这是我踩过的坑）。
    # 注意：「数量」中日同码位（实测 '数量'=='数量' 为 True），不是中文残留，不查。
    zh_patterns = [
        (re.compile(r"第\s*\{[^}]*\}\s*行"), "「第 {n} 行」应为「{n} 行目」"),
        (re.compile(r"缺失\s*\{"), "「缺失 {x}」应为「{x} が欠落」等日文写法"),
    ]
    for lineno, raw in enumerate(src.splitlines(), 1):
        line = raw.strip()
        if line.startswith(('"""', "'''")):
            in_doc = not in_doc
            continue
        if in_doc or line.startswith("#"):
            continue
        if any(k in line for k in ("raise ", "ContractDataError(")):
            continue
        code = raw.split("#", 1)[0]
        if not re.search(r"(locator|original|suggested)\s*=", code):
            continue
        for pat, why in zh_patterns:
            if pat.search(code):
                bad.append(f"L{lineno}: {why}")
    record(
        "F4 界面",
        "engine.py 不产出中文 locator/original/suggested",
        not bad,
        f"引擎给客户看的字段里还有中文写法：{bad[:6]}" if bad else "",
    )


def _guard(group: str, fn) -> None:
    """每组独立跑，一组崩了不影响其余组 —— 要看到全部缺口，不是一个一个修。"""
    start = len(RESULTS)
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        record(group, f"{fn.__name__} 执行", False, f"{type(e).__name__}: {e}")
    if len(RESULTS) == start:
        # 函数内部一条记录都没留下 —— 必须显式报出来，不能沉默通过
        record(group, f"{fn.__name__} 无产出结果", False, "该检查没有记录任何结果，视为未通过")


def main() -> int:
    _guard("F1 阈值", f1_threshold)
    _guard("F2 阻断项", f2_review_gate)
    _guard("F3 状态", f3_fourth_state)
    _guard("F3 往返", f3_rulebook_roundtrip)
    _guard("F3 反向", f3_negative_threshold)
    _guard("F4 界面", f4_ui_language)
    _guard("F4 界面", f4_no_escaped_cjk)
    _guard("F4 界面", f4_mapping_values_are_ja)
    _guard("F4 界面", f4_engine_has_no_ui_text)
    _guard("F4 产物", f4_outputs)
    _guard("F4 三层", f4_xlsx_layers)

    # 结果按分组稳定输出，不依赖 record() 的插入顺序
    group = ""
    for g, name, ok, detail in RESULTS:
        if g != group:
            print(f"\n── {g} " + "─" * 40)
            group = g
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        if not ok and detail:
            print(f"      {detail}")

    passed = sum(1 for _, _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{'=' * 60}\n{passed}/{total} 通过")
    if passed != total:
        print("不能交付。")
        return 1
    print("可以交付。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
