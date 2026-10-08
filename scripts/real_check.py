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


def main() -> None:
    # 直接读库拿全字段（reasons / conf 不在对外接口的列表里，这里只用于自测）
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM photos").fetchall()]
    conn.close()
    real = [r for r in rows if ROOT in (r["folder"] or "").lower()]
    lines: list[str] = [f"真实素材照片数：{len(real)}", ""]

    hit = 0
    stat = collections.Counter()
    matrix = collections.defaultdict(collections.Counter)
    confs: list[int] = []
    srcs: collections.Counter = collections.Counter()
    confs_rule: list[int] = []

    for r in real:
        folder = r["folder"] or ""
        tip = next((t for t in TOPIC_DIRS if t in folder), "")
        expect = FOLDER2ID.get(tip, "")
        matrix[tip][r["theme"]] += 1
        confs.append(int(round((r["conf"] or 0) * 100)))
        # conf 是规则分类器的打分；模型接管过主题的照片，这个数跟显示的主题无关
        src = db.theme_src(r)
        srcs[src] += 1
        if src == "rule":
            confs_rule.append(int(round((r["conf"] or 0) * 100)))
        stat[r["theme"]] += 1
        if r["theme"] == expect:
            hit += 1

    lines.append(f"**纯视觉命中参考答案：{hit} / {len(real)} = {round(100 * hit / max(1, len(real)))}%**")
    lines.append(f"平均置信度（规则打分，仅对主题来自本地规则的照片有意义）："
                 f"{round(sum(confs) / max(1, len(confs)))}%　"
                 f"低于 {round(config.CONF_THRESHOLD * 100)}% 的有 {sum(1 for c in confs if c < config.CONF_THRESHOLD * 100)} 张")
    lines.append(f"　主题来源分布：{dict(srcs)}"
                 + (f"　— 其中确实来自规则的 {len(confs_rule)} 张，打分平均 "
                    f"{round(sum(confs_rule) / len(confs_rule))}%" if confs_rule else "　— 本批已无规则主题"))
    lines.append(f"实际落位分布：{dict(stat)}")
    lines.append("")

    lines.append("—— 每个题材的去向（参考答案 → 实际分类）——")
    for tip in TOPIC_DIRS:
        row = matrix[tip]
        total = sum(row.values())
        parts = "，".join(f"{config.theme_meta(k)['name']}×{v}" for k, v in row.most_common())
        ok = row.get(FOLDER2ID.get(tip, ""), 0)
        lines.append(f"{tip}（{total} 张）准 {ok} → {parts}")

    lines.append("")
    lines.append("—— 判错的样张（看它当时给了什么理由，方便决定要不要补规则）——")
    shown = 0
    for r in real:
        folder = r["folder"] or ""
        tip = next((t for t in TOPIC_DIRS if t in folder), "")
        if r["theme"] == FOLDER2ID.get(tip, ""):
            continue
        reasons = json.loads(r["reasons"] or "[]")
        lines.append(f"[{tip}] {pathlib.Path(r['path']).name} → 判为 {config.theme_meta(r['theme'])['name']}"
                     f"（置信 {round((r['conf'] or 0) * 100)}%）：{' / '.join(reasons[:3])}")
        shown += 1
        if shown >= 25:
            lines.append("……（其余略）")
            break

    out = config.DATA_DIR / "real_check.txt"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已写入 {out}")


if __name__ == "__main__":
    main()
