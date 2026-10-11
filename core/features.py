"""图像特征提取：Pillow + numpy 完成色彩/构图/纹理/EXIF 分析，OpenCV 作为可选增强。

iPhone 的 .heic / .heif 靠 pillow-heif（也是可选增强），没装不影响启动，只是这类图读不了。
"""
from __future__ import annotations

import math
import re
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from .config import ANALYZE_SIZE, MIN_PIXELS

try:  # OpenCV 可选：连通域 + 直线检测，缺失时自动降级为纯 numpy 实现
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

try:  # HEIC/HEIF 可选：装了才认 iPhone 默认格式，导入失败不得影响启动，只记个标记
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF_OK = True
except Exception:  # pragma: no cover
    HEIF_OK = False

Image.MAX_IMAGE_PIXELS = None  # 不因为超大图报错

# 文件名关键词线索（中英）
KEYWORDS: dict[str, re.Pattern] = {
    "portrait": re.compile(r"selfie|portrait|人脸|人像|自拍|写真|合影|婚纱|全家福|photo_?\d*of_me", re.I),
    "nature": re.compile(r"landscape|mountain|beach|ocean|sea|sky|sunset|forest| scenery|"
                         r"风景|山|海|湖|沙滩|草原|日落|日出|天空|公园|自然", re.I),
    "city": re.compile(r"city|building|street|architecture|skyline|downtown|"
                       r"城市|建筑|街景|楼| bridge|桥梁|天际线|地铁", re.I),
    "food": re.compile(r"food|cake|coffee|lunch|dinner|brunch|pizza|restaurant|drink|"
                       r"美食|吃|餐|火锅|烧烤|面|咖啡|奶茶|蛋糕|下午茶|食堂", re.I),
    "animal": re.compile(r"cat|dog|pet|bird|fish|puppy|kitten|animal|"
                         r"猫|狗|宠物|鸟|鱼|动物|兔|马", re.I),
    "night": re.compile(r"night|dark|firework|lantern|neon|夜景|夜晚|夜间|灯光|烟花|霓虹|星空", re.I),
    "screenshot": re.compile(r"screen_?shot|screenshot|capture|snip|image_\d{4}_\d{6}|"
                             r"截图|截屏|屏幕快照|录屏|微信图片_", re.I),
    "mono": re.compile(r"mono|black_?and_?white|黑白|gray_?scale", re.I),
}

_TAKEN_FMT = ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%dT%H:%M:%S")


# ---------------------------------------------------------------- 基础数学

def _hsv(arr: np.ndarray):
    """输入 HxWx3 float(0-1)，返回 H(0-360)/S/V。"""
    mx = arr.max(axis=2)
    mn = arr.min(axis=2)
    diff = mx - mn
    s = np.divide(diff, mx, out=np.zeros_like(mx), where=mx > 1e-6)
    h = np.zeros_like(mx)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    nz = diff > 1e-6
    m = nz & (mx == r)
    h[m] = (60.0 * ((g[m] - b[m]) / diff[m]) + 360.0) % 360.0
    m = nz & (mx == g) & (mx != r)
    h[m] = 60.0 * ((b[m] - r[m]) / diff[m]) + 120.0
    m = nz & (mx == b) & (mx != r) & (mx != g)
    h[m] = 60.0 * ((r[m] - g[m]) / diff[m]) + 240.0
    return h, s, mx


def _skin_mask(r, g, b, h, s, v) -> np.ndarray:
    """YCbCr 色度区间 + 亮度/饱和约束：真人肤色比棕色毛发、食物更亮更淡。"""
    cb = 0.5 - 0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 0.5 + 0.5 * r - 0.418688 * g - 0.081312 * b
    return ((cr > 0.52) & (cr < 0.66) & (cb > 0.31) & (cb < 0.48)
            & ((h < 48) | (h > 348)) & (v > 0.48) & (s > 0.08) & (s < 0.60))


def _gradient(gray: np.ndarray):
    gx = np.zeros_like(gray)
    gy = np.zeros_like(gray)
    gx[:, 1:-1] = gray[:, 2:] - gray[:, :-2]
    gy[1:-1, :] = gray[2:, :] - gray[:-2, :]
    return gx, gy


def _block_std(arr: np.ndarray, n: int = 6) -> float:
    """图像分块的均值离散度：反映画面分区差异（主体 vs 背景）。"""
    h, w = arr.shape[:2]
    bh, bw = max(h // n, 1), max(w // n, 1)
    crop = arr[:bh * n, :bw * n].reshape(n, bh, n, bw, -1).mean(axis=(1, 3))
    return float(np.std(crop))


def _ring_sharpness(gmag: np.ndarray, ratio: float = 0.22) -> tuple[float, float]:
    h, w = gmag.shape
    dh, dw = int(h * ratio), int(w * ratio)
    border = np.concatenate([gmag[:dh, :].ravel(), gmag[-dh:, :].ravel(),
                             gmag[:, :dw].ravel(), gmag[:, -dw:].ravel()])
    center = gmag[dh:h - dh, dw:w - dw]
    return float(np.mean(center)) if center.size else 0.0, float(np.mean(border))


# ---------------------------------------------------------------- EXIF

def _read_exif(img: Image.Image) -> dict:
    out = {"camera": "", "date": "", "software": "", "orientation": 1,
           "iso": 0, "focal": "", "gps": "", "has_camera": False}
    try:
        exif = img.getexif()
    except Exception:
        return out
    if not exif:
        return out

    def txt(tag):
        try:
            v = exif.get(tag)
        except Exception:
            v = None
        return str(v).strip(" \x00") if v is not None else ""

    make, model = txt(271), txt(272)
    camera = " ".join(p for p in (make, model) if p).strip()
    out["camera"] = camera
    out["has_camera"] = bool(camera)
    out["software"] = txt(305)
    out["date"] = txt(306)
    try:
        out["orientation"] = int(exif.get(274, 1) or 1)
    except Exception:
        pass
    try:  # Exif sub-IFD
        sub = exif.get_ifd(0x8769)
        out["date"] = str(sub.get(36867) or sub.get(36868) or out["date"])
        if sub.get(34855):
            out["iso"] = int(sub.get(34855)[0] if isinstance(sub.get(34855), (list, tuple))
                             else sub.get(34855))
        f = sub.get(37385) or sub.get(37377)
        if f:
            out["focal"] = f"{float(f):.0f}mm" if not isinstance(f, (list, tuple)) else ""
    except Exception:
        pass
    try:  # GPS（仅展示，不参与分类）
        gps = exif.get_ifd(0x8825)
        if gps:
            def dms(val, ref):
                try:
                    d, m, sec = (float(v) for v in val)
                except Exception:
                    return None
                v = d + m / 60.0 + sec / 3600.0
                return -v if str(ref) in ("S", "W") else v
            lat, lon = dms(gps.get(2, ()), gps.get(1, "N")), dms(gps.get(4, ()), gps.get(3, "E"))
            if lat is not None and lon is not None:
                out["gps"] = f"{lat:.4f}, {lon:.4f}"
    except Exception:
        pass
    return out


def _taken_timestamp(exif: dict, fallback: float) -> float:
    for fmt in _TAKEN_FMT:
        try:
            return time.mktime(time.strptime(exif.get("date", "")[:19], fmt))
        except Exception:
            continue
    return fallback


# ---------------------------------------------------------------- 主分析

def analyze(path: str | Path) -> dict:
    """返回 {width,height,taken_at,exif,feats}；无法解码时抛 ValueError。"""
    p = Path(path)
    try:
        ctx = Image.open(p)
    except Exception as exc:
        # HEIC 最常见的卡点：没装 pillow-heif。报错得照着能做的写，不能只说“打不开”
        if p.suffix.lower() in (".heic", ".heif") and not HEIF_OK:
            raise ValueError("HEIC 读不了：装一下 pillow-heif（pip install pillow-heif）"
                             "再重启服务，或先在 Windows 上把这些 .heic 转成 jpg") from exc
        raise ValueError(f"打不开这张图（{type(exc).__name__}）") from exc
    with ctx as img:
        fmt = (img.format or "").lower()
        exif = _read_exif(img)
        img = img.convert("RGB")
        full_w, full_h = img.size
        img = ImageOps.exif_transpose(img)

    feats = _feats(img)
    feats.update({
        "full_w": full_w, "full_h": full_h,
        "megapixels": round(full_w * full_h / 1e6, 2),
        "format": fmt,
        "has_camera": 1.0 if exif["has_camera"] else 0.0,
        "name_hint": {k: (1.0 if pat.search(p.name) else 0.0) for k, pat in KEYWORDS.items()},
    })
    if full_w * full_h < MIN_PIXELS:
        raise ValueError("图片尺寸过小")
    # 拍摄时间只认真实 EXIF（DateTimeOriginal / DateTime）；读不到就记为 0（时间未知），
    # 不再拿文件 mtime（导入/复制时间）冒充拍摄时间。
    shot = _taken_timestamp(exif, 0.0)
    return {
        "width": full_w, "height": full_h,
        "taken_at": shot,
        "month": time.strftime("%Y-%m", time.localtime(shot)) if shot else "",
        "exif": exif, "feats": feats,
    }


def _feats(img: Image.Image) -> dict:
    img = img.copy()
    img.thumbnail((ANALYZE_SIZE, ANALYZE_SIZE), Image.BILINEAR)
    arr = np.asarray(img, dtype=np.float32) / 255.0
    h, w = arr.shape[:2]
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    hue, sat, val = _hsv(arr)
    gray = val
    f: dict[str, float] = {}

    f["aspect"] = round(w / max(h, 1), 3)
    f["brightness"] = float(np.mean(val))
    f["contrast"] = float(np.percentile(val, 95) - np.percentile(val, 5))
    f["sat_mean"] = float(np.mean(sat))
    f["sat_p75"] = float(np.percentile(sat, 75))
    f["val_p90"] = float(np.percentile(val, 90))
    f["dark_ratio"] = float(np.mean(val < 0.14))
    f["light_ratio"] = float(np.mean(val > 0.86))
    f["neutral_ratio"] = float(np.mean(sat < 0.12))       # 低饱和（灰、白、黑）
    f["gray_ratio"] = float(np.mean((sat < 0.10) & (val > 0.12) & (val < 0.88)))
    f["white_bg"] = float(np.mean((sat < 0.10) & (val > 0.90)))

    # 色度分区
    f["warm_ratio"] = float(np.mean(((hue < 62) | (hue > 338)) & (sat > 0.28) & (val > 0.18)))
    f["green_ratio"] = float(np.mean((hue > 65) & (hue < 175) & (sat > 0.16) & (val > 0.08)))
    blue = (hue > 185) & (hue < 265) & (sat > 0.12)
    f["blue_ratio"] = float(np.mean(blue))
    f["brown_ratio"] = float(np.mean((hue > 10) & (hue < 45) & (sat > 0.14) & (val < 0.62)))
    f["purple_ratio"] = float(np.mean((hue > 265) & (hue < 335) & (sat > 0.16)))

    # 饱和度/色度鲜艳度（Hasler-Süsstrink）
    rg = r - g
    yb = 0.5 * (r + g) - b
    f["colorfulness"] = round(float(
        math.hypot(rg.std(), yb.std()) + 0.3 * math.hypot(rg.mean(), yb.mean())), 4)

    # 皮肤与主体
    skin = _skin_mask(r, g, b, hue, sat, val)
    f["skin_ratio"] = float(np.mean(skin))
    f["skin_blob"], f["face_like"] = _skin_blob(skin)
    brown = (hue > 8) & (hue < 48) & (sat > 0.16) & (val > 0.12) & (val < 0.72)
    f["brown_blob"] = _largest_blob(brown)
    f["subject_center"] = _center_share(brown | skin)      # 主体是否在画面中心

    # 天空：上部蓝色/亮白且纹理弱，同时与下部地面拉开差异
    top_n = max(int(h * 0.34), 1)
    top = slice(0, top_n)
    skyish = (((b > r + 0.02) & (b > g + 0.005)) | ((sat < 0.13) & (val > 0.72))) & (val > 0.30)
    veg = (hue > 65) & (hue < 175) & (sat > 0.14) & (val > 0.08)
    f["sky_ratio"] = round(float(np.mean(skyish[top])), 4)
    f["sky_bottom"] = round(float(np.mean(skyish[h - top_n:])), 4)
    f["sky_diff"] = round(max(0.0, f["sky_ratio"] - f["sky_bottom"]), 4)  # 真空空的“上有天”
    f["ground_green"] = round(float(np.mean(veg[h - top_n:])), 4)          # 下部位草地/山体绿植
    f["bg_uniform"] = _bg_uniformity(arr[top], arr[h - top_n:])            # 上下同色 → 影棚/背景布

    # 边缘 / 纹理 / 直线结构
    gx, gy = _gradient(gray)
    gmag = np.hypot(gx, gy)
    f["edge_density"] = round(float(np.mean(gmag)), 4)
    f["strong_edge"] = round(float(np.mean(gmag > 0.10)), 4)
    f["top_edge"], f["bottom_edge"] = round(float(np.mean(gmag[: h // 2])), 4), round(float(np.mean(gmag[h // 2:])), 4)
    f["center_sharp"], f["border_sharp"] = _ring_sharpness(gmag)
    f["sharp_diff"] = round(f["center_sharp"] - f["border_sharp"], 4)  # >0：背景虚化，人像常见
    f["block_diversity"] = round(_block_std(arr), 4)
    f["orient_conc"] = _orientation_concentration(gx, gy, gmag)
    f["line_score"] = _line_score(gray, gmag)

    # 截图 / 文档特征：大面积纯色块、上下状态栏、极低色度
    f["flat_top_bar"] = _bar_uniformity(arr[: max(int(h * 0.045), 1)])
    f["flat_bottom_bar"] = _bar_uniformity(arr[h - max(int(h * 0.045), 1):])
    f["mono_text_rows"] = _text_rows(gmag)
    f["palette_flat"] = _palette_flat(arr)

    # 中心 / 四周分区统计（区分“主体”与“环境”）
    cy0, cy1, cx0, cx1 = int(h * 0.28), int(h * 0.72), int(w * 0.28), int(w * 0.72)
    for key, m in (("green", veg), ("brown", brown), ("warm", (hue < 62) | (hue > 338)),
                   ("blue", blue), ("skin", skin)):
        sel = m[cy0:cy1, cx0:cx1]
        sur = np.concatenate([m[:cy0].ravel(), m[cy1:].ravel(),
                              m[:, :cx0].ravel(), m[:, cx1:].ravel()])
        f[f"{key}_center"] = round(float(np.mean(sel)), 4)
        f[f"{key}_border"] = round(float(np.mean(sur)), 4)

    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in f.items()}


def _bg_uniformity(top: np.ndarray, bottom: np.ndarray) -> float:
    """上部与下部平均色的接近度：高 → 整图背景单一（影棚/纯色底）。"""
    d = np.linalg.norm(top.reshape(-1, 3).mean(0) - bottom.reshape(-1, 3).mean(0))
    return round(1.0 - min(1.0, float(d) * 3.2), 4)


def _largest_blob(mask: np.ndarray) -> float:
    """最大同色连通域占画面比例（主体面积信号）。"""
    if cv2 is None or float(np.mean(mask)) < 0.01:
        return 0.0
    try:
        m = (mask * 255).astype(np.uint8)
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        n, _, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
        if n <= 1:
            return 0.0
        return round(float(stats[1:, cv2.CC_STAT_AREA].max()) / max(mask.size, 1), 4)
    except Exception:
        return 0.0


def _center_share(mask: np.ndarray) -> float:
    h, w = mask.shape
    c = mask[int(h * 0.3):int(h * 0.7), int(w * 0.3):int(w * 0.7)]
    e = np.concatenate([mask[:int(h * 0.3)].ravel(), mask[int(h * 0.7):].ravel(),
                        mask[:, :int(w * 0.3)].ravel(), mask[:, int(w * 0.7):].ravel()])
    return round(float(c.mean() - e.mean()), 4)


def _orientation_concentration(gx, gy, gmag) -> float:
    """梯度方向直方图的集中度：高=画面以横竖直线为主（建筑/截图）。"""
    m = gmag > max(0.06, gmag.mean() * 1.2)
    if not np.any(m):
        return 0.0
    ang = (np.arctan2(gy[m], gx[m]) + np.pi) % np.pi          # 0..pi
    hist, _ = np.histogram(ang, bins=12, range=(0, np.pi), weights=gmag[m])
    total = hist.sum()
    if total <= 0:
        return 0.0
    hist = hist / total
    return float(np.sort(hist)[-2:].sum())                     # 两个最强方向的占比


def _line_score(gray, gmag) -> float:
    """长直线密度：优先用 OpenCV HoughLinesP，不可用时给出基于梯度的近似值。"""
    if cv2 is None:
        strong = gmag > max(0.10, gmag.mean() * 1.6)
        return round(min(1.0, float(np.mean(strong)) * 6.0), 4)
    try:
        g8 = (np.clip(gray, 0, 1) * 255).astype(np.uint8)
        edges = cv2.Canny(g8, 60, 150)
        diag = math.hypot(*gray.shape)
        lines = cv2.HoughLinesP(edges, 1, np.pi / 90, threshold=60,
                                minLineLength=int(max(gray.shape) * 0.26), maxLineGap=6)
        if lines is None:
            return 0.0
        segs = np.asarray(lines).reshape(-1, 4)
        long_en = [s for s in segs if math.hypot(s[2] - s[0], s[3] - s[1]) > diag * 0.30]
        axis = [s for s in long_en
                if abs(s[3] - s[1]) < 12 or abs(s[2] - s[0]) < 12]   # 水平/垂直主导
        return round(min(1.0, (len(long_en) + len(axis)) / 30.0), 4)
    except Exception:
        return 0.0


def _skin_blob(skin: np.ndarray) -> tuple[float, float]:
    """最大肤色连通域占比 + 是否具备人脸形态（比例、填充率、位置）。"""
    ratio = float(skin.mean())
    if cv2 is None or ratio < 0.01:
        return 0.0, 0.0
    try:
        mask = (skin * 255).astype(np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        n, _, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
        if n <= 1:
            return 0.0, 0.0
        areas = stats[1:, cv2.CC_STAT_AREA]
        i = int(np.argmax(areas)) + 1
        area = float(areas[i - 1]) / max(skin.size, 1)
        bw, bh = stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT]
        fill = stats[i, cv2.CC_STAT_AREA] / max(bw * bh, 1)
        cx, cy = cents[i]
        h, w = skin.shape
        face_like = 1.0 if (area > 0.015 and 0.55 < bw / max(bh, 1) < 1.25
                          and fill > 0.52 and 0.12 < cx / w < 0.88 and cy / h < 0.82) else 0.0
        return round(area, 4), face_like
    except Exception:
        return 0.0, 0.0


def _bar_uniformity(bar: np.ndarray) -> float:
    """条带内颜色是否高度统一（截图状态栏/导航栏的典型特征）。"""
    if bar.size == 0:
        return 0.0
    flat = 1.0 - min(1.0, float(np.std(bar)) * 8.0)
    return round(flat, 4)


def _text_rows(gmag: np.ndarray) -> float:
    """横向边缘在行方向上的周期密度：文字/列表排版的信号。"""
    row_edge = gmag.mean(axis=1)
    peaks = float(np.mean(row_edge > row_edge.mean() * 1.35))
    return round(peaks, 4)


def _palette_flat(arr: np.ndarray) -> float:
    """量化后主色占比：截图/文档常有超大比例纯色背景。"""
    q = (np.clip(arr, 0, 1) * 8).astype(np.uint8)
    codes = (q[..., 0].astype(np.uint32) << 12) | (q[..., 1].astype(np.uint32) << 8) | q[..., 2]
    counts = np.bincount(codes.ravel())
    return round(float(counts.max()) / max(codes.size, 1), 4)


def is_probably_readable(path: str | Path) -> bool:
    try:
        with Image.open(path) as im:
            im.verify()
        return True
    except Exception:
        return False
