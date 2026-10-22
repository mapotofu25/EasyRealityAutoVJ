#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""QA 独立复核 —— B. UI 行为 + D. 边界/可疑点（**自写**，offscreen 真构造面板）。

用 config.app_base_dir monkeypatch 到临时目录 + 自造 2 个包（不碰生产数据）。
  B0   构造：本机 2 + 导入 4（PackA 2 / PackB 1 / 早期 1）
  B1   勾选 2 行 → 切排序 → 勾选仍在（★反向：打桩 _checked_keys→空 ⇒ 丢）
  B2   「全选」只勾**可见**行（先按来源筛掉一半再全选；切回全部后隐藏行未勾）
  B3   来源三档：仅本机 / PackA / 全部，行集正确
  B4   行文本：(PackA) / (早期导入) / 📦 前缀
  B5   勾选优先：勾 2 + Ctrl 另选 1 → 删除目标 = 勾选那 2（菜单文案 + _resolve_targets）
  B6   勾选后删除：数据删掉 + 刷新后勾选不残留；未删且勾选的行仍保留
  B7   底部标签：有勾选含「已勾选 N 首」；无勾选不含
  D1   键唯一性：本机路径与 @imported 伪路径不可能撞
  D2   refresh 内部不 cascade（重填来源下拉不应触发二次 refresh）
  D3   3000 行：refresh / 勾选保留 的耗时（本地假 meta）
  D4   包名 = 内置选项名（「全部」）→ 下拉出现同名两项，但按键选择仍正确

跑法：QT_QPA_PLATFORM=offscreen python tools/_qa_ui.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

RESULTS = []


def check(tag, ok, detail=""):
    print("  [%s] %-6s %s" % ("PASS" if ok else "FAIL", tag, detail))
    RESULTS.append((tag, ok, detail))
    return ok


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
    """PackA 2 首 / PackB 1 首 / 早期导入 1 首。"""
    import imported_lib as IL
    conn = IL.open_meta(base)
    try:
        IL.add_song(conn, 1001, {"file_name": "a1.mp3", "title": "A1",
                                 "artist": "aa", "genres": ["House"],
                                 "grid": {"bpm": 124, "dur": 200}}, pack="PackA")
        IL.add_song(conn, 1002, {"file_name": "a2.mp3", "title": "A2",
                                 "artist": "bb", "genres": ["House"],
                                 "grid": {"bpm": 126, "dur": 210}}, pack="PackA")
        IL.add_song(conn, 1003, {"file_name": "b1.mp3", "title": "B1",
                                 "artist": "cc", "genres": ["Techno"],
                                 "grid": {"bpm": 138, "dur": 220}}, pack="PackB")
        IL.add_song(conn, 1004, {"file_name": "e1.mp3", "title": "E1",
                                 "artist": "dd", "genres": [], "grid": {}},
                    pack="")
        conn.commit()
    finally:
        conn.close()


import panels as _P                                   # noqa: E402

Qt = _P.Qt


def _keys(p):
    return [p.list.item(i).data(Qt.UserRole) for i in range(p.list.count())]


def _set_checked(p, key, on=True):
    st = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
    for i in range(p.list.count()):
        it = p.list.item(i)
        if it.data(Qt.UserRole) == key:
            it.setCheckState(st)
            return True
    return False


def _menu_texts(p, pos):
    """用子类覆盖 exec 拦截弹菜单，收集菜单项文本。"""
    class _Cap(_P.QMenu):
        last = None

        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._t = []
            _Cap.last = self

        def exec(self, *a, **k):
            return None

        def addAction(self, *a, **k):
            act = super().addAction(*a, **k)
            try:
                self._t.append(act.text())
            except Exception:                                     # noqa: BLE001
                pass
            return act

    orig = p.__class__.__mro__  # noqa
    import panels
    old = panels.QMenu
    panels.QMenu = _Cap
    try:
        p._ctx_menu(pos)
        return list(_Cap.last._t) if _Cap.last else []
    finally:
        panels.QMenu = old


def main():
    from PySide6.QtWidgets import QApplication, QMessageBox
    from PySide6.QtCore import QPoint
    app = QApplication.instance() or QApplication([])

    import config
    base = tempfile.mkdtemp(prefix="qa_ui_")
    try:
        _make_imported(base)
        config.app_base_dir = lambda: base
        import panels
        Qt = panels.Qt

        p = panels.MusicLibraryPanel(_FakeMain(["C:/fake/a.mp3", "C:/fake/b.mp3"]))
        p.resize(1000, 640)
        p.refresh()

        n_imp = sum(1 for k in _keys(p) if str(k).startswith("@imported:"))
        check("B0", p.list.count() == 6 and n_imp == 4,
              "rows=%d imp=%d" % (p.list.count(), n_imp))

        # ---------- B1 跨 refresh 保留 ----------
        _set_checked(p, "@imported:1001")
        _set_checked(p, "@imported:1002")
        p.cb_sort.setCurrentIndex(3)           # BPM 低→高 → 触发 refresh
        now = set(p._checked_keys())
        check("B1", now == {"@imported:1001", "@imported:1002"},
              "切排序后 checked=%s" % sorted(now))

        # ★反向：打桩 _checked_keys 返回空 ⇒ 勾选丢失
        orig_ck = panels.MusicLibraryPanel._checked_keys
        panels.MusicLibraryPanel._checked_keys = lambda self: set()
        try:
            _set_checked(p, "@imported:1001")
            p.refresh()
            lost = len(p._checked_keys())
        finally:
            panels.MusicLibraryPanel._checked_keys = orig_ck
        check("B1-rev", lost == 0, "打桩后勾选数=%d（应 0）" % lost)
        p.cb_sort.setCurrentIndex(0)
        p.refresh()

        # ---------- B2 全选只勾可见 ----------
        cb = p.cb_src
        # 先切到 PackA（2 行可见）
        cb.setCurrentIndex(cb.findData("pack:PackA", Qt.UserRole))
        p.refresh()
        vis = set(_keys(p))
        p.select_all()
        vis_all_checked = all(p.list.item(i).checkState() == Qt.CheckState.Checked
                              for i in range(p.list.count()))
        # 切回全部：被隐藏的行不应被勾上
        cb.setCurrentIndex(cb.findData("__all__", Qt.UserRole))
        p.refresh()
        checked = set(p._checked_keys())
        check("B2", vis == {"@imported:1001", "@imported:1002"}
              and vis_all_checked and checked == vis,
              "可见=%s 全选后勾选=%s（应只这 2）" % (sorted(vis), sorted(checked)))
        p.clear_all()

        # ---------- B3 来源三档 ----------
        res3 = {}
        for lab, key in (("仅本机", "__local__"), ("PackA", "pack:PackA"),
                         ("全部", "__all__")):
            cb.setCurrentIndex(cb.findData(key, Qt.UserRole))
            p.refresh()
            res3[lab] = set(_keys(p))
        ok3 = (res3["仅本机"] == {"C:/fake/a.mp3", "C:/fake/b.mp3"}
               and res3["PackA"] == {"@imported:1001", "@imported:1002"}
               and len(res3["全部"]) == 6)
        check("B3", ok3, "仅本机=%d PackA=%d 全部=%d"
              % (len(res3["仅本机"]), len(res3["PackA"]), len(res3["全部"])))

        # ---------- B4 行文本 ----------
        cb.setCurrentIndex(cb.findData("__all__", Qt.UserRole))
        p.refresh()
        t = {p.list.item(i).data(Qt.UserRole): p.list.item(i).text()
             for i in range(p.list.count())}
        has_pre = all("📦" in t[k] for k in t if str(k).startswith("@imported:"))
        check("B4", "(PackA)" in t.get("@imported:1001", "")
              and "(早期导入)" in t.get("@imported:1004", "")
              and "(PackB)" in t.get("@imported:1003", "") and has_pre,
              "1001=%r" % t.get("@imported:1001", "")[:70])

        # ---------- B5 勾选优先 ----------
        p.clear_all()
        p.refresh()
        _set_checked(p, "@imported:1001")
        _set_checked(p, "@imported:1002")
        # Ctrl 另选 1003（只选中、不勾选）
        for i in range(p.list.count()):
            if p.list.item(i).data(Qt.UserRole) == "@imported:1003":
                p.list.item(i).setSelected(True)
        checked = [x.data(Qt.UserRole) for x in p._checked_items()]
        selected = [x.data(Qt.UserRole) for x in p.list.selectedItems()]
        loc, imp, mode = panels.MusicLibraryPanel._resolve_targets(
            "@imported:1003", checked, selected)
        # 真弹菜单（拦截 exec）看文案
        pos = QPoint(5, 5)
        it0 = p.list.item(0)
        r = p.list.visualItemRect(it0)
        if not r.isNull():
            pos = r.center()
        texts = _menu_texts(p, pos)
        hit = any("从导入库移除勾选的 2 首" in x for x in texts)
        check("B5", mode is True and set(imp) == {"@imported:1001", "@imported:1002"}
              and set(selected) == {"@imported:1003"} and hit,
              "mode=%s imp=%s selected=%s 菜单含勾选文案=%s"
              % (mode, sorted(imp), sorted(selected), hit))

        # ---------- B6 勾选后删除 ----------
        orig_q = QMessageBox.question
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Yes)
        try:
            # 勾选 1001/1002/1003，但只删 1001/1002 → 1003 应保留勾选
            _set_checked(p, "@imported:1003")
            p._remove_imported(["@imported:1001", "@imported:1002"])
        finally:
            QMessageBox.question = orig_q
        c = sqlite3.connect(os.path.join(base, "imported_meta.db"))
        nrow = c.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
        gone = c.execute("SELECT COUNT(*) FROM songs WHERE song_id IN (1001,1002)"
                         ).fetchone()[0]
        c.close()
        keys = set(_keys(p))
        still_checked = set(p._checked_keys())
        no_residual = (("@imported:1001" not in keys)
                       and ("@imported:1002" not in keys))
        check("B6", nrow == 2 and gone == 0 and no_residual
              and still_checked == {"@imported:1003"},
              "库剩 %d 行；删掉 1001/1002=gone%s；残留勾选=%s（应只 1003）"
              % (nrow, gone, sorted(still_checked)))
        p.clear_all()

        # ---------- B7 底部标签 ----------
        p.refresh()
        _set_checked(p, "@imported:1003")
        p._update_lbl()
        lbl_on = p.lbl.text()
        p.clear_all()
        p._update_lbl()
        lbl_off = p.lbl.text()
        check("B7", ("已勾选 1 首" in lbl_on) and ("已勾选" not in lbl_off),
              "有勾选=%r / 无勾选=%r" % (lbl_on[-24:], lbl_off[-24:]))

        # ---------- D1 键唯一性 ----------
        # 本机路径（Windows）不可能以 '@imported:' 开头（':' 是非法路径字符）
        fake_local = [p_ for p_ in _keys(p)
                      if not str(p_).startswith("@imported:")]
        imp_keys = [k for k in _keys(p) if str(k).startswith("@imported:")]
        okd1 = (len(set(imp_keys)) == len(imp_keys)
                and not (set(fake_local) & set(imp_keys))
                and all(":" not in os.path.splitdrive(str(k))[1] for k in fake_local))
        check("D1", okd1, "本机键=%s 导入键=%s（无交集、无重复）"
              % (fake_local[:2], imp_keys[:3]))

        # ---------- D2 refresh 内不 cascade ----------
        calls = {"n": 0}
        orig_refresh = p.refresh

        def counted():
            calls["n"] += 1
            return orig_refresh()
        p.refresh = counted
        calls["n"] = 0
        p.refresh()                            # 单次 refresh（走 wrapper）
        cascade = calls["n"]
        p.refresh = orig_refresh
        check("D2", cascade == 1,
              "单次 refresh 内部实际调用 refresh 次数=%d（应 1，无 cascade）" % cascade)

        # ---------- D3 3000 行性能 ----------
        base2 = tempfile.mkdtemp(prefix="qa_perf_")
        try:
            import imported_lib as IL
            conn = IL.open_meta(base2)
            for i in range(3000):
                IL.add_song(conn, 100000 + i,
                            {"file_name": "p%d.mp3" % i, "title": "T%d" % i,
                             "genres": ["House"],
                             "grid": {"bpm": 120 + (i % 40), "dur": 200}},
                            pack="Pack%d" % (i % 3))
            conn.commit()
            conn.close()
            config.app_base_dir = lambda: base2
            p2 = panels.MusicLibraryPanel(_FakeMain([]))
            p2.resize(1000, 640)
            t0 = time.time()
            p2.refresh()
            t_first = time.time() - t0
            # 勾 200 行后切排序 → 观察保留耗时
            for i in range(min(200, p2.list.count())):
                p2.list.item(i).setCheckState(Qt.CheckState.Checked)
            t0 = time.time()
            p2.cb_sort.setCurrentIndex(2)      # 触发 refresh + 恢复勾选
            t_resort = time.time() - t0
            kept = len(p2._checked_keys())
            check("D3", t_first < 3.0 and t_resort < 3.0 and kept == 200,
                  "3000 行：首刷 %.0fms / 切排序(含保留200勾选) %.0fms / 保留=%d"
                  % (t_first * 1000, t_resort * 1000, kept))
        finally:
            shutil.rmtree(base2, ignore_errors=True)
            config.app_base_dir = lambda: base

        # ---------- D4 包名 == 内置选项名 ----------
        base3 = tempfile.mkdtemp(prefix="qa_clash_")
        try:
            import imported_lib as IL
            conn = IL.open_meta(base3)
            IL.add_song(conn, 2001, {"file_name": "z.mp3", "title": "Z",
                                     "grid": {"bpm": 120}}, pack="全部")
            conn.commit()
            conn.close()
            config.app_base_dir = lambda: base3
            p3 = panels.MusicLibraryPanel(_FakeMain(["C:/fake/x.mp3"]))
            p3.refresh()
            cb3 = p3.cb_src
            i_all = cb3.findData("__all__", Qt.UserRole)
            i_pk = cb3.findData("pack:全部", Qt.UserRole)
            txt_all = cb3.itemText(i_all)
            txt_pk = cb3.itemText(i_pk)
            # 选 pack:全部 → 只显示那首导入歌（不含本机）
            cb3.setCurrentIndex(i_pk)
            p3.refresh()
            rows = set(_keys(p3))
            # 选 __all__ → 两首都在
            cb3.setCurrentIndex(i_all)
            p3.refresh()
            rows_all = set(_keys(p3))
            check("D4", i_pk >= 0 and i_all >= 0
                  and rows == {"@imported:2001"} and len(rows_all) == 2
                  and txt_all == txt_pk,
                  "两个下拉项显示文本相同(%r==%r) 但按键选择正确：pack:全部→%s, 全部→%d 行"
                  % (txt_all, txt_pk, sorted(rows), len(rows_all)))
        finally:
            shutil.rmtree(base3, ignore_errors=True)
            config.app_base_dir = lambda: base
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print("\n" + "=" * 62)
    bad = [t for t, ok, _ in RESULTS if not ok]
    if bad:
        print("结论：%d 项失败：%s" % (len(bad), "、".join(bad)))
        return 1
    print("结论：全部通过（共 %d 项）" % len(RESULTS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
