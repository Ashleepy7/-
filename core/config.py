"""全局配置：路径、主题字典、扫描参数。"""
from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
THUMB_DIR = DATA_DIR / "thumbs"
DB_PATH = DATA_DIR / "album.db"
BACKUP_DIR = DATA_DIR / "backup"        # album.db 的自动备份目录（见 db.backup）
SAMPLE_DIR = DATA_DIR / "samples"

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".bmp", ".gif",
    ".webp", ".tif", ".tiff", ".jfif",
    # iPhone 默认格式：能不能读取决于有没有装 pillow-heif（见 features.HEIF_OK）
    ".heic", ".heif",
}
SKIP_DIR_NAMES = {
    "windows", "winnt", "$recycle.bin", "node_modules", "__pycache__",
    ".git", "appdata", "program files", "program files (x86)",
    "programdata", "system volume information", "venv", ".venv", "site-packages",
}

THUMB_SIZE = 420          # 缩略图长边像素
ANALYZE_SIZE = 256        # 特征分析用的缩放尺寸
MIN_PIXELS = 6000         # 小于此像素数的图片忽略（图标、缓存图等）
CONF_THRESHOLD = 0.28     # 置信度（领先幅度+得分强度）低于该值时归入「未归类」

# ---- 主题字典（内置特征识别的分类体系）----
THEMES: dict[str, dict[str, str]] = {
    "portrait":   {"name": "人像自拍", "emoji": "🧑", "color": "#f472b6", "desc": "人物、自拍、合影"},
    "nature":     {"name": "自然风光", "emoji": "🏞️", "color": "#34d399", "desc": "山川湖海、天空绿地"},
    "city":       {"name": "城市建筑", "emoji": "🏙️", "color": "#60a5fa", "desc": "楼宇街道、几何线条"},
    "food":       {"name": "美食饮品", "emoji": "🍜", "color": "#fb923c", "desc": "餐点、咖啡、暖色特写"},
    "animal":     {"name": "动物宠物", "emoji": "🐾", "color": "#a78bfa", "desc": "猫狗宠物、野生动物"},
    "night":      {"name": "夜景暗光", "emoji": "🌃", "color": "#818cf8", "desc": "夜晚、灯光、暗调"},
    "mono":       {"name": "黑白影像", "emoji": "🎞️", "color": "#cbd5e1", "desc": "低饱和、黑白灰"},
    "screenshot": {"name": "截图文档", "emoji": "📄", "color": "#22d3ee", "desc": "屏幕截图、文字资料"},
    "other":      {"name": "未归类", "emoji": "🗂️", "color": "#94a3b8", "desc": "特征不明确，待人工整理"},
}
UNKNOWN_ID = "other"


def theme_meta(tid: str) -> dict[str, str]:
    return THEMES.get(tid, THEMES[UNKNOWN_ID])


def ensure_dirs() -> None:
    for d in (DATA_DIR, THUMB_DIR):
        d.mkdir(parents=True, exist_ok=True)
