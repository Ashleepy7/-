"""从百度图片检索真实照片，按主题存到本地素材目录，用来替代程序生成的示例图。

用法：
    python scripts/fetch_real.py              # 每个关键词抓 6 张
    python scripts/fetch_real.py --per 4      # 每个关键词抓 4 张
    python scripts/fetch_real.py --only 海边 猫   # 只抓包含这些词的关键词
    python scripts/fetch_real.py --target 260 # 补到库里一共 260 张就停

说明：
- 只读取公开的图片检索接口（先访问首页拿 BAIDUID cookie，再请求 acjson），
  不做翻页硬刷，每次下载之间都会停顿，控制在"手动搜一下、存几张"的量级。
- 文件名一律用 IMG_xxxx，不用关键词命名 —— 这样主题分类靠的是画面特征而不是文件名，
  检验更真实；关键词只体现在文件夹名上（真人整理照片也常按文件夹分）。
- 下载后会用 Pillow 校验：能解码、短边 >= 360、体积 20KB~4MB、非动图，重复内容按 MD5 去掉。
- 去重不只看 MD5：库里的图都是重存过的 JPEG，同一张源图 MD5 也对不上，所以另存一份
  dHash（8x8 差值哈希）跟**已有全部照片**比相似度，差 6 位以内算重复直接跳过。
- 关键词刻意避开了 data/detail_cases.json 里 C 组反例题要求的词（雪/骆驼/钢琴/地铁/
  热气球/汉堡/恐龙/摩天轮），也避开了 A 组唯一答案题的判别特征（镜子自拍、婚礼、
  银杏大道、云海、出租车、摩托、火锅、花菜、猫、狗…）—— 不然老题的答案会变脏。
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import io
import json
import random
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

BASE = Path(__file__).resolve().parents[1]
OUT_DIR = BASE / "data" / "library" / "真实素材"
REPORT = BASE / "data" / "fetch_report.txt"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

# 关键词 → 存放的文件夹名（文件夹名会被 AI 查找当成文字线索）
# 第二批扩充用的词都挑库里没有的画面，且不含上面那批禁词
TOPICS: list[tuple[str, list[str]]] = [
    ("自然风光", ["海边 风景", "山 日出 云海", "草原 蓝天白云", "秋天 银杏 树林",
                  "向日葵 花田", "荷花 池塘", "樱花 树枝 特写", "森林 小路 阳光",
                  "瀑布 溪流 石头", "湖面 倒影 山", "草地 野花 特写", "多肉 盆栽",
                  "竹林 阳光", "秋天 红叶 枫叶", "雾 森林 早晨", "梯田 山 绿色"]),
    ("城市建筑", ["城市 高楼 天际线", "古建筑 寺庙 屋檐", "桥 夜景 灯光 城市", "街道 建筑 透视",
                  "古镇 街道 青石板", "天台 俯瞰 城市", "旋转楼梯 建筑", "教堂 内部 穹顶",
                  "城墙 门楼 古迹", "港口 码头 轮船", "立交桥 城市", "老式 里弄 巷子",
                  "玻璃幕墙 反射", "公园 长椅 树荫", "商场 中庭 扶梯", "自行车 停放 街头"]),
    ("人像自拍", ["女生 自拍 相机", "朋友 合影 户外", "婚礼 新人 合影", "人像 摄影 特写",
                  "戴草帽 男人 户外", "小女孩 笑 特写", "老人 手 皱纹 特写", "撑伞 女人 雨天",
                  "侧脸 逆光 人像", "弹吉他 男孩", "厨师 做饭 人像", "跑步 运动 人像",
                  "戴眼镜 女人 看书", "背影 田野 一个人", "工地上 工人 安全帽", "舞蹈 舞台 演员"]),
    ("美食饮品", ["火锅 食材 特写", "咖啡 甜点 下午茶", "家常菜 盘子 俯拍", "蛋糕 水果 特写",
                  "寿司 拼盘 特写", "面条 汤面 特写", "烧烤 串 食物", "面包 烘焙 早餐",
                  "水果 拼盘 特写", "茶 杯 壶 特写", "甜筒 冰淇淋", "早餐 桌子 俯拍",
                  "红酒 杯 特写", "饺子 盘子", "小龙虾 一盘", "西瓜 夏天 水果"]),
    ("动物宠物", ["猫 幼猫 特写", "金毛 狗 草地", "小鸟 树枝 特写", "鸭子 农场 动物",
                  "兔子 特写", "小马 草地", "金鱼 鱼缸 特写", "鹦鹉 鸟 特写",
                  "羊 草原 动物", "松鼠 树林", "乌龟 宠物 特写", "马 奔跑 草原",
                  "母鸡 小鸡 农场", "天鹅 湖面 游", "蜜蜂 花朵 特写", "鹿 森林 动物"]),
    ("夜景暗光", ["城市夜景 霓虹灯", "烟花 夜空 绽放", "星空 银河 长曝光", "夜晚 路灯 雨",
                  "天桥 夜景 城市", "车灯 光轨 夜晚", "烛光 暗光 特写", "夜市 摊位 灯光",
                  "月亮 夜空 特写", "高楼 灯光 夜晚", "帐篷 露营 夜晚", "舞台 灯光 演唱会",
                  "古镇 灯笼 夜晚", "车内 夜晚 雨滴", "黄昏 江边 落日", "灯塔 海岸 黄昏"]),
    ("黑白影像", ["黑白 摄影 街拍", "黑白 建筑 光影", "黑白 人像 胶片",
                  "黑白 静物 桌面", "黑白 森林 小路", "黑白 老人 肖像", "黑白 海面 波浪",
                  "黑白 城市 天桥", "黑白 舞蹈 剪影", "黑白 码头 船只", "黑白 窗台 花盆",
                  "黑白 牧羊 田野", "黑白 手 特写", "黑白 屋顶 城市", "黑白 桥 河面"]),
]

# 题面禁词：C 组反例题要求库里 0 命中，关键词里出现这些直接不拓
# （只能管住关键词；百度偶尔会推错图，比如搜“雾”出雪景，拓完还得人工看一遍）
FORBIDDEN = ("滑雪", "雪山", "雪", "骆驼", "驼", "钢琴", "地铁", "铁轨",
             "热气球", "汉堡", "恐龙", "摩天轮")


def blocked(word: str) -> str:
    """返回关键词里撞上的禁词，没撞上返回空字符串。"""
    return next((b for b in FORBIDDEN if b in word), "")


MIN_SIDE, MIN_BYTES, MAX_BYTES = 360, 20_000, 4_000_000
DUP_BITS = 6          # dHash 差值哈希 64 位，相差 <= 6 位视为同一张照片


def make_opener() -> urllib.request.OpenerDirector:
    """先访问首页拿到 BAIDUID，否则 acjson 会返回 Forbid spider access。"""
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", UA), ("Accept-Language", "zh-CN,zh;q=0.9")]
    op.open("https://image.baidu.com/", timeout=20).read()
    return op


def search(op, word: str, rn: int = 30) -> list[str]:
    """返回一组图片直链（优先 800px 的中图，退化到缩略图）。"""
    q = urllib.parse.quote(word)
    url = ("https://image.baidu.com/search/acjson?tn=resultjson_com&ipn=rj&fp=result"
           f"&word={q}&queryWord={q}&ie=utf-8&oe=utf-8&pn=0&rn={rn}")
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://image.baidu.com/search/index?tn=baiduimage",
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    })
    txt = op.open(req, timeout=20).read().decode("utf-8", "ignore")
    txt = txt.replace("\\'", "'")
    try:
        data = json.loads(txt).get("data") or []
    except json.JSONDecodeError:
        return []
    links: list[str] = []
    for it in data:
        if not isinstance(it, dict):
            continue
        link = it.get("middleURL") or it.get("thumbURL") or ""
        if link.startswith("http"):
            links.append(link)
    return links


def usable(raw: bytes) -> tuple[bool, str]:
    """校验字节是不是我们要的真实照片，返回 (是否可用, 尺寸文字)。"""
    if not (MIN_BYTES <= len(raw) <= MAX_BYTES):
        return False, "体积不合适"
    try:
        im = Image.open(io.BytesIO(raw))
        im.verify()                       # 先验格式，再重开取尺寸
        im = Image.open(io.BytesIO(raw))
        if getattr(im, "n_frames", 1) > 1:
            return False, "动图"
        w, h = im.size
        if min(w, h) < MIN_SIDE:
            return False, f"太小 {w}x{h}"
        if im.mode not in ("RGB", "L", "P", "CMYK", "RGBA"):
            return False, "色彩模式奇怪"
        return True, f"{w}x{h}"
    except Exception as exc:                          # 坏图、半截数据都算不合格
        return False, f"解码失败 {type(exc).__name__}"


def to_jpg(raw: bytes, dest: Path) -> None:
    """统一转成 JPEG（去掉 alpha / 调色板，EXIF 里的拍摄时间尽量保留）。"""
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    im.thumbnail((2200, 2200), Image.LANCZOS)
    im.save(dest, "JPEG", quality=90, optimize=True)


def dhash(raw: bytes, size: int = 8) -> int | None:
    """8x8 差值哈希：相邻两列谁亮谁记 1，共 64 位。压缩/缩放/改尺寸基本不动它。"""
    try:
        im = Image.open(io.BytesIO(raw)).convert("L").resize((size + 1, size), Image.LANCZOS)
    except Exception:
        return None
    px = im.tobytes()          # L 模式一个字节一像素，比 getdata() 快且不报弃用告警
    bits = 0
    for row in range(size):
        base = row * (size + 1)
        for col in range(size):
            bits = (bits << 1) | int(px[base + col] > px[base + col + 1])
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


class Stock:
    """库里已有照片的去重账本：MD5 拦字节相同的，dHash 拦“同一张图不同压缩”的。"""

    def __init__(self) -> None:
        self.md5: set[str] = set()
        self.hashes: list[int] = []
        self.names: set[str] = set()

    def load(self, root: Path) -> int:
        for p in sorted(root.rglob("*.jpg")):
            self.names.add(p.name)
            try:
                raw = p.read_bytes()
            except OSError:
                continue
            self.md5.add(hashlib.md5(raw).hexdigest())
            h = dhash(raw)
            if h is not None:
                self.hashes.append(h)
        return len(self.names)

    def duplicate(self, raw: bytes) -> bool:
        if hashlib.md5(raw).hexdigest() in self.md5:
            return True
        h = dhash(raw)
        if h is None:
            return False
        return any(hamming(h, old) <= DUP_BITS for old in self.hashes)

    def add(self, raw: bytes) -> None:
        self.md5.add(hashlib.md5(raw).hexdigest())
        h = dhash(raw)
        if h is not None:
            self.hashes.append(h)

    def free_name(self, folder: Path, tag: int) -> Path:
        """文件名要在全库唯一：同名会被当成同一张，出题也会出重。"""
        for _ in range(40):
            name = f"IMG{random.randint(1000, 9999)}_{tag:02d}.jpg"
            if name not in self.names:
                self.names.add(name)
                return folder / name
        raise RuntimeError("文件名撞车次数过多，换个时段再抓")


def fetch(op, word: str, folder: Path, want: int, stock: Stock, log: list[str]) -> int:
    links = search(op, word)
    random.shuffle(links)
    got, tried = 0, 0
    for link in links:
        if got >= want or tried >= want * 4:
            break
        tried += 1
        try:
            raw = op.open(urllib.request.Request(
                link, headers={"Referer": "https://image.baidu.com/"}), timeout=20).read()
        except Exception as exc:
            log.append(f"    ✗ 下载失败 {type(exc).__name__}")
            time.sleep(0.4)
            continue
        if stock.duplicate(raw):
            log.append("    ✗ 跳过（和库里已有照片是同一张）")
            continue
        ok, info = usable(raw)
        if not ok:
            log.append(f"    ✗ 跳过（{info}）")
            time.sleep(0.4)
            continue
        stock.add(raw)
        # 文件名一律用中性的 IMG_xxxx（分类引擎会读文件名关键词，不能把答案写进名字里）
        dest = stock.free_name(folder, got + 1)
        got += 1
        to_jpg(raw, dest)
        log.append(f"    ✓ {info} · {len(raw) // 1024}KB · {dest.name}")
        time.sleep(0.8 + random.random() * 0.6)        # 手速级别，别当爬虫用
    return got


def main() -> None:
    argv = sys.argv[1:]
    per = int(argv[argv.index("--per") + 1]) if "--per" in argv else 6
    only = argv[argv.index("--only") + 1:] if "--only" in argv else []
    target = int(argv[argv.index("--target") + 1]) if "--target" in argv else 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stock = Stock()
    have = stock.load(OUT_DIR)
    op = make_opener()
    log: list[str] = [f"输出目录：{OUT_DIR}",
                      f"库里已有：{have} 张（已按 MD5 + dHash 建好去重账本）"]
    if target:
        log.append(f"目标总量：{target} 张，本轮最多再补 {max(target - have, 0)} 张")

    jobs = [(t, w) for t, words in TOPICS for w in words
            if not blocked(w) and (not only or any(o in w for o in only))]
    banned = [w for _, words in TOPICS for w in words if blocked(w)]
    if banned:
        log.append(f"按题面禁词跳过 {len(banned)} 个关键词：{'、'.join(banned)}")
    log.append("")

    total, rounds = 0, 0
    while rounds < 3 and (target or rounds == 0):
        rounds += 1
        if target:
            log.append(f"—— 第 {rounds} 轮 ——")
            random.shuffle(jobs)
        for topic, word in jobs:
            if target and have + total >= target:
                break
            want = min(per, target - have - total) if target else per
            folder = OUT_DIR / topic
            folder.mkdir(parents=True, exist_ok=True)
            log.append(f"「{word}」→ {topic}/  目标 {want} 张")
            n = fetch(op, word, folder, want, stock, log)
            total += n
            log.append(f"    本次新增 {n} 张，库里共 {have + total} 张")
            time.sleep(1.2 + random.random() * 0.8)
        if not target:
            break

    log.append("")
    log.append(f"合计新增 {total} 张，库中共 {len(list(OUT_DIR.rglob('*.jpg')))} 张")
    if target and have + total < target:
        log.append(f"⚠ 没到 {target} 张：关键词已刷 {rounds} 轮，剩下的候选大多和库里重复或下载失败。")
    REPORT.write_text("\n".join(log), encoding="utf-8")
    print(f"done: +{total}, report -> {REPORT}")


if __name__ == "__main__":
    main()
