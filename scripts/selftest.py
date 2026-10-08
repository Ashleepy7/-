"""引擎自测：生成示例照片 → 提取特征 → 主题分类，输出命中率与混淆情况。

用法：
    python scripts/selftest.py            # 概览
    python scripts/selftest.py --verbose  # 打印每张图的判定依据
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import classifier, features  # noqa: E402
from core.config import SAMPLE_DIR, THEMES, ensure_dirs  # noqa: E402
from core.samples import generate  # noqa: E402

VERBOSE = "--verbose" in sys.argv


def main() -> int:
    ensure_dirs()
    paths = generate(per_theme=6)
    print(f"示例照片 {len(paths)} 张 → {SAMPLE_DIR}\n")
    ok = ok_nohint = total = 0
    misses: dict[str, list[str]] = defaultdict(list)
    score_rows: list[tuple[str, dict[str, float]]] = []

    for p in paths:
        truth = Path(p).parent.name
        data = features.analyze(p)
        theme, conf, scores, reasons = classifier.classify(data["feats"])
        nohint_feats = dict(data["feats"])
        nohint_feats["name_hint"] = {}
        t2, c2, _, _ = classifier.classify(nohint_feats)
        total += 1
        ok += theme == truth
        ok_nohint += t2 == truth
        score_rows.append((truth, scores))
        flag = "✅" if theme == truth else "❌"
        note = "" if t2 == truth else f"   (去掉文件名关键词后→{t2})"
        if theme != truth:
            misses[f"{truth} → {theme}"].append(Path(p).name)
        if VERBOSE or theme != truth:
            f = data["feats"]
            print(f"{flag} {truth:<11} → {theme:<11} conf={conf:.2f}{note}")
            print(f"   特征 sky={f.get('sky_ratio',0):.2f} green={f.get('green_ratio',0):.2f} "
                  f"warm={f.get('warm_ratio',0):.2f} skin={f.get('skin_ratio',0):.2f} "
                  f"blob={f.get('skin_blob',0):.2f} face={f.get('face_like',0):g} "
                  f"sat={f.get('sat_mean',0):.2f} line={f.get('line_score',0):g} "
                  f"conc={f.get('orient_conc',0):.2f} flat={f.get('palette_flat',0):.2f} "
                  f"white={f.get('white_bg',0):.2f} bright={f.get('brightness',0):.2f} "
                  f"无EXIF={1 - f.get('has_camera', 0):g}")
            print(f"   得分 {sorted(((round(v,2), k) for k, v in scores.items()), reverse=True)[:4]}")
            print(f"   依据 {'；'.join(reasons)}")

    print(f"\n命中率（含文件名关键词）：{ok}/{total} = {ok / max(total,1):.0%}")
    print(f"命中率（仅画面特征）  ：{ok_nohint}/{total} = {ok_nohint / max(total,1):.0%}")
    for key, names in misses.items():
        print(f"  误判 {key}：{len(names)} 张，例如 {names[:3]}")

    print("\n各类主题的平均得分分布：")
    print(f"{'主题':<12}" + "".join(f"{THEMES[t]['name'][:3]:>8}" for t in THEMES if t != "other"))
    for truth in sorted({t for t, _ in score_rows}):
        rows = [s for t, s in score_rows if t == truth]
        line = f"{truth:<12}"
        for tid in THEMES:
            if tid == "other":
                continue
            avg = sum(r.get(tid, 0) for r in rows) / max(len(rows), 1)
            line += f"{avg:>8.1f}"
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
