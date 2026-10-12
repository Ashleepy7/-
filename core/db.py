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
    # 黑白/单色风格轴：与内容主题正交（1=黑白风格，0=否），本地特征回填、不走模型。
    ("mono_style", "INTEGER DEFAULT 0"),
    # 人工审核台：gt_theme = 人工确认的「评测参考答案」（real_check/detail_check 优先用），
    # reviewed = 是否已在审核台被人工过目（1=看过），用于可疑筛选的进度。
    ("gt_theme", "TEXT DEFAULT ''"),
    ("reviewed", "INTEGER DEFAULT 0"),
]


def _conn() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    # WAL：读写不再互斥——后台批量写（导入 / AI 重标注）时，前台读图不被挡在写锁外，
    # 从根上治历史上的 "database is locked"（timeout=15 只是让读干等，WAL 才是真并行读）。
    # journal_mode 会持久写进库头，重复设置幂等无害；synchronous=NORMAL 是每连接才生效、
    # 不持久，随 WAL 一起放开写吞吐。busy_timeout 仍由 connect(timeout=15) 负责，
    # 写串行仍靠全局 _LOCK——WAL 只让读不堵，不代表能并发写，两者都保持不变。
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def theme_src(row: dict) -> str:
    """这张照片当前显示的 theme 是谁定的：manual（人工）/ ai（模型）/ rule（本地规则）。

    界面只在返回 rule 时显示置信度百分比，而 conf 永远是规则分类器留下的分数（见
    library.reanalyze）。要让这个百分比“真实有效”，前提是它衡量的正是当前显示的主题：
      · 显示主题就是规则算的（无 ai_theme）→ rule，百分比有效；
      · 模型判的、且规则也投了同一个主题（撞车）→ 借规则的分佐证，仍归 rule，有效；
      · 模型判的、但规则和它不一致 → 那个分数是“别的主题”的，配当前主题是张冠李戴，
        归 ai、藏起百分比。
    人工设定的主题与规则分数无关，一律归 manual，不显示百分比。
    """
    if row.get("manual"):
        return "manual"
    theme = (row.get("theme") or "").strip()
    ai = (row.get("ai_theme") or "").strip()
    rule = (row.get("rule_theme") or "").strip()
    if ai and theme == ai:
        return "rule" if (rule and theme == rule) else "ai"
    return "rule"


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

    WAL 注意：开启 WAL 后 data/ 下会多出 album.db-wal、album.db-shm 两个附属文件，
    未落盘的写可能还在 -wal 里。备份走的是 sqlite backup API，会把 WAL 一并收敛进
    单个 dst 文件，副本自包含、能独立打开。手动还原时务必：先停服务（干净关闭会
    checkpoint 并把 -wal 合回主库、删掉附属文件），或盖回主库后一并删掉旧的
    album.db-wal / album.db-shm——只 Copy-Item 覆盖主库却残留旧 -wal，会让旧数据“复活”。
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
    if data.get("mono_style") is not None:
        extra += ", mono_style=?"
        args.append(int(bool(data["mono_style"])))
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


def all_photo_feats() -> list[tuple[int, dict]]:
    """回填风格轴用：返回所有已分析过（feats 非空）的 (id, feats)。仅读库、不解码。"""
    rows = _rows("SELECT id, feats FROM photos WHERE feats IS NOT NULL AND feats != ''")
    out: list[tuple[int, dict]] = []
    for r in rows:
        try:
            out.append((r["id"], json.loads(r["feats"] or "{}")))
        except (ValueError, TypeError):
            continue
    return out


def set_mono_style(pid: int, val: int) -> None:
    with _LOCK, closing(_conn()) as conn:
        conn.execute("UPDATE photos SET mono_style=? WHERE id=?", (int(bool(val)), pid))
        conn.commit()


# ---------------- 人工审核台 ----------------

def update_review(pid: int, patch: dict) -> dict | None:
    """审核台逐张落库。白名单字段，一次可改多个；未知键忽略。

    - theme: 人工定主题（置 manual=1，与 set_manual_theme 同义）；传空串则回到自动结果。
    - mono_style / reviewed: 0/1 开关。
    - caption / tags(list) / people(int) / gt_theme。
    返回更新后的整行（无此行返回 None）。
    """
    row = get_photo(pid)
    if row is None:
        return None
    sets, args = [], []
    if "theme" in patch:
        theme = (patch.get("theme") or "").strip()
        cur = (row.get("theme") or "").strip()
        if theme and theme != cur:            # 人工真的改成另一个主题才置 manual
            sets += ["theme=?", "manual=1"]; args += [theme]
        elif not theme:                        # 交回自动：按显示优先级 模型>规则 回落，清 manual
            back = row.get("ai_theme") or row.get("rule_theme") or cur
            sets += ["theme=?", "manual=0"]; args += [back]
        # theme==cur 且本来就是自动判的 → 没动主题，不写、不误置 manual
    if "mono_style" in patch:
        sets.append("mono_style=?"); args.append(int(bool(patch["mono_style"])))
    if "reviewed" in patch:
        sets.append("reviewed=?"); args.append(int(bool(patch["reviewed"])))
    if "gt_theme" in patch:
        sets.append("gt_theme=?"); args.append((patch.get("gt_theme") or "").strip())
    if "caption" in patch:
        sets.append("caption=?"); args.append(str(patch.get("caption") or ""))
    if "tags" in patch:
        tags = patch.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()]
        sets.append("tags=?"); args.append(json.dumps(list(tags)[:12], ensure_ascii=False))
    if "people" in patch:
        try:
            ppl = int(patch.get("people"))
        except (TypeError, ValueError):
            ppl = -1
        sets.append("people=?"); args.append(max(ppl, -1))
    if sets:
        args.append(pid)
        _run(f"UPDATE photos SET {', '.join(sets)} WHERE id=?", args)
    return get_photo(pid)


# 可疑筛选：每个 flag 对应一段 WHERE。审核台的标签页按此递“最可能有问题的”照片。
REVIEW_FLAGS: dict[str, str] = {
    "disagree": "status='done' AND ai_theme!='' AND rule_theme!='' AND ai_theme!=rule_theme",   # 模型≠规则
    "lowconf": "status='done' AND manual=0 AND ai_theme='' AND conf<0.55",     # 规则也没把握
    "unannotated": "status='done' AND ai_status!='done'",                      # 还没跑模型标注
    "ungroundtruth": "status='done' AND gt_theme=''",                          # 评测还没有人工真值
    "unreviewed": "status='done' AND reviewed=0",                             # 还没人工过目
    "mono": "status='done' AND mono_style=1",                                   # 黑白风格样张
}


def review_where(flag: str = "", q: str = "", theme: str = "") -> tuple[str, list]:
    where, args = ["status='done'"], []
    if flag == "all" or not flag:
        pass
    elif flag in REVIEW_FLAGS:
        where.append(REVIEW_FLAGS[flag])
    else:
        where.append("reviewed=0")            # 未知 flag 归到未审
    if theme:
        where.append("theme=?"); args.append(theme)
    if q:
        where.append("(filename LIKE ? OR folder LIKE ? OR caption LIKE ? OR gt_theme LIKE ?)")
        like = f"%{q}%"; args += [like, like, like, like]
    return " AND ".join(where), args


def review_list(flag: str = "", q: str = "", theme: str = "",
                limit: int = 60, offset: int = 0) -> list[dict]:
    w, args = review_where(flag, q, theme)
    sql = """SELECT id, path, filename, folder, width, height, bytes, taken_at, month,
                    theme, conf, manual, status, caption, tags, ai_theme, ai_status,
                    rule_theme, people, mono_style, gt_theme, reviewed
             FROM photos WHERE %s
             ORDER BY reviewed ASC, conf ASC, id DESC LIMIT ? OFFSET ?""" % w
    return _rows(sql, tuple(args) + (limit, offset))


def review_count(flag: str = "", q: str = "", theme: str = "") -> int:
    w, args = review_where(flag, q, theme)
    rows = _rows(f"SELECT COUNT(*) AS c FROM photos WHERE {w}", args)
    return rows[0]["c"] if rows else 0


def review_flags() -> dict:
    """各筛选项的条数，审核台标签页上显量。"""
    out = {"all": _rows("SELECT COUNT(*) AS c FROM photos WHERE status='done'")[0]["c"]}
    for name, cond in REVIEW_FLAGS.items():
        out[name] = _rows(f"SELECT COUNT(*) AS c FROM photos WHERE {cond}")[0]["c"]
    return out


def update_ai(pid: int, data: dict) -> None:
    """写入 AI 视觉标注结果。

    模型给出的主题会直接接管 theme（人工设定过的不动），
    原规则分类先备份进 rule_theme，关掉 AI 时能还原。
    """
    with _LOCK, closing(_conn()) as conn:
        cur = conn.execute(
            "SELECT theme, manual, rule_theme, caption, tags, people FROM photos WHERE id=?",
            (pid,)).fetchone()
        if cur is None:
            return
        ai_theme = data.get("theme") or ""
        # 人工设定优先于模型；模型没给主题就保持现状
        new_theme = ai_theme if (ai_theme and not cur["manual"]) else cur["theme"]
        backup = cur["rule_theme"] or cur["theme"]    # 多次标注也只留最早那份规则结果
        # 新值为空而旧值非空时保留旧值：避免“标注失败/重跑”把已有（含人工纠正的）
        # 描述、标签、人数抹成空。只有拿到新的非空结果才覆盖。
        new_cap = data.get("caption", "")
        keep_cap = new_cap if new_cap else (cur["caption"] or "")
        new_tags = data.get("tags", [])
        keep_tags = json.dumps(new_tags, ensure_ascii=False) if new_tags else (cur["tags"] or "[]")
        new_people = int(data.get("people", -1) if data.get("people") is not None else -1)
        keep_people = new_people if new_people >= 0 else int(cur["people"] if cur["people"] is not None else -1)
        conn.execute(
            """UPDATE photos SET caption=?, tags=?, ai_theme=?, ai_status=?, ai_err=?,
                              people=?, rule_theme=?, theme=? WHERE id=?""",
            (keep_cap, keep_tags,
             ai_theme, data.get("status", "done"), data.get("error", ""),
             keep_people, backup, new_theme, pid),
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
                 order: str = "taken", year: str = "", shot: str = "") -> list[dict]:
    where, args = ["1=1"], []
    if theme:
        where.append("theme=?")
        args.append(theme)
    if shot == "unknown":
        where.append("NULLIF(taken_at,0) IS NULL")
    if year:
        where.append("substr(month,1,4)=?")
        args.append(year)
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
    sort = {"taken": "NULLIF(taken_at,0) DESC",
            "name": "filename ASC",
            "size": "bytes DESC",
            "conf": "conf DESC",
            "added": "added_at DESC"}.get(order, "NULLIF(taken_at,0) DESC")
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
                     caption, tags, ai_theme, ai_status, rule_theme, people, mono_style FROM photos
              WHERE {where}
              ORDER BY NULLIF(taken_at,0) DESC LIMIT ?"""
    return _rows(sql, tuple(args) + (cap,))


def count_photos(theme: str = "", q: str = "", root: str = "", month: str = "",
                 folder: str = "", year: str = "", shot: str = "") -> int:
    where, args = ["1=1"], []
    if theme:
        where.append("theme=?")
        args.append(theme)
    if shot == "unknown":
        where.append("NULLIF(taken_at,0) IS NULL")
    if year:
        where.append("substr(month,1,4)=?")
        args.append(year)
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


def group_years() -> list[dict]:
    """年→月层级：先按年份聚合，年份内再列各月及张数。按时间倒序（新→旧）。"""
    tree: dict[str, dict] = {}
    for r in _rows("SELECT month, COUNT(*) AS c FROM photos "
                   "WHERE month != '' GROUP BY month ORDER BY month DESC"):
        y = (r["month"] or "")[:4]
        node = tree.setdefault(y, {"year": y, "c": 0, "months": []})
        node["c"] += r["c"]
        node["months"].append({"key": r["month"], "c": r["c"]})
    return list(tree.values())


def stats() -> dict:
    total = _rows("SELECT COUNT(*) AS c, SUM(bytes) AS b FROM photos")[0]
    done = _rows("SELECT COUNT(*) AS c FROM photos WHERE status='done'")[0]["c"]
    err = _rows("SELECT COUNT(*) AS c FROM photos WHERE status='error'")[0]["c"]
    unknown = _rows("SELECT COUNT(*) AS c FROM photos WHERE NULLIF(taken_at,0) IS NULL")[0]["c"]
    return {"total": total["c"], "bytes": total["b"] or 0, "done": done, "errors": err,
            "shot_unknown": unknown, "shot_known": total["c"] - unknown}
