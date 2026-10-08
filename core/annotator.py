"""批量语义标注：并发调用视觉模型，给照片写"画面描述 + 标签 + 主题"。

结果只进 data/album.db，绝不改原图。随时可取消；单张失败不影响其它张。
只有“再试也没用”的故障（没 Key / 连不上 / 超时 / 鉴权失败 / 限流）连续撞三次才整体停下，
而且停下前一定把具体错因带回去，不能拿一个笼统的“失败了”糊弄界面。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import db, llm

# 熔断只看这几类：整台机器/整个服务都不行，继续跑只会白烧钱
_FATAL_KIND = {"nokey", "net", "timeout"}
_FATAL_STATUS = {401, 402, 403, 429}


def _fatal(res: dict) -> bool:
    """400 / 404 / 5xx 不算致命：本地服务常常是某张图尺寸超限、某个字段不认，换一张就成了。"""
    kind = res.get("kind") or ""
    status = int(res.get("status") or 0)
    return kind in _FATAL_KIND or (kind == "http" and status in _FATAL_STATUS)


def annotate_one(pid: int, path: str) -> dict:
    try:
        label = llm.describe_image(path)
        if not label["caption"] and not label["tags"]:
            raise llm.LlmError("empty", "模型没给出内容")
        db.update_ai(pid, {**label, "status": "done", "error": ""})
        return {"ok": True, "pid": pid}
    except llm.LlmError as exc:
        db.update_ai(pid, {"status": "error", "error": exc.msg, "caption": "", "tags": []})
        return {"ok": False, "pid": pid, "kind": exc.kind, "status": exc.status,
                "msg": exc.msg, "file": Path(path).name}
    except Exception as exc:  # 文件被删、解码失败等
        msg = f"{type(exc).__name__}: {exc}"
        db.update_ai(pid, {"status": "error", "error": msg[:120], "caption": "", "tags": []})
        return {"ok": False, "pid": pid, "kind": "file", "status": 0,
                "msg": msg, "file": Path(path).name}


def run(ids: list[int], *, cancel: threading.Event | None = None,
        progress=None, workers: int | None = None) -> dict:
    """逐张标注。progress(done, total, name) 用于回填任务进度。"""
    pairs = db.path_pairs(ids)
    total = len(pairs)
    cfg = llm.load()
    workers = workers or int(cfg["workers"])
    done = good = err = 0          # done=跑过的张数（进度用），good=真正拿到描述的张数
    consecutive = 0
    notes: list[str] = []          # 前几张失败的具体原因，中止时要能给人看
    abort = ""
    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, 8)))
    try:
        futs = {pool.submit(annotate_one, pid, p): p for pid, p in pairs}
        for fut in as_completed(futs):
            if cancel is not None and cancel.is_set():
                for f in futs:
                    f.cancel()
                abort = "已停止标注"
                break
            res = fut.result()
            done += 1
            name = Path(futs[fut]).name
            if not res["ok"]:
                err += 1
                detail = f"{res.get('file') or name}：{res.get('msg') or '未知错误'}"
                if len(notes) < 6 and detail not in notes:
                    notes.append(detail)
                # 单张问题（400 / 解码失败）不往熔断上记，免得一张坑把全库停掉
                consecutive = consecutive + 1 if _fatal(res) else 0
                if consecutive >= 3:
                    abort = ((res.get("msg") or "模型调用失败")
                             + "（失败详情：" + "；".join(notes[:3]) + "）")
                    for f in futs:
                        f.cancel()
                    break
            else:
                good += 1
                consecutive = 0
            if progress:
                progress(done, total, name)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    st = db.ai_counts()
    return {"ok": not abort, "done": good, "tried": done, "errors": err, "abort": abort,
            "details": notes, "remain": st["none"], "stat": llm.stat()}
