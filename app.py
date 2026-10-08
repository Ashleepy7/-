"""灵犀相册 · 本地 Web 相册（Flask）。

启动：python app.py  → 浏览器访问 http://127.0.0.1:8000
只读取照片，绝不修改 / 移动 / 删除任何源文件；分类结果存在 data/album.db。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file

from core import db, library, samples, thumbs
from core import classifier, features, llm, query
from core.config import SAMPLE_DIR, THEMES, UNKNOWN_ID, ensure_dirs, theme_meta

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.config["TEMPLATES_AUTO_RELOAD"] = True   # 改完 templates/ 刷新页面即可，无需重启
app.jinja_env.auto_reload = True
ensure_dirs()
db.init_db()

# 启动先备份一次 album.db。备份失败不得影响启动，只进日志和顶部任务条各说一句。
_BACKUP = db.backup()
if _BACKUP["ok"]:
    print(f"  🗄️ 已备份 → data/backup/{_BACKUP['file']}（{_BACKUP['bytes']} 字节）")
else:
    _msg = f"启动时自动备份失败：{_BACKUP['error']}（软件照常可用，但标注成果只有一份）"
    print(f"  ⚠️ {_msg}")
    library.JOB["message"] = _msg

PAGE_MAX = 800


def _num(v, default: int, lo: int, hi: int) -> int:
    """把界面传来的数字参数收进合法区间：?page=abc 这类胡填归回默认值，不该报 500。"""
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return default
    return min(max(n, lo), hi)


def _tags(v) -> list[str]:
    """tags 在库里是 JSON 文本，接口统一返回数组。"""
    if isinstance(v, list):
        return [str(x) for x in v][:10]
    try:
        out = json.loads(v or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x) for x in out if str(x).strip()][:10]


def _row_out(r: dict) -> dict:
    meta = theme_meta(r.get("theme") or UNKNOWN_ID)
    taken = r.get("taken_at") or r.get("mtime") or 0
    return {
        "id": r["id"],
        "name": r["filename"],
        "folder": r["folder"],
        "theme": r.get("theme") or UNKNOWN_ID,
        "theme_name": meta["name"],
        "theme_emoji": meta["emoji"],
        "theme_color": meta["color"],
        "conf": round((r.get("conf") or 0) * 100),
        # 主题来源：不是 rule 时，conf 只是规则分类器的分数，跟显示的主题无关
        "theme_src": db.theme_src(r),
        "manual": bool(r.get("manual")),
        "status": r.get("status"),
        "width": r.get("width") or 0,
        "height": r.get("height") or 0,
        "ratio": round((r.get("width") or 1) / max(r.get("height") or 1, 1), 3),
        "bytes": r.get("bytes") or 0,
        "taken_at": taken,
        "taken": time.strftime("%Y-%m-%d %H:%M", time.localtime(taken)) if taken else "",
        "month": r.get("month") or "",
        "thumb": f"/api/thumb/{r['id']}",
        "caption": r.get("caption") or "",
        "tags": _tags(r.get("tags")),
        "ai_status": r.get("ai_status") or "none",
    }


# ---------------------------------------------------------------------- 页面

@app.get("/")
def index():
    return render_template("index.html", themes=THEMES)


# ---------------------------------------------------------------------- 状态

@app.get("/api/state")
def api_state():
    st = db.stats()
    counts = db.theme_counts()
    roots = db.list_roots()
    return jsonify({
        "ok": True,
        "stats": {"total": st["total"], "bytes": st["bytes"],
                  "done": st["done"], "errors": st["errors"],
                  "organized": st["total"] - counts.get(UNKNOWN_ID, 0)},
        "job": library.snapshot(),
        "roots": roots,
        "suggested": _suggest_paths(),
        "themes": [{"id": tid, **meta, "count": counts.get(tid, 0)}
                  for tid, meta in THEMES.items()],
        "months": db.group_counts("month")[:24],
        "folders": db.group_counts("folder")[:24],
        "ai_examples": query.EXAMPLES,
        "ai": db.ai_counts(),
        "llm": llm.public(),
        "backup": db.last_backup(),
    })


def _suggest_paths() -> list[dict]:
    """给出用户机上常见的照片目录，方便一键导入。"""
    home = Path.home()
    cands = [home / "Pictures", home / "Photos", home / "Downloads",
             home / "Pictures" / "Camera Roll", home / "DCIM"]
    out = []
    for c in cands:
        try:
            if c.exists() and any(c.glob("*")):
                out.append({"path": str(c), "label": c.name.title()})
        except OSError:
            continue
    return out


@app.get("/api/job")
def api_job():
    return jsonify(library.snapshot())


@app.post("/api/job/cancel")
def api_job_cancel():
    return jsonify(library.cancel())


# ---------------------------------------------------------------------- 导入

@app.post("/api/import")
def api_import():
    body = request.get_json(force=True, silent=True) or {}
    raw = body.get("paths") or ([body.get("path")] if body.get("path") else [])
    paths = [str(p).strip().strip('"') for p in raw if str(p).strip()]
    if not paths:
        return jsonify({"ok": False, "error": "请填写至少一个文件夹地址"}), 400
    bad = [p for p in paths if not os.path.exists(p)]
    if bad:
        return jsonify({"ok": False, "error": f"路径不存在：{bad[0]}"}), 400
    job = library.start(paths, bool(body.get("recursive", True)))
    return jsonify(job)


@app.post("/api/reclassify")
def api_reclassify():
    if not db.stats()["total"]:
        return jsonify({"ok": False, "error": "相册还是空的，先导入照片"}), 400
    return jsonify(library.start([], recursive=True, reclassify=True))


@app.post("/api/demo")
def api_demo():
    """生成示例照片并导入，用来快速体验主题分类。"""
    body = request.get_json(force=True, silent=True) or {}
    per_theme = _num(body.get("per_theme") or 6, 6, 1, 20)
    created = samples.generate(per_theme=per_theme)
    return jsonify({"ok": True, "created": len(created),
                    "job": library.start([str(SAMPLE_DIR)], recursive=True)})


@app.post("/api/library/clear")
def api_clear():
    db.clear_library()
    return jsonify({"ok": True})


@app.delete("/api/root")
def api_root_delete():
    root = (request.args.get("root") or "").strip()
    if root:
        db.remove_root(root)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------- 列表

@app.get("/api/photos")
def api_photos():
    theme = request.args.get("theme", "").strip()
    q = request.args.get("q", "").strip()
    month = request.args.get("month", "").strip()
    folder = request.args.get("folder", "").strip()
    root = request.args.get("root", "").strip()
    page = _num(request.args.get("page") or 1, 1, 1, 10 ** 6)
    size = _num(request.args.get("size") or 200, 200, 1, PAGE_MAX)
    order = request.args.get("order", "taken")
    total = db.count_photos(theme=theme, q=q, month=month, folder=folder, root=root)
    rows = db.query_photos(theme=theme, q=q, month=month, folder=folder, root=root,
                           limit=size, offset=(page - 1) * size, order=order)
    return jsonify({"total": total, "page": page, "size": size,
                    "items": [_row_out(r) for r in rows]})


# ---------------------------------------------------------------------- AI 查找

@app.post("/api/ai/search")
def api_ai_search():
    """自然语言查找：把一句口语拆成检索条件，命中结果全部本地算出。

    engine 默认 auto（配好了就用模型，不然自动走本地规则），可传 rules 强制不联网。
    ignore 是用户点名不要的条件，keep 是点名不许放宽的条件，都是条件 cid 列表。
    """
    body = request.get_json(force=True, silent=True) or {}
    text = str(body.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "先说说你要找的照片：大概什么时候拍的、拍的是什么"}), 400
    limit = _num(body.get("limit") or 120, 120, 1, 500)
    ignore = [str(x) for x in (body.get("ignore") or []) if str(x)]
    keep = [str(x) for x in (body.get("keep") or []) if str(x)]
    engine = str(body.get("engine") or "auto")
    if engine not in ("auto", "qwen", "rules"):
        engine = "auto"
    res = query.search(text, limit=limit, ignore=ignore, engine=engine, keep=keep)
    res["items"] = [_ai_out(r) for r in res["items"]]
    return jsonify(res)


# ---------------------------------------------------------------- AI 模型设置

_NUM_FIELDS = ("timeout", "workers", "max_px")


@app.get("/api/llm/config")
def api_llm_config_get():
    return jsonify({"ok": True, "config": llm.public(), "ai": db.ai_counts(),
                    "stat": llm.stat()})


@app.post("/api/llm/config")
def api_llm_config_save():
    body = request.get_json(force=True, silent=True) or {}
    patch: dict = {}
    for k in ("enabled", "base_url", "text_model", "vl_model"):
        if k in body:
            patch[k] = body[k]
    for k in _NUM_FIELDS:
        if k in body and body[k] not in (None, ""):
            try:
                patch[k] = int(float(body[k]))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": f"{k} 得填数字"}), 400
    key = str(body.get("api_key") or "").strip()
    if key:
        patch["api_key"] = key                  # 留空 = 不改已存的那份
    if body.get("clear_key"):
        patch["api_key"] = ""
    if patch:
        try:
            llm.save(patch)
        except (TypeError, ValueError) as exc:
            return jsonify({"ok": False, "error": f"配置没写对：{exc}"}), 400
    return jsonify({"ok": True, "config": llm.public(), "ai": db.ai_counts(),
                    "stat": llm.stat()})


@app.post("/api/llm/test")
def api_llm_test():
    """拿一张小图各试一次文本/视觉模型，报错说清是哪一步不行。"""
    res = llm.test_connection()
    return jsonify(res), (200 if res.get("ok") else 400)


@app.post("/api/ai/label")
def api_ai_label():
    """让模型逐张看图写描述，跑在和导入识别同一个后台任务里。"""
    if not llm.ready():
        return jsonify({"ok": False,
                        "error": "先在设置里启用 AI 模型（云端填 API Key，本地模型只需把地址指向 127.0.0.1）"}), 400
    if not db.stats()["total"]:
        return jsonify({"ok": False, "error": "相册还是空的，先导入照片"}), 400
    body = request.get_json(force=True, silent=True) or {}
    job = library.start([], annotate=True, annotate_force=bool(body.get("force")))
    if not job.get("ok"):
        msg = "已经有任务在跑，先等它结束或点停止" if job.get("busy") \
              else job.get("message", "启动失败")
        return jsonify({"ok": False, "error": msg}), 409
    pending = db.ai_pending_ids(force=bool(body.get("force")))
    job["pending"] = len(pending)
    return jsonify(job)


@app.post("/api/ai/reset")
def api_ai_reset():
    """清掉语义标注结果，主题还原成规则分类（原图始终不动）。"""
    if library.is_busy():
        return jsonify({"ok": False, "error": "识别任务还在跑，先点顶部「停止」再清除"}), 409
    db.reset_ai()
    return jsonify({"ok": True, "ai": db.ai_counts(),
                    "note": "已清除 AI 描述，主题回到本地规则分类结果"})


def _ai_out(r: dict) -> dict:
    """AI 命中项：在前端通用结构上附「匹配度 + 命中理由」。"""
    out = _row_out(r)
    out["match"] = r.get("match") or 0
    out["hits"] = r.get("hits") or []
    out["miss"] = r.get("miss") or []
    return out


@app.get("/api/photo/<int:pid>")
def api_photo(pid: int):
    r = db.get_photo(pid)
    if not r:
        abort(404)
    out = _row_out(r)
    out.update({
        "path": r["path"],
        "exif": json.loads(r["exif"] or "{}"),
        "feats": json.loads(r["feats"] or "{}"),
        "scores": json.loads(r["scores"] or "{}"),
        "reasons": json.loads(r["reasons"] or "[]"),
        "caption": r.get("caption") or "",
        "tags": _tags(r.get("tags")),
        "people": r.get("people"),
        "ai_status": r.get("ai_status") or "none",
        "ai_err": r.get("ai_err") or "",
        "rule_theme": r.get("rule_theme") or "",
        "themes": [{"id": tid, **m} for tid, m in THEMES.items()],
    })
    return jsonify(out)


# ---------------------------------------------------------------------- 主题操作

@app.post("/api/photo/<int:pid>/theme")
def api_photo_theme(pid: int):
    theme = (request.get_json(force=True, silent=True) or {}).get("theme", "")
    if theme not in THEMES:
        return jsonify({"ok": False, "error": "未知主题"}), 400
    if not db.get_photo(pid):
        abort(404)
    db.set_manual_theme(pid, theme)          # 仅在软件内重新分组，不动源文件
    return jsonify({"ok": True, "theme": theme, "theme_name": THEMES[theme]["name"]})


@app.post("/api/photo/<int:pid>/auto")
def api_photo_auto(pid: int):
    """取消人工设定，立刻对这张照片重新跑一次自动识别。"""
    r = db.get_photo(pid)
    if not r:
        abort(404)
    db.clear_manual(pid)
    try:
        data = features.analyze(r["path"])
    except Exception as exc:
        return jsonify({"ok": False, "error": f"原图无法解析：{exc}"}), 500
    theme, conf, scores, reasons = classifier.classify(data["feats"])
    db.update_analysis(pid, {
        "width": data["width"], "height": data["height"],
        "taken_at": data["taken_at"], "month": data["month"],
        "exif": data["exif"], "feats": data["feats"], "theme": theme, "conf": conf,
        "scores": scores, "reasons": reasons, "status": "done",
    })
    return jsonify({"ok": True, "theme": theme, "theme_name": THEMES[theme]["name"],
                    "conf": round(conf * 100), "reasons": reasons})


@app.delete("/api/photo/<int:pid>")
def api_photo_delete(pid: int):
    """从相册数据库移除记录（磁盘上的原图保持不变）。"""
    db.delete_photo(pid)
    return jsonify({"ok": True, "note": "仅从相册移除，原图仍在原位置"})


# ---------------------------------------------------------------------- 图片

@app.get("/api/thumb/<int:pid>")
def api_thumb(pid: int):
    r = db.get_photo(pid)
    if not r:
        abort(404)
    fp = thumbs.ensure_thumb(r["path"], r.get("mtime", 0), r.get("bytes", 0))
    if not fp:
        abort(404)
    return send_file(fp, mimetype="image/jpeg",
                     max_age=86400 * 7, conditional=True)


@app.get("/api/file/<int:pid>")
def api_file(pid: int):
    r = db.get_photo(pid)
    if not r or not os.path.exists(r["path"]):
        abort(404)
    return send_file(r["path"], conditional=True,
                     max_age=3600)


@app.errorhandler(404)
def not_found(_e):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "not found"}), 404
    return render_template("index.html", themes=THEMES), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"\n  📚 灵犀相册已启动 → http://127.0.0.1:{port}\n  （关闭此窗口即停止服务）\n")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
