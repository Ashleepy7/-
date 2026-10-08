"""AI 查找端到端自测：对一批口语句子跑真实检索，看理解、命中与放宽是否合理。

用法：python scripts/ai_search_test.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import db, query  # noqa: E402

QUESTIONS = [
    "去年夏天在海边拍的照片",
    "宝宝的照片",
    "上次聚餐吃的",
    "有没有猫的照片",
    "截图发票",
    "夜景",
    "黑白的照片",
    "城市建筑",
    "自然风光",
    "最近一个月的照片",
    "2019年的照片",              # 库里多半没有 → 应触发放宽或空结果
    "晚上拍的清晰的",
    "颜色鲜艳的",
    "文件很大的图",
    "上面的照片",                # 没实义 → 应给出「没听懂」提示
    "吃饭",
    "云南",                      # 只能靠文件名/文件夹线索
]


def main() -> int:
    db.init_db()
    lines: list[str] = [f"库中共 {db.stats()['total']} 张照片", ""]
    for q in QUESTIONS:
        res = query.search(q, limit=5)
        lines.append(f"「{q}」  候选 {res['scanned']} 张 → 命中 {res['total']} 张")
        for c in res["conds"]:
            tag = "已忽略" if c["ignored"] else ("硬" if c["hard"] else "软")
            lines.append(f"   - [{c['kind']}] {tag} {c['label']}")
        if res["note"]:
            lines.append(f"   note: {res['note']}")
        if res["relaxed"]:
            lines.append(f"   relaxed: {'、'.join(res['relaxed'])}")
        for it in res["items"][:4]:
            day = time.strftime("%Y-%m-%d", time.localtime(it["taken_at"] or it["mtime"] or 0))
            lines.append(f"      {it['match']:>3}%  {it['theme'] or '?':<11} {day:<11} "
                         f"{it['filename'][:34]}  ← {' / '.join(it['hits'])}")
        lines.append("")
    report = "\n".join(lines)
    dest = Path(__file__).resolve().parents[1] / "data" / "ai_search_report.txt"
    dest.write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
