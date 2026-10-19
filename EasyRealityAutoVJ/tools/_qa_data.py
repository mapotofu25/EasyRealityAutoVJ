#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""QA 独立复核 —— A. 数据迁移与兼容（**自写**，不复用工程师 _libui_verify.py）。

判据：
  A-PRE   生产库副本：迁移前 15 列 / 40 行 / 无 pack
  A1      display_rows(生产副本) == 40 行（**不是 []**）
  A2      调用后 PRAGMA 出现 pack 列
  A3      旧行 pack == ''
  A4      count() == 40
  A5      lookup_grids() / lookup_genres() 不抛、数据对
  A6      open_meta 连续 3 次幂等不炸
  A7      未迁移旧库 + import_into() → 成功且 pack 正确写入
  A-REV   反向复现：把 display_rows 逻辑「改回直连」→ 未迁移库返回 []（证明 A1 非空过）
  A-LOCK  并发：另一个连接持写锁时 open_meta 的 ALTER 是否被卡 / 失败是否可自愈
  A-NAME  包名边界：与内置选项同名 / 中文空格括号超长 / 两目录同名包

跑法：
    QT_QPA_PLATFORM=offscreen P:\\ERAVJ\\AutoVJ\\venv\\Scripts\\python.exe tools\\_qa_data.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

RESULTS = []


def check(tag, ok, detail=""):
    print("  [%s] %-8s %s" % ("PASS" if ok else "FAIL", tag, detail))
    RESULTS.append((tag, ok, detail))
    return ok


OLD_SCHEMA = (
    "CREATE TABLE songs (song_id INTEGER PRIMARY KEY,"
    " file_name TEXT, file_size INTEGER, duration REAL,"
    " artist TEXT, title TEXT, rel_path TEXT,"
    " genres TEXT, styles TEXT, source TEXT, level TEXT, ai_zh TEXT,"
    " grid TEXT, cover_path TEXT, imported_at REAL)")


def _make_old_db(base, rows):
    """造未迁移旧库（15 列、无 pack），塞 rows 行。"""
    import imported_lib as IL
    db = IL.meta_db_path(base)
    c = sqlite3.connect(db)
    c.execute(OLD_SCHEMA)
    for sid in rows:
        c.execute("INSERT INTO songs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (sid, "old%d.mp3" % sid, 0, 120.0, "A", "T", "r",
                   '["House"]', "[]", "", "", "[]",
                   '{"bpm":128,"dur":200,"src":"vdj","conf":0.9,"anchor":1.0,"beat_len":0.5}',
                   "", 0.0))
    c.commit()
    c.close()


def test_production_copy():
    """A-PRE / A1..A5：用**生产库真实数据**验证迁移不丢 40 行。

    ⚠ 生产库可能已被运行中的 App 自动迁移（open_meta 自愈）⇒ 不能假设它仍是 15 列。
      这里从生产库读出**真实数据行**，重建成 15 列「旧结构」库再测，
      保证无论生产库当前处于哪一状态，迁移判据都确定可复现。
    """
    print("\n=== A. 生产库真实数据迁移 ===")
    import imported_lib as IL
    prod = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ",
                        IL.DB_NAME)
    tmp = tempfile.mkdtemp(prefix="qa_prod_")
    try:
        if not os.path.isfile(prod):
            return check("A-PRE", False, "生产库不存在：%s" % prod)
        src = sqlite3.connect("file:%s?mode=ro" % prod.replace("\\", "/"),
                              uri=True)
        cols_now = [r[1] for r in src.execute("PRAGMA table_info(songs)")]
        data = list(src.execute(
            "SELECT song_id, file_name, file_size, duration, artist, title,"
            " rel_path, genres, styles, source, level, ai_zh, grid,"
            " cover_path, imported_at FROM songs ORDER BY song_id"))
        src.close()
        print("    生产库当前列数=%d（pack 在=%s），读出真实数据 %d 行"
              % (len(cols_now), "pack" in cols_now, len(data)))

        # 用真实数据重建成「旧结构」（15 列、无 pack）
        c = sqlite3.connect(IL.meta_db_path(tmp))
        c.execute(OLD_SCHEMA)
        c.executemany("INSERT INTO songs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      data)
        c.commit()
        c.close()

        c = sqlite3.connect(IL.meta_db_path(tmp))
        cols = [r[1] for r in c.execute("PRAGMA table_info(songs)")]
        n = c.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
        c.close()
        check("A-PRE", len(cols) == 15 and "pack" not in cols and n == 40,
              "旧结构重建：cols=%d has_pack=%s rows=%d"
              % (len(cols), "pack" in cols, n))

        rows = IL.display_rows(tmp)          # 首次调用 → 应触发迁移且返回 40 行
        check("A1", len(rows) == 40, "display_rows -> %d 行" % len(rows))

        c = sqlite3.connect(IL.meta_db_path(tmp))
        cols2 = [r[1] for r in c.execute("PRAGMA table_info(songs)")]
        packs = {r[0] for r in c.execute("SELECT pack FROM songs")}
        c.close()
        check("A2", "pack" in cols2, "cols=%d, pack 在末尾=%s"
              % (len(cols2), cols2[-1] == "pack"))
        check("A3", packs == {""}, "distinct pack=%r" % (packs,))

        check("A4", IL.count(tmp) == 40, "count=%d" % IL.count(tmp))

        g = IL.lookup_grids(tmp)
        ge = IL.lookup_genres(tmp)
        check("A5", isinstance(g, dict) and isinstance(ge, dict) and len(ge) == 40,
              "lookup_grids=%d lookup_genres=%d" % (len(g), len(ge)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_idempotent():
    print("\n=== A6. 迁移幂等 ===")
    import imported_lib as IL
    base = tempfile.mkdtemp(prefix="qa_idem_")
    try:
        _make_old_db(base, [1, 2, 3])
        ok = True
        detail = ""
        try:
            for i in range(3):
                c = IL.open_meta(base)
                c.close()
        except Exception as e:                                    # noqa: BLE001
            ok = False
            detail = "%s: %s" % (type(e).__name__, e)
        c = sqlite3.connect(IL.meta_db_path(base))
        cols = [r[1] for r in c.execute("PRAGMA table_info(songs)")]
        c.close()
        check("A6", ok and cols.count("pack") == 1,
              "连续 3 次 open_meta ok=%s pack 列数=%d %s"
              % (ok, cols.count("pack"), detail))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_old_db_import_path():
    print("\n=== A7. 未迁移旧库 + import_into ===")
    import imported_lib as IL
    from erlib.format import LibWriter
    from erlib.transfer import import_into
    base = tempfile.mkdtemp(prefix="qa_imp_")
    pkgdir = tempfile.mkdtemp(prefix="qa_pkg_")
    try:
        _make_old_db(base, [10])            # 旧库已有 1 行历史数据
        pkg = os.path.join(pkgdir, "新包.eravjlib")
        w = LibWriter(pkg, root="", resume=False)
        w.add_song(5001, {"file_name": "s.mp3", "duration": 100.0,
                          "artist": "DJ", "title": "S",
                          "genres": ["Techno"],
                          "grid": {"bpm": 130, "dur": 100}},
                   fps=[("h1", 0)], cover_bytes=b"\xff\xd8\xff", commit=True)
        w.close()
        st = import_into(pkg, base)
        c = sqlite3.connect(IL.meta_db_path(base))
        rows = dict(c.execute("SELECT song_id, pack FROM songs"))
        c.close()
        check("A7", st.get("songs") == 1 and rows.get(5001) == "新包"
              and rows.get(10) == "",
              "st=%s rows=%r" % (st, rows))
    finally:
        shutil.rmtree(base, ignore_errors=True)
        shutil.rmtree(pkgdir, ignore_errors=True)


def _buggy_display_rows(base_dir):
    """反向复现：旧实现 = 直连 meta_db_path + SELECT 引用 pack（无迁移）。"""
    import imported_lib as IL
    out = []
    try:
        if not os.path.isfile(IL.meta_db_path(base_dir)):
            return out
        c = sqlite3.connect(IL.meta_db_path(base_dir), timeout=30.0)
        try:
            for row in c.execute(
                    "SELECT song_id, file_name, duration, artist, title, "
                    "genres, grid, pack FROM songs ORDER BY song_id"):
                out.append(row)
        finally:
            c.close()
    except Exception:                                             # noqa: BLE001
        pass
    return out


def test_reverse():
    print("\n=== A-REV. 反向复现（直连实现 → []） ===")
    import imported_lib as IL
    base = tempfile.mkdtemp(prefix="qa_rev_")
    try:
        _make_old_db(base, [88])
        buggy = _buggy_display_rows(base)
        good = IL.display_rows(base)          # 修复版（走 open_meta）
        check("A-REV", buggy == [] and len(good) == 1,
              "直连实现=%d 行 / 修复版=%d 行" % (len(buggy), len(good)))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_lock():
    """并发：另一个连接持写锁时，open_meta 的 ALTER 会不会被卡 & 是否自愈。"""
    print("\n=== A-LOCK. 并发写锁 ===")
    import imported_lib as IL
    base = tempfile.mkdtemp(prefix="qa_lock_")
    try:
        _make_old_db(base, [1])
        # 让库进入 WAL（生产库就是 WAL）
        c0 = sqlite3.connect(IL.meta_db_path(base))
        c0.execute("PRAGMA journal_mode=WAL")
        c0.close()

        holder = sqlite3.connect(IL.meta_db_path(base), timeout=30.0)
        holder.execute("BEGIN IMMEDIATE")     # 抢住写锁
        holder.execute("SELECT COUNT(*) FROM songs").fetchone()

        res = {}

        def _worker():
            t0 = time.time()
            try:
                c = IL.open_meta(base)
                res["elapsed"] = time.time() - t0
                res["ok"] = True
                try:
                    cols = {r[1] for r in c.execute("PRAGMA table_info(songs)")}
                    res["pack"] = "pack" in cols
                finally:
                    c.close()
            except Exception as e:                                # noqa: BLE001
                res["ok"] = False
                res["err"] = "%s: %s" % (type(e).__name__, e)

        th = threading.Thread(target=_worker)
        th.start()
        # 持锁 2s 后释放；观察 open_meta 是否被卡住（阻塞等待）
        time.sleep(2.0)
        blocked = th.is_alive()
        holder.rollback()
        holder.close()
        th.join(timeout=40.0)
        elapsed = res.get("elapsed")
        check("A-LOCK", (not blocked) or res.get("ok") is True,
              "持锁 2s 时 open_meta %s；释放后 ok=%s elapsed=%s pack列=%s err=%s"
              % ("被阻塞(在等待)" if blocked else "未阻塞",
                 res.get("ok"), ("%.2fs" % elapsed) if elapsed else None,
                 res.get("pack"), res.get("err", "")))

        # 失败自愈：ALTER 失败一次后，下一次 open_meta 是否补上列
        base2 = tempfile.mkdtemp(prefix="qa_lock2_")
        try:
            _make_old_db(base2, [1])
            c0 = sqlite3.connect(IL.meta_db_path(base2))
            c0.execute("PRAGMA journal_mode=WAL")
            c0.close()
            h = sqlite3.connect(IL.meta_db_path(base2), timeout=0.1)
            h.execute("BEGIN IMMEDIATE")
            # 复刻 _migrate 的 ALTER，但用极短超时模拟"没抢到锁"
            got_err = ""
            try:
                t = sqlite3.connect(IL.meta_db_path(base2), timeout=0.1)
                t.execute("ALTER TABLE songs ADD COLUMN pack TEXT DEFAULT ''")
                t.commit()
                t.close()
            except Exception as e:                                # noqa: BLE001
                got_err = type(e).__name__
            h.rollback()
            h.close()
            # 再正常 open_meta 一次 → 列应补上（自愈）
            c = IL.open_meta(base2)
            cols = {r[1] for r in c.execute("PRAGMA table_info(songs)")}
            c.close()
            check("A-LOCK2", "pack" in cols,
                  "锁超时下 ALTER 异常=%r；下次 open_meta 自愈 pack列=%s"
                  % (got_err, "pack" in cols))
        finally:
            shutil.rmtree(base2, ignore_errors=True)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_pack_names():
    print("\n=== A-NAME. 包名边界 ===")
    import imported_lib as IL
    base = tempfile.mkdtemp(prefix="qa_name_")
    try:
        names = ["全部", "仅本机", "早期导入", "我的 曲库(2026)",
                 "包" + "超长" * 300, "a/b\\c"]
        conn = IL.open_meta(base)
        for i, pk in enumerate(names):
            IL.add_song(conn, 2000 + i, {"file_name": "x.mp3", "title": "X"},
                        pack=pk)
        # 早期导入（空包名）
        IL.add_song(conn, 2100, {"file_name": "y.mp3", "title": "Y"}, pack="")
        conn.commit()
        conn.close()
        rows = IL.display_rows(base)
        got = {r["sid"]: r["pack"] for r in rows}
        ok = all(got.get(2000 + i) == pk for i, pk in enumerate(names))
        check("A-NAME", ok and len(rows) == len(names) + 1,
              "写入/读回 %d 行；含同名内置(%r)与超长包名" % (len(rows), names[:3]))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main():
    test_production_copy()
    test_idempotent()
    test_old_db_import_path()
    test_reverse()
    test_lock()
    test_pack_names()
    print("\n" + "=" * 62)
    bad = [t for t, ok, _ in RESULTS if not ok]
    if bad:
        print("结论：%d 项失败：%s" % (len(bad), "、".join(bad)))
        return 1
    print("结论：全部通过（共 %d 项）" % len(RESULTS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
