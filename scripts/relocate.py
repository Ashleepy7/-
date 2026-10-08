"""项目搬家后用：把数据库里记录的照片绝对路径改写到新位置。

现象：数据库（data/album.db）存的是每张照片的绝对路径。
把整个项目文件夹挪走或改名后，路径全部失效，界面会变成一堆打不开的图。

用法：
    python scripts/relocate.py            # 只检查、打印将要改什么，不动库
    python scripts/relocate.py --apply    # 真正改写
    python scripts/relocate.py --apply --old "D:\\旧位置\\相册软件"   # 自动判断不准时手动给旧路径

新路径永远以本脚本所在的项目目录为准（所以必须先把整个文件夹搬好再跑）。

原理：对每条已经不存在的路径，从最深层父目录往上找祖先 A，
只要「项目目录 / (该路径去掉 A 的前缀)」这个文件真的存在，就认定 A 对应现在的项目目录，
然后按这个前缀批量改写 photos.path / photos.folder / roots.root。
判定基于"文件是否真的存在"，不会凭字符串瞎猜。
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core import config              # noqa: E402

NEW_BASE = pathlib.Path(__file__).resolve().parents[1]


def _rebase(p: str, old: str, new: str) -> str:
    return new + p[len(old):] if p.lower().startswith(old.lower()) else p


def _guess_old(path: str) -> str | None:
    """从一条失效路径反推它当初的根：往上试祖先，看去掉前缀后能不能落在项目里。"""
    q = pathlib.Path(path)
    for anc in [q.parent, *list(q.parents)[:-1]]:
        try:
            rel = q.relative_to(anc)
        except ValueError:
            continue
        if (NEW_BASE / rel).exists():
            return str(anc)
    return None


def main() -> None:
    apply = "--apply" in sys.argv
    argv = sys.argv
    old_base = argv[argv.index("--old") + 1] if "--old" in argv else ""
    new_base = str(NEW_BASE)

    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT id, path, folder FROM photos").fetchall()]
    roots = [r["root"] for r in conn.execute("SELECT root FROM roots").fetchall()]
    conn.close()

    missing = [r for r in rows if not pathlib.Path(r["path"]).exists()]
    print(f"库内 {len(rows)} 条记录，其中 {len(missing)} 条路径已失效")
    if not missing:
        print("所有照片都在原位，不需要迁移。")
        return

    # 推断旧根：取第一条能反推出结果的失效路径
    old = old_base or _guess_old(missing[0]["path"])
    if not old:
        print("没能自动判断旧根目录，请用 --old 指定，例如：")
        print('  python scripts/relocate.py --apply --old "C:\\\\旧位置\\\\相册软件"')
        return
    print(f"旧根：{old}\n新根：{new_base}")

    fixed = skipped = 0
    updates: list[tuple[str, str, int]] = []      # (新 path, 新 folder, id)
    for r in rows:
        p = _rebase(r["path"], old, new_base)
        f = _rebase(r["folder"], old, new_base)
        if p == r["path"] and f == r["folder"]:
            continue
        if not pathlib.Path(p).exists():
            skipped += 1
            print(f"  仍找不到：{p}")
            continue
        updates.append((p, f, r["id"]))
        fixed += 1
    new_roots = [(_rebase(x, old, new_base), x) for x in roots]

    print(f"可修复 {fixed} 条，仍缺失 {skipped} 条，导入根 {len(new_roots)} 条")
    if not apply:
        print("（这是预览，加 --apply 才会写入数据库）")
        return
    if not updates:
        print("没有可修复的记录，未改动数据库。")
        return

    conn = sqlite3.connect(config.DB_PATH)
    with conn:
        conn.executemany("UPDATE photos SET path=?, folder=? WHERE id=?", updates)
        for new_r, old_r in new_roots:
            conn.execute("UPDATE roots SET root=? WHERE root=?", (new_r, old_r))
    left = [r for r in sqlite3.connect(config.DB_PATH).execute("SELECT path FROM photos")
            if not pathlib.Path(r[0]).exists()]
    conn.close()
    print(f"已改写 {len(new_roots) + len(updates)} 条记录（{fixed} 张路径 + {len(new_roots)} 条导入根）")
    print(f"复核：仍有 {len(left)} 条路径指不到真实文件")
    print("刷新界面即可；缩略图缓存按路径+修改时间命名，必要时可删 data/thumbs 重新生成。")


if __name__ == "__main__":
    main()
