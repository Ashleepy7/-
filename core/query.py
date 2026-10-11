"""AI 查找：把一句中文口语（时间 + 内容 + 画面感觉）解析成检索条件，在本地库里打分召回。

两条理解路径：
- AI 模型（core/llm.py，云端要 API Key，本地模型不用）：负责“这句话到底在说什么”与“图里有什么”；
- 本地规则（本文件后半部分的词表与正则）：没配好 / 连不上 / 额度用完时自动回落。
两条路径产出同一种条件结构，后续的 SQL 缩小候选、逐张打分、硬条件放宽、命中理由
都由本地完成，因此“我理解成了什么”和“为什么是这张”始终可解释。
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Callable, Iterable

from . import classifier, db, llm
from .config import THEMES

# ---------------------------------------------------------------- 基础词表

_CN_INT = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
           "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}

NUM = r"(?:\d{1,2}|十[一二]?|[一二两三四五六七八九十])"

# 内容词 → 主题
THEME_WORDS: dict[str, list[str]] = {
    "portrait": ["人像", "自拍", "合影", "合照", "证件照", "写真", "肖像", "人物", "人们",
                 "人家", "亲人", " selfie",
                 "宝宝", "孩子", "小孩", "女儿", "儿子", "婴儿", "娃", "孕妇",
                 "家人", "全家福", "一家", "老婆", "女朋友", "男友", "闺蜜", "同学",
                 "老师", "毕业", "婚纱照", "婚礼", " selfie"],
    "nature": ["风景", "风光", "自然", "大自然", "山", "雪山", "海边", "海滩", "沙滩", "海岛",
               "湖", "河", "海", "草原", "森林", "树林", "树", "花", "樱花", "荷花", "菊花",
               "天空", "云", "日出", "日落", "晚霞", "朝霞", "彩虹", "雪景", "沙漠", "瀑布",
               "田野", "农村", "公园", "植物园", "露营", "徒步", "郊外", "草地", "春天", "秋天",
               "大海", "白云", "云朵", "树木", "树叶", "鲜花", "花朵", "花园", "开花", "花海"],
    "city": ["建筑", "城市", "楼", "高楼", "房子", "街道", "马路", "巷子", "桥", "塔",
             "天际线", "摩天轮", "地铁", "火车站", "机场", "高铁", "公交", "广场", "老城",
             "古城", "教堂", "寺庙", "校园", "工厂", "工地", "玻璃幕墙", "街景"],
    "food": ["美食", "吃的", "好吃", "餐", "吃饭", "饭", "米饭", "菜", "菜肴", "火锅",
             "烧烤", "串串", "面", "面条", "面食", "小笼包", "寿司", "刺身", "披萨", "汉堡",
             "包子", "饺子", "零食", "水果", "蛋糕", "甜品", "点心",
             "面包", "咖啡", "茶", "饮茶", "喝茶", "饮料", "酒", "啤酒", "白酒", "奶茶", "聚餐",
             "年夜饭", "外卖", "餐厅", "饭店", "食堂", "自助餐", "下午茶", "早餐", "夜宵"],
    "animal": ["猫", "猫咪", "小猫", "狗", "狗狗", "小狗", "犬", "宠物", "毛孩子", "兔子", "鸟",
               "小鸟", "小马", "小鸡", "小鱼", "马", "牛", "奶牛", "羊", "绵羊", "猪", "鸡", "鸭",
               "熊猫", "动物园", "野生动物", "鱼", "金鱼",
               "水族", "松鼠", "刺猬", "蜜蜂", "蝴蝶", "恐龙"],
    "night": ["夜景", "夜晚", "夜里", "晚上", "夜色", "霓虹", "灯光", "路灯", "星空",
              "星星", "月亮", "烟花", "焰火", "演唱会", "音乐节", "酒吧", "夜市", "烛光"],
    # mono（黑白）已从互斥主题词表移出：它是与内容正交的风格轴，由 _look_conds 的
    # mono_style 条件负责，不再把“黑白人像”强行归成 theme=mono。
    "screenshot": ["截图", "屏幕", "截屏", "长截图", "文档", "文字", "资料", "课件",
                   "ppt", "幻灯片", "表格", "二维码", "发票", "收据", "合同", "证书",
                   "账单", "订单", "聊天记录", "备忘录", "网页", "代码", "界面", "游戏画面"],
}

# 季节 → 月份
SEASONS = {"春": (3, 4, 5), "夏": (6, 7, 8), "秋": (9, 10, 11), "冬": (12, 1, 2)}

# 公历近似节日 → (月, 日) 区间
HOLIDAYS = {
    "元旦": ((1, 1), (1, 3)), "过年": ((1, 15), (2, 20)), "春节": ((1, 15), (2, 20)),
    "情人节": ((2, 12), (2, 16)), "妇女节": ((3, 7), (3, 9)), "清明": ((4, 3), (4, 8)),
    "劳动节": ((5, 1), (5, 5)), "五一": ((5, 1), (5, 5)), "青年节": ((5, 3), (5, 6)),
    "儿童节": ((6, 1), (6, 2)), "端午": ((5, 25), (6, 25)), "暑假": ((7, 1), (8, 31)),
    "七夕": ((8, 10), (8, 25)), "中秋": ((9, 10), (10, 10)), "国庆": ((10, 1), (10, 8)),
    "十一": ((10, 1), (10, 8)), "双十一": ((11, 8), (11, 12)), "圣诞": ((12, 22), (12, 26)),
    "寒假": ((1, 15), (2, 20)), "跨年": ((12, 29), (12, 31)),
}

# 时段 → 小时区间
DAYPARTS = [
    (("凌晨", "深夜", "午夜"), 0, 6),
    (("清早", "清晨", "早晨", "早上", "一早"), 5, 9),
    (("上午", "午前"), 8, 11),
    (("中午", "午间", "正午", "午休"), 11, 13),
    (("下午", "午后"), 13, 18),
    (("傍晚", "黄昏", "日落时分"), 17, 20),
    (("晚上", "夜里", "夜晚", "夜间", "晚间", "下晚"), 19, 24),
    (("白天", "日光", "阳光下"), 6, 18),
]

COLOR_FEATS = {
    "绿": ("green_ratio", 0.22, "绿"), "绿色": ("green_ratio", 0.22, "绿"),
    "蓝": ("blue_ratio", 0.20, "蓝"), "蓝色": ("blue_ratio", 0.20, "蓝"),
    "蓝天": ("blue_ratio", 0.22, "蓝"),
    "红": ("warm_ratio", 0.20, "暖红"), "橙色": ("warm_ratio", 0.20, "暖橙"),
    "暖色": ("warm_ratio", 0.20, "暖色"),
    "紫": ("purple_ratio", 0.06, "紫"), "粉": ("purple_ratio", 0.05, "粉"),
    "棕": ("brown_ratio", 0.16, "棕"), "大地色": ("brown_ratio", 0.16, "棕褐"),
}

# 画面感觉词 → (说明, 判定函数)
VISUALS: list[tuple[list[str], str, Callable[[dict], bool], float]] = [
    (["明亮", "很亮", "亮堂", "阳光", "通透", "高调"], "画面偏亮",
     lambda f: f.get("brightness", 0) > 0.58, 1.3),
    (["暗", "昏暗", "阴沉", "低调", "黑漆漆"], "画面偏暗",
     lambda f: f.get("brightness", 1) < 0.34 or f.get("dark_ratio", 0) > 0.24, 1.3),
    (["鲜艳", "色彩丰富", "五颜六色", "颜色多", "浓郁"], "色彩鲜艳",
     lambda f: f.get("colorfulness", 0) > 0.22 and f.get("sat_mean", 0) > 0.28, 1.3),
    (["素", "淡雅", "性冷淡", "颜色少", "单调"], "色彩素净",
     lambda f: f.get("colorfulness", 1) < 0.12 or f.get("sat_mean", 1) < 0.16, 1.2),
    (["清晰", "高清", "细节", "锐利"], "画面清晰",
     lambda f: f.get("center_sharp", 0) > 0.06 and f.get("strong_edge", 0) > 0.05, 1.2),
    (["模糊", "糊", "虚化", "失焦", "朦胧"], "画面偏软/糊",
     lambda f: f.get("center_sharp", 1) < 0.03, 1.2),
    (["背景干净", "背景简洁", "纯色背景", "证件"], "背景均匀简洁",
     lambda f: f.get("bg_uniform", 0) > 0.5, 1.2),
    (["特写", "近景", "微距", "主体突出"], "主体近景特写",
     lambda f: f.get("subject_center", 0) > 0.18 and f.get("edge_density", 1) < 0.22, 1.2),
    (["大场景", "远景", "全景", "开阔", "广角"], "开阔大场景",
     lambda f: f.get("edge_density", 1) < 0.12 and f.get("sky_ratio", 0) > 0.1, 1.2),
    (["横图", "横向", "宽幅", "全景横"], "横向构图",
     lambda f: f.get("aspect", 0) >= 1.25, 1.0),
    (["竖图", "纵向", "长图", "手机竖屏"], "竖向构图",
     lambda f: 0 < f.get("aspect", 1) <= 0.85, 1.0),
    (["方正", "方形"], "接近正方形",
     lambda f: 0.88 <= f.get("aspect", 0) <= 1.12, 1.0),
]

# 单字内容词：「猫」「狗」这类几乎不会误伤，可当强过滤；
# 「面 / 花 / 海 / 人」这类常出现在「里面 / 花瓣 / 海量 / 别人」里，只能当加分项。
# 「鹅」被移出强字集：白鹅≠天鹅≠企鹅是不同动物，靠一个「鹅」字会把「白天鹅」
# 命到「白鹅」照片上（闭卷评测 C 组反例就是这么漏的）。单独搜「鹅」仍走整词命中。
STRONG_SINGLE = {"猫", "狗", "犬", "娃", "兔", "龟", "蛇", "鹰", "鸭", "喵", "汪"}

# 这些单字几乎必然出现在别的词里（云南 / 上面 / 别人 / 喝茶 / 骑马 …），
# 干脆不参与主题判断，宁可漏一个弱信号，也不想“上面的照片”读成“找美食”。
RISKY_SINGLE = {
    "面", "云", "人", "花", "海", "树", "茶", "酒", "菜", "饭", "包", "马", "牛", "羊",
    "鸡", "图", "照", "文", "字", "门", "路", "房", "桥", "楼", "塔", "河", "湖", "碗",
}

# 停用词：切剩余名词时用
STOPS = [
    "有没有", "帮我", "找找", "找一下", "我想看", "想看", "看看", "查找", "搜索", "哪些",
    "哪里", "什么", "怎么", "几个", "一些", "那些", "这些", "全部", "所有", "照片", "相片",
    "图片", "照", "片", "的", "了", "吗", "呢", "吧", "呀", "哦", "啊", "就", "是", "有",
    "和", "与", "及", "或者", "还是", "但", "把", "给", "我", "你", "他", "她", "它们",
    "时候", "那会儿", "那段", "时间", "拍", "照过", "记录", "大概", "好像", "记得", "大约",
    "关于", "里面", "里", "中", "上", "下", "过", "地", "得", "很", "太", "比较", "一点",
    "点儿", "吧", "嘛", "就是", "还有", "以及", "这种", "那种", "某", "整个", "去年", "今年",
    "最近", "上个", "这个", "上月", "本月", "当月", "上半年", "下半年", "季度", "半个月",
    "昨天", "前天", "以前", "之前", "左右", "大约", "差不多", "那种", "来着", "一下",
    "那天", "哪天", "当天", "出去玩", "去玩", "留着", "存着", "拍的", "拍过", "一批",
    "好多", "每一", "一下", "来着", "这样", "那样", "某些",
    "颜色", "色彩", "画面", "看上去", "看着", "感觉", "有点", "应该是", "好像", "那些",
]

SCAN_CAP = 8000          # 单次 AI 检索一次最多读多少张（按拍摄时间倒序）

# 放宽的“够看”门槛：结果少于这个数、且还有可放宽的硬条件时，继续放宽下一条，
# 而不是“有一两张能看就停”。只放宽排序序列里的可松条件（时间→文字线索→画面→不含），
# 绝不为凑数去碰内容/主题硬门禁——那等于拿不相干的图填满三张（C 组反例就是这么防的）。
MIN_RELAX_RESULTS = 3

# 给界面当“试试这样问”的示例
EXAMPLES = [
    "去年夏天在海边拍的照片",
    "宝宝的照片",
    "上次聚餐吃的",
    "国庆出去玩拍的",
    "发票和二维码的截图",
    "夜景和烟花的照片",
    "黑白的人像",
    "上个月文件很大看不清的图",
]

WORD2THEME: dict[str, str] = {}
for _tid, _ws in THEME_WORDS.items():
    for _w in _ws:
        _w = _w.strip().lower()
        if _w and _w not in RISKY_SINGLE:
            WORD2THEME[_w] = _tid

HOLIDAY_KEYS = sorted(HOLIDAYS, key=len, reverse=True)
SEASON_KEYS = ["春天", "春季", "夏天", "夏季", "盛夏", "秋天", "秋季", "金秋",
               "冬天", "冬季", "隆冬"]
DAYPART_KEYS: list[tuple[str, int, int]] = []
for _hs, _a, _b in DAYPARTS:
    for _h in _hs:
        DAYPART_KEYS.append((_h, _a, _b))
DAYPART_KEYS.sort(key=lambda x: -len(x[0]))

# 时段词（白天/傍晚/夜里…）只有“确实在说时间”时才生成条件。
# 判定：词后紧跟虚词/标点/数字/串尾，或紧跟时间提示字 → 算在说时段；
# 词后紧贴其它实义 CJK 字（如“白天鹅”的“鹅”）→ 时段词被嵌进了名词里，不当时间词。
# 不删词表，只是加一层“看紧跟字”的上下文判定（评测反例：
# “白天鹅”被切成“白天”+“鹅”，软时段命中把整库 292 张全拉进来）。
_TIME_FOLLOW_OK = set(
    "的了在时地得，。、；！？：,.!?;: "
    "拍照那这去来到起过就很太和与从向朝前后左右上下每整又刚才正零点半分"
    "0123456789"          # “晚上八点”这类接小时数字也算时间
)


def _daypart_hit(text: str) -> tuple[str, int, int] | None:
    """返回第一个“真的在说时间”的时段词；被嵌进名词的（白天鹅）跳过。"""
    for word, a, b in DAYPART_KEYS:
        idx = text.find(word)
        while idx != -1:
            end = idx + len(word)
            nxt = text[end:end + 1]
            if (not nxt) or nxt in _TIME_FOLLOW_OK:
                return word, a, b
            idx = text.find(word, end)     # 这一处嵌在词里，找下一处
    return None
COLOR_KEYS = sorted(COLOR_FEATS, key=len, reverse=True)
VISUAL_KEYS: list[tuple[str, int, Callable[[dict], bool], float]] = []
for _ws, _label, _fn, _w in VISUALS:
    for _x in _ws:
        VISUAL_KEYS.append((_x, _w, _fn, _label))
VISUAL_KEYS.sort(key=lambda x: -len(x[0]))
STOP_RE = re.compile("|".join(re.escape(w) for w in sorted(STOPS, key=len, reverse=True)))

# 切碎后剥掉两端没意义的虚词（“在老家”→“老家”，“拍的那张”→ 空）
_EDGE = "在和跟到对关于用拍看想记找这那某个张本次每里的了着我你他她它们就还都有要把给被"


# ---------------------------------------------------------------- 小工具

def _cn2i(s: str) -> int | None:
    s = s.strip()
    if s.isdigit():
        return int(s)
    return _CN_INT.get(s)


def _ts(d: dt.date, end: bool = False) -> float:
    """日期 → 本地时间戳（end=True 取当天 23:59:59）。"""
    t = dt.datetime(d.year, d.month, d.day, 23, 59, 59 if end else 0)
    return t.timestamp()


def _mk_cid(kind: str, label: str) -> str:
    return f"{kind}:{label}"


def _cond(kind: str, label: str, weight: float, test: Callable[[dict], bool],
          hard: bool = False, sql: tuple[str, list[Any]] | None = None,
          grade: Callable[[dict], float] | None = None) -> dict:
    # grade：命中后按特征强弱给一个“排序微加分”（只影响同分时的先后，不影响能否入围、
    # 不影响 match 分母）——用来把“更黑白”“更蓝”的照片排到前头，而不是让一批同命中的照片抓阄。
    return {"kind": kind, "label": label, "w": round(weight, 2), "hard": hard,
            "cid": _mk_cid(kind, label), "test": test, "sql": sql, "grade": grade, "ignored": False}


def _feats(r: dict) -> dict:
    if "_f" not in r:
        try:
            r["_f"] = json.loads(r.get("feats") or "{}")
        except (TypeError, ValueError):
            r["_f"] = {}
    return r["_f"]


def _exif(r: dict) -> dict:
    if "_e" not in r:
        try:
            r["_e"] = json.loads(r.get("exif") or "{}")
        except (TypeError, ValueError):
            r["_e"] = {}
    return r["_e"]


def _shot_ts(r: dict) -> float:
    # 只认真实拍摄时间（EXIF）；taken_at=0 代表时间未知，不拿文件 mtime 凑。
    return float(r.get("taken_at") or 0)


# ---------------------------------------------------------------- 时间解析

def _parse_time(text: str) -> tuple[list[tuple[float, float]], list[int], list[str], str]:
    """返回 (绝对时间区间, 月份集合, 条件标签, 已消费掉时间词的文本)。

    最后那个文本把命中过的时间片段替换成 ◆，后续抽关键词时直接用，
    避免「最近三个月」被二次当成文件名线索。
    """
    now = dt.datetime.now()
    ranges: list[tuple[float, float]] = []
    labels: list[str] = []
    months: set[int] = set()
    years: set[int] = set()
    work = text
    this_year = now.year

    def eat(pat: str, flags: int = 0) -> re.Pattern:
        return re.compile(pat, flags)

    # 1) 完整日期 2023-05-01 / 2023.5.1 / 2023年5月1日
    for m in eat(r"(\d{4})\s*[年./\-](\d{1,2})\s*[月./\-](\d{1,2})\s*[日号]?").finditer(work):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            day = dt.date(y, mo, d)
        except ValueError:
            continue
        ranges.append((_ts(day), _ts(day, True)))
        years.add(y)
        labels.append(f"{y}年{mo}月{d}日")
    work = eat(r"(\d{4})\s*[年./\-](\d{1,2})\s*[月./\-](\d{1,2})\s*[日号]?").sub("◆", work)

    # 2) 四位年份（先吃掉，否则「2023年」会被后面的相对时长规则误读成「23年」）
    for m in eat(r"(\d{4})\s*年").finditer(work):
        years.add(int(m.group(1)))
        labels.append(f"{m.group(1)}年")
    work = eat(r"(\d{4})\s*年").sub("◆", work)

    # 3) 相对：N 年前 / 最近 N 天 / 三个月内
    for m in eat(rf"(最近|近|过去|这)?\s*({NUM})\s*(天|周|个月|年)(前|以内|内|以来)?").finditer(work):
        head, num, unit, tail = m.group(1), _cn2i(m.group(2)), m.group(3), m.group(4)
        if not num:
            continue
        step = {"天": dt.timedelta(days=1), "周": dt.timedelta(days=7),
                "个月": dt.timedelta(days=30), "年": dt.timedelta(days=365)}[unit]
        if tail == "前":                       # 「三年前」= 那个时间点前后
            a, b = now - step * (num + 0.5), now - step * max(num - 0.5, 0)
            labels.append(f"约{num}{unit}前")
        else:                                  # 最近 N 个单位（「三个月」也算在内）
            a, b = now - step * num, now
            labels.append(f"最近{num}{unit}")
        ranges.append((a.timestamp(), b.timestamp()))
    work = eat(rf"(最近|近|过去|这)?\s*({NUM})\s*(天|周|个月|年)(前|以内|内|以来)?").sub("◆", work)

    # 3a) 上半年 / 下半年（先于「半年」处理，否则“上半年”会被读成“最近半年”）
    if "上半年" in work:
        months.update({1, 2, 3, 4, 5, 6})
        labels.append("上半年")
        work = work.replace("上半年", "◆", 1)
    if "下半年" in work:
        months.update({7, 8, 9, 10, 11, 12})
        labels.append("下半年")
        work = work.replace("下半年", "◆", 1)

    # 3b) 半年 / 半个月
    for word, days, lab_pre, lab_ago in (("半年", 182, "最近半年", "约半年前"),
                                         ("半个月", 15, "最近半个月", "约半个月前")):
        m = re.search(word, work)
        if not m:
            continue
        ago = work[m.end():m.end() + 1] == "前"
        step = dt.timedelta(days=days)
        if ago:
            ranges.append(((now - step * 1.5).timestamp(), (now - step * 0.5).timestamp()))
            labels.append(lab_ago)
        else:
            ranges.append(((now - step).timestamp(), now.timestamp()))
            labels.append(lab_pre)
        work = work.replace(word, "◆", 1)

    # 4) 具体某月某日（不带年）
    for m in eat(rf"({NUM})\s*月\s*({NUM})\s*[日号]").finditer(work):
        mo, d = _cn2i(m.group(1)), _cn2i(m.group(2))
        if mo and d and 1 <= mo <= 12 and 1 <= d <= 31:
            months.add(mo)
            labels.append(f"{mo}月{d}日")
            yr = min(years) if years else this_year
            try:
                day = dt.date(yr, mo, d)
                ranges.append((_ts(day), _ts(day, True)))
            except ValueError:
                pass
    work = eat(rf"({NUM})\s*月\s*({NUM})\s*[日号]").sub("◆", work)

    # 5) 今年 / 去年 / 前年 + 上半年 / 下半年 / 第X季度
    rel_map = {"今年": 0, "这一年": 0, "去年": 1, "上一年": 1, "前年": 2, "大前年": 3}
    for w, back in rel_map.items():
        if w in work:
            years.add(this_year - back)
            labels.append(w)
            work = work.replace(w, "◆", 1)
    if "明年" in work:
        work = work.replace("明年", "◆", 1)
    for m in eat(r"第\s*([一二三四1-4])\s*季度").finditer(work):
        q = _cn2i(m.group(1)) or 0
        months.update({(q - 1) * 3 + i for i in (1, 2, 3)})
        labels.append(f"第{q}季度")
    work = eat(r"第\s*([一二三四1-4])\s*季度").sub("◆", work)

    # 6) 季节
    for s in SEASON_KEYS:
        if s in work:
            months.update(SEASONS[s[0]])
            labels.append(s)
            work = work.replace(s, "◆", 1)

    # 7) 节日
    for h in HOLIDAY_KEYS:
        if h in work:
            (mo1, d1), (mo2, d2) = HOLIDAYS[h]
            yr = min(years) if years else this_year
            try:
                a, b = _ts(dt.date(yr, mo1, d1)), _ts(dt.date(yr, mo2, d2), True)
            except ValueError:
                continue
            if a > now.timestamp():                # 今年的节日还没到 → 用上一年
                a, b = _ts(dt.date(yr - 1, mo1, d1)), _ts(dt.date(yr - 1, mo2, d2), True)
            ranges.append((a, b))
            labels.append(h)
            work = work.replace(h, "◆", 1)

    # 8) 月份（避开 星期六 / 上个月 这类）
    for m in eat(rf"(?<![星期礼周])({NUM})\s*月份?(?!头)").finditer(work):
        mo = _cn2i(m.group(1))
        if mo and 1 <= mo <= 12:
            months.add(mo)
            labels.append(f"{mo}月")
    work = eat(rf"(?<![星期礼周])({NUM})\s*月份?(?!头)").sub("◆", work)
    if "上个月" in work or "上月" in work:
        y, mo = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
        years.add(y)
        months.add(mo)
        labels.append("上个月")
        work = work.replace("上个月", "◆", 1).replace("上月", "◆", 1)
    if any(w in work for w in ("这个月", "本月", "当月")):
        years.add(now.year)
        months.add(now.month)
        labels.append("这个月")

    # 9) 只有年份 / 年份+月份 → 展开成具体区间，避免月份兼底误命中其它年份
    if not ranges and years:
        for y in sorted(years):
            if months:
                for mo in months:
                    d0 = dt.date(y, mo, 1)
                    d1 = dt.date(y + (1 if mo == 12 else 0), (mo % 12) + 1, 1) - dt.timedelta(days=1)
                    ranges.append((_ts(d0), _ts(d1, True)))
                months = set()
            else:
                ranges.append((_ts(dt.date(y, 1, 1)), _ts(dt.date(y, 12, 31), True)))

    return ranges, sorted(months), list(dict.fromkeys(labels)), work


def _merge_spans(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """合并相邻/重叠区间：「上半年」的 6 个月会缩成 1 段，SQL 更短也更准。"""
    out: list[tuple[float, float]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1] + 2:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _time_cond(text: str) -> tuple[dict | None, str, str]:
    """把时间表达合成一个条件：能确定区间就走强过滤，只有月份则按月匹配。"""
    ranges, months, labels, masked = _parse_time(text)
    if not labels or (not ranges and not months):
        return None, "", text
    label = "时间：" + "、".join(labels[:3]) + ("…" if len(labels) > 3 else "")

    if ranges:
        spans = _merge_spans(ranges)

        def test(r, sp=tuple(spans), mo=tuple(months)):
            ts = _shot_ts(r)
            if not ts:
                return False
            if any(a <= ts <= b for a, b in sp):
                return True
            if mo:                       # 「3月5日」这类没落成年份的兼底
                return dt.datetime.fromtimestamp(ts).month in mo
            return False

        frags, args = [], []
        # 区间太多时先用一个大区间缩小候选（判定仍在 Python 里按真实区间做）
        use = spans if len(spans) <= 10 else [(spans[0][0], max(b for _a, b in spans))]
        for a, b in use:
            frags.append("(NULLIF(taken_at,0) BETWEEN ? AND ?)")
            args += [a, b]
        sql = ("(" + " OR ".join(frags) + ")", args)
        return _cond("time", label, 3.0, test, hard=True, sql=sql), label, masked

    mset = ",".join(["?"] * len(months))
    args = [f"{m:02d}" for m in months]
    sql = (f"(substr(month, 6, 2) IN ({mset}) OR (COALESCE(month,'')='' AND "
           f"NULLIF(taken_at,0) IS NOT NULL AND strftime('%m', taken_at, 'localtime') IN ({mset})))",
           args + args)

    def test2(r, mo=tuple(months)):
        ts = _shot_ts(r)
        if not ts:
            return False
        return dt.datetime.fromtimestamp(ts).month in mo

    return _cond("time", label, 2.6, test2, hard=True, sql=sql), label, masked


def _daypart_cond(text: str) -> dict | None:
    hit = _daypart_hit(text)
    if hit:
        word, a, b = hit
        if word in ("晚上", "夜里", "夜晚", "夜间", "晚间", "下晚"):
            label = f"时段：{word}（{a}:00–{b if b < 24 else 24}:00）"
        else:
            label = f"时段：{word}（{a}:00–{b}:00）"

        def test(r, a=a, b=b, word=word):
            ts = _shot_ts(r)
            if not ts:
                return False
            h = dt.datetime.fromtimestamp(ts).hour
            return a <= h < b

        return _cond("daypart", label, 1.5, test)
    return None


def _weekday_cond(text: str) -> dict | None:
    weekend = any(w in text for w in ("周末", "双休", "周日", "星期天", "礼拜天", "周六", "星期六"))
    workday = any(w in text for w in ("工作日", "上班时间", "平日里"))
    if not (weekend or workday):
        return None
    want = (5, 6) if weekend else (0, 1, 2, 3, 4)
    label = "周末" if weekend else "工作日"

    def test(r, want=want):
        ts = _shot_ts(r)
        if not ts:
            return False
        return dt.datetime.fromtimestamp(ts).weekday() in want

    return _cond("daypart", f"日期性质：{label}", 1.3, test)


# ---------------------------------------------------------------- 内容解析

def _theme_cond(text: str) -> tuple[list[dict], list[str], dict[str, list[str]]]:
    """内容词 → 主题。

    双字以上的词（火锅、海边、截图）命中即可作为强过滤；单字词（面、花、马、海）
    很容易在「里面 / 花瓣 / 海量 / 别人」这类词组里被误伤，所以只当加分项，不缩小候选。
    """
    low = text.lower()
    strong: dict[str, list[str]] = {}
    weak: dict[str, list[str]] = {}
    for word, tid in WORD2THEME.items():
        if not word or word not in low:
            continue
        if len(word) >= 2 or word in STRONG_SINGLE:
            strong.setdefault(tid, []).append(word)
        else:
            weak.setdefault(tid, []).append(word)
    if not strong and not weak:
        return [], [], {}

    conds: list[dict] = []
    words: list[str] = []
    if strong:
        ids = sorted(strong, key=lambda k: -max(len(w) for w in strong[k]))
        label = "主题：" + " 或 ".join(THEMES[i]["name"] for i in ids)

        def test(r, ids=tuple(ids)):
            return r.get("theme") in ids

        # 主题跟模型那条路一样只排座次、不当门禁：主题本身可能是标错的（黑白街拍被
        # 归成 mono），拿它硬卡会把描述里明明写着隧道、斑马线的照片整张刷掉。
        conds.append(_cond("theme", label, 2.6, test))
        words += [w for ws in strong.values() for w in ws]
    extra = {k: v for k, v in weak.items() if k not in strong}
    if extra:
        ids2 = sorted(extra, key=lambda k: -max(len(w) for w in extra[k]))
        label2 = "主题倾向：" + " 或 ".join(THEMES[i]["name"] for i in ids2)

        def test2(r, ids=tuple(ids2)):
            return r.get("theme") in ids

        conds.append(_cond("theme", label2, 2.0 if not strong else 1.4, test2))
        words += [w for ws in extra.values() for w in ws]
    return conds, words, (strong or weak)


# 剩下的片段如果只是一些虚词/否定词，就不要当成文件名线索
_KW_STOP = {"照片", "图片", "相片", "这张", "那张", "什么", "时候", "怎么", "为什",
            "可以", "就是", "还有", "别的", "其它", "其他", "里面", "外面",
            "上面", "下面", "中间", "一些", "一点", "不错", "没事"}


def _keyword_conds(masked: str, consumed: Iterable[str], theme_found: bool) -> list[dict]:
    """把剩下的中文/英文名词挑出来当文件名、文件夹关键词（软条件，只加分）。"""
    left = masked
    for c in consumed:
        if c:
            left = left.replace(c, "◆")
    left = STOP_RE.sub("◆", left)
    left = re.sub(r"[0-9〇一二三四五六七八九十]{1,3}\s*(天|周|月|年|号|日|点|时|岁)", "◆", left)
    left = re.sub(r"[，。、；：？！,;:.?!\s()（）\"'']+", "◆", left)
    out: list[dict] = []
    seen: set[str] = set()
    for seg in left.split("◆"):
        s = seg.strip().strip(_EDGE)
        if (len(s) < 2 or s in seen or s in _KW_STOP
                or (s[0] in "不没" and len(s) <= 2)):
            continue
        if not re.fullmatch(r"[\w\u4e00-\u9fff·]+", s):
            continue
        seen.add(s)
        lab = f"文字线索：{s}"
        w = 2.0 if not theme_found else 1.2

        def test(r, s=s):
            hay = (r.get("filename", "") + " " + r.get("folder", "")).lower()
            return s.lower() in hay

        out.append(_cond("keyword", lab, w, test))
    return out


def _look_conds(text: str) -> tuple[list[dict], list[str]]:
    conds: list[dict] = []
    words: list[str] = []
    used: set[str] = set()
    for word, weight, fn, label in VISUAL_KEYS:
        if word in text and label not in used:
            used.add(label)
            conds.append(_cond("visual", f"画面：{label}", weight,
                               (lambda fn=fn: lambda r: fn(_feats(r)))(fn)))
    # 同一组说法全当已消费，避免「大场景远景」里的「远景」再被当成文件名线索
    words += [w for w, _k, _f, lab in VISUAL_KEYS if lab in used and w in text]
    for word in COLOR_KEYS:
        if word in text and word not in used:
            used.add(word)
            words.append(word)
            key, lo, disp = COLOR_FEATS[word]
            lab = f"色彩：偏{disp}"
            if lab not in [c["label"] for c in conds]:
                conds.append(_cond("visual", lab, 1.4,
                                   (lambda k=key, lo=lo: lambda r: _feats(r).get(k, 0) >= lo)()))
    if any(w in text for w in ("黑白", "单色", "灰度", "没有颜色", "去色")):
        words += [w for w in ("黑白", "单色") if w in text]
        # 优先走风格轴 mono_style（本地特征免费回填）；特征补位保证未回填时也能用。
        # grade 用 mono_score 拉序：越黑白越前，不影响 match 分母。
        conds.append(_cond(
            "visual", "风格：黑白/单色", 1.8,
            lambda r: int(r.get("mono_style") or 0) == 1
            or _feats(r).get("gray_ratio", 0) > 0.5
            or _feats(r).get("sat_mean", 1) < 0.12,
            grade=lambda r: 0.3 * classifier.mono_score(_feats(r))))
    size_big = re.search(r"(文件|体积|图片|照片)\s*(很|比较|挺|太|蛮|超|多)?\s*大", text)
    if size_big or any(w in text for w in ("大图", "占地方")):
        words.append(size_big.group(0) if size_big else "大图")
        conds.append(_cond("visual", "文件体积大（≥2MB）", 1.2,
                           lambda r: (r.get("bytes") or 0) >= 2 * 1024 * 1024))
    size_small = re.search(r"(文件|体积|图片)\s*(很|比较|挺|太|蛮)?\s*小", text)
    if size_small or any(w in text for w in ("小图",)):
        words.append(size_small.group(0) if size_small else "小图")
        conds.append(_cond("visual", "文件体积小（≤300KB）", 1.2,
                           lambda r: 0 < (r.get("bytes") or 0) <= 300 * 1024))
    return conds, words


def _meta_conds(text: str) -> tuple[list[dict], list[str]]:
    conds: list[dict] = []
    words: list[str] = []
    for w in ("有定位", "带定位", "gps", "GPS", "拍于哪里", "地点信息"):
        if w in text:
            words.append(w)
            conds.append(_cond("meta", "含 GPS 定位信息", 1.5,
                               lambda r: bool(_exif(r).get("gps"))))
            break
    for w in ("相机拍的", "单反", "相机", "微单", "镜头"):
        if w in text:
            words.append(w)
            conds.append(_cond("meta", "由相机记录拍摄信息", 1.4,
                               lambda r: bool(_exif(r).get("camera"))))
            break
    m = re.search(r"(佳能|尼康|索尼|sony|canon|nikon|富士|fuji|iphone|iPhone|华为|小米|三星|samsung|oppo|vivo)",
                  text)
    if m:
        brand = m.group(1).lower()
        words.append(m.group(1))
        conds.append(_cond("meta", f"拍摄设备含「{m.group(1)}」", 2.2,
                           lambda r, b=brand: b in (_exif(r).get("camera") or "").lower()))
    for w in ("微信保存", "朋友圈", "别人发的", "转发的"):
        if w in text:
            words.append(w)
            conds.append(_cond("meta", "像是保存/转发来的（无拍摄参数）", 1.4,
                               lambda r: not _exif(r).get("camera")))
            break
    return conds, words


# ---------------------------------------------------------------- 解析入口

def parse(text: str) -> dict:
    """把一句话拆成检索条件列表。"""
    text = (text or "").strip()
    # 否定说法先归一：「看不清 / 不清晰」是糊，不能当成“画面清晰”
    for src, dst in (("看不太清", "模糊"), ("看不清", "模糊"),
                     ("看不清楚", "模糊"), ("不清晰", "模糊"), ("不清楚", "模糊"),
                     ("不太清晰", "模糊"), ("不太清楚", "模糊"), ("不够清晰", "模糊"),
                     ("不鲜艳", "颜色少"), ("不够亮", "偏暗"), ("不太亮", "偏暗")):
        text = text.replace(src, dst)
    consumed: list[str] = []
    conds: list[dict] = []

    tc, _, masked = _time_cond(text)
    if tc:
        conds.append(tc)
    dp = _daypart_cond(masked)
    if dp:
        conds.append(dp)
    wd = _weekday_cond(masked)
    if wd:
        conds.append(wd)

    theme_conds, words, thits = _theme_cond(text)
    conds += theme_conds
    consumed += words
    for s in SEASON_KEYS + HOLIDAY_KEYS:
        if s in text:
            consumed.append(s)
    for word, _a, _b in DAYPART_KEYS:
        # 只把“真的在说时间”的时段词归入已消费，嵌在名词里的（白天鹅的白天）
        # 不消费，让后面的关键词切分能把它当内容词看待。
        idx = text.find(word)
        while idx != -1:
            end = idx + len(word)
            nxt = text[end:end + 1]
            if (not nxt) or nxt in _TIME_FOLLOW_OK:
                consumed.append(word)
                break
            idx = text.find(word, end)
    if any(w in text for w in ("周末", "工作日", "双休", "周日", "星期天", "礼拜天",
                               "周六", "星期六", "上班时间", "平日里")):
        consumed += [w for w in ("周末", "双休", "周日", "星期天", "礼拜天", "周六", "星期六",
                                  "工作日", "上班时间", "平日里") if w in text]

    # 黑白不再当互斥主题：过去这里在无主题命中时补一条 theme=="mono"，
    # mono 移出主题池后那条永不命中，交给 _look_conds 的 mono_style 风格轴。
    look, look_words = _look_conds(masked)
    meta, meta_words = _meta_conds(masked)
    conds += look + meta
    conds += _keyword_conds(masked, consumed + look_words + meta_words, bool(theme_conds))

    # 去重（同一标签只留一个）
    uniq: dict[str, dict] = {}
    for c in conds:
        uniq.setdefault(c["cid"], c)
    conds = list(uniq.values())
    return {"text": text, "conds": conds, "words": words, "theme_hits": thits}


# ---------------------------------------------------------------- 模型理解

_MONTH_DAYS = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
_SEASON_ID = {"spring": "春", "summer": "夏", "autumn": "秋", "winter": "冬",
              "春": "春", "夏": "夏", "秋": "秋", "冬": "冬",
              "春天": "春", "夏天": "夏", "秋天": "秋", "冬天": "冬"}


def _feats_hint() -> str:
    """把本地能判定的画面条件递给模型，不让它自由发挥。"""
    labs: list[str] = []
    for _w, _k, _f, lab in VISUAL_KEYS:
        if lab not in labs:
            labs.append(lab)
    labs += [f"偏{v[2]}" for v in COLOR_FEATS.values()]
    labs += ["黑白灰"]
    return "、".join(dict.fromkeys(labs))


def _day(s: Any) -> dt.date | None:
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y-%m", "%Y%m%d"):
        try:
            d = dt.datetime.strptime(str(s or "").strip()[:10], fmt)
            return d.date()
        except ValueError:
            continue
    return None


def _month_end(y: int, m: int) -> dt.date:
    """当月最后一天（闰年 2 月算对）。"""
    if m == 2:
        return dt.date(y, 2, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28)
    return dt.date(y, m, _MONTH_DAYS[m - 1])


def _span_of(a: dt.date, b: dt.date) -> tuple[float, float]:
    return _ts(a), _ts(b, end=True)


def _range_cond(spans: list[tuple[float, float]], label: str) -> dict | None:
    if not spans:
        return None
    spans = _merge_spans(spans)

    def test(r, sp=tuple(spans)):
        ts = _shot_ts(r)
        return bool(ts) and any(a <= ts <= b for a, b in sp)

    use = spans if len(spans) <= 10 else [(spans[0][0], max(b for _a, b in spans))]
    frags, args = [], []
    for a, b in use:
        frags.append("(NULLIF(taken_at,0) BETWEEN ? AND ?)")
        args += [a, b]
    return _cond("time", "时间：" + label, 3.0, test, hard=True,
                 sql=("(" + " OR ".join(frags) + ")", args))


def _llm_time_cond(t: dict) -> dict | None:
    """模型只负责把“去年夏天”翻译成日期，时间算术全部在本地完成。"""
    kind = str(t.get("kind") or "none").lower()
    today = dt.date.today()
    hint = str(t.get("hint") or "").strip()
    spans: list[tuple[float, float]] = []
    label = hint

    if kind in ("exact", "day"):
        d = _day(t.get("date") or t.get("from"))
        if d:
            spans, label = [_span_of(d, d)], (hint or f"{d.year}年{d.month}月{d.day}日")
    elif kind in ("range", "between"):
        a, b = _day(t.get("from")), _day(t.get("to"))
        if a and b and a > b:
            a, b = b, a
        far = dt.date(2100, 12, 31)          # 只给了一端时用“足够远”兜底，不靠猜天数
        if a and b:
            spans, label = [_span_of(a, b)], (hint or f"{a.month}月到{b.month}月" if a.year == b.year
                                              else hint or f"{a}~{b}")
        elif a:
            spans, label = [_span_of(a, far)], (hint or f"{a.year}年{a.month}月之后")
        elif b:
            spans, label = [(_ts(dt.date(1970, 1, 1)), _ts(b, end=True))], (hint or f"{b.year}年{b.month}月之前")
    elif kind == "month":
        d = _day(t.get("month"))
        if d:
            spans = [_span_of(d.replace(day=1), _month_end(d.year, d.month))]
            label = hint or f"{d.year}年{d.month}月"
    elif kind == "year":
        try:
            y = int(t.get("year") or 0)
        except (TypeError, ValueError):
            y = 0
        if y > 1900:
            spans = [_span_of(dt.date(y, 1, 1), dt.date(y, 12, 31))]
            label = hint or f"{y}年"
    elif kind == "season":
        season = _SEASON_ID.get(str(t.get("season") or "").strip().lower())
        try:
            y = int(t.get("year") or today.year)
        except (TypeError, ValueError):
            y = today.year
        if season:
            for m in SEASONS[season]:
                spans.append(_span_of(dt.date(y, m, 1), _month_end(y, m)))
            label = hint or f"{y}年{season}季"
    elif kind == "relative":
        try:
            n = int(t.get("days") or 0)
        except (TypeError, ValueError):
            n = 0
        side = str(t.get("side") or "before").lower()
        n = max(1, min(n, 3650))
        if side == "around":
            spans = [_span_of(today - dt.timedelta(days=n), today + dt.timedelta(days=n))]
            label = hint or f"前后{n}天"
        else:
            spans = [_span_of(today - dt.timedelta(days=n), today)]
            label = hint or f"最近{n}天"
    if not spans:
        return None
    return _range_cond(spans, label or "那段时间")


def _content_hay(r: dict) -> str:
    """内容匹配只看描述 + 标签 + 文件名。

    故意不含文件夹名和主题名：那会让“人像”一个词命中整个「人像自拍」文件夹，
    闭卷评测里一句话返回 25 张就是这么来的。
    """
    if "_ch" not in r:
        try:
            tags = " ".join(str(x) for x in json.loads(r.get("tags") or "[]"))
        except (TypeError, ValueError):
            tags = ""
        r["_ch"] = " ".join([str(r.get("caption") or ""), tags,
                             str(r.get("filename") or "")]).lower()
    return r["_ch"]


def _rarer(word: str, total: int) -> float:
    """词越常见越不值钱：满库都写着“人像”，它不该跟只有一张有的“隧道”同样加分。"""
    if total <= 0:
        return 1.0
    n = db._rows("SELECT COUNT(*) AS c FROM photos WHERE caption LIKE ? OR tags LIKE ?",
                 (f"%{word}%", f"%{word}%"))[0]["c"]
    rate = n / total
    if rate <= 0.03:
        return 1.0
    if rate <= 0.10:
        return 0.7
    if rate <= 0.25:
        return 0.45
    return 0.25


def _frags(word: str) -> list[str]:
    """长词的二字片段：模型说“蝴蝶发夹”、照片写“蝴蝶发饰”，靠片段对得上。"""
    w = str(word).strip()
    if len(w) < 3:
        return []
    return sorted({w[i:i + 2] for i in range(len(w) - 1)})


# 词形兜底：同一件东西在照片描述里常被写成另一种词形（名词↔动作、简称↔全称）。
# 只收“同一个东西”：自行车→骑车（同一个东西的不同写法），
# 不收场景和联想：滑雪→雪山/雪地不收、地铁→车厢不收（那是部件）。
# 它只用来扩“用户点名的那个东西”，所以不可能把模型联想出的场景洗成硬条件。
_FORMS: dict[str, list[str]] = {
    "自行车": ["骑车", "骑行", "单车", "脚踏车"],
    "单车": ["骑车", "自行车"],
    "脚踏车": ["骑车", "自行车"],
    "摩托车": ["摩托", "骑车", "机车"],
    "马路": ["街道", "街面", "路面", "公路", "斑马线", "小巷"],
    "街道": ["马路", "街面", "路面", "小巷"],
    "圆窗": ["花窗", "圆形窗", "拱窗", "窗洞"],
    "窗户": ["窗口", "窗前", "窗边", "花窗"],
    "太阳": ["日落", "夕阳", "落日", "日出", "朝阳"],
    "饭菜": ["菜肴", "一桌菜", "家常菜", "饭桌", "酒菜"],
    "车子": ["汽车", "出租车", "摩托车", "公交车", "卡车", "轿车", "车灯"],
    "合影": ["合照", "全家福", "合影照"],
    "合照": ["合影", "全家福"],
    "手机": ["移动电话", "举手机", "看手机"],
    "相机": ["照相机", "单反", "微单"],
    "跑步": ["奔跑", "跑动", "慢跑", "跑者"],
    "伞": ["雨伞", "阳伞", "伞面"],
    "裙子": ["长裙", "短裙", "连衣裙", "纱裙", "裙摆", "粉裙"],
    "隧道": ["涵洞", "地下通道"],
    "楼梯": ["台阶", "阶梯"],
    "海滩": ["沙滩", "海边", "海岸"],
    "女人": ["女子", "女士", "女生"],
    "男人": ["男子", "男士", "男生"],
    "女生": ["女子", "女孩", "女士"],
    "小孩": ["儿童", "宝宝", "婴儿"],
    "斑马线": ["人行横道", "马路"],
}


def _forms_of(word: str) -> list[str]:
    """一个词进、几个说法出：用户点名的词本身 + 它的其他词形（最多 5 个）。"""
    w = str(word).strip().lower()
    out = [w] if w else []
    for f in _FORMS.get(w, []):
        f = f.lower()
        if f not in out:
            out.append(f)
    return out[:5]


def _llm_theme_conds(ids: list[str]) -> list[dict]:
    ids = [str(i).strip() for i in ids if str(i).strip() in THEMES][:2]
    if not ids:
        return []
    label = "主题：" + " 或 ".join(THEMES[i]["name"] for i in ids)

    def test(r, ids=tuple(ids)):
        return r.get("theme") in ids

    # 主题只排座次，不当门禁：黑白街拍被归成 mono 之后，拿“城市建筑”硬卡
    # 会把描述里明明写着隧道、斑马线的照片整张刷掉（评测里的两条漏检就是这个）。
    return [_cond("theme", label, 2.6, test)]


def _anchor(word: str, text: str) -> str:
    """模型给的内容词能不能归到一个“用户原词”名下：能则返回用户说的那个片段，不能则返空。

    三种情算原词（用户明说过的）：
      1) 整词就是用户写出来的（骆驼、地铁、斑马线）；
      2) 词里有一个二字片段是用户写出来的—模型把“骑车”说成“骑车人”、
         把“玻璃”说成“玻璃杯”，这种是同东西的另一种说法，不是联想；
      3) 词里含有用户说出的单字强物体词（STRONG_SINGLE：“猫”→“宠物猫”）。

    都不算的才是联想：“滑雪”→“雪山/雪地”，雪山里没有一个字跟滑雪说法相同，
    它是模型自己端上来的场景，不能单独把照片带进结果。
    """
    w = str(word).strip().lower()
    t = str(text).lower()
    if not w:
        return ""
    if w in t:
        return w
    for i in range(len(w) - 1):
        g = w[i:i + 2]
        if g in t:
            return g
    for ch in w:
        if ch in STRONG_SINGLE and ch in t:
            return ch
    return ""


def _must_terms(must: Any) -> list[str]:
    """模型点名的“用户要看的那个东西”：允许写成 车子|汽车|出租车 这种同义串。"""
    if isinstance(must, (list, tuple)):
        raw = "|".join(str(m) for m in must)
    else:
        raw = str(must or "")
    out: list[str] = []
    for part in re.split(r"[|/、,，;；\s]+", raw):
        p = part.strip().lower()
        if 1 <= len(p) <= 12 and p not in out:
            out.append(p)
    return out[:3]


def _fam_of(words: list[str]) -> tuple[tuple, tuple, tuple]:
    """一个“东西族”：整词 + 它们的二字片段 + 族里的强单字。"""
    keys = tuple(w for w in words if w)
    frags = tuple(sorted({g for k in keys for g in _frags(k)}))
    strong = tuple(sorted({ch for k in keys for ch in k if ch in STRONG_SINGLE}))
    return keys, frags, strong


def _evidence(hay: str, keys: tuple, frags: tuple, strong: tuple) -> bool:
    """照片的文字里有没有这个东西的证据：整词 → 二字片段同现 → 强单字。

    片段同现是“蝴蝶发夹”能对上“蝴蝶发饰”的关键，也是“摩天轮”不能对上“摩天大楼”
    的关键：只对上头两个字的片段不算，得两个以上片段同时出现（主体片段 + 类别片段）。
    强单字只包猫狗这类几乎不会误伤的（STRONG_SINGLE），“车/路/花”这类通用字不算。
    """
    if any(k and k in hay for k in keys):
        return True
    if sum(1 for f in frags if f in hay) >= 2:
        return True
    return any(ch and ch in hay for ch in strong)


def _llm_object_conds(objs: list[str], excls: list[str], text: str = "",
                     must: Any = "") -> list[dict]:
    """画面内容：只有用户点名的东西能把照片带进结果，模型联想出的场景只能加分。

    入围条件只卡一个族：“must”（模型替用户点名的那样东西，允许带同义叫法）。
    模型没给 must 时退回词法判断：只认能在用户原句里找到字的模型词。
    两者都一个也没有时（用户根本没点名东西），不许生成硬条件，联想词全部只加分。

    为什么这么设：闭卷评测 8 条库里 0 出现的反例误报了 7 条，全部是模型联想词
    把不相干的照片带进来的（“滑雪”→“雪山”命中雪山夜景）。联想词自始至终不参与入围。
    """
    objs = [str(o).strip().lower() for o in objs if 2 <= len(str(o).strip()) <= 12][:6]
    excls = [str(o).strip() for o in excls if 2 <= len(str(o).strip()) <= 12][:4]
    out: list[dict] = []
    # 只有“大部分照片都已有描述”时才敢把内容当硬条件：刚跑了一半就硬卡，
    # 没标注过的那些会被 LIKE 直接刷掉，看着就像“相册里没有”。
    c = db.ai_counts()
    cover = c["done"] / max(1, c["done"] + c["error"] + c["none"])
    annotated = cover >= 0.6
    total = db._rows("SELECT COUNT(*) AS c FROM photos")[0]["c"]
    named = [o for o in objs if _anchor(o, text)]
    terms = _must_terms(must)
    if terms:
        # 词形兜底：把“自行车”扩成“自行车/骑车/单车…”，但只扩用户点名的东西
        terms = list(dict.fromkeys(f for t in terms for f in _forms_of(t)))[:6]
        keys, frags, strong = _fam_of(terms)
        gate = "内容（用户点名要看：" + "|".join(keys[:4]) + "）"
    elif named:
        keys, frags, strong = _fam_of(sorted(set(named) | {_anchor(o, text) for o in named}))
        gate = "内容：" + "、".join(keys[:4])
    else:
        keys = frags = strong = ()
    if keys:
        kk, ff, ss = keys, frags, strong

        def hit(r, kk=kk, ff=ff, ss=ss):
            return _evidence(_content_hay(r), kk, ff, ss)

        sqls, args = [], []
        for k in list(kk) + list(ff) + list(ss):
            sqls.append("(caption LIKE ? OR tags LIKE ? OR filename LIKE ?)")
            args += [f"%{k}%"] * 3
        out.append(_cond("object", gate, 0.6, hit, hard=annotated,
                         sql=("(" + " OR ".join(sqls) + ")", args)))
    elif objs:
        # 用户根本没点名东西（“太阳快落山那会儿拍的”、“拍风景的”），模型只能给联想词：
        # 这时没有任何词有资格当硬条件，否则只能描述黄昏的人会被说成“相册里没有”。
        # 联想词全部只参加加分，但多中几个给多一点分，不让单靠一个“天空”挤上来的图抢前。
        tup = tuple(objs[:4])

        def _nassoc(r, tup=tup):
            hay = _content_hay(r)
            return sum(1 for k in tup if any(f in hay for f in (k,) + tuple(_frags(k))))

        for n in (1, 2, 3):
            if n <= len(tup):
                out.append(_cond("object", f"内容相关词（不单独入围）中 {n} 个",
                                 0.7, lambda r, n=n: _nassoc(r) >= n))
    # 计分：主词命中给最高分，其余内容词按“多中了几个不同的词”线性加分。
    # 联想词只值原词的四成：它不能把照片带进来，也不该把名次顶到天上去。
    cand = list(dict.fromkeys([k for k in keys] + objs[:4]))
    orig_set = set(keys) | {o for o in cand if _anchor(o, text)}
    for o in cand:
        scale = _rarer(o, total)
        base = 2.2 if o in orig_set else 0.9
        out.append(_cond("object", f"内容细则：{o}", round(base * scale, 2),
                         lambda r, key=o: key in _content_hay(r)))
        fs = [f for f in _frags(o) if f != o]
        if fs:
            low = tuple(fs)
            out.append(_cond("object", "内容线索：" + "、".join(fs), round(0.9 * scale * (1.0 if o in orig_set else 0.5), 2),
                             lambda r, key=o, low=low:
                             key not in _content_hay(r)
                             and any(f in _content_hay(r) for f in low)))
    if len(cand) >= 2:
        tup = tuple(cand)

        def _nhit(r, tup=tup):
            hay = _content_hay(r)
            return sum(1 for k in tup if k in hay)

        for n in range(2, min(4, len(cand)) + 1):
            out.append(_cond("object", f"内容多中 {n} 词", 0.55,
                             lambda r, n=n: _nhit(r) >= n))
    if excls:
        # “不含人像”这类排除不能只靠 caption/tags 子串 NOT LIKE：标注里写“人物/女人/自拍”
        # 不等于“人像”，子串排除基本挡不住人像（评测里“不要人像”仍返回一堆人像）。
        # 主手段落到主题层：排除词能归成某个主题的（人像→portrait），变成 theme≠该主题的硬条件；
        # 排人像时再补一层“无人”软加分（不门禁，只把没人的风景顶上去）；文本 NOT LIKE 降为软补充。
        excl_tids = {WORD2THEME[str(e).lower()] for e in excls if str(e).lower() in WORD2THEME}
        excl_tids = {t for t in excl_tids if t in THEMES}
        if excl_tids:
            tset = tuple(sorted(excl_tids))
            names = "、".join(THEMES[t]["name"] for t in tset)
            out.append(_cond("exclude", f"排除主题：非{names}", 1.8,
                             lambda r, ts=tset: r.get("theme") not in ts,
                             hard=True,
                             sql=("theme NOT IN (%s)" % ",".join("?" * len(tset)), list(tset))))
        if "portrait" in excl_tids:
            def _no_people(r):
                p = r.get("people")
                try:
                    return int(p) == 0
                except (TypeError, ValueError):
                    return False
            out.append(_cond("exclude", "画面无人（排除人像的补充）", 0.8, _no_people))
        frags, args = [], []
        for o in excls:
            frags.append("(caption NOT LIKE ? AND tags NOT LIKE ?)")
            args += [f"%{o}%", f"%{o}%"]
        out.append(_cond("exclude", "不含：" + "、".join(excls), 0.6,
                         lambda r, es=tuple(excls): not any(e.lower() in _content_hay(r) for e in es),
                         hard=False,
                         sql=("(" + " AND ".join(frags) + ")", args)))
    return out


def _llm_look_conds(labels: list[str]) -> list[dict]:
    """模型选的画面条件 → 本地原有的特征判定（不盲信，只对得上号的才用）。"""
    want = {str(x).strip() for x in labels if str(x).strip()}
    out: list[dict] = []
    for word, weight, fn, lab in VISUAL_KEYS:
        if lab not in want and word not in want:
            continue
        if any(c["label"] == f"画面：{lab}" for c in out):
            continue
        out.append(_cond("visual", f"画面：{lab}", weight,
                         (lambda fn=fn: lambda r: fn(_feats(r)))(fn)))
    for key, (fkey, lo, disp) in COLOR_FEATS.items():
        if f"偏{disp}" in want or key in want:
            lab = f"色彩：偏{disp}"
            if not any(c["label"] == lab for c in out):
                # 把库内色调统计用作排序微加分：命中同一色彩条件的一批照片里，
                # 该颜色占比越高的越往前，不再是“100 张并列、靠时间抽阅”。
                out.append(_cond("visual", lab, 1.4,
                                 (lambda k=fkey, lo=lo: lambda r: _feats(r).get(k, 0) >= lo)(),
                                 grade=(lambda k=fkey, lo=lo: lambda r: round(
                                     0.2 * min(1.5, _feats(r).get(k, 0) / lo), 3))()))
    if any("黑白" in w for w in want):
        def _mono_grade(r):
            f = _feats(r)
            s = f.get("sat_mean", 1.0)
            g = f.get("gray_ratio", 0)
            return round(0.25 * max(0.0, (0.12 - min(s, 0.12)) / 0.12)
                         + 0.15 * min(1.0, g / 0.6), 3)
        out.append(_cond("visual", "色彩：黑白灰（低饱和）", 1.8,
                         lambda r: _feats(r).get("gray_ratio", 0) > 0.5
                         or _feats(r).get("sat_mean", 1) < 0.12,
                         grade=_mono_grade))
    return out


def _llm_word_conds(words: list[str]) -> list[dict]:
    out: list[dict] = []
    for w in words:
        w = str(w).strip()
        if len(w) < 2 or len(w) > 20 or any(c["label"] == f"文字线索：{w}" for c in out):
            continue

        def test(r, w=w):
            hay = (r.get("filename", "") + " " + r.get("folder", "")).lower()
            return w.lower() in hay

        out.append(_cond("keyword", f"文字线索：{w}", 1.8, test))
    return out


def _llm_people_cond(n: Any) -> list[dict]:
    """人数：永远只是加分项，绝不入围硬条件。

    容差按期望人数分档：≤ 4 人必须数对（3 人不能当 4 人用），
    > 4 人时标注本身容易数错，保留 ±1，但只给一半加分。
    """
    try:
        n = int(n)
    except (TypeError, ValueError):
        return []
    if n < 0 or n > 20:
        return []

    def _p(r):
        p = r.get("people")
        try:
            p = int(p)
        except (TypeError, ValueError):
            return None
        return p if p >= 0 else None

    def exact(r, n=n):
        p = _p(r)
        return p is not None and p == n

    out = [_cond("object", f"人数：恰好 {n} 人", 1.6, exact)]
    if n > 4:
        def near(r, n=n):
            p = _p(r)
            return p is not None and abs(p - n) == 1

        out.append(_cond("object", f"人数：{n} 上下差 1 人", 0.7, near))
    return out


def _intent_conds(text: str, intent: dict) -> list[dict]:
    """把模型返回的意图摊成条件列表。

    单独拆出来有两个用处：判分脚本可以先存一份意图再离线重放（省 token，
    也保证改动前后用的是同一次模型输出），以及让内容条件能对照用户原句。
    """
    conds: list[dict] = []
    tc = _llm_time_cond(intent.get("time") or {})
    if tc:
        conds.append(tc)
    conds += _llm_theme_conds(intent.get("themes") or [])
    conds += _llm_object_conds(intent.get("objects") or [],
                              intent.get("exclude_objects") or [], text,
                              intent.get("must") or "")
    conds += _llm_look_conds(intent.get("feats") or [])
    conds += _llm_word_conds(intent.get("words") or [])
    conds += _llm_people_cond(intent.get("people"))
    uniq: dict[str, dict] = {}
    for c in conds:
        uniq.setdefault(c["cid"], c)
    return list(uniq.values())


def parse_qwen(text: str) -> dict:
    """用模型理解一句话，产出与规则解析完全同构的条件列表。"""
    intent = llm.parse_query(text, feats_hint=_feats_hint())
    return {"text": text, "conds": _intent_conds(text, intent), "words": [],
            "theme_hits": {}, "intent": intent,
            "note": str(intent.get("note") or "").strip()[:60]}


# ---------------------------------------------------------------- 检索

def _candidate_sql(conds: list[dict]) -> tuple[str, list[Any]]:
    where, args = ["1=1"], []
    for c in conds:
        if c["hard"] and not c["ignored"] and c.get("sql"):
            frag, extra = c["sql"]
            where.append(frag)
            args += extra
    return " AND ".join(where), args


def _score_rows(rows: list[dict], usable: list[dict], denom: float) -> list[dict]:
    """逐张判定：硬条件不满足的直接跳过，其余按命中权重算匹配度。"""
    scored: list[dict] = []
    # 低权重软条件不得单独把照片送进结果：当整句话一个硬条件都没有、且可用条件 ≥2 时，
    # 一张照片至少要被 2 个条件同时命中才入围。因为此时没有任何门禁在挡，
    # 一个宽泛软条件（通用时段/文字线索/大类主题）会命中几乎全库→“全都给你”。
    # 用“共命中 ≥2”而不是“权重≥某阈值”：前者可解释（“同时中了两句你说的话”）、
    # 不随句长调参，也不会被单个宽泛条件刷单。单条件查询（如“宝宝的照片”只有
    # 主题人像一个软条件）len(usable)==1 不触发，不会误伤“把我的人像都给我”。
    soft_only_gate = (not any(c["hard"] for c in usable)) and len(usable) >= 2
    for r in rows:
        hits = [c for c in usable if _safe(c["test"], r)]
        if not hits:
            continue
        if soft_only_gate and len(hits) < 2:
            continue                     # 纯靠一个宽泛软条件命中的，不收
        if any(c["hard"] and c not in hits for c in usable):
            continue                     # 硬条件不满足的不展示
        got = sum(c["w"] for c in hits)
        item = dict(r)
        item["_hit"] = [c["label"] for c in hits]
        item["_miss"] = [c["label"] for c in usable if c not in hits]
        item["_match"] = min(100, round(100 * got / denom))
        # 同一个命中集里用 grade 拉开差距（越黑白/越蓝越前），不靠时间戳抓阅。
        boost = sum((c["grade"](r) or 0) for c in hits if c.get("grade"))
        item["_order"] = got + boost + (0.5 if r.get("manual") else 0.4)
        scored.append(item)
    return scored


def _parse_with(text: str, engine: str) -> tuple[dict, str, str]:
    """选一条理解路径，顺带告诉界面「这次是谁在理解」。

    模型只要出一点问题就让本地规则接手，不能因为外网或本地服务不可用而搜不了照片。
    engine 里的 qwen 只是个字段名，实际指当前配的那个模型（云端或本地）。
    """
    cfg = llm.load()
    name = str(cfg.get("text_model") or "AI 模型")
    if engine == "rules":
        return parse(text), "rules", "本次由本地规则理解（已按要求跳过 AI 模型）"
    only = engine == "qwen"          # 用户点名只要模型，回落时要把这层意思说清楚
    if not llm.ready():
        why = (f"你指定了只用 {name}，但它还没启用：在设置里填好接口地址（云端要 Key，"
               f"本地模型不用）并打开开关" if only
               else "AI 模型还没启用")
        return parse(text), "rules", f"本次由本地规则理解（{why}）"
    try:
        parsed = parse_qwen(text)
    except llm.LlmError as exc:
        why = f"你指定了只用 {name}，但它没调用成功：{exc.msg}" if only \
              else f"{name} 没调用成功：{exc.msg}"
        return parse(text), "rules", f"本次由本地规则理解（{why}）"
    except Exception as exc:            # 模型字段奇形怪状也不能让查找直接崩
        return parse(text), "rules", f"本次由本地规则理解（{name} 的返回用不上：{type(exc).__name__}）"
    if parsed["conds"]:
        extra = f"：{parsed['note']}" if parsed.get("note") else ""
        return parsed, "qwen", f"本次由 {name} 理解{extra}"
    fallback = parse(text)              # 模型什么都没听出来，还有一套关键词可以试
    if fallback["conds"]:
        why = f"你指定了只用 {name}，但它没听出可用条件" if only \
              else f"{name} 没听出可用条件"
        return fallback, "rules", f"本次由本地规则理解（{why}）"
    return parsed, "qwen", f"本次由 {name} 理解（没听出可用条件）"


def search(text: str = "", limit: int = 80, ignore: Iterable[str] = (),
           engine: str = "auto", keep: Iterable[str] = ()) -> dict:
    """自然语言查找：返回结构化意图 + 命中照片（含匹配度与理由）。

    engine=auto 先让配好的模型读这句话，不可用则回到本地规则（见 _parse_with）。
    两条路径只负责“把句子拆成条件”，时间/主题/内容这类硬条件先走 SQL 缩小范围，
    所有条件再在 Python 里逐张判定，因此每条命中都能说清「为什么是这张」。
    硬条件一个都不命中时会逐条放宽；keep 里的条件表示用户坚持要，不参与放宽。
    """
    parsed, eng, eng_note = _parse_with(text, engine)
    who = {"engine": eng, "engine_note": eng_note, "intent": parsed.get("intent") or {},
           "keep": []}
    if not parsed["conds"]:
        return {"ok": True, "text": text, "conds": [], "items": [], "total": 0,
                "scanned": 0, "relaxed": [], "truncated": False, "words": parsed["words"],
                **who,
                "note": "这句里没找到能对应相册的线索：说说大概什么时候拍的（去年夏天、国庆）、"
                        "拍的是什么（火锅、截图、海边）、或者画面感觉（夜景、黑白、很亮）。"}
    manual_off = set(ignore or ())
    all_cids = {c["cid"] for c in parsed["conds"]}
    manual_on = all_cids & set(keep or ())
    who["keep"] = sorted(manual_on)
    for c in parsed["conds"]:
        c["ignored"] = c["cid"] in manual_off

    # 参与放宽的只有硬条件里用户没点名坚持的那些
    hard_cids = _drop_order([c for c in parsed["conds"]
                             if c["hard"] and c["cid"] not in manual_off
                             and c["cid"] not in manual_on])
    rows: list[dict] = []
    usable: list[dict] = []
    scored: list[dict] = []
    denom = 1.0
    relaxed: list[str] = []
    # 放宽到「结果够看」为止：够 MIN_RELAX_RESULTS 张才停，否则只要还有可放宽的
    # 硬条件就接着放宽下一条（hard_cids 只含可松条件，内容/主题硬门禁不在其中）。
    for k in range(len(hard_cids) + 1):
        skip = set(hard_cids[:k])
        for c in parsed["conds"]:
            c["ignored"] = c["cid"] in manual_off or c["cid"] in skip
        usable = [c for c in parsed["conds"] if not c["ignored"]]
        if not usable:
            relaxed = [c["label"] for c in parsed["conds"] if c["cid"] in skip]
            break
        where, args = _candidate_sql(parsed["conds"])
        rows = db.search_rows(where, args, cap=SCAN_CAP)
        relaxed = [c["label"] for c in parsed["conds"] if c["cid"] in skip]
        # 自动放宽过硬条件时，分母仍包含被放宽的条件，不让结果看起来像“100% 符合”；
        # 但用户手动关掉的条件不计入分母（他本来就不要求这个）。
        wsum = sum(c["w"] for c in usable) or 1.0
        w_all = sum(c["w"] for c in parsed["conds"] if c["cid"] not in manual_off) or wsum
        denom = w_all if relaxed else wsum
        scored = _score_rows(rows, usable, denom)
        # 够看（≥MIN_RELAX_RESULTS）或可放宽的硬条件已经全部放完，才收尾；
        # 否则继续放宽下一条——修的是「只 1 张就停」把还能救的结果卡在 1~2 张。
        if len(scored) >= MIN_RELAX_RESULTS or k == len(hard_cids):
            break

    # 用户点名坚持的条件不参与放宽，以它筛空了就把“怎么放弃”说一句
    keep_labels = "、".join(c["label"] for c in parsed["conds"] if c["cid"] in manual_on)
    keep_tip = (f"（你坚持了「{keep_labels}」，点它就能先放弃再看）" if keep_labels else "")

    if not usable:
        if relaxed:
            tip = ("能放宽的条件都放宽完了，还是没有照片：先确认那个时间段的照片已经导入，"
                   "或者换个说法（只说内容不说时间）。已放宽：" + "、".join(relaxed) + "。" + keep_tip)
        else:
            tip = "条件都被关掉了，已没有可用线索：点上面带 ✕ 的条件恢复一个再找。"
        return {"ok": True, "text": text, "conds": _out(parsed["conds"], manual_on),
                "items": [],
                "total": 0, "scanned": 0, "relaxed": relaxed, "truncated": False,
                "words": parsed["words"], "note": tip, **who}

    scored.sort(key=lambda x: (-x["_match"], -x["_order"], -_shot_ts(x)))
    # 砍掉明显不相干的：只靠一个通用词挤进候选的照片分会很低，不倒给用户看。
    # 上限压在 45 分以下，是为了长句子（条件多、单张分数天然低）也能出结果。
    if scored:
        floor = min(45, scored[0]["_match"] * 0.5)
        scored = [r for r in scored if r["_match"] >= floor]
    total = len(scored)
    out = [_item(r) for r in scored[:max(1, min(limit, 500))]]

    note = ""
    if relaxed:
        note = "没找到完全符合的，已放宽：" + "、".join(relaxed)
    if not out:
        if relaxed:
            note += ("；能放宽的条件已经放宽完了，再找不到就是相册里确实没有，"
                     "先确认这批照片已经导入。" + keep_tip)
        else:
            note = ("相册里没有符合这句话的照片，可以说得再宽一点（只说内容或只说时间）。"
                    + keep_tip)
    elif not relaxed and len(usable) == 1:
        note = f"只识别出一个条件：{usable[0]['label']}"
    elif not relaxed and not any(c["kind"] in ("time", "theme", "keyword") for c in usable):
        note = "这句话主要靠画面感觉匹配"
    return {"ok": True, "text": text, "conds": _out(parsed["conds"], manual_on), "items": out,
            "total": total, "scanned": len(rows), "note": note.strip(),
            "relaxed": relaxed, "truncated": len(rows) >= SCAN_CAP,
            "words": parsed["words"], **who}


def _drop_order(hard_conds: list[dict]) -> list[str]:
    """可放宽的硬条件及顺序：时间 → 文字线索 → 画面 → 不含。

    时间最容易因为 EXIF 缺失而误杀，所以第一个拆。
    **主题不在序列里了**：两条理解路径的主题条件都已降为软加权（主题是模型标的，
    它自己就可能标错，没资格当门槛），这里再留着只会让人以为还能放宽它。
    **“内容”不参与放宽**：用户要找的就是画面里那个东西，把它放宽掉等于拿不相干的
    图凑数（闭卷评测里“照片里有圣诞树”返回两张抽象插画，就是这么来的）。
    内容确实一个都没命中，就照实说相册里没有，不要硬给结果。
    """
    prio = {"time": 0, "keyword": 1,
            "visual": 2, "daypart": 2, "meta": 2, "exclude": 3}
    relaxable = [c for c in hard_conds if c["kind"] not in ("object", "theme")]
    return [c["cid"] for c in sorted(relaxable, key=lambda c: prio.get(c["kind"], 3))]


def _safe(fn: Callable[[dict], bool], r: dict) -> bool:
    try:
        return bool(fn(r))
    except Exception:
        return False


def _out(conds: list[dict], keep: Iterable[str] = ()) -> list[dict]:
    kept = set(keep or ())
    return [{"cid": c["cid"], "kind": c["kind"], "label": c["label"],
             "weight": c["w"], "hard": c["hard"], "ignored": c["ignored"],
             "kept": c["cid"] in kept} for c in conds]


def _tags_list(r: dict) -> list[str]:
    try:
        v = json.loads(r.get("tags") or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x) for x in v if str(x).strip()][:10] if isinstance(v, list) else []


def _item(r: dict) -> dict:
    """保留数据库行的常用字段，附加匹配信息，由接口层再格式化成前端结构。"""
    out = {k: v for k, v in r.items()
           if not k.startswith("_") and k not in ("feats", "exif", "path")}
    out["match"] = r["_match"]
    out["hits"] = r["_hit"]
    out["miss"] = r["_miss"]
    out["tags"] = _tags_list(r)      # 前端拿到的是数组，不用自己解 JSON
    return out
