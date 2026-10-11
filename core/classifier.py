"""主题分类规则引擎：把 features.py 的量化特征映射为 9 大相册主题。

不依赖任何机器学习模型，全部为可解释的色彩 / 构图 / 纹理 / EXIF 规则，
每条判断都会生成中文依据（reasons），方便用户在界面上核对与人工纠正。
"""
from __future__ import annotations

from .config import CONF_THRESHOLD, THEMES, UNKNOWN_ID


def ramp(x: float, lo: float, hi: float) -> float:
    """线性归一化：x<=lo → 0，x>=hi → 1。"""
    if hi == lo:
        return 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


class _Score:
    __slots__ = ("value", "notes")

    def __init__(self) -> None:
        self.value = 0.0
        self.notes: list[tuple[float, str]] = []

    def plus(self, weight: float, term: float, msg: str) -> None:
        self.value += weight * term
        if term >= 0.5:
            self.notes.append((weight * term, msg))

    def minus(self, weight: float, term: float, msg: str) -> None:
        self.value -= weight * term
        if term >= 0.5:
            self.notes.append((-weight * term, msg))

    def top_notes(self, n: int = 3) -> list[str]:
        return [m for _, m in sorted(self.notes, key=lambda t: -abs(t[0]))[:n] if m]


# ---------------------------------------------------------------- 各主题打分

def _portrait(f: dict, s: _Score) -> None:
    skin, blob, face = f.get("skin_ratio", 0), f.get("skin_blob", 0), f.get("face_like", 0)
    s.plus(2.4, ramp(skin, 0.04, 0.26), f"肤色区域占比 {skin:.0%}")
    s.plus(2.0, ramp(blob, 0.012, 0.13), f"存在成片肤色主体（{blob:.0%} 画面）")
    s.plus(1.3, face, "肤色连通域呈人脸比例与位置")
    s.plus(0.9, ramp(f.get("subject_center", 0), 0.02, 0.16), "暖色主体居于画面中心")
    s.plus(0.8, ramp(f.get("sharp_diff", 0), 0.008, 0.05), "中心清晰、边缘虚化（浅景深）")
    s.plus(0.7, ramp(f.get("bg_uniform", 0), 0.62, 0.92), "上下背景色调一致（背景布/大光圈）")
    s.plus(0.5, ramp(0.98 - f.get("aspect", 1), 0, 0.35), "竖幅构图，常见于人物照")
    s.minus(1.1, ramp(f.get("sky_diff", 0), 0.22, 0.45), "上方为真实天空、下方为地面")
    s.minus(1.0, ramp(f.get("white_bg", 0), 0.32, 0.60), "大面积纯白背景，更像截图")


def _nature(f: dict, s: _Score) -> None:
    sky, diff = f.get("sky_ratio", 0), f.get("sky_diff", 0)
    green, ggreen = f.get("green_ratio", 0), f.get("ground_green", 0)
    s.plus(1.5, ramp(sky, 0.12, 0.50), f"上部天空/远景亮区占比 {sky:.0%}")
    s.plus(1.6, ramp(diff, 0.08, 0.36), "上亮下暗，典型的天与地层次")
    s.plus(1.9, ramp(green, 0.10, 0.42), f"植被绿色占比 {green:.0%}")
    s.plus(1.0, ramp(ggreen, 0.10, 0.40), "下方为草地/山体绿植")
    s.plus(0.8, ramp(f.get("blue_ratio", 0), 0.18, 0.50), "蓝调（天空或水面）占比高")
    s.plus(0.5, ramp(f.get("colorfulness", 0), 0.16, 0.36), "色彩鲜艳通透")
    s.plus(0.4, ramp(f.get("aspect", 1), 1.20, 1.65), "横幅画面，常见于风景")
    s.minus(1.4, ramp(f.get("line_score", 0), 0.35, 0.75), "存在大量笔直人工线条")
    s.minus(1.1, ramp(f.get("neutral_ratio", 0), 0.55, 0.82), "整体低饱和（灰调）")
    s.minus(0.9, ramp(f.get("bg_uniform", 0), 0.70, 0.94), "上下同色，缺乏天空/地面层次")
    s.minus(1.2, ramp(f.get("skin_blob", 0), 0.045, 0.14), "画面被肤色主体占据")


def _city(f: dict, s: _Score) -> None:
    line, conc, neutral = f.get("line_score", 0), f.get("orient_conc", 0), f.get("neutral_ratio", 0)
    s.plus(2.2, ramp(line, 0.15, 0.55), f"检测到较多长直线（{int(line * 30)} 条主导）")
    s.plus(1.6, ramp(conc, 0.45, 0.72), "边缘方向集中在横竖两向")
    s.plus(1.2, ramp(neutral, 0.35, 0.68), f"混凝土灰调占比 {neutral:.0%}")
    s.plus(0.6, ramp(f.get("gray_ratio", 0), 0.25, 0.50), "中灰墙面/路面色调明显")
    s.plus(0.5, ramp(f.get("aspect", 1), 1.20, 1.70), "横幅画面利于街景建筑")
    s.minus(1.2, ramp(f.get("green_ratio", 0), 0.32, 0.58), "植被茂密，更像自然风光")
    s.minus(1.0, ramp(f.get("skin_ratio", 0), 0.08, 0.22), "肤色占比过高")


def _food(f: dict, s: _Score) -> None:
    warm, satm = f.get("warm_ratio", 0), f.get("sat_mean", 0)
    s.plus(2.2, ramp(warm, 0.12, 0.42), f"暖红/橙黄色调占比 {warm:.0%}")
    s.plus(1.6, ramp(satm, 0.24, 0.48), "整体饱和度较高（食物色泽）")
    s.plus(0.9, ramp(f.get("sat_p75", 0), 0.32, 0.62), "局部色彩浓烈")
    s.plus(0.7, ramp(f.get("brown_ratio", 0), 0.06, 0.24), "棕色系（烘焙/肉食）明显")
    s.plus(0.6, ramp(1.0 - f.get("green_border", 0), 0.55, 0.85), "周围无户外植被（室内餐桌）")
    s.plus(0.5, ramp(1.25 - f.get("aspect", 1), 0, 0.40), "近距特写构图")
    s.minus(1.3, ramp(f.get("sky_ratio", 0), 0.20, 0.45), "上方为天空，非餐桌场景")
    s.minus(1.6, ramp(f.get("green_ratio", 0), 0.35, 0.62), "绿色植被为主，更像户外/宠物")
    s.minus(1.0, ramp(f.get("line_score", 0), 0.40, 0.72), "直线结构过多")
    s.minus(1.1, f.get("face_like", 0), "检测到人脸形态，优先归为人像")


def _animal(f: dict, s: _Score) -> None:
    blob, center = f.get("brown_blob", 0), f.get("subject_center", 0)
    gborder, gc = f.get("green_border", 0), f.get("green_center", 0)
    s.plus(1.9, ramp(blob, 0.05, 0.26), f"中心有成团棕黄主体（{blob:.0%} 画面）")
    s.plus(1.2, ramp(center, 0.02, 0.18), "主体集中在画面中央，背景在外圈")
    s.plus(1.1, ramp(gborder - gc, 0.04, 0.22), "周围是草地/自然环境")
    s.plus(0.6, ramp(f.get("strong_edge", 0), 0.06, 0.16), "细密纹理（毛发/羽毛）")
    s.minus(1.2, ramp(f.get("line_score", 0), 0.35, 0.70), "人工直线结构过多")
    s.minus(1.3, ramp(f.get("warm_ratio", 0), 0.32, 0.55), "整幅暖色特写，更像美食")
    s.minus(0.9, ramp(f.get("sky_diff", 0), 0.25, 0.50), "大面积天空，主体不明确")
    s.minus(1.2, f.get("face_like", 0), "检测到人脸形态，优先归为人像")


def _night(f: dict, s: _Score) -> None:
    bright, dark, light = f.get("brightness", 1), f.get("dark_ratio", 0), f.get("light_ratio", 0)
    spot = light if bright < 0.45 else 0.0
    s.plus(2.0, ramp(0.42 - bright, 0, 0.22), f"整体偏暗（亮度 {bright:.2f}）")
    s.plus(1.4, ramp(dark, 0.25, 0.60), f"暗部占比 {dark:.0%}")
    s.plus(1.6, ramp(spot, 0.01, 0.12), "暗背景中存在高亮光源（灯/烟火）")
    s.plus(0.6, ramp(f.get("contrast", 0), 0.45, 0.80), "明暗对比强烈")
    s.plus(0.5, ramp(f.get("purple_ratio", 0) + f.get("blue_ratio", 0), 0.25, 0.55), "偏冷的蓝紫色调")
    s.minus(0.9, ramp(f.get("white_bg", 0), 0.30, 0.55), "大面积纯白背景")
    s.minus(0.8, ramp(bright, 0.55, 0.80), "画面整体明亮")
    # 注：试过给夜景加“强结构直线→归城市”的罚项，但实测净负（夜景天际线本身
    # 就含大量建筑直线，会误伤正当夜景）；且城市/夜景的实际错判来自模型侧，
    # 不靠规则旋钮掩盖。故保留旧阈值，城市/夜景的定义改在 config desc + 未来重标注 prompt。


def _mono(f: dict, s: _Score) -> None:
    # 已废弃：黑白不再当“和肖像/城市互斥的内容主题”抢一个名额（那会把黑白人像
    # 判错）。风格判定改用下面的 mono_score()，_mono 仅为旧数据行保留、不进 SCORERS。
    satm, neutral = f.get("sat_mean", 1), f.get("neutral_ratio", 0)
    s.plus(3.0, ramp(0.11 - satm, 0, 0.06), f"几乎无彩色（平均饱和 {satm:.2f}）")
    s.plus(1.2, ramp(neutral, 0.85, 0.98), "灰阶覆盖全图")
    s.minus(1.4, ramp(f.get("palette_flat", 0), 0.40, 0.70), "超大纯色块，更像截图")
    s.minus(0.8, ramp(f.get("white_bg", 0), 0.45, 0.70), "纯白背景占比过高")


def mono_score(f: dict) -> float:
    """黑白/单色风格强度 0~1。与内容正交：一张黑白人像仍然是人像，只是额外带“黑白”风格。

    用现有 feats（sat_mean/neutral_ratio/palette_flat/white_bg）本地算，
    不需解码也不需模型，因此可全库免费回填。
    """
    f = f or {}
    satm = f.get("sat_mean", 1.0)
    # 低饱和主导：sat_mean<=0.05 给满，0.11 以上为 0
    sat_strength = ramp(0.11 - satm, 0.0, 0.06)
    # 灰阶覆盖度作为辅助证据（不能单独拉高，否则低饱和的截图也进来）
    gray_strength = ramp(f.get("neutral_ratio", 0), 0.80, 0.95)
    base = 0.78 * sat_strength + 0.22 * gray_strength
    # 降权非摄影的黑白：超大纯色块 / 纯白底（那些是图形/截图不是黑白照片）
    base -= 0.45 * ramp(f.get("palette_flat", 0), 0.40, 0.70)
    base -= 0.30 * ramp(f.get("white_bg", 0), 0.45, 0.70)
    return round(max(0.0, min(1.0, base)), 3)


def is_mono(f: dict, thr: float = 0.5) -> bool:
    """黑白风格判定：强度过阈即为黑白（thr 可在评测里调）。"""
    return mono_score(f) >= thr


def _screenshot(f: dict, s: _Score) -> None:
    flat, white = f.get("palette_flat", 0), f.get("white_bg", 0)
    bars = (f.get("flat_top_bar", 0) + f.get("flat_bottom_bar", 0)) / 2
    s.plus(1.4, 1.0 - f.get("has_camera", 0.0), "文件不含相机 EXIF 信息")
    s.plus(1.5, ramp(flat, 0.25, 0.60), f"超大单一色块占比 {flat:.0%}")
    s.plus(1.3, ramp(white, 0.25, 0.55), f"白底占比 {white:.0%}")
    s.plus(1.1, ramp(bars, 0.55, 0.90), "上下边缘为纯色条（状态栏/导航栏）")
    s.plus(0.9, ramp(f.get("orient_conc", 0), 0.50, 0.78), "边缘方向高度规整（文字排版）")
    s.plus(0.7, ramp(f.get("mono_text_rows", 0), 0.25, 0.50), "存在周期性横向纹理（行文本）")
    s.plus(0.5, ramp(abs(f.get("aspect", 1) - 0.462), 0, 0.06)
           + ramp(abs(f.get("aspect", 1) - 1.777), 0, 0.06), "画面比例符合常见屏幕分辨率")
    s.minus(1.4, ramp(f.get("colorfulness", 0), 0.22, 0.45), "色彩丰富，为真实拍摄照片")
    s.minus(1.2, ramp(f.get("sky_ratio", 0), 0.20, 0.45), "上方为天空，非界面截图")


SCORERS = {
    "portrait": _portrait,
    "nature": _nature,
    "city": _city,
    "food": _food,
    "animal": _animal,
    "night": _night,
    "screenshot": _screenshot,
    # 注：mono 已从互斥主题池移出，改走正交风格轴（mono_score/is_mono）。
}

BASE_SCORE = 0.55          # 未归类基准线：没有任何主题明显胜出时落到未归类
NAME_HINT_WEIGHT = 1.35    # 文件名关键词的加权


def classify(feats: dict) -> tuple[str, float, dict[str, float], list[str]]:
    """返回 (主题id, 置信度 0-1, 各主题得分, 判定依据)。"""
    f = feats or {}
    scores: dict[str, float] = {}
    notes: dict[str, list[str]] = {}
    hints = f.get("name_hint", {}) or {}

    for tid, fn in SCORERS.items():
        sc = _Score()
        try:
            fn(f, sc)
        except Exception:  # 单个主题异常不影响其它主题
            sc.value = 0.0
        hint = hints.get(tid, 0.0)
        if hint:
            sc.plus(NAME_HINT_WEIGHT, hint, f"文件名命中关键词（{THEMES[tid]['name']}）")
        scores[tid] = round(max(0.0, sc.value), 3)
        notes[tid] = sc.top_notes()

    scores[UNKNOWN_ID] = BASE_SCORE
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    best, best_score = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0

    # 置信度 = 与第二名的领先幅度 + 自身得分强度
    gap = (best_score - second) / max(best_score, 1e-6)
    strength = min(1.0, best_score / 4.5)
    conf = round(0.55 * gap + 0.45 * strength, 3)

    # 得分太低或主题之间难分难解 → 未归类
    if best == UNKNOWN_ID or best_score < 1.2 or conf < CONF_THRESHOLD:
        best, conf = UNKNOWN_ID, round(min(conf, 0.35), 3)

    reasons = notes.get(best, []) if best != UNKNOWN_ID else \
        ["各项特征均不明显，建议人工确认或用文件名关键词辅助"]
    if not reasons:
        reasons = [f"{THEMES[best]['name']}特征胜出（得分 {scores[best]:.1f}）"]
    return best, round(conf, 3), scores, reasons
