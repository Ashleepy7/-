"""视觉语言模型客户端：看图写描述 + 中文句子理解。

只依赖标准库，走 OpenAI 兼容接口（/chat/completions），云端（DeepSeek / 百炼 / 硅基流动等）和本地
（Ollama / LM Studio / llama.cpp server）都认：base_url 指到 127.0.0.1/localhost 就当作本地服务，
自动去掉 Key 要求与 response_format 字段，并给足超时。
配置存在 data/llm.json（含 API Key，属本地私密文件，不要提交/外发）。
没配好或调用失败时，上层会退回原本的离线规则实现，软件依然可用。
"""
from __future__ import annotations

import base64
import copy
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageOps

from .config import DATA_DIR, THEMES

CONF_PATH = DATA_DIR / "llm.json"

DEFAULTS: dict = {
    "enabled": False,
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "api_key": "",
    "text_model": "qwen-plus",
    "vl_model": "qwen-vl-plus",
    "timeout": 180,         # 单次请求秒数：本地模型跑 CPU/显存小模型很慢，要给足
    "workers": 4,           # 批量标注并发数，太高会被限流
    "max_px": 1024,         # 送进模型的图先压到该边长以内，省 token
}

# 指到本机的一律当本地模型服务处理（Ollama 默认 11434、LM Studio 默认 1234）
_LOCAL_NET = re.compile(
    r"^(https?://)?(127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\])(:\d+)?([/?]|$)", re.I)

_THEMAINE = "\n".join(
    f"- {tid}：{m['name']}（{m['desc']}）" for tid, m in THEMES.items())

_LOCK = threading.Lock()
# parse_query 的结果缓存：只缓存「模型解析」这一条路径，本地规则解析不经过这里。
# 命中的判据是下面这五件事都没变：句子、可用画面条件、日期、文本模型、接口地址。
_CACHE: dict = {}
_CACHE_ON = True
_CACHE_MAX = 200          # 条数上限，超出丢最旧的（dict 按插入顺序）
_STAT: dict = {"calls": 0, "fails": 0, "in_tok": 0, "out_tok": 0, "ms": 0, "cache_hits": 0}


def clear_cache() -> None:
    """清空解析缓存。评测脚本重复跑同一套题时必须调，否则量到的是复读不是模型。"""
    with _LOCK:
        _CACHE.clear()


def set_cache(on: bool) -> None:
    """缓存总开关。评测脚本置 False：整轮跑题每次都真发请求，轮与轮之间不复读。"""
    global _CACHE_ON
    with _LOCK:
        _CACHE_ON = bool(on)


class LlmError(Exception):
    """把失败原因分类，方便上层决定"回落"还是"报错"。"""

    def __init__(self, kind: str, msg: str, status: int = 0):
        super().__init__(msg)
        self.kind = kind
        self.msg = msg
        self.status = status        # HTTP 状态码，0 表示不是服务端报错


# ---------------------------------------------------------------- 配置

def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        raw = json.loads(CONF_PATH.read_text(encoding="utf-8"))
        cfg.update({k: v for k, v in raw.items() if k in DEFAULTS})
    except (OSError, json.JSONDecodeError):
        pass
    # 环境变量兜底：方便命令行/CI 使用，不用把 key 写进文件
    if not cfg["api_key"]:
        cfg["api_key"] = os.getenv("DASHSCOPE_API_KEY", "")
    env_base = os.getenv("DASHSCOPE_BASE_URL")
    if env_base and cfg["base_url"] == DEFAULTS["base_url"]:
        cfg["base_url"] = env_base
    cfg["base_url"] = (cfg["base_url"] or "").rstrip("/")
    try:
        cfg["timeout"] = int(float(cfg["timeout"]))
    except (TypeError, ValueError):
        cfg["timeout"] = DEFAULTS["timeout"]
    # 本地服务回答一句可能要几分钟，界面里哪怕填小了也兜到 180s
    if is_local(cfg) and cfg["timeout"] < 60:
        cfg["timeout"] = DEFAULTS["timeout"]
    return cfg


def is_local(cfg: dict | None = None) -> bool:
    """base_url 指向本机就当作本地模型服务：不要 Key、多半不认 response_format、且很慢。"""
    cfg = cfg or load()
    return bool(_LOCAL_NET.match((cfg.get("base_url") or "").strip()))


def save(patch: dict) -> dict:
    cfg = load()
    for k, v in (patch or {}).items():
        if k in DEFAULTS and v is not None:
            cfg[k] = v
    if isinstance(cfg.get("api_key"), str):
        cfg["api_key"] = cfg["api_key"].strip()
    if isinstance(cfg.get("workers"), str):
        cfg["workers"] = int(re.sub(r"\D", "", cfg["workers"]) or 4)
    cfg["workers"] = max(1, min(16, int(cfg["workers"])))
    cfg["timeout"] = max(5, min(900, int(float(cfg["timeout"]) or 180)))
    cfg["max_px"] = max(512, min(2048, int(cfg["max_px"])))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONF_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONF_PATH)
    with _LOCK:
        _CACHE.clear()
    return cfg


def public() -> dict:
    """给前端看的配置：Key 只留尾 4 位。"""
    cfg = load()
    key = cfg.get("api_key") or ""
    local = is_local(cfg)
    return {
        "enabled": bool(cfg["enabled"]),
        "base_url": cfg["base_url"],
        "text_model": cfg["text_model"],
        "vl_model": cfg["vl_model"],
        "timeout": cfg["timeout"],
        "workers": cfg["workers"],
        "max_px": cfg["max_px"],
        "local": local,                       # 本地服务：免费、不外传、不要 Key
        "needs_key": bool(cfg["enabled"] and not key and not local),
        "has_key": bool(key),
        "key_tail": key[-4:] if len(key) >= 8 else ("已填" if key else ""),
        "from_env": bool(os.getenv("DASHSCOPE_API_KEY")),
        "conf_path": str(CONF_PATH),
        "stat": dict(_STAT),
    }


def ready() -> bool:
    """能不能调用模型：开了开关，并且要么有 Key，要么地址是本机服务（不需要 Key）。"""
    cfg = load()
    return bool(cfg["enabled"] and (cfg["api_key"] or is_local(cfg)))


# ---------------------------------------------------------------- 调用

def _post(cfg: dict, payload: dict) -> dict:
    url = cfg["base_url"] + "/chat/completions"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    t0 = time.time()
    try:
        raw = urllib.request.urlopen(req, timeout=cfg["timeout"]).read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:300]
        if exc.code == 401:
            # 「Key 与地址必须同地域」只有百炼这类分地域的服务才成立，别的服务瞎提反而误导
            region = ("；百炼还要 Key 与接口地址是同一个地域" if "dashscope" in cfg["base_url"] else "")
            kind = (f"鉴权失败（云端服务需要 API Key，Key 填错或已过期{region}；"
                    "填本机地址 127.0.0.1 不需要 Key）")
        elif exc.code == 429:
            kind = "额度用完或限流"
        else:
            kind = f"服务返回 {exc.code}"
        raise LlmError("http", f"{kind}：{_brief(detail)}", status=exc.code) from exc
    except urllib.error.URLError as exc:
        raise LlmError("net", f"连不上模型服务（网络或接口地址不对）：{exc.reason}") from exc
    except TimeoutError as exc:
        raise LlmError("timeout",
                       f"模型 {cfg['timeout']} 秒内没回答（本地模型可以把超时调更长）") from exc
    with _LOCK:
        _STAT["calls"] += 1
        _STAT["ms"] += int((time.time() - t0) * 1000)
    try:
        return json.loads(raw.decode("utf-8", "ignore"))
    except json.JSONDecodeError as exc:
        raise LlmError("badjson", "模型返回的内容不是合法 JSON") from exc


def _brief(text: str) -> str:
    """从错误响应里挑人类能看懂的一句话。"""
    try:
        d = json.loads(text)
        return str(d.get("message") or d.get("error") or text)[:160]
    except (json.JSONDecodeError, TypeError):
        return re.sub(r"\s+", " ", text)[:160]


def chat(messages: list[dict], model_key: str = "text_model",
         json_out: bool = True, temperature: float = 0.1) -> str:
    """一次对话调用，返回文本内容。json_out=True 时要求模型只输出 JSON。

    本地服务（llama.cpp / 旧版 Ollama 等）看到 response_format 常常直接回 400，
    所以：本机地址不主动带这个字段，万一服务端还是回 400 就去掉它再试一次。
    """
    cfg = load()
    local = is_local(cfg)
    if not cfg["api_key"] and not local:
        raise LlmError("nokey", "还没有填写 API Key（用本地模型把地址填成 "
                                "http://127.0.0.1:11434/v1 就不需要 Key）")
    payload = {
        "model": cfg[model_key],
        "messages": messages,
        "temperature": temperature,
        "seed": 42,
    }
    if json_out and not local:
        payload["response_format"] = {"type": "json_object"}
    last: Exception | None = None
    for attempt in range(3):                     # 超时/限流重试，400 则卸掉 json 约束重试
        try:
            data = _post(cfg, payload)
            choice = (data.get("choices") or [{}])[0]
            content = ((choice.get("message") or {}).get("content") or "").strip()
            if not content:
                raise LlmError("empty", "模型没有返回内容")
            usage = data.get("usage") or {}
            with _LOCK:
                _STAT["in_tok"] += int(usage.get("prompt_tokens") or 0)
                _STAT["out_tok"] += int(usage.get("completion_tokens") or 0)
            return content
        except LlmError as exc:
            last = exc
            if exc.status == 400 and "response_format" in payload:
                payload.pop("response_format")   # 服务端不认这种说法，换普通文本请求
                continue
            if attempt >= 2 or exc.kind not in ("timeout", "net", "http"):
                break
            time.sleep(1.2)
    with _LOCK:
        _STAT["fails"] += 1
    raise last if last else LlmError("unknown", "调用失败")


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def chat_json(messages: list[dict], model_key: str = "text_model") -> dict:
    text = chat(messages, model_key=model_key)
    text = _FENCE.sub("", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise LlmError("badjson", f"模型没按格式返回：{text[:80]}")
    try:
        out = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LlmError("badjson", f"JSON 解析失败：{exc}") from exc
    return out if isinstance(out, dict) else {}


# ---------------------------------------------------------------- 视觉标注

VL_PROMPT = """你是照片库的自动标注员。只看这张图，输出严格 JSON，不要解释：
{
 "caption": "不超过 20 字的中文画面描述，写清主体+动作+场景",
 "tags": ["3 到 8 个短词，含主体、场景、物品、时间/光线"],
 "theme": "从下列编号里选最贴切的一个",
 "main": "画面主体，2 到 4 字",
 "people": 图中人数（整数，看不清填 0）,
 "text": "若整张图是屏幕截图或文档则填主要文字，否则填空字符串"
}
主题编号：
""" + _THEMAINE + """

规则：
1 截图、软件界面、纯文字页面一律 theme=screenshot；
2 有明显的猫狗等宠物 theme=animal，人占主体 theme=portrait；
3 只描述真实可见的内容，不要臆测地点名人名；
4 只输出 JSON。"""


def shrink(path: str | Path, max_px: int) -> bytes:
    """压成小 JPEG 再送模型，画质够用且省 token。"""
    im = Image.open(path)
    if getattr(im, "n_frames", 1) > 1:
        im.seek(0)
    im = ImageOps.exif_transpose(im)   # 按 EXIF 方向转正：不转的话手机竖拍送进去是侧躺的，
    im = im.convert("RGB")             # 模型会说错“上下/竖构图”（thumbs.py 早就转了）
    im.thumbnail((max_px, max_px), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82, optimize=True)
    return buf.getvalue()


def describe_image(path: str | Path) -> dict:
    cfg = load()
    raw = shrink(path, int(cfg["max_px"]))
    data_url = "data:image/jpeg;base64," + base64.b64encode(raw).decode()
    out = chat_json([{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": data_url}},
        {"type": "text", "text": VL_PROMPT},
    ]}], model_key="vl_model")
    return normalize_label(out)


def normalize_label(out: dict) -> dict:
    """把模型返回的字段收拢成库内统一结构，顺手挡掉越界的主题值。"""
    theme = str(out.get("theme") or "").strip()
    if theme not in THEMES:
        theme = ""
    tags = out.get("tags") if isinstance(out.get("tags"), list) else []
    clean: list[str] = []
    for t in tags:
        t = str(t).strip().strip("#")
        if 1 <= len(t) <= 12 and t not in clean:
            clean.append(t)
    try:
        people = max(0, min(20, int(out.get("people") or 0)))
    except (TypeError, ValueError):
        people = 0
    return {
        "caption": str(out.get("caption") or "").strip()[:80],
        "tags": clean[:10],
        "theme": theme,
        "main": str(out.get("main") or "").strip()[:16],
        "people": people,
        "text": str(out.get("text") or "").strip()[:120],
    }


# ---------------------------------------------------------------- 句子理解

def parse_query(text: str, feats_hint: str = "") -> dict:
    """把用户的一句话解析成结构化意图（后续由本地代码翻译成条件）。

    同一句话、同一份画面条件、同一天、同一模型与地址，结果会缓存，命中就直接返回、
    不再发请求（不再烧 token，也不计入 stat.calls）。缓存 miss 时模型调用失败依旧
    原样抛 LlmError，由上层回落本地规则——这回落底线不能被缓存逻辑挡住。
    """
    cfg = load()
    today = time.localtime()
    key = (text, feats_hint, f"{today.tm_year}-{today.tm_mon:02d}-{today.tm_mday:02d}",
           str(cfg["text_model"]), str(cfg["base_url"]))
    with _LOCK:
        hit = _CACHE.get(key) if _CACHE_ON else None
        if hit is not None:
            _STAT["cache_hits"] += 1
    if hit is not None:
        return copy.deepcopy(hit)        # 返副本：调用方改了字段不能污染缓存

    feats_hint = feats_hint or "（无）"
    out = chat_json([
        {"role": "system", "content": QUERY_SYS.replace("{today}",
         f"{today.tm_year}年{today.tm_mon}月{today.tm_mday}日")},
        {"role": "user", "content": json.dumps({
            "句子": text,
            "主题编号": {tid: m["name"] for tid, m in THEMES.items()},
            "可用画面条件": feats_hint,
        }, ensure_ascii=False)},
    ], model_key="text_model")
    with _LOCK:
        if _CACHE_ON:
            _CACHE[key] = copy.deepcopy(out)
            while len(_CACHE) > _CACHE_MAX:      # FIFO：丢最旧的一条
                _CACHE.pop(next(iter(_CACHE)), None)
    return out


QUERY_SYS = """你是一个本地照片库的检索意图解析器。今天是 {today}。
用户会用一句中文口语描述想找的照片，你要输出严格 JSON：
{
 "time": {"kind": "none"} 或 {"kind":"exact","date":"YYYY-MM-DD"}
   或 {"kind":"range","from":"YYYY-MM-DD","to":"YYYY-MM-DD"}
   或 {"kind":"month","month":"YYYY-MM"} 或 {"kind":"year","year":2025}
   或 {"kind":"season","year":2025,"season":"summer"}（春spring夏summer秋autumn冬winter）
   或 {"kind":"relative","days":3,"side":"before"}（今天往前 3 天；side 用 before/around）
   节日请换算成具体日期范围并给 hint 字段，例如 {"kind":"range","from":"...","to":"...","hint":"春节"}
 "themes": ["最贴切的主题编号，可 0 到 2 个"],
 "must": "用户这句话点名要照片里出现的那一样东西，只写 1 个；可以再用 | 带上它的别的叫法，"
   "最多 3 个说法（例：车子|汽车|出租车、结婚|婚礼、土豆|马铃薯、相机|照相机）。"
   "这些说法必须指同一个东西：能说'照片里有汽车'就能说'照片里有车子'。"
   "不是同一个东西的一律不许写进来：滑雪≠雪山/雪地（那是场景）、地铁≠车厢（那是部件）、"
   "热气球≠天空（那是背景）。用户没有点名具体东西时留空字符串",
 "objects": ["画面里可能出现的东西或场景，2 到 6 字，如 海滩 生日蛋糕 金毛 雪山"
   "（想不出来就留空数组；这里可以放你由用户的话联想到的周边场景）"],
 "exclude_objects": ["明确说不要的东西，如 自拍 猫"],
 "feats": ["从可用画面条件里选，可空"],
 "words": ["只可能出现在文件名/文件夹名里的专有词，如人名、景点名"],
 "people": 期望人数，不确定填 null,
 "sort": "time" 或 "match"（说"最近的"用 time，其余默认 match）,
 "note": "一句话说明你怎么理解的，不超过 20 字"
}
规则：
1 时间信息只放进 time，不要重复进 objects；
2 "去年/上个月/上周/前几天"按今天日期换算成绝对日期；
3 拿不准的字段宁可留空数组，不要编造；
4 must 里只放用户自己点名的那个东西（或其同一种东西的别的叫法），相册里有没有它
  决定了这句话成不成立；你联想出来的场景只能放 objects，不能放 must；
5 只输出 JSON。"""


def stat() -> dict:
    """累计调用次数与 token 用量，给界面展示成本。"""
    with _LOCK:
        return dict(_STAT)


def test_connection() -> dict:
    """前端"测试连接"用：文本、视觉各试一次极小的请求。"""
    cfg = load()
    local = is_local(cfg)
    if not cfg["api_key"] and not local:
        return {"ok": False,
                "error": "还没有填写 API Key；用本地模型（Ollama / LM Studio）把接口地址填成 "
                         "http://127.0.0.1:11434/v1 这类形式即可，不需要 Key"}
    res: dict = {"ok": True, "local": local, "text": {}, "vision": {}}
    try:
        t0 = time.time()
        txt = chat([{"role": "user", "content": "只回复两个字：通了"}],
                   json_out=False)
        res["text"] = {"model": cfg["text_model"], "reply": txt[:20],
                       "ms": int((time.time() - t0) * 1000)}
    except LlmError as exc:
        return {"ok": False, "stage": "text", "error": exc.msg, "kind": exc.kind,
                "status": exc.status, "local": local}
    try:
        # 1x1 的空白图，只为验证视觉模型可用
        buf = io.BytesIO()
        Image.new("RGB", (32, 32), (128, 128, 128)).save(buf, "JPEG")
        url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        t0 = time.time()
        txt = chat([{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": url}},
            {"type": "text", "text": "这是什么颜色？只回答颜色名。"}]}],
            model_key="vl_model", json_out=False)
        res["vision"] = {"model": cfg["vl_model"], "reply": txt[:20],
                         "ms": int((time.time() - t0) * 1000)}
    except LlmError as exc:
        return {"ok": False, "stage": "vision", "error": exc.msg, "kind": exc.kind,
                "status": exc.status, "local": local, "text": res["text"]}
    return res
