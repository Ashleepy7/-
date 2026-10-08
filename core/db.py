"""SQLite 元数据库：照片记录、主题结果、导入根目录。只读原图，绝不修改源文件。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing
from typing import Any, Iterable

from .config import BACKUP_DIR, DB_PATH, ensure_dirs

_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    path      TEXT UNIQUE NOT NULL,
    filename  TEXT NOT NULL,
    folder    TEXT NOT NULL,
    ext       TEXT,
    width     INTEGER DEFAULT 0,
    height    INTEGER DEFAULT 0,
    bytes     INTEGER DEFAULT 0,
    mtime     REAL DEFAULT 0,
    taken_at  REAL DEFAULT 0,
    month     TEXT DEFAULT '',
    added_at  REAL DEFAULT 0,
    exif      TEXT DEFAULT '{}',
    feats     TEXT DEFAULT '{}',
    theme     TEXT DEFAULT '',
    conf      REAL DEFAULT 0,
    scores    TEXT DEFAULT '{}',
    reasons   TEXT DEFAULT '[]',
    manual    INTEGER DEFAULT 0,
    status    TEXT DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_photos_theme  ON photos(theme);
CREATE INDEX IF NOT EXISTS idx_photos_month  ON photos(month);
CREATE INDEX IF NOT EXISTS idx_photos_folder ON photos(folder);
CREATE INDEX IF NOT EXISTS idx_photos_status ON photos(status);
CREATE TABLE IF NOT EXISTS roots (
    root     TEXT PRIMARY KEY,
    recursive INTEGER DEFAULT 1,
    imported_at REAL
);
"""

# AI 视觉标注结果：单独一组列，老库启动时自动补上（ALTER TABLE ADD COLUMN 很快）
_AI_COLS: list[tuple[str, str]] = [
    ("caption", "TEXT DEFAULT ''"),      # 一句话画面描述
    ("tags", "TEXT DEFAULT '[]'"),       # 语义标签
    ("ai_theme", "TEXT DEFAULT ''"),     # 模型判的主题
    ("ai_status", "TEXT DEFAULT 'none'"),  # none / done / error
    ("ai_err", "TEXT DEFAULT ''"),
    ("people", "INTEGER DEFAULT -1"),   # 模型看到的人数，-1 表示未标注
    ("rule_theme", "TEXT DEFAULT ''"),   # 用 AI 结果覆盖前，备份原规则分类
]


def _conn() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def theme_src(row: dict) -> str:
    """这张照片当前显示的 theme 是谁定的：manual（人工）/ ai（模型）/ rule（本地规则）。

    只有返回 rule 时 conf 才是这个主题的自信度：模型标注会把 theme 覆盖成 ai_theme，
    而 conf 一直是规则分类器留下的分数（见 library.reanalyze）。“恢复自动识别”
    会把 theme 写回 rule_theme，那时该主题确实是规则算出来的，百分比重新有意义。
    界面据此决定该不该显示置信度百分比。
    """
    if row.get("manual"):
        return "manual"
    theme = (row.get("theme") or "").strip()
    ai = (row.get("ai_theme") or "").strip()
    rule = (row.get("rule_theme") or "").strip()
    return "ai" if (ai and theme == ai and theme != rule) else "rule"


def init_db() -> None:
    with _LOCK, closing(_conn()) as conn:
        conn.executescript(_SCHEMA)
        have = {r["name"] for r in conn.execute("PRAGMA table_info(photos)").fetchall()}
        for name, decl in _AI_COLS:
            if name not in have:
                conn.execute(f"ALTER TABLE photos ADD COLUMN {name} {decl}")
        conn.commit()


# ---------------- 备份 ----------------

BACKUP_KEEP = 7      # 只保留最近 7 份，多余的删
_LAST_BACKUP: dict = {"ok": False, "at": 0.0, "file": "", "bytes": 0,
                      "error": "还没备份过"}


def _prune_backups() -> int:
    """清掉超出 7 份的旧备份。文件名是定宽时间戳，按名字排就是按时间排。"""
    files = sorted(BACKUP_DIR.glob("album-*.db"))    # 旧的在前、新的在后
    stale = files[:-BACKUP_KEEP]                     # 只丢最旧的，刚拍的那份必须在
    for p in stale:
        try:
            p.unlink()
        except OSError:
            pass          # 正被别的程序占用就留着，下次再清
    return len(stale)


def backup() -> dict:
    """给 album.db 拍一份快照到 data/backup/album-YYYYMMDD-HHMMSS.db。

    走 sqlite 连接级的 backup API，不用 shutil.copy —— 后者可能拷到写到一半的库，
    那种备份打不开，等于没备份。任何失败都只返回 dict 绝不抛：备份不能拖垮启动。
    只备份、不做自动还原：手动拿这份 db 文件盖回 album.db 就是还原。
    """
    dst = BACKUP_DIR / f"album-{time.strftime('%Y%m%d-%H%M%S')}.db"
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        with _LOCK, closing(_conn()) as src, closing(sqlite3.connect(dst)) as out:
            src.backup(out)           # 事务级拷贝，不会拷到写一半的库
        dropped = _prune_backups()
        _LAST_BACKUP.update({"ok": True, "at": time.time(), "file": dst.name,
                             "bytes": dst.stat().st_size, "dropped": dropped, "error": ""})
    except (sqlite3.Error, OSError) as exc:
        try:
            dst.unlink(missing_ok=True)   # 清掉写坏的半成品，免得占着 7 份名额
        except OSError:
            pass
        _LAST_BACKUP.update({"ok": False, "at": time.time(), "file": dst.name,
                             "bytes": 0, "dropped": 0, "error": f"{type(exc).__name__}: {exc}"})
    return dict(_LAST_BACKUP)


def last_backup() -> dict:
    """最近一次备份的结果，给接口/日志展示。"""
    return dict(_LAST_BACKUP)


def _rows(sql: str, args: Iterable[Any] = ()) -> list[dict]:
    with _LOCK, closing(_conn()) as conn:
        return [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]


def _run(sql: str, args: Iterable[Any] = ()) -> None:
    with _LOCK, closing(_conn()) as conn:
        conn.execute(sql, tuple(args))
        conn.commit()


# ---------------- 写入 ----------------

def insert_scan_record(rec: dict) -> int | None:
    """扫描阶段写入基础信息（不解码，速度快）。已存在则返回 None。"""
    try:
        with _LOCK, closing(_conn()) as conn:
            cur = conn.execute(
                """INSERT INTO photos(path, filename, folder, ext, bytes, mtime, added_at, status)
                   VALUES(?,?,?,?,?,?,?, 'pending')""",
                (rec["path"], rec["filename"], rec["folder"], rec["ext"],
                 rec.get("bytes", 0), rec.get("mtime", 0), time.time()),
            )
            conn.commit()
            return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None


def update_analysis(pid: int, data: dict) -> None:
    """写回特征分析与规则分类结果。

    有语义标注结果时，theme 让位给模型（原规则结果存进 rule_theme 备查），
    所以上层传进来的 theme 已经是“应该展示哪个”，这里只多存一份备份。
    """
    extra, args = "", []
    if data.get("rule_theme") is not None:
        extra, args = ", rule_theme=?", [data["rule_theme"]]
    with _LOCK, closing(_conn()) as conn:
        conn.execute(
            f"""UPDATE photos SET width=?, height=?, taken_at=?, month=?, exif=?, feats=?,
                               theme=?, conf=?, scores=?, reasons=?, status=?{extra} WHERE id=?""",
            (data["width"], data["height"], data.get("taken_at", 0), data.get("month", ""),
             json.dumps(data.get("exif", {}), ensure_ascii=False),
             json.dumps(data.get("feats", {}), ensure_ascii=False, default=float),
             data["theme"], data["conf"],
             json.dumps(data.get("scores", {}), ensure_ascii=False, default=float),
             json.dumps(data.get("reasons", []), ensure_ascii=False),
             data.get("status", "done"), *args, pid),
        )
        conn.commit()


def update_ai(pid: int, data: dict) -> None:
    """写入 AI 视觉标注结果。

    模型给出的主题会直接接管 theme（人工设定过的不动），
    原规则分类先备份进 rule_theme，关掉 AI 时能还原。
    """
    with _LOCK, closing(_conn()) as conn:
        cur = conn.execute(
            "SELECT theme, manual, rule_theme FROM photos WHERE id=?", (pid,)).fetchone()
        if cur is None:
            return
        ai_theme = data.get("theme") or ""
        # 人工设定优先于模型；模型没给主题就保持现状
        new_theme = ai_theme if (ai_theme and not cur["manual"]) else cur["theme"]
        backup = cur["rule_theme"] or cur["theme"]    # 多次标注也只留最早那份规则结果
        conn.execute(
            """UPDATE photos SET caption=?, tags=?, ai_theme=?, ai_status=?, ai_err=?,
                              people=?, rule_theme=?, theme=? WHERE id=?""",
            (data.get("caption", ""),
             json.dumps(data.get("tags", []), ensure_ascii=False),
             ai_theme, data.get("status", "done"), data.get("error", ""),
             int(data.get("people", -1) if data.get("people") is not None else -1),
             backup, new_theme, pid),
        )
        conn.commit()


def ai_pending_ids(force: bool = False) -> list[int]:
    """已经解析成功但还没做过语义标注的照片。"""
    sql = "SELECT id FROM photos WHERE status='done'"
    if not force:
        sql += " AND ai_status!='done'"
    sql += " ORDER BY id"
    return [r["id"] for r in _rows(sql)]


def ai_counts() -> dict:
    rows = _rows("SELECT ai_status, COUNT(*) AS c FROM photos GROUP BY ai_status")
    out = {r["ai_status"] or "none": r["c"] for r in rows}
    return {"done": out.get("done", 0), "error": out.get("error", 0),
            "none": out.get("none", 0)}


def reset_ai() -> None:
    """清掉标注结果，并把主题还原成规则分类（用于“关掉 AI”）。"""
    with _LOCK, closing(_conn()) as conn:
        conn.execute(
            """UPDATE photos SET theme=rule_theme
               WHERE rule_theme!='' AND manual=0 AND ai_theme!=''""")
        conn.execute("UPDATE photos SET ai_theme='', caption='', tags='[]', "
                     "ai_status='none', ai_err='', people=-1, rule_theme=''")
        conn.commit()


def set_manual_theme(pid: int, theme: str) -> None:
    """人工调整主题：仅记录在数据库，不移动文件。manual=1 后重新分类不再覆盖。"""
    _run("UPDATE photos SET theme=?, manual=1 WHERE id=?", (theme, pid))


def clear_manual(pid: int) -> None:
    _run("UPDATE photos SET manual=0 WHERE id=?", (pid,))


def delete_photo(pid: int) -> None:
    _run("DELETE FROM photos WHERE id=?", (pid,))


def clear_library(remove_roots: bool = True) -> None:
    with _LOCK, closing(_conn()) as conn:
        conn.execute("DELETE FROM photos")
        if remove_roots:
            conn.execute("DELETE FROM roots")
        conn.commit()


def reset_status() -> None:
    """让所有照片重新参与分析（人工设定的主题由 worker 保留，不被覆盖）。"""
    _run("UPDATE photos SET status='pending'")


def pending_ids(force: bool = False) -> list[int]:
    if force:
        return [r["id"] for r in _rows("SELECT id FROM photos ORDER BY id")]
    return [r["id"] for r in _rows("SELECT id FROM photos WHERE status!='done' ORDER BY id")]


def existing_paths() -> set[str]:
    return {r["path"] for r in _rows("SELECT path FROM photos")}


def path_pairs(ids: list[int]) -> list[tuple[int, str]]:
    """批量取 id → 文件路径（后台逐张标注时用，避免一张一次查询）。"""
    out: list[tuple[int, str]] = []
    ids = list(ids)
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        ph = ",".join("?" * len(chunk))
        out += [(r["id"], r["path"])
                for r in _rows(f"SELECT id, path FROM photos WHERE id IN ({ph})", chunk)]
    return out


# ---------------- 根目录 ----------------

def add_root(root: str, recursive: bool = True) -> None:
    _run(
        """INSERT INTO roots(root, recursive, imported_at) VALUES(?,?,?)
           ON CONFLICT(root) DO UPDATE SET recursive=excluded.recursive, imported_at=excluded.imported_at""",
        (root, 1 if recursive else 0, time.time()),
    )


def remove_root(root: str) -> None:
    _run("DELETE FROM roots WHERE root=?", (root,))


def list_roots() -> list[dict]:
    return _rows("SELECT * FROM roots ORDER BY imported_at DESC")


# ---------------- 查询 ----------------

def get_photo(pid: int) -> dict | None:
    rows = _rows("SELECT * FROM photos WHERE id=?", (pid,))
    return rows[0] if rows else None


def query_photos(theme: str = "", q: str = "", root: str = "", month: str = "",
                 folder: str = "", limit: int = 500, offset: int = 0,
                 order: str = "taken") -> list[dict]:
    where, args = ["1=1"], []
    if theme:
        where.append("theme=?")
        args.append(theme)
    if month:
        where.append("month=?")
        args.append(month)
    if folder:
        where.append("folder=?")
        args.append(folder)
    if root:
        where.append("(folder=? OR folder LIKE ? OR folder LIKE ?)")
        args += [root, root + "\\%", root + "/%"]
    if q:
        where.append("(filename LIKE ? OR folder LIKE ? OR theme LIKE ?)")
        like = f"%{q}%"
        args += [like, like, like]
    sort = {"taken": "COALESCE(NULLIF(taken_at,0), mtime) DESC",
            "name": "filename ASC",
            "size": "bytes DESC",
            "conf": "conf DESC",
            "added": "added_at DESC"}.get(order, "COALESCE(NULLIF(taken_at,0), mtime) DESC")
    sql = f"""SELECT id, path, filename, folder, width, height, bytes, mtime, taken_at,
                     month, theme, conf, manual, status, caption, tags, ai_theme, ai_status,
                     rule_theme, people
              FROM photos WHERE {' AND '.join(where)} ORDER BY {sort} LIMIT ? OFFSET ?"""
    args += [limit, offset]
    return _rows(sql, args)


def search_rows(where: str = "1=1", args: Iterable[Any] = (), cap: int = 4000) -> list[dict]:
    """AI 查找用：额外取出 feats / exif / 语义标注原文，按拍摄时间倒序，最多 cap 条。"""
    sql = f"""SELECT id, path, filename, folder, width, height, bytes, mtime, taken_at,
                     month, theme, conf, manual, status, feats, exif,
                     caption, tags, ai_theme, people FROM photos
              WHERE {where}
              ORDER BY COALESCE(NULLIF(taken_at,0), mtime) DESC LIMIT ?"""
    return _rows(sql, tuple(args) + (cap,))


def count_photos(theme: str = "", q: str = "", root: str = "", month: str = "",
                 folder: str = "") -> int:
    where, args = ["1=1"], []
    if theme:
        where.append("theme=?")
        args.append(theme)
    if month:
        where.append("month=?")
        args.append(month)
    if folder:
        where.append("folder=?")
        args.append(folder)
    if root:
        where.append("(folder=? OR folder LIKE ? OR folder LIKE ?)")
        args += [root, root + "\\%", root + "/%"]
    if q:
        where.append("(filename LIKE ? OR folder LIKE ?)")
        like = f"%{q}%"
        args += [like, like]
    rows = _rows(f"SELECT COUNT(*) AS c FROM photos WHERE {' AND '.join(where)}", args)
    return rows[0]["c"] if rows else 0


def theme_counts() -> dict[str, int]:
    return {r["theme"] or "other": r["c"]
            for r in _rows("SELECT theme, COUNT(*) AS c FROM photos GROUP BY theme")}


def group_counts(field: str) -> list[dict]:
    """按月份或文件夹聚合。"""
    col = field if field in ("month", "folder") else "month"
    rows = _rows(
        f"""SELECT {col} AS key, COUNT(*) AS c FROM photos
            WHERE {col} != '' GROUP BY {col} ORDER BY {col} DESC""")
    return rows


def stats() -> dict:
    total = _rows("SELECT COUNT(*) AS c, SUM(bytes) AS b FROM photos")[0]
    done = _rows("SELECT COUNT(*) AS c FROM photos WHERE status='done'")[0]["c"]
    err = _rows("SELECT COUNT(*) AS c FROM photos WHERE status='error'")[0]["c"]
    return {"total": total["c"], "bytes": total["b"] or 0, "done": done, "errors": err}
