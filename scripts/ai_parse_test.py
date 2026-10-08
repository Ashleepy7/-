"""AI 查找解析自测：打印每句话被理解成的条件，人工核对是否符合直觉。

用法：python scripts/ai_parse_test.py
"""
from __future__ import annotations

import datetime as dt  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import query  # noqa: E402

SENTENCES = [
    # —— 时间 ——
    "去年夏天在海边拍的照片",
    "2023年3月的截图",
    "最近三个月的照片",
    "三年前的全家福",
    "国庆出去玩拍的",
    "上个月的本月总结",
    "今年上半年的聚会",
    "5月2日那天",
    "第三季度的照片",
    "差不多半年前拍的宝宝",
    "2024年的夜景",
    "寒假在老家的照片",
    # —— 内容 ——
    "宝宝的照片",
    "上次聚餐吃的",
    "有没有我家猫的拍照",
    "婚礼上的合影",
    "发票和二维码的截图",
    "城市建筑天际线",
    "黑白照片",
    "夜景霓虹灯",
    # —— 画面感觉 ——
    "晚上拍的清晰一点的照片",
    "颜色很鲜艳的花",
    "背景干净的证件照",
    "大场景远景的风光",
    "竖图的截图",
    "文件大的高清图",
    # —— 组合 / 刁钻 ——
    "我记得大概前年冬天在下雪的地方",
    "帮我找找周末和闺蜜吃饭的照片",
    "单反拍的有定位的风景",
    "iphone拍的",
    "微信保存的那些图",
    "去年在云南古城里拍的",
    "里面颜色比较素的那张照片",   # 单字「面」不应把主题限死成美食
    "上面的照片",                 # 无实义
    "今年上半年的事",              # 不应误读成「最近半年」
    "国庆那周的",                  # 正在国庆周内时不应回退到去年
    "和闺蜜吃饭的照片",            # 人像为硬条件，美食只加分
]


def span_text(c: dict) -> str:
    """把时间条件的 SQL 参数还原成人眼能核对的日期。"""
    if c["kind"] != "time" or not c.get("sql"):
        return ""
    args = c["sql"][1]
    if all(isinstance(a, str) for a in args):
        return " 月份" + "/".join(args)
    days = [dt.date.fromtimestamp(a) for a in args[0::2]]
    ends = [dt.date.fromtimestamp(b) for b in args[1::2]]
    return " 区间" + "、".join(f"{a}~{b}" for a, b in zip(days, ends))


def main() -> int:
    lines: list[str] = []

    def out(s: str = "") -> None:
        lines.append(s)

    for s in SENTENCES:
        p = query.parse(s)
        out(f"\n「{s}」")
        if not p["conds"]:
            out("   （没解析出任何条件）")
            continue
        for c in p["conds"]:
            out(f"   [{c['kind']:<8}]{'硬' if c['hard'] else '软'} w={c['w']:<5} "
                f"{c['label']}{span_text(c)}")
    out(f"\n共 {len(SENTENCES)} 句。")
    report = "\n".join(lines)
    print(report)
    dest = Path(__file__).resolve().parents[1] / "data" / "ai_parse_report.txt"
    dest.write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
