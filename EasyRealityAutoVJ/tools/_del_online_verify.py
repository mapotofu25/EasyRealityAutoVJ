#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证 2026-10-09 新增：**erlib 删除/联网补跑** + **ERAVJ 曲库删除/批量删除**。

## 判据
Part A —— erlib（数据层）
  A1 `LibWriter.delete_songs()`：songs/fingerprints/covers **三表一起删**，别的歌不受影响
  A2 反向：只删 songs 的旧行为 ⇒ "指纹也没了"判据失败（证明测试有效）
  A3 `LibWriter.update_genres()`：**只**改曲风列，grid/指纹/封面原样保留（补跑曲风的关键）
  A4 `OnlineThread.pending()`：跳过 `level=track` 与 `source∈discogs/itunes`，只返回要补的

Part B —— ERAVJ（曲库面板）
  B1 曲库列表为 ExtendedSelection（可多选）
  B2 `_remove_imported`：确认=Yes ⇒ imported 库的 songs/封面/指纹都被删
  B3 **二次确认**：确认=No ⇒ 一个字节都不删（安全判据）
  B4 `remove_music_many`：批量移除 N 首，library/meta 同步、一次完成

跑法：QT_QPA_PLATFORM=offscreen python tools/_del_online_verify.py [--old]
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

FAILS = []


def uri(p):
    return "file:%s?mode=ro" % Path(p).as_posix()


def count(db, table, where="", args=()):
    c = sqlite3.connect(uri(db), uri=True)
    try:
        q = "SELECT COUNT(*) FROM %s" % table
        if where:
            q += " WHERE " + where
        return c.execute(q, args).fetchone()[0]
    finally:
        c.close()


def part_erlib(old):
    print("=" * 72)
    print("Part A —— erlib（删除 / 联网补跑数据层）" + ("  [反向模式]" if old else ""))
    print("=" * 72)
    from erlib.format import LibWriter, LibReader

    tmp = tempfile.mkdtemp(prefix="erlib_del_")
    work = os.path.join(tmp, "w.eravjlib")

    # ---- 造 2 首歌 ----
    w = LibWriter(work, root="X:/fake", resume=False, app_ver="test")
    fps1 = [("h%03d" % i, i) for i in range(50)]
    fps2 = [("k%03d" % i, i) for i in range(30)]
    w.add_song(111, {"rel_path": "a.flac", "file_name": "a.flac",
                     "title": "A", "artist": "X",
                     "grid": {"bpm": 128.0, "src": "bt", "beat_len": 0.47,
                              "anchor": 0.1, "conf": 0.9, "dur": 200.0}},
               fps1, b"\xff\xd8fakejpgA")
    w.add_song(222, {"rel_path": "b.flac", "file_name": "b.flac",
                     "title": "B", "artist": "Y",
                     "grid": {"bpm": 90.0, "src": "bt", "beat_len": 0.66,
                              "anchor": 0.2, "conf": 0.8, "dur": 180.0}},
               fps2, b"\xff\xd8fakejpgB")
    w.commit()
    print("造工作库：2 首（指纹 %d + %d）" % (len(fps1), len(fps2)))

    # ---- A1 删除（或旧行为）----
    if old:
        # 反向：只删 songs（旧"删不干净"的写法）
        w.conn.execute("DELETE FROM songs WHERE song_id=?", (111,))
        w.conn.commit()
        n_del = 1
    else:
        n_del = w.delete_songs([111])
    fp1 = count(work, "fingerprints", "song_id=?", (111,))
    cv1 = count(work, "covers", "song_id=?", (111,))
    sg1 = count(work, "songs", "song_id=?", (111,))
    sg2 = count(work, "songs", "song_id=?", (222,))
    fp2 = count(work, "fingerprints", "song_id=?", (222,))
    ok1 = (n_del == 1 and sg1 == 0 and fp1 == 0 and cv1 == 0
           and sg2 == 1 and fp2 == len(fps2))
    print("\nA1 delete_songs(111)：返回 %d；sid111 → songs=%d fps=%d covers=%d "
          "（应全 0）；sid222 → songs=%d fps=%d（应 1/%d）%s"
          % (n_del, sg1, fp1, cv1, sg2, fp2, len(fps2), "✓" if ok1 else " ★"))
    if not ok1:
        FAILS.append("A1")
    if old:
        print("   （反向模式：这里必须 FAIL —— 指纹/封面没删干净）")

    # ---- A3 update_genres 只改曲风列 ----
    before_grid = w.conn.execute(
        "SELECT grid, fp_count, cover_size FROM songs WHERE song_id=222"
    ).fetchone()
    w.update_genres(222, {"artist": "Y2", "title": "B2", "genre": "House",
                          "genres": ["House", "Deep House"], "source": "discogs",
                          "level": "track"})
    w.commit()
    after_grid = w.conn.execute(
        "SELECT grid, fp_count, cover_size FROM songs WHERE song_id=222"
    ).fetchone()
    after_src = w.conn.execute(
        "SELECT artist, source, level, genres FROM songs WHERE song_id=222"
    ).fetchone()
    fp2b = count(work, "fingerprints", "song_id=?", (222,))
    ok3 = (before_grid == after_grid and fp2b == len(fps2)
           and after_src[1] == "discogs" and after_src[2] == "track"
           and "House" in (after_src[3] or ""))
    print("\nA3 update_genres 只改曲风列：grid/fp_count/cover_size %s；"
          "指纹仍 %d 条；source=%s level=%s %s"
          % ("原样 ✓" if before_grid == after_grid else "★ 被改了",
             fp2b, after_src[1], after_src[2], "✓" if ok3 else " ★"))
    if not ok3:
        FAILS.append("A3")
    w.close()

    # ---- A4 pending() 筛选 ----
    from erlib.app import OnlineThread
    ot = OnlineThread(work, "X:/fake")
    # 222 已是 track（刚 update 过）⇒ pending 应为空
    p = ot.pending()
    ok4 = (len(p) == 0)
    print("\nA4 pending()：222 已是 level=track ⇒ 待补 %d 首（应 0）%s"
          % (len(p), "✓" if ok4 else " ★"))
    if not ok4:
        FAILS.append("A4")


class _Cfg(dict):
    def save(self):
        pass


def part_eravj(old):
    print()
    print("=" * 72)
    print("Part B —— ERAVJ（曲库删除 / 批量删除）" + ("  [反向模式]" if old else ""))
    print("=" * 72)
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])

    # 临时数据目录（模拟 exe 的 %LOCALAPPDATA%\AutoVJ，避免碰真实数据）
    tmp = tempfile.mkdtemp(prefix="eravj_del_")
    import config
    config.app_base_dir = lambda: tmp
    import imported_lib as IL
    import panels

    # 造 3 首导入歌（含封面 + 指纹）
    conn = IL.open_meta(tmp)
    cd = IL.cover_dir(tmp)
    os.makedirs(cd, exist_ok=True)
    for sid in (101, 202, 303):
        IL.add_song(conn, sid, {"file_name": "s%d.flac" % sid, "title": "T%d" % sid,
                                "artist": "A", "genres": ["House"]})
        with open(os.path.join(cd, "%d.jpg" % sid), "wb") as f:
            f.write(b"\xff\xd8j")
    conn.commit()
    conn.close()
    # 指纹主库
    fdb = os.path.join(tmp, "fingerprints.db")
    c = sqlite3.connect(fdb)
    c.execute("CREATE TABLE IF NOT EXISTS fingerprints ("
              "hash TEXT NOT NULL, song_id INTEGER NOT NULL, offset INTEGER NOT NULL)")
    c.executemany("INSERT INTO fingerprints VALUES (?,?,?)",
                  [("h%d_%d" % (sid, i), sid, i) for sid in (101, 202, 303)
                   for i in range(10)])
    c.commit()
    c.close()

    # ---- B1 多选模式 ----
    class _FakeMain:
        def __init__(self):
            self.cfg = _Cfg({"ui": {"music_cover_size": 96},
                             "music_meta": {}, "music_library": []})

        def __getattr__(self, name):
            return lambda *a, **k: None

    p = panels.MusicLibraryPanel(_FakeMain())
    from PySide6.QtWidgets import QAbstractItemView
    okb1 = (p.list.selectionMode()
            == QAbstractItemView.SelectionMode.ExtendedSelection)
    print("\nB1 曲库列表多选模式：%s" % ("✓ ExtendedSelection" if okb1 else "★ 不是"))
    if not okb1:
        FAILS.append("B1")

    # ---- B2/B3 删除 + 二次确认 ----
    def _fake_q(yes):
        def q(*a, **k):
            return (QMessageBox.StandardButton.Yes if yes
                    else QMessageBox.StandardButton.No)
        return q

    # ---- B3 二次确认：选「否」必须一个字节都不动 ----
    orig_q = QMessageBox.question
    if old:
        # 反向模式：模拟"没有二次确认"的实现（直接删，不看用户点啥）
        IL.remove_songs(tmp, [101])
        p.refresh()
    else:
        QMessageBox.question = _fake_q(False)
        try:
            p._remove_imported(["@imported:101"])
        finally:
            QMessageBox.question = orig_q
    still = count(os.path.join(tmp, "imported_meta.db"), "songs", "song_id=?", (101,))
    okb3 = (still == 1)
    print("B3 二次确认选「否」⇒ 数据还在：songs=%d（应 1）%s"
          % (still, "✓" if okb3 else " ★ 没确认就删了！"))
    if not okb3:
        FAILS.append("B3")

    # ---- B2 确认删除 ----
    QMessageBox.question = _fake_q(True)
    try:
        p._remove_imported(["@imported:101", "@imported:202"])
    finally:
        QMessageBox.question = orig_q
    sg101 = count(os.path.join(tmp, "imported_meta.db"), "songs", "song_id=?", (101,))
    sg202 = count(os.path.join(tmp, "imported_meta.db"), "songs", "song_id=?", (202,))
    sg303 = count(os.path.join(tmp, "imported_meta.db"), "songs", "song_id=?", (303,))
    fp101 = count(fdb, "fingerprints", "song_id=?", (101,))
    cv101 = 1 if os.path.isfile(os.path.join(cd, "101.jpg")) else 0
    fp303 = count(fdb, "fingerprints", "song_id=?", (303,))
    okb2 = (sg101 == 0 and sg202 == 0 and sg303 == 1
            and fp101 == 0 and cv101 == 0 and fp303 == 10)
    print("B2 确认删除 2 首：songs(101/202/303)=%d/%d/%d（应 0/0/1）；"
          "指纹(101)=%d（应 0）；封面(101)=%d（应 0）；指纹(303)=%d（应 10）%s"
          % (sg101, sg202, sg303, fp101, cv101, fp303, "✓" if okb2 else " ★"))
    if not okb2:
        FAILS.append("B2")

    # ---- B4 remove_music_many（不碰真实 cfg，用 mock）----
    class _M:
        def __init__(self):
            self.cfg = _Cfg({"ui": {}, "music_cover_size": 96,
                             "music_meta": {"p1": {"song_id": 1},
                                            "p2": {"song_id": 2},
                                            "p3": {"song_id": 3}},
                             "music_library": ["p1", "p2", "p3"]})
            self.music_dialog = None

        def __getattr__(self, name):
            return lambda *a, **k: None

    # 借用 ui_main.MainWindow 的**同一个函数对象**（unbound：把 mock self 传进去），
    # 不构造整个窗口 —— 保证测的就是产品代码，不是复制品。
    import ui_main
    m = _M()
    n_rm = ui_main.MainWindow.remove_music_many(m, ["p1", "p3"])
    okb4 = (n_rm == 2 and m.cfg["music_library"] == ["p2"]
            and set(m.cfg["music_meta"].keys()) == {"p2"})
    print("\nB4 remove_music_many(['p1','p3'])：移除 %d 首；"
          "剩 library=%s meta=%s %s"
          % (n_rm, m.cfg["music_library"], sorted(m.cfg["music_meta"]),
             "✓" if okb4 else " ★"))
    if not okb4:
        FAILS.append("B4")


def main():
    old = "--old" in sys.argv
    part_erlib(old)
    part_eravj(old)
    print()
    if FAILS:
        print("结论：★ 判据失败：%s" % "、".join(FAILS))
        return 1
    print("结论：✅ 全部通过（删除三表联动 + 补跑只动曲风列 + 多选 + 二次确认 + 批量移除）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
