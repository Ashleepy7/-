"""扫描导入与后台任务管理（单任务队列，全程只读原图）。"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from . import annotator, classifier, db, features, llm
from .config import IMAGE_EXTS, SKIP_DIR_NAMES

JOB: dict = {
    "active": False, "phase": "idle", "total": 0, "done": 0,
    "current": "", "message": "尚未导入照片", "errors": 0,
    "skipped": 0, "started_at": 0.0, "finished_at": 0.0,
    "engine": "",           # rules / qwen
}
_LOCK = threading.Lock()
_CANCEL = threading.Event()
_THREAD: threading.Thread | None = None


def snapshot() -> dict:
    with _LOCK:
        return dict(JOB)


def _set(**kw) -> None:
    with _LOCK:
        JOB.update(kw)


def cancel() -> dict:
    _CANCEL.set()
    _set(message="正在停止…")
    return snapshot()


def is_busy() -> bool:
    with _LOCK:
        return bool(JOB["active"])


# ---------------------------------------------------------------- 扫描

def _iter_images(roots: list[str], recursive: bool) -> list[str]:
    files = []
    for root in roots:
        rp = Path(root)
        if rp.is_file():
            if rp.suffix.lower() in IMAGE_EXTS:
                files.append(str(rp.resolve()))
            continue
        if not rp.exists():
            continue
        if recursive:
            for dirpath, dirnames, filenames in os.walk(rp, topdown=True, onerror=lambda e: None):
                dirnames[:] = [d for d in dirnames
                               if not d.startswith(".") and d.lower() not in SKIP_DIR_NAMES
                               and not os.path.islink(os.path.join(dirpath, d))]
                for name in filenames:
                    if name.startswith("."):
                        continue
                    if os.path.splitext(name)[1].lower() in IMAGE_EXTS:
                        files.append(os.path.join(dirpath, name))
        else:
            try:
                entries = list(os.scandir(rp))
            except OSError:
                continue
            for e in entries:
                if e.is_file(follow_symlinks=False) and os.path.splitext(e.name)[1].lower() in IMAGE_EXTS:
                    files.append(e.path)
    return sorted(set(files))


def _heic_note(roots: list[str], added: int) -> str:
    """扫完一张没登记、目录里却明明有 HEIC：说清原因，不能只丢一句「发现 0 个图片文件」。"""
    if added:
        return ""
    n = 0
    for r in roots:
        rp = Path(r)
        base = rp if rp.is_dir() else rp.parent
        try:
            n += sum(1 for p in base.rglob("*") if p.suffix.lower() in (".heic", ".heif"))
        except OSError:
            continue
    if not n:
        return ""
    if not features.HEIF_OK:
        return (f"这里有 {n} 个 .heic/.heif（iPhone 默认格式），但没装 pillow-heif，读不了："
                "装完重启服务，或先在 Windows 上把它们转成 jpg")
    return f"这里有 {n} 个 .heic/.heif，已经在库里了，本次没有新增"


def _worker(roots: list[str] | None, recursive: bool, reclassify: bool,
            annotate: bool = False, annotate_force: bool = False) -> None:
    try:
        # --- 阶段 1：扫描登记（只 stat，不解码，速度极快）---
        known = db.existing_paths() if roots else set()
        added = 0
        note = ""
        if roots:
            _set(phase="scanning", total=0, done=0, message="正在扫描文件夹…")
            files = _iter_images(roots, recursive)
            _set(total=len(files), message=f"发现 {len(files)} 个图片文件，正在登记…")
            for i, fp in enumerate(files, 1):
                if _CANCEL.is_set():
                    _set(message="已停止导入")
                    return
                if fp in known:
                    _set(done=i)
                    continue
                try:
                    st = os.stat(fp)
                except OSError:
                    _set(done=i, errors=JOB["errors"] + 1)
                    continue
                if db.insert_scan_record({
                    "path": fp, "filename": os.path.basename(fp),
                    "folder": os.path.dirname(fp), "ext": os.path.splitext(fp)[1].lower(),
                    "bytes": st.st_size, "mtime": st.st_mtime,
                }):
                    added += 1
                _set(done=i)
            for r in roots:
                db.add_root(r, recursive)
            note = _heic_note(roots, added)

        # --- 阶段 2：特征分析 + 主题分类 ---
        ids = db.pending_ids(force=reclassify)
        _set(phase="classifying", total=len(ids), done=0,
             message=(f"正在识别 {len(ids)} 张照片的主题…" if ids
                      else "没有需要识别的新照片" + (("；" + note) if note else "")))
        for i, pid in enumerate(ids, 1):
            if _CANCEL.is_set():
                _set(message="已停止识别")
                break
            row = db.get_photo(pid)
            if not row:
                _set(done=i)
                continue
            _set(done=i, current=Path(row["path"]).name)
            try:
                data = features.analyze(row["path"])
                theme, conf, scores, reasons = classifier.classify(data["feats"])
                rule_theme = theme
                mono_style = 1 if classifier.is_mono(data["feats"]) else 0
                if row.get("manual") and row.get("theme"):
                    theme = row["theme"]      # 保留人工设定的主题
                elif row.get("ai_theme"):
                    theme = row["ai_theme"]   # 模型看过的图，主题以模型为准
                db.update_analysis(pid, {
                    "width": data["width"], "height": data["height"],
                    "taken_at": data["taken_at"], "month": data["month"],
                    "exif": data["exif"], "feats": data["feats"],
                    "theme": theme, "conf": conf, "scores": scores,
                    "reasons": reasons, "status": "done",
                    "rule_theme": rule_theme, "mono_style": mono_style,
                })
            except Exception as exc:  # 单张损坏/格式异常不中断整体任务
                _set(errors=JOB["errors"] + 1)
                try:
                    db.update_analysis(pid, {
                        "width": 0, "height": 0, "taken_at": row.get("mtime", 0),
                        "month": time.strftime("%Y-%m", time.localtime(row.get("mtime", 0))),
                        "exif": {}, "feats": {}, "theme": "other", "conf": 0.0,
                        "scores": {}, "reasons": [f"无法解析：{exc}"], "status": "error",
                    })
                except Exception:
                    pass
        st = db.stats()
        ai_msg = ""
        if annotate:
            if llm.ready():
                ids = db.ai_pending_ids(force=annotate_force)
                vl = llm.load()["vl_model"]          # 进度条上直接报出在用哪个模型
                if ids:
                    _set(phase="ai", total=len(ids), done=0, engine="qwen",
                         message=f"{vl} 正在看图说话：0/{len(ids)} 张…")
                    res = annotator.run(
                        ids, cancel=_CANCEL,
                        progress=lambda d, t, name: _set(
                            done=d, current=name,
                            message=f"{vl} 正在看图说话：{d}/{t} 张…"))
                    ai_msg = (f"；{vl} 给 {res['done']} 张写了描述"
                              + (f"，{res['errors']} 张失败" if res["errors"] else ""))
                    if res["abort"]:
                        ai_msg += f"，中断原因：{res['abort']}"
                else:
                    ai_msg = f"；{vl} 已标注过全部照片"
            else:
                ai_msg = "；未启用 AI 模型，已用本地规则分类"
        _set(phase="idle", done=JOB["total"],
             message=(f"完成：新增 {added} 张，库内共 {st['total']} 张"
                      + (f"，{st['errors']} 张无法解析" if st["errors"] else "")
                      + (("；" + note) if note else "")
                      + ai_msg))
    except Exception as exc:  # pragma: no cover
        _set(phase="idle", message=f"任务异常：{exc}")
    finally:
        # 任务收尾再拍一份快照：识别/标注结果刚写完库，这一刻最值钱也最容易丢。
        # 备份失败不能把已完成的任务弄挂，只在消息后面追一句。
        bk = db.backup()
        _set(active=False, current="", finished_at=time.time(),
             message=snapshot()["message"] + (
                 f"；已备份 {bk['file']}" if bk["ok"]
                 else f"；自动备份失败：{bk['error']}"))


def backfill_mono_style(thr: float = 0.5) -> dict:
    """全库免费回填黑白风格轴：只读已存的 feats、用本地规则重算 mono_style，
    不解码图片也不调模型（“先不重标注”约束下唯一能把风格轴铺满的手段）。"""
    items = db.all_photo_feats()
    on = 0
    for pid, feats in items:
        v = 1 if classifier.is_mono(feats, thr) else 0
        db.set_mono_style(pid, v)
        on += v
    return {"ok": True, "scanned": len(items), "mono": on}


def start(paths: list[str] | None, recursive: bool = True, reclassify: bool = False,
          annotate: bool | None = None, annotate_force: bool = False) -> dict:
    """启动后台任务。paths 为空表示只处理库中待识别的照片。

    annotate=None 自动按“是否配好 AI 模型”决定要不要跑看图说话阶段。
    """
    global _THREAD
    if annotate is None:
        annotate = llm.ready()
    with _LOCK:
        if JOB["active"]:
            return {"ok": False, "busy": True, **JOB}
        _CANCEL.clear()
        JOB.update({"active": True, "phase": "starting", "total": 0, "done": 0,
                    "errors": 0, "started_at": time.time(), "finished_at": 0.0,
                    "message": "任务已启动…",
                    "engine": "qwen" if (annotate and llm.ready()) else "rules"})
    cleaned = [str(Path(p).expanduser().resolve()) for p in (paths or []) if str(p).strip()]
    bad = [p for p in cleaned if not os.path.exists(p)]
    if cleaned and len(bad) == len(cleaned):
        _set(active=False, phase="idle", message="所填路径都不存在，请检查文件夹地址")
        return {"ok": False, "error": "路径不存在", **snapshot()}
    _THREAD = threading.Thread(
        target=_worker,
        args=(cleaned, recursive, reclassify, annotate, annotate_force), daemon=True)
    _THREAD.start()
    return {"ok": True, **snapshot()}
