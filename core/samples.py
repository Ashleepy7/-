"""示例照片生成器：用 numpy + Pillow 合成各主题的示意图，便于快速体验分类效果。

生成的文件只放在 data/samples/ 下，不会触碰用户任何真实照片。
"""
from __future__ import annotations

import time

import numpy as np
from PIL import Image, ImageDraw

from .config import SAMPLE_DIR

_RNG = np.random.default_rng(20261005)


# ------------------------------------------------------------------ 小工具

def _new(w: int, h: int, col: tuple[int, int, int]) -> Image.Image:
    return Image.new("RGB", (w, h), col)


def _f(img: Image.Image) -> np.ndarray:
    return np.asarray(img, dtype=np.float32) / 255.0


def _img(arr: np.ndarray) -> Image.Image:
    return Image.fromarray((np.clip(arr, 0, 1) * 255).astype(np.uint8))


def _noise(arr: np.ndarray, amp: float = 0.02) -> np.ndarray:
    return arr + _RNG.normal(0, amp, arr.shape).astype(np.float32)


def _blob(arr: np.ndarray, cx: int, cy: int, r: int, add: float) -> None:
    """给局部区域加一层柔光，模拟光斑/虚化。"""
    y0, y1 = max(cy - r, 0), min(cy + r, arr.shape[0])
    x0, x1 = max(cx - r, 0), min(cx + r, arr.shape[1])
    if y1 > y0 and x1 > x0:
        reg = arr[y0:y1, x0:x1]
        yy, xx = np.mgrid[y0:y1, x0:x1]
        d = np.clip(1 - np.hypot(xx - cx, yy - cy) / max(r, 1), 0, 1)
        arr[y0:y1, x0:x1] = np.clip(reg + d[..., None] * add, 0, 1)


# ------------------------------------------------------------------ 各主题画面

def _nature() -> Image.Image:
    w, h = 1280, 860
    arr = _f(_new(w, h, (128, 186, 242)))
    grad = np.linspace(0.10, 0.34, h).astype(np.float32)
    arr[..., 0] += grad[:, None] * 0.55
    arr[..., 1] += grad[:, None] * 0.42
    horizon = int(h * 0.50)
    img = _img(arr)
    d = ImageDraw.Draw(img)
    for i, (amp, col) in enumerate([(70, (74, 116, 104)), (110, (42, 132, 60))]):
        xs = np.arange(w)
        ys = horizon + i * 46 - amp * np.abs(np.sin(xs / (250 + i * 110)) * 1.2 + i)
        d.polygon(list(zip(xs.tolist(), ys.astype(int).tolist())) + [(w, h), (0, h)], fill=col)
    d.ellipse([w * 0.72, h * 0.06, w * 0.80, h * 0.14], fill=(255, 248, 214))
    for _ in range(10):  # 云：低饱和亮块
        cx, cy = int(_RNG.uniform(0, w)), int(_RNG.uniform(0, horizon * 0.7))
        rw, rh = int(_RNG.uniform(70, 200)), int(_RNG.uniform(18, 46))
        d.ellipse([cx - rw, cy - rh, cx + rw, cy + rh], fill=(236, 243, 250))
    arr = _noise(_f(img), 0.012)
    return _img(arr)


def _portrait() -> Image.Image:
    w, h = 900, 1240
    arr = _f(_new(w, h, (150, 162, 176)))
    arr += np.linspace(0.10, -0.06, h).astype(np.float32)[:, None, None] * np.array(
        [0.5, 0.6, 0.8], np.float32)[None, None, :]
    img = _img(np.clip(arr, 0, 1))
    d = ImageDraw.Draw(img)
    cx = w // 2 + int(_RNG.uniform(-40, 40))
    cy = int(h * 0.36)
    hr = int(min(w, h) * 0.235)          # 头部占比接近真实自拍
    tone = int(_RNG.integers(0, 3))
    skin_col = [(226, 176, 140), (206, 150, 116), (150, 100, 74)][tone]
    hair_col = [(28, 24, 22), (46, 32, 22), (18, 18, 20)][tone]
    d.ellipse([cx - hr * 1.35, cy - hr * 1.45, cx + hr * 1.35, cy + hr * 1.6], fill=hair_col)
    d.ellipse([cx - hr, cy - hr * 1.05, cx + hr, cy + hr * 1.12], fill=skin_col)      # 脸
    d.ellipse([cx - hr * 0.5, cy + hr * 0.82, cx + hr * 0.5, cy + hr * 1.55], fill=skin_col)
    d.rectangle([cx - hr * 2.2, cy + hr * 1.55, cx + hr * 2.2, h], fill=(58, 78, 122))  # 衣服
    d.ellipse([cx - hr * 1.05, cy - hr * 1.18, cx + hr * 1.05, cy - hr * 0.18], fill=hair_col)
    out = _f(img)
    # 背景柔化：上/下抹平，中心保留细节（浅景深信号）
    k = int(h * 0.16)
    out[:k] = np.clip(out[:k].mean(axis=(0, 1), keepdims=True) * 0.55 + out[:k] * 0.45, 0, 1)
    out[-k:] = np.clip(out[-k:].mean(axis=(0, 1), keepdims=True) * 0.55 + out[-k:] * 0.45, 0, 1)
    return _img(_noise(out, 0.012))


def _city() -> Image.Image:
    w, h = 1280, 900
    img = _new(w, h, (178, 188, 200))
    d = ImageDraw.Draw(img)
    x = 0
    while x < w:
        bw = int(_RNG.uniform(90, 230))
        bh = int(_RNG.uniform(h * 0.32, h * 0.88))
        tone = int(_RNG.uniform(60, 160))
        col = (tone, tone + 2, tone + 8)
        d.rectangle([x, h - bh, x + bw, h], fill=col)
        for wy in range(h - bh + 16, h - 24, 26):            # 规整窗格 → 强横竖线条
            for wx in range(x + 12, max(x + bw - 12, x + 13), 22):
                d.rectangle([wx, wy, wx + 11, wy + 15],
                            fill=(int(tone * 0.5), int(tone * 0.52), int(tone * 0.6)))
        x += bw + int(_RNG.uniform(4, 26))
    d.rectangle([0, h - 44, w, h], fill=(82, 82, 86))
    return _img(_noise(_f(img), 0.01))


def _food() -> Image.Image:
    w, h = 1000, 1000
    img = _new(w, h, (76, 40, 22))
    d = ImageDraw.Draw(img)
    d.ellipse([w * 0.05, h * 0.08, w * 0.95, h * 0.94], fill=(232, 226, 214))
    d.ellipse([w * 0.15, h * 0.18, w * 0.85, h * 0.84], fill=(186, 92, 40))
    for _ in range(28):
        cx, cy = _RNG.uniform(w * 0.22, w * 0.78), _RNG.uniform(h * 0.26, h * 0.76)
        r = _RNG.uniform(26, 76)
        col = (int(_RNG.uniform(160, 250)), int(_RNG.uniform(46, 152)), int(_RNG.uniform(10, 70)))
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)
    for _ in range(12):  # 蔬菜点缀
        cx, cy = _RNG.uniform(w * 0.25, w * 0.75), _RNG.uniform(h * 0.3, h * 0.7)
        r = _RNG.uniform(14, 32)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(58, 142, 46))
    return _img(_noise(_f(img), 0.018))


def _animal() -> Image.Image:
    w, h = 1100, 900
    img = _new(w, h, (62, 122, 52))
    d = ImageDraw.Draw(img)
    cx, cy = w // 2, int(h * 0.56)
    body = (122, 78, 38)
    d.ellipse([cx - 400, cy - 240, cx + 400, cy + 300], fill=body)      # 身体占画面主体
    d.ellipse([cx - 210, cy - 400, cx + 230, cy - 60], fill=(144, 96, 46))
    d.polygon([(cx - 180, cy - 360), (cx - 100, cy - 500), (cx - 40, cy - 330)], fill=body)
    d.polygon([(cx + 70, cy - 350), (cx + 150, cy - 495), (cx + 215, cy - 320)], fill=body)
    for _ in range(2600):  # 毛发细密纹理
        px, py = _RNG.uniform(cx - 380, cx + 380), _RNG.uniform(cy - 400, cy + 290)
        ln = _RNG.uniform(6, 18)
        v = int(_RNG.uniform(70, 175))
        d.line([px, py, px + 3, py + ln], fill=(v, int(v * 0.68), int(v * 0.4)), width=2)
    d.ellipse([cx - 130, cy - 260, cx - 70, cy - 200], fill=(24, 20, 16))
    d.ellipse([cx + 62, cy - 260, cx + 122, cy - 200], fill=(24, 20, 16))
    d.ellipse([cx - 36, cy - 150, cx + 42, cy - 96], fill=(32, 24, 20))
    return _img(_noise(_f(img), 0.02))


def _night() -> Image.Image:
    w, h = 1200, 800
    img = _new(w, h, (9, 12, 26))
    d = ImageDraw.Draw(img)
    for x in range(0, w, 92):  # 建筑剪影 + 亮窗
        top = int(_RNG.uniform(h * 0.28, h * 0.72))
        d.rectangle([x, top, x + 74, h], fill=(5, 6, 12))
        for wy in range(top + 18, h - 20, 34):
            for wx in range(x + 12, x + 66, 22):
                if _RNG.random() > 0.45:
                    v = _RNG.uniform(0.6, 1.0)
                    d.rectangle([wx, wy, wx + 11, wy + 17],
                                fill=(int(255 * v), int(220 * v), int(130 * v)))
    d.rectangle([0, int(h * 0.78), w, h], fill=(12, 12, 20))
    for _ in range(90):  # 路灯、车灯、霓虹
        cx, cy = _RNG.uniform(0, w), _RNG.uniform(h * 0.55, h)
        r = _RNG.uniform(3, 13)
        col = (255, int(_RNG.uniform(150, 240)), int(_RNG.uniform(60, 200)))
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)
    out = _f(img)
    for _ in range(28):
        _blob(out, int(_RNG.uniform(0, w)), int(_RNG.uniform(h * 0.5, h)),
              int(_RNG.uniform(40, 110)), _RNG.uniform(0.06, 0.16))
    return _img(_noise(out, 0.008))


def _mono() -> Image.Image:
    w, h = 1000, 750
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    base = 0.40 + 0.20 * np.sin(xx / 90.0) + 0.13 * np.cos(yy / 60.0)
    base = np.clip(base + _RNG.normal(0, 0.05, (h, w)), 0.06, 0.92)
    arr = np.stack([base] * 3, axis=-1).astype(np.float32)
    img = _img(arr)
    d = ImageDraw.Draw(img)
    d.line([0, h * 0.66, w, h * 0.30], fill=(30, 30, 30), width=24)
    d.ellipse([w * 0.60, h * 0.08, w * 0.80, h * 0.28], fill=(222, 222, 222))
    out = _noise(_f(img), 0.02)
    out[..., 0] += _RNG.normal(0, 0.003, (h, w))  # 极轻微彩色残留
    return _img(out)


def _screenshot() -> Image.Image:
    w, h = 1080, 2340
    img = _new(w, h, (248, 248, 250))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, 96], fill=(230, 231, 235))                # 状态栏
    d.rectangle([40, 34, 150, 62], fill=(36, 36, 40))
    d.rectangle([w - 170, 40, w - 60, 58], fill=(48, 48, 52))
    d.rectangle([0, 96, w, 232], fill=(34, 116, 230))               # 标题栏
    d.rectangle([60, 150, 420, 182], fill=(246, 248, 252))
    y = 280
    while y < h - 160:                                              # 文本行
        bw = int(_RNG.uniform(320, w - 140))
        d.rounded_rectangle([60, y, 60 + bw, y + 78], radius=18, fill=(228, 230, 236))
        for i in range(2):
            d.rectangle([88, y + 22 + i * 24, 88 + int(bw * _RNG.uniform(0.4, 0.86)),
                         y + 36 + i * 24], fill=(56, 58, 64))
        y += 116
    d.rectangle([0, h - 100, w, h], fill=(236, 236, 240))           # 底部导航条
    d.rounded_rectangle([w * 0.36, h - 62, w * 0.64, h - 46], radius=8, fill=(30, 30, 34))
    return _img(_noise(_f(img), 0.006))


BUILDERS = {
    "nature": _nature, "portrait": _portrait, "city": _city, "food": _food,
    "animal": _animal, "night": _night, "mono": _mono, "screenshot": _screenshot,
}
_NAMES = {
    "nature": ["山间徒步", "湖边日落", "草原天空"],
    "portrait": ["selfie", "人像写真", "自拍"],
    "city": ["citywalk", "建筑", "街景"],
    "food": ["美食", "food", "下午茶"],
    "animal": ["宠物", "cat", "狗子"],
    "night": ["夜景", "neon", "烟花"],
    "mono": ["黑白", "mono"],
    "screenshot": ["Screenshot", "截图", "屏幕快照"],
}


def generate(per_theme: int = 6) -> list[str]:
    """生成示例照片，返回 [路径]。每次调用会先清空 data/samples/。"""
    if SAMPLE_DIR.exists():  # 只清理自己生成的示例目录
        for old in SAMPLE_DIR.glob("*/*.jpg"):
            try:
                old.unlink()
            except OSError:
                pass
    created: list[str] = []
    stamp = int(time.time()) % 10000
    for theme, builder in BUILDERS.items():
        folder = SAMPLE_DIR / theme
        folder.mkdir(parents=True, exist_ok=True)
        pool = _NAMES[theme]
        for i in range(per_theme):
            img = builder()
            base = f"{pool[i % len(pool)]}_{stamp}_{i}"
            name = "".join(c for c in base if c not in '\\/:*?"<>|') + ".jpg"
            kwargs: dict = {"quality": 90}
            if theme != "screenshot":  # 模拟相机写入的 EXIF
                try:
                    exif = Image.Exif()
                    exif[271] = "DemoCam"
                    exif[272] = f"PX-{100 + i}"
                    exif[306] = time.strftime("%Y:%m:%d %H:%M:%S")
                    kwargs["exif"] = exif.tobytes()
                except Exception:
                    pass
            img.save(folder / name, "JPEG", **kwargs)
            created.append(str(folder / name))
    return created
