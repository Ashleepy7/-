"""真实素材核对：拿百度图片抓来的照片当"外部考卷"，看纯视觉分类有多准。

这些照片的文件名都是中性的 IMG_xxxx（分类引擎读不到题材提示），
但所在文件夹名就是我检索时的题材，可以当成参考答案对一下。

用法：python scripts/real_check.py     （输出 data/real_check.txt）
"""
from __future__ import annotations

import collections
import json
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core import config, db, llm                # noqa: E402

# 绕开解析缓存：本脚本现在只读 album.db、不调模型，所以用开头 clear 一次就够；
# 写在这里是为了以后接上检索时不会悄悄量到复读。
llm.clear_cache()

ROOT = str((config.DATA_DIR / "library" / "真实素材")).lower()

# 文件夹名 → 主题 id
FOLDER2ID = {m["name"]: tid for tid, m in config.THEMES.items()}

# 抓取脚本里的题材文件夹（与 fetch_real.py 的 TOPICS 保持一致）
TOPIC_DIRS = ["自然风光", "城市建筑", "人像自拍", "美食饮品", "动物宠物", "夜景暗光", "黑白影像"]


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """比例置信区间；样本小的题材用它才能判“是真涨跌还是抓阄”。"""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * (p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5
    return (max(0.0, (c - m) / d), min(1.0, (c + m) / d))


def _need_n(p: float, e: float, z: float = 1.96) -> int:
    """要把手径收到 ±e（比例置信区间半宽）所需的样本量（正态近似）。"""
    if not (0.0 < p < 1.0) or e <= 0:
        p = min(max(p, 0.02), 0.98)
    return int(-(-z * z * p * (1 - p) // (e * e)))   # ceil


def main() -> None:
    # 直接读库拿全字段（reasons / conf / rule_theme 不在对外接口的列表里，这里只用于自测）
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM photos").fetchall()]
    conn.close()
    real = [r for r in rows if ROOT in (r["folder"] or "").lower()]
    gt_n = sum(1 for r in real if (r.get("gt_theme") or "").strip())
    lines: list[str] = [f"真实素材照片数：{len(real)}",
                        f"参考答案：优先人工审核台的 gt_theme（已设 {gt_n} 张），其余用文件夹名",
                        ""]

    # 关键：模型接管的 theme 与规则产的 conf/reasons 是两套东西，必须分开统计，
    # 不许再把规则的置信度当成模型那套系统的自信度报出去（评测口径问题①④）。
    hit = 0
    matrix = collections.defaultdict(collections.Counter)
    stat = collections.Counter()
    # 按主题来源分桶：ai=模型判定 / rule=本地规则 / manual=人工
    by_src = {"ai": [0, 0], "rule": [0, 0], "manual": [0, 0]}   # [命中, 总数]
    confs_rule: list[int] = []
    # 分题材（以文件夹参考答案为准）：[命中, 总数]
    by_topic: dict[str, list[int]] = {t: [0, 0] for t in TOPIC_DIRS}
    wrongs: list[dict] = []
    # 同为“全库口径”的链路对比：都拿 260 张比，才说得清“模型接管是否合理”
    rule_all = [0, 0]      # [命中, 总数]：假如全库用规则(rule_theme备份)
    ai_all = [0, 0]        # 模型有输出(ai_theme 非空)的那些

    for r in real:
        folder = r["folder"] or ""
        tip = next((t for t in TOPIC_DIRS if t in folder), "")
        gt = (r.get("gt_theme") or "").strip()
        expect = gt or FOLDER2ID.get(tip, "")     # 人工真值优先，没人工设定才用文件夹名
        src = db.theme_src(r)
        ok = r["theme"] == expect
        if ok:
            hit += 1
        matrix[tip][r["theme"]] += 1
        stat[r["theme"]] += 1
        rule_all[1] += 1
        rule_all[0] += int((r.get("rule_theme") or "") == expect and bool(r.get("rule_theme")))
        ai_v = r.get("ai_theme") or ""
        if ai_v:
            ai_all[1] += 1
            ai_all[0] += int(ai_v == expect)
        if src in by_src:
            by_src[src][1] += 1
            by_src[src][0] += int(ok)
        if src == "rule":
            confs_rule.append(int(round((r["conf"] or 0) * 100)))
        if tip in by_topic:
            by_topic[tip][1] += 1
            by_topic[tip][0] += int(ok)
        if not ok:
            wrongs.append({"tip": tip, "r": r, "src": src, "expect": expect})

    n = len(real)
    lines.append(f"—— 合计主题命中率（模型+规则+人工混合来源，只比趋势）——")
    lo, hi = _wilson(hit, n)
    lines.append(f"**{hit} / {n} = {round(100 * hit / max(1, n))}%**　95%CI {lo:.0%}~{hi:.0%}")
    srcn = collections.Counter(db.theme_src(r) for r in real)
    lines.append(f"　来源构成：模型 {srcn.get('ai', 0)} 张、规则 {srcn.get('rule', 0)} 张、"
                 f"人工 {srcn.get('manual', 0)} 张")
    rlo, rhi = _wilson(rule_all[0], rule_all[1])
    lines.append("")
    lines.append("—— 链路对比（同为全库 260 张口径，证模型接管合理）——")
    if ai_all[1]:
        alo, ahi = _wilson(ai_all[0], ai_all[1])
        lines.append(f"　若主题全听模型(ai_theme)：{ai_all[0]}/{ai_all[1]} = "
                     f"{round(100 * ai_all[0] / ai_all[1])}%　95%CI {alo:.0%}~{ahi:.0%}")
    lines.append(f"　若主题全走规则(rule_theme)：{rule_all[0]}/{rule_all[1]} = "
                 f"{round(100 * rule_all[0] / max(1, rule_all[1]))}%　95%CI {rlo:.0%}~{rhi:.0%}")
    lines.append("　→ 两个都是“拿同一批图、只换决策引擎”的同口径对比，故可直接比高低；"
                 "与下面“分来源”那组不同（那是按实际接管者分组、人群不一样）。")
    lines.append("")

    lines.append("—— 分开报的两套证据（按实际接管者分组；与上面同口径链路对比不是一回事）——")
    for key, label in (("ai", "【模型证据】视觉模型判定的主题"),
                       ("rule", "【规则证据】本地规则判定的主题"),
                       ("manual", "【人工】用户手动设定的主题")):
        h_k, n_k = by_src[key]
        if n_k == 0:
            continue
        wlo, whi = _wilson(h_k, n_k)
        extra = ""
        if key == "rule" and confs_rule:
            avg = round(sum(confs_rule) / len(confs_rule))
            low = sum(1 for c in confs_rule if c < config.CONF_THRESHOLD * 100)
            extra = f"　规则平均置信度 {avg}%（仅此 {len(confs_rule)} 张；低于 "\
                    f"{round(config.CONF_THRESHOLD * 100)}% 的有 {low} 张）"
        lines.append(f"{label}：{h_k}/{n_k} = {round(100 * h_k / n_k)}%　"
                     f"95%CI {wlo:.0%}~{whi:.0%}{extra}")
    lines.append("　注：模型接管的照片不产出规则置信度/理由，上面那行“规则平均置信度”"
                 "只描述规则那批，别当成 92% 那套系统的自信度。")
    lines.append(f"实际落位分布：{dict(stat)}")
    lines.append("")

    lines.append("—— 每个题材的去向（参考答案 → 实际分类；含样本量与 95%CI，"
                 "n<40 时单题材涨跌不可信）——")
    for tip in TOPIC_DIRS:
        row = matrix[tip]
        total = sum(row.values())
        ok_h, ok_n = by_topic[tip]
        tlo, thi = _wilson(ok_h, ok_n)
        parts = "，".join(f"{config.theme_meta(k)['name']}×{v}" for k, v in row.most_common())
        flag = "　⚠样本偏小" if ok_n < 40 else ""
        note = "　（互斥主题旧口径：黑白已改按下方风格轴测）" if tip == "黑白影像" else ""
        lines.append(f"{tip}（{total} 张）准 {ok_h} = {round(100 * ok_h / max(1, ok_n))}%"
                     f"　95%CI {tlo:.0%}~{thi:.0%}{flag}{note} → {parts}")

    # —— 黑白风格轴（②）：mono 已从 9 选 1 互斥池移出，改按正交风格测 ——
    lines.append("")
    lines.append("—— 黑白风格轴（mono 已移出互斥主题，改按正交风格测；本地特征免费回填、未重标注）——")
    mtp = mfn = mfp = 0
    fp_from = collections.Counter()
    for r in real:
        t = next((x for x in TOPIC_DIRS if x in (r["folder"] or "")), "")
        pos = int(r.get("mono_style") or 0) == 1
        gold = (t == "黑白影像")
        if gold and pos:
            mtp += 1
        elif gold and not pos:
            mfn += 1
        elif pos and not gold:
            mfp += 1
            fp_from[t] += 1
    ngold = mtp + mfn
    mlo, mhi = _wilson(mtp, ngold)
    npred = mtp + mfp
    lines.append(f"　风格召回：黑白影像文件夹被标 mono_style=1 的 {mtp}/{ngold} = "
                 f"{round(100 * mtp / max(1, ngold))}%　95%CI {mlo:.0%}~{mhi:.0%}")
    lines.append(f"　风格精确：所有 mono_style=1 中确属黑白文件夹的 {mtp}/{npred} = "
                 f"{round(100 * mtp / max(1, npred))}%（其它文件夹误报 {mfp} 张：{dict(fp_from)}）")
    lines.append("　→ 旧口径里黑白人像被‘判成 portrait’算错；新口径下它同时=人像(内容)+"
                 "mono_style=1(风格)，不再矛盾。漏掉的　多=淡彩/低饱和，靠本地特征不够，待重标注补。")

    lines.append("")
    lines.append("—— 按题材扩样需求（现半宽太宽的收不到 ±10pp，标出还差多少张才能判单题材涨跌）——")
    lines.append("　（黑白影像用风格轴召回算，其余用主题命中率算；±e=想收到的半宽）")
    want = [("黑白影像", mtp, ngold)]
    for tp in TOPIC_DIRS:
        if tp == "黑白影像":
            continue
        h, t = by_topic[tp]
        want.append((tp, h, t))
    for name, h, t in want:
        p = h / max(1, t)
        lo10 = _need_n(p, 0.10)
        lo7 = _need_n(p, 0.07)
        cur = _wilson(h, t)
        hw = (cur[1] - cur[0]) / 2 * 100
        if t >= lo10:
            lines.append(f"　{name}（n={t}, 现±{hw:.0f}pp）已达 ±10pp，无需扩样")
        else:
            lines.append(f"　{name}（n={t}, 现±{hw:.0f}pp, p={round(100 * p)}%）"
                         f"→ ±10pp 需 n≈{lo10}（还差 {lo10 - t}）；±7pp 需 n≈{lo7}（还差 {lo7 - t}）")
    lines.append("")
    lines.append("—— 判错的样张（标明这张主题是谁定的；规则的置信/理由只贴在规则判定的那张上）——")
    shown = 0
    for w in wrongs:
        r, tip, src = w["r"], w["tip"], w["src"]
        head = f"[{tip}] {pathlib.Path(r['path']).name} → 判为 {config.theme_meta(r['theme'])['name']}"
        if src == "rule":
            reasons = json.loads(r["reasons"] or "[]")
            lines.append(head + f"｜主题来源=规则（置信 {round((r['conf'] or 0) * 100)}%）："
                         f"{' / '.join(reasons[:3])}")
        else:
            who = "模型" if src == "ai" else "人工"
            rt = r.get("rule_theme") or ""
            rtname = config.theme_meta(rt)["name"] if rt else "（无）"
            lines.append(head + f"｜主题来源={who}——这是 {who} 的判定，不附规则置信度/理由；"
                         f"仅供参考：规则那套会给「{rtname}」（非本次判定依据）")
        shown += 1
        if shown >= 25:
            lines.append("……（其余略）")
            break

    out = config.DATA_DIR / "real_check.txt"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已写入 {out}")


if __name__ == "__main__":
    main()
