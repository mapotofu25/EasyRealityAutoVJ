#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验收「曲库面板三项增强」（2026-10-09）。

覆盖：
  A. 数据迁移 —— 旧结构库 open_meta 后补上 `pack` 列（幂等）
  B. 导入带包名 —— import_into() 写入的 pack == 包文件名（去扩展名）
  C. UI 真构造（offscreen）—— 复选框 / 跨 refresh 保留 / 全选全不选 /
     来源筛选 / 行尾包名 / 勾选优先
  D. i18n 覆盖 + 静态扫描 + py_compile（由 run_all.sh 之外单独跑，这里内置调用）

跑法（必须用项目 venv）：
    set QT_QPA_PLATFORM=offscreen
    P:\\ERAVJ\\AutoVJ\\venv\\Scripts\\python.exe tools\\_libui_verify.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

FAILS = []


def check(tag, ok, detail=""):
    print("  [%s] %s%s" % ("✓" if ok else "★FAIL", tag,
                           ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(tag)
    return ok


# ==========================================================================
# A. 迁移：旧结构库 → open_meta 自动补 pack 列（幂等）
# ==========================================================================
def test_migration():
    print("\n=== A. 数据迁移（补 pack 列） ===")
    import imported_lib as IL
    base = tempfile.mkdtemp(prefix="libui_mig_")
    try:
        db = IL.meta_db_path(base)
        # 手工造「旧结构」库（**不带 pack 列**），并塞一行历史数据
        c = sqlite3.connect(db)
        c.execute(
            "CREATE TABLE songs (song_id INTEGER PRIMARY KEY,"
            " file_name TEXT, file_size INTEGER, duration REAL,"
            " artist TEXT, title TEXT, rel_path TEXT,"
            " genres TEXT, styles TEXT, source TEXT, level TEXT, ai_zh TEXT,"
            " grid TEXT, cover_path TEXT, imported_at REAL)")
        c.execute("INSERT INTO songs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (7, "old.mp3", 0, 120.0, "A", "T", "r", "[]", "[]", "", "",
                   "[]", "{}", "", 0.0))
        c.commit()
        c.close()
        pre = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(songs)")}
        check("A1 旧库初始无 pack 列", "pack" not in pre, "cols=%s" % sorted(pre))

        conn = IL.open_meta(base)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(songs)")}
        finally:
            conn.close()
        check("A2 open_meta 后 pack 列存在", "pack" in cols, "cols=%s" % sorted(cols))

        c = sqlite3.connect(db)
        try:
            val = c.execute("SELECT pack FROM songs WHERE song_id=7").fetchone()[0]
        finally:
            c.close()
        check("A3 旧行 pack 补为 ''", val == "", "pack=%r" % (val,))

        # 幂等：再调一次不炸、不重复 ALTER
        try:
            conn = IL.open_meta(base)
            conn.close()
            ok = True
        except Exception as e:                                     # noqa: BLE001
            ok = False
            print("    异常：%s" % e)
        check("A4 再调 open_meta 幂等不炸", ok)

        # add_song(带 pack) 能写入并被 display_rows 读回
        conn = IL.open_meta(base)
        try:
            IL.add_song(conn, 9, {"file_name": "n.mp3", "title": "N",
                                  "genres": ["House"],
                                  "grid": {"bpm": 128, "dur": 200}}, pack="PackZ")
            conn.commit()
        finally:
            conn.close()
        rows = {r["sid"]: r for r in IL.display_rows(base)}
        check("A5 add_song 写入 pack 可被 display_rows 读回",
              rows.get(9, {}).get("pack") == "PackZ",
              "pack=%r" % (rows.get(9, {}).get("pack"),))

        # ---- A6/A7：display_rows 直连未迁移旧库必须不丢数据 ----
        # 真实缺陷场景：老用户升级后**还没点过任何导入** ⇒ _migrate 没跑过 ⇒
        # 库里没有 pack 列；此时打开曲库面板 → display_rows() 直连 + SELECT pack
        # 会抛 "no such column: pack" 被 except 吞掉 ⇒ 返回 [] ⇒ 导入的歌全消失。
        base2 = tempfile.mkdtemp(prefix="libui_dr_")
        try:
            _make_old_db(base2, sid=55, name="legacy.mp3")
            got = IL.display_rows(base2)     # 直接调（不先 open_meta）
            check("A6 display_rows 直调未迁移旧库不丢数据（应 1 行）",
                  len(got) == 1 and got[0]["sid"] == 55,
                  "rows=%d" % len(got))
            cols = {r[1] for r in sqlite3.connect(
                IL.meta_db_path(base2)).execute("PRAGMA table_info(songs)")}
            check("A7 display_rows 顺带完成迁移（pack 列已存在）",
                  "pack" in cols, "cols=%s" % sorted(cols))
        finally:
            shutil.rmtree(base2, ignore_errors=True)

        # ---- 反向复现：把 display_rows「改回直连」⇒ A6 判据必须失败 ----
        base3 = tempfile.mkdtemp(prefix="libui_dr2_")
        try:
            _make_old_db(base3, sid=77, name="old2.mp3")
            buggy = _buggy_display_rows(base3)   # 旧实现（直连 + SELECT pack）
            check("A6(反) 旧直连实现在未迁移库上返回 []（证明 A6 非空过）",
                  buggy == [], "rows=%d" % len(buggy))
        finally:
            shutil.rmtree(base3, ignore_errors=True)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _make_old_db(base, sid, name):
    """造「未迁移旧库」：15 列、**无 pack 列**，塞 1 行数据。"""
    import imported_lib as IL
    db = IL.meta_db_path(base)
    c = sqlite3.connect(db)
    c.execute(
        "CREATE TABLE songs (song_id INTEGER PRIMARY KEY,"
        " file_name TEXT, file_size INTEGER, duration REAL,"
        " artist TEXT, title TEXT, rel_path TEXT,"
        " genres TEXT, styles TEXT, source TEXT, level TEXT, ai_zh TEXT,"
        " grid TEXT, cover_path TEXT, imported_at REAL)")
    c.execute("INSERT INTO songs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (sid, name, 0, 120.0, "A", "T", "r", "[]", "[]", "", "",
               "[]", "{}", "", 0.0))
    c.commit()
    c.close()


def _buggy_display_rows(base_dir):
    """反向复现用：旧实现（sqlite3 **直连** meta_db_path + SELECT 引用 pack）。"""
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
    except Exception:                                              # noqa: BLE001
        pass
    return out


# ==========================================================================
# B. 导入带包名：import_into() 写 pack == 包文件名
# ==========================================================================
def test_import_pack():
    print("\n=== B. 导入写入包名 ===")
    import imported_lib as IL
    from erlib.format import LibWriter
    from erlib.transfer import import_into
    base = tempfile.mkdtemp(prefix="libui_imp_")
    pkgdir = tempfile.mkdtemp(prefix="libui_pkg_")
    try:
        pkg = os.path.join(pkgdir, "我的曲库.eravjlib")
        w = LibWriter(pkg, root="", resume=False)
        w.add_song(4242, {"file_name": "s.mp3", "duration": 100.0,
                          "artist": "DJ", "title": "Song",
                          "genres": ["Techno"],
                          "grid": {"bpm": 130, "dur": 100}},
                   fps=[("h1", 0), ("h2", 1)], cover_bytes=b"\xff\xd8\xff", commit=True)
        w.close()

        st = import_into(pkg, base)
        check("B1 import_into 成功", st.get("songs") == 1, "st=%s" % st)

        c = sqlite3.connect(IL.meta_db_path(base))
        try:
            pack = c.execute("SELECT pack FROM songs WHERE song_id=4242").fetchone()[0]
        finally:
            c.close()
        check("B2 pack == 包名（去扩展名）", pack == "我的曲库", "pack=%r" % (pack,))
    finally:
        shutil.rmtree(base, ignore_errors=True)
        shutil.rmtree(pkgdir, ignore_errors=True)


# ==========================================================================
# C. UI 真构造（offscreen）
# ==========================================================================
class _Cfg(dict):
    def save(self):
        pass


class _FakeMain:
    def __init__(self, lib):
        self.cfg = _Cfg({"ui": {"music_cover_size": 64},
                         "music_meta": {}, "music_library": list(lib)})

    def __getattr__(self, name):
        return lambda *a, **k: None


def _make_imported(base):
    """临时 imported_meta.db：PackA 2 首 / PackB 1 首 / 早期导入 1 首。"""
    import imported_lib as IL
    conn = IL.open_meta(base)
    try:
        IL.add_song(conn, 1001, {"file_name": "a1.mp3", "title": "A1",
                                 "genres": ["House"], "grid": {"bpm": 124, "dur": 200}},
                    pack="PackA")
        IL.add_song(conn, 1002, {"file_name": "a2.mp3", "title": "A2",
                                 "genres": ["House"], "grid": {"bpm": 126, "dur": 210}},
                    pack="PackA")
        IL.add_song(conn, 1003, {"file_name": "b1.mp3", "title": "B1",
                                 "genres": ["Techno"], "grid": {"bpm": 138, "dur": 220}},
                    pack="PackB")
        IL.add_song(conn, 1004, {"file_name": "e1.mp3", "title": "E1",
                                 "genres": [], "grid": {}}, pack="")
        conn.commit()
    finally:
        conn.close()


def _row_keys(p):
    from PySide6.QtCore import Qt
    return [p.list.item(i).data(Qt.UserRole) for i in range(p.list.count())]


def test_ui():
    print("\n=== C. UI（offscreen 真构造） ===")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    import config
    base = tempfile.mkdtemp(prefix="libui_ui_")
    try:
        _make_imported(base)
        config.app_base_dir = lambda: base
        import panels
        Qt = panels.Qt

        # 2 首不存在的"本机"歌（用于「仅本机」语义）
        p = panels.MusicLibraryPanel(_FakeMain(["C:/fake/a.mp3", "C:/fake/b.mp3"]))
        p.resize(900, 600)
        p.refresh()

        n_imp = sum(1 for k in _row_keys(p) if str(k).startswith("@imported:"))
        check("C0 导入歌 4 首 + 本机 2 首 = 6 行",
              p.list.count() == 6 and n_imp == 4,
              "rows=%d imp=%d" % (p.list.count(), n_imp))

        # ---- T-a 复选框存在、默认未勾选 ----
        all_unchecked = all(p.list.item(i).checkState() == Qt.CheckState.Unchecked
                            for i in range(p.list.count()))
        has_flag = all(bool(p.list.item(i).flags() & Qt.ItemFlag.ItemIsUserCheckable)
                       for i in range(p.list.count()))
        check("T-a 每行有复选框且默认未勾选", all_unchecked and has_flag,
              "unchecked=%s flag=%s" % (all_unchecked, has_flag))

        # ---- T-b 跨 refresh 保留勾选 ----
        def _set_checked(key, on=True):
            for i in range(p.list.count()):
                if p.list.item(i).data(Qt.UserRole) == key:
                    p.list.item(i).setCheckState(
                        Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        _set_checked("@imported:1001")
        _set_checked("@imported:1002")
        keep = {"@imported:1001", "@imported:1002"}
        p.cb_sort.setCurrentIndex(1)          # 切「曲名」排序 → 触发 refresh
        now = set(p._checked_keys())
        check("T-b 切排序后仍保留勾选", now == keep, "checked=%s" % sorted(now))

        # 反向：把勾选键收集打空（= 关掉保留机制）⇒ 必须 0 勾选
        orig_ck = panels.MusicLibraryPanel._checked_keys
        panels.MusicLibraryPanel._checked_keys = lambda self: set()
        try:
            # 重建前先手动勾上，刷新后再看（模拟"保留机制失效"）
            _set_checked("@imported:1001")
            p.refresh()
            n_after = len(p._checked_keys())
        finally:
            panels.MusicLibraryPanel._checked_keys = orig_ck
        check("T-b(反) 关掉保留机制后勾选丢失", n_after == 0,
              "checked=%d" % n_after)
        p.cb_sort.setCurrentIndex(0)
        p.refresh()

        # ---- T-c 全选 / 全不选（作用于当前显示行） ----
        # 先筛到 PackA（2 行可见）
        cb = p.cb_src
        i_packA = cb.findData("pack:PackA", Qt.UserRole)
        cb.setCurrentIndex(i_packA)
        p.refresh()
        vis = _row_keys(p)
        p.select_all()
        all_checked = all(p.list.item(i).checkState() == Qt.CheckState.Checked
                          for i in range(p.list.count()))
        # 隐藏行（PackB/早期导入/本机）不受影响：切回全部后它们仍未勾选
        p.clear_all()
        none_checked = all(p.list.item(i).checkState() == Qt.CheckState.Unchecked
                           for i in range(p.list.count()))
        check("T-c 全选作用于可见行 / 全不选清空",
              all_checked and none_checked and set(vis) == {"@imported:1001",
                                                            "@imported:1002"},
              "vis=%s" % sorted(vis))

        # ---- T-d 来源筛选 ----
        cb.setCurrentIndex(cb.findData("__local__", Qt.UserRole))
        p.refresh()
        imp_rows = [k for k in _row_keys(p) if str(k).startswith("@imported:")]
        check("T-d1 「仅本机」→ 导入行 0", len(imp_rows) == 0 and p.list.count() == 2,
              "imp=%d rows=%d" % (len(imp_rows), p.list.count()))

        cb.setCurrentIndex(cb.findData("pack:PackA", Qt.UserRole))
        p.refresh()
        keys = set(_row_keys(p))
        check("T-d2 「PackA」→ 只有 PackA 的行",
              keys == {"@imported:1001", "@imported:1002"}, "keys=%s" % sorted(keys))

        # ---- T-e 行尾包名 ----
        cb.setCurrentIndex(0)                 # 全部
        p.refresh()
        txt_by_key = {p.list.item(i).data(Qt.UserRole): p.list.item(i).text()
                      for i in range(p.list.count())}
        has_packA = "(PackA)" in txt_by_key.get("@imported:1001", "")
        has_early = "(早期导入)" in txt_by_key.get("@imported:1004", "")
        check("T-e1 导入行含 (PackA)", has_packA,
              txt_by_key.get("@imported:1001", "")[:80])
        check("T-e2 pack 空显示 (早期导入)", has_early,
              txt_by_key.get("@imported:1004", "")[:80])

        # ---- T-f 勾选优先（纯函数 _resolve_targets） ----
        RT = panels.MusicLibraryPanel._resolve_targets
        loc, imp, mode = RT("@imported:1003",
                            ["@imported:1001", "@imported:1002"],
                            ["@imported:1003"])
        check("T-f1 有勾选 → 操作对象=勾选集（非选中集）",
              mode is True and set(imp) == {"@imported:1001", "@imported:1002"}
              and loc == [], "loc=%s imp=%s mode=%s" % (loc, imp, mode))
        loc2, imp2, mode2 = RT("C:/fake/a.mp3",
                               ["C:/fake/a.mp3", "@imported:1001"],
                               ["@imported:1002"])
        check("T-f2 混合勾选 → 本机/导入各归各类",
              mode2 is True and loc2 == ["C:/fake/a.mp3"]
              and imp2 == ["@imported:1001"],
              "loc=%s imp=%s" % (loc2, imp2))
        loc3, imp3, mode3 = RT("@imported:1003", [],
                              ["@imported:1002", "@imported:1003"])
        check("T-f3 无勾选 → 退回选中集（旧行为）",
              mode3 is False and set(imp3) == {"@imported:1002", "@imported:1003"},
              "imp=%s mode=%s" % (imp3, mode3))
        loc4, imp4, mode4 = RT("C:/fake/a.mp3", [], [])
        check("T-f4 无勾选无选中 → 仅右键项",
              mode4 is False and loc4 == ["C:/fake/a.mp3"] and imp4 == [],
              "loc=%s" % loc4)

        # ---- T-g 「已勾选 N 首」计数 + 右键菜单（勾选态） ----
        p.refresh()
        _set_checked("@imported:1001")
        _set_checked("@imported:1002")
        p._update_lbl()
        has_cnt = "已勾选 2 首" in p.lbl.text()
        check("T-g1 底部计数含「已勾选 2 首」", has_cnt, p.lbl.text())

        # ⚠ PySide6 **不能** monkeypatch 内建 QMenu.exec（C++ 槽优先，改了也没用）
        #   ⇒ 用**子类**覆盖 exec 阻断弹出（Python 层覆盖生效），并捕获菜单项文本。
        class _CapturingMenu(panels.QMenu):
            last = None

            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self._texts = []
                _CapturingMenu.last = self

            def exec(self, *a, **k):
                return None

            def addAction(self, *a, **k):
                act = super().addAction(*a, **k)
                try:
                    self._texts.append(act.text())
                except Exception:                                  # noqa: BLE001
                    pass
                return act

        orig_menu = panels.QMenu
        panels.QMenu = _CapturingMenu
        try:
            p._ctx_menu(panels.QPoint(5, 5))
            texts = list(_CapturingMenu.last._texts) if _CapturingMenu.last else []
        except Exception as e:                                     # noqa: BLE001
            texts = []
            print("    异常：%s" % e)
        finally:
            panels.QMenu = orig_menu
        hit = any("从导入库移除勾选的 2 首" in t for t in texts)
        check("T-g2 勾选态右键菜单=按勾选集（2 首导入）", hit, "菜单=%s" % texts)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main():
    test_migration()
    test_import_pack()
    test_ui()
    print("\n" + "=" * 60)
    if FAILS:
        print("结论：★ %d 项失败：%s" % (len(FAILS), "、".join(FAILS)))
        return 1
    print("结论：✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
