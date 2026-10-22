# -*- coding: utf-8 -*-
"""★ 第九轮（框选多选）实测 —— **真窗口** + 真 `time.sleep` 造"长按"。

跑法：<Cue>\\venv\\Scripts\\python.exe -u <Cue>\\tools\\_fix10_boxsel_verify.py [OUT_JSON]

覆盖：
  ① 空白左键"立即移动"（<250ms） ⇒ **框选**（多选、属性面板灰化）
  ② 空白左键"长按 ≥250ms 后移动" ⇒ **平移**（view0 变化，不进多选）
  ③ 点空白不拖 ⇒ 取消选择（保留原行为）
  ④ 多选"整组一起移动" ⇒ 相对位置不变、一次 undo
  ⑤ 单击片段 ⇒ 收敛为单选（sel_multi 清空、属性面板恢复可编辑）
  ⑥ 多选整组再制 ⇒ 片段数 +N（各自独立副本）
"""
import json
import os
import sys
import tempfile
import time

CUE = r"<项目根>\EasyRealityCue"
sys.path.insert(0, os.path.join(CUE, "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ERCUE_NO_AUTOSAVE"] = "1"

import logging                                    # noqa: E402
logging.disable(logging.CRITICAL)

import numpy as np                                 # noqa: E402
from PySide6.QtCore import QPoint, Qt              # noqa: E402
from PySide6.QtGui import QContextMenuEvent        # noqa: E402
from PySide6.QtTest import QTest                   # noqa: E402
from PySide6.QtWidgets import QApplication         # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(CUE, "tools", "_fix10_boxsel_verify.json")
res = {}
app = QApplication.instance() or QApplication([])


def _make_mp4(path, frames=100, fps=10, w=320, h=180):
    import av
    c = av.open(path, "w")
    st = c.add_stream("mpeg4", rate=fps)
    st.width, st.height = w, h
    st.pix_fmt = "yuv420p"
    for i in range(frames):
        img = np.zeros((h, w, 3), np.uint8)
        img[:, :, 2] = 180
        for pk in st.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
            c.mux(pk)
    for pk in st.encode():
        c.mux(pk)
    c.close()


tmp = tempfile.mkdtemp(prefix="ercue_fix10_")
mp4 = os.path.join(tmp, "clip10s.mp4")
_make_mp4(mp4)

from ercue.ui.window import MainWindow             # noqa: E402
from ercue.ui import theme                          # noqa: E402


def _new_window():
    try:
        w = MainWindow(restore_workspace=False, autosave=False)
    except TypeError:
        w = MainWindow()
    w.thumbs_enabled = False
    w._interactive = False
    w.setAttribute(Qt.WA_DontShowOnScreen, True)
    w.resize(1280, 860)
    w.show()
    for _ in range(6):
        app.processEvents()
    return w


def _setup_two():
    """1 层 2 片段（at=2 → 2~12、at=15 → 15~25），视口 0~40。"""
    w = _new_window()
    w.new_sheet("FIX10", 1920, 1080)
    ids = w.add_media_paths([mp4])
    w.add_track("曲", "a", 300.0, 120.0)
    ly = w._layers()[0]
    it1 = w.add_cue(ly.lyr_id, ids[0], 2.0)
    it2 = w.add_cue(ly.lyr_id, ids[0], 15.0)
    w._rebuild_runtime()
    tl = w.timeline
    tl.resize(1100, 260)
    tl.set_view(0.0, 40.0)
    for _ in range(6):
        app.processEvents()
    return w, ly, it1, it2, tl


def _setup_three():
    """1 层 3 片段（2~12、14~24、28~38），视口 0~40。框 0.5~24 只选中前两个。"""
    w = _new_window()
    w.new_sheet("FIX10b", 1920, 1080)
    ids = w.add_media_paths([mp4])
    w.add_track("曲", "a", 300.0, 120.0)
    ly = w._layers()[0]
    it1 = w.add_cue(ly.lyr_id, ids[0], 2.0)
    it2 = w.add_cue(ly.lyr_id, ids[0], 14.0)
    it3 = w.add_cue(ly.lyr_id, ids[0], 28.0)
    w._rebuild_runtime()
    tl = w.timeline
    tl.resize(1100, 260)
    tl.set_view(0.0, 40.0)
    for _ in range(6):
        app.processEvents()
    return w, ly, it1, it2, it3, tl


def _row_y(tl, row=0):
    return int(theme.RULER_H + tl.row_h * row + tl.row_h / 2)


def _row_top(tl, row=0):
    return int(theme.RULER_H + tl.row_h * row + 4)


def _row_bot(tl, row=0):
    return int(theme.RULER_H + tl.row_h * row + tl.row_h - 4)


def _n(w):
    return sum(len(l.items) for l in w._layers())


def _shutdown(w):
    try:
        w.shutdown()
    except Exception:                              # noqa: BLE001
        pass


def _box_select(w, tl, x_from_sec, x_to_sec, row=0, hold_ms=0.0):
    """在空白处按下 → (可选长按) → 移动到另一侧 → 松开。返回 (press_ok, ) 无。"""
    x0 = int(tl._x(x_from_sec))
    y0 = _row_top(tl, row)
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x0, y0))
    if hold_ms > 0:
        time.sleep(hold_ms / 1000.0)
    x1 = int(tl._x(x_to_sec))
    y1 = _row_bot(tl, row)
    QTest.mouseMove(tl, QPoint((x0 + x1) // 2, (y0 + y1) // 2))
    QTest.mouseMove(tl, QPoint(x1, y1))
    for _ in range(2):
        app.processEvents()
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x1, y1))
    for _ in range(4):
        app.processEvents()


# ==========================================================================
# ⓪ 橡皮筋"画出来了"（像素证据）：期间抓帧 vs 平时抓帧 ⇒ 框内出现虚线差异
# ==========================================================================
def _np_rgb(img):
    from PySide6.QtGui import QImage
    im = img.toImage().convertToFormat(QImage.Format_ARGB32)
    w, h = im.width(), im.height()
    bpl = im.bytesPerLine()
    a = np.frombuffer(bytes(im.constBits()), dtype=np.uint8, count=bpl * h).reshape(h, bpl)
    return a[:, :w * 4].reshape(h, w, 4)[:, :, :3].astype(np.int16)


def t0_box_pixels():
    w, ly, it1, it2, tl = _setup_two()
    try:
        r = {}
        for _ in range(4):
            app.processEvents()
        base = _np_rgb(tl.grab())                    # 无框
        x0 = int(tl._x(0.5)); y0 = int(theme.RULER_H + 4)
        x1 = int(tl._x(30.0)); y1 = int(theme.RULER_H + tl.row_h - 4)
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x0, y0))
        QTest.mouseMove(tl, QPoint((x0 + x1) // 2, (y0 + y1) // 2))
        QTest.mouseMove(tl, QPoint(x1, y1))
        for _ in range(3):
            app.processEvents()
        r["box_active"] = (tl._box is not None)
        during_img = tl.grab()
        shot = os.path.join(os.path.dirname(OUT), "_fix10_boxselect.png")
        during_img.save(shot)
        r["screenshot"] = shot
        during = _np_rgb(during_img)
        d = np.abs(during - base).max(axis=2)
        r["diff_px"] = int((d > 8).sum())
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x1, y1))
        for _ in range(3):
            app.processEvents()
        r["bug"] = not (r["box_active"] and r["diff_px"] > 0)
        res["⓪_box_drawn"] = r
    finally:
        _shutdown(w)


# ==========================================================================
# ① 立即移动 ⇒ 框选（多选 + 属性灰化）
# ==========================================================================
def t1_box_select_immediate():
    w, ly, it1, it2, tl = _setup_two()
    try:
        r = {}
        # 空白起点（2 之前、15 之前的空白：用 x(0.3) 落在两片段左侧空白；y 在 row0）
        x0 = int(tl._x(0.5))
        y0 = _row_top(tl, 0)
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x0, y0))
        # 立即移动（<250ms）⇒ 进入 box 模式
        x1 = int(tl._x(30.0))
        y1 = _row_bot(tl, 0)
        QTest.mouseMove(tl, QPoint((x0 + x1) // 2, (y0 + y1) // 2))
        QTest.mouseMove(tl, QPoint(x1, y1))
        for _ in range(2):
            app.processEvents()
        r["during_box_active"] = (tl._box is not None)
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x1, y1))
        for _ in range(5):
            app.processEvents()
        sel = tl.multi_selection()
        r["multi_count"] = len(sel)
        r["selected_ids"] = sorted([i for (_l, i) in sel])
        r["props_enabled"] = w.props_panel.isEnabled()
        r["view0_unchanged"] = abs(tl.view0 - 0.0) < 1e-6
        ok = (r["during_box_active"] and len(sel) == 2 and not r["props_enabled"]
              and r["view0_unchanged"])
        r["bug"] = (not ok)
        res["①_box_select_immediate"] = r
    finally:
        _shutdown(w)


# ==========================================================================
# ② 长按 ⇒ 平移（不进多选）
# ==========================================================================
def t2_long_press_pan():
    w, ly, it1, it2, tl = _setup_two()
    try:
        r = {}
        x0 = int(tl._x(35.0))          # 视口右侧空白
        y0 = _row_top(tl, 0)
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x0, y0))
        time.sleep(0.35)               # ★ 长按 > 250ms
        QTest.mouseMove(tl, QPoint(x0 - 120, y0))
        for _ in range(2):
            app.processEvents()
        r["pan_active"] = (tl._pan is not None)
        r["box_active"] = (tl._box is not None)
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x0 - 120, y0))
        for _ in range(4):
            app.processEvents()
        r["view0_after"] = round(float(tl.view0), 3)
        r["multi_count"] = len(tl.multi_selection())
        ok = (r["pan_active"] and not r["box_active"] and r["view0_after"] > 0.5
              and r["multi_count"] == 0)
        r["bug"] = (not ok)
        res["②_long_press_pan"] = r
    finally:
        _shutdown(w)


# ==========================================================================
# ③ 点空白不拖 ⇒ 取消选择
# ==========================================================================
def t3_blank_click_clears():
    w, ly, it1, it2, tl = _setup_two()
    try:
        tl.set_selected(ly.lyr_id, it1.item_id)
        app.processEvents()
        r = {"before": list(tl.sel)}
        xb = int(tl._x(35.0))
        y = _row_y(tl, 0)
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xb, y))
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xb, y))
        for _ in range(4):
            app.processEvents()
        r["after"] = list(tl.sel)
        r["multi_count"] = len(tl.multi_selection())
        ok = (r["after"] == ["", ""] and r["multi_count"] == 0)
        r["bug"] = (not ok)
        res["③_blank_click_clears"] = r
    finally:
        _shutdown(w)


# ==========================================================================
# ④ 多选整组一起移动（相对位置不变、一次 undo）
# ==========================================================================
def t4_group_move():
    w, ly, it1, it2, tl = _setup_two()
    try:
        _box_select(w, tl, 0.5, 30.0)     # 立即移动 ⇒ 选中两个
        sel = tl.multi_selection()
        dur1b = (None if it1.end_sec is None else float(it1.end_sec) - float(it1.at_sec))
        dur2b = (None if it2.end_sec is None else float(it2.end_sec) - float(it2.at_sec))
        r = {"multi_before": len(sel), "at1_before": round(float(it1.at_sec), 3),
             "at2_before": round(float(it2.at_sec), 3)}
        undo0 = len(w.session.undo)
        y = _row_y(tl, 0)
        # 按在 it1 上（多选成员）⇒ 整组拖
        xp = int((tl._x(float(it1.at_sec)) + tl._x(float(it1.end_sec))) / 2)
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xp, y))
        QTest.mouseMove(tl, QPoint(xp + 120, y))
        for _ in range(2):
            app.processEvents()
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xp + 120, y))
        for _ in range(5):
            app.processEvents()
        d1 = float(it1.at_sec) - 2.0
        d2 = float(it2.at_sec) - 15.0
        dur1a = (None if it1.end_sec is None else float(it1.end_sec) - float(it1.at_sec))
        dur2a = (None if it2.end_sec is None else float(it2.end_sec) - float(it2.at_sec))
        r.update({"at1_after": round(float(it1.at_sec), 3),
                  "at2_after": round(float(it2.at_sec), 3),
                  "delta1": round(d1, 3), "delta2": round(d2, 3),
                  "undo_delta": len(w.session.undo) - undo0,
                  "rel_before": round(15.0 - 2.0, 3),
                  "rel_after": round(float(it2.at_sec) - float(it1.at_sec), 3),
                  "dur_kept": (dur1a is not None and dur2a is not None
                               and abs(dur1a - dur1b) < 1e-6 and abs(dur2a - dur2b) < 1e-6)})
        ok = (len(sel) == 2 and d1 > 1.0 and abs(d1 - d2) < 0.05
              and abs(r["rel_after"] - r["rel_before"]) < 0.05
              and r["undo_delta"] == 1 and r["dur_kept"])
        r["bug"] = (not ok)
        res["④_group_move"] = r
    finally:
        _shutdown(w)


# ==========================================================================
# ⑤ 单击片段 ⇒ 收敛为单选（属性恢复可编辑）
# ==========================================================================
def t5_click_collapses_to_single():
    w, ly, it1, it2, tl = _setup_two()
    try:
        _box_select(w, tl, 0.5, 30.0)
        r = {"multi_after_box": len(tl.multi_selection()),
             "props_enabled_in_multi": w.props_panel.isEnabled(),
             "lbl_sel_multi": w.props_panel.lbl_sel.text()}
        y = _row_y(tl, 0)
        xp = int((tl._x(float(it1.at_sec)) + tl._x(float(it1.end_sec))) / 2)
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xp, y))
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xp, y))
        for _ in range(5):
            app.processEvents()
        r["multi_after_click"] = len(tl.multi_selection())
        r["sel_after_click"] = list(tl.sel)
        r["props_enabled_after_click"] = w.props_panel.isEnabled()
        r["lbl_sel_single"] = w.props_panel.lbl_sel.text()
        r["hint_in_multi"] = ("已选中 2 个片段" in r["lbl_sel_multi"]
                              and "不可编辑" in r["lbl_sel_multi"])
        ok = (r["multi_after_box"] == 2 and not r["props_enabled_in_multi"]
              and r["hint_in_multi"]
              and r["multi_after_click"] == 0 and r["sel_after_click"][1] == it1.item_id
              and r["props_enabled_after_click"]
              and "不可编辑" not in r["lbl_sel_single"])
        r["bug"] = (not ok)
        res["⑤_click_collapses_to_single"] = r
    finally:
        _shutdown(w)


# ==========================================================================
# ⑥ 多选整组再制（片段数 +N）
# ==========================================================================
def t6_group_duplicate():
    w, ly, it1, it2, tl = _setup_two()
    try:
        _box_select(w, tl, 0.5, 30.0)
        n0 = _n(w)
        u0 = len(w.session.undo)
        r = {"multi": len(tl.multi_selection()), "items_before": n0}
        # (a) 菜单"复制片段"多选分支：整组复制（各右移 0.5s）+ **一次 undo**
        w._copy_selection()
        for _ in range(5):
            app.processEvents()
        r["items_after_menu_copy"] = _n(w)
        r["undo_delta_menu_copy"] = len(w.session.undo) - u0
        # (b) CDR 整组再制：按成员拖动 → 右键 → 拖 → 松右键 → 松左键
        n1 = _n(w)
        u1 = len(w.session.undo)
        y = _row_y(tl, 0)
        xp = int((tl._x(float(it1.at_sec)) + tl._x(float(it1.end_sec))) / 2)
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xp, y))
        QTest.mouseMove(tl, QPoint(xp + 80, y))
        app.processEvents()
        tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse,
                                              QPoint(xp + 80, y), QPoint(1, 1)))
        QTest.mouseMove(tl, QPoint(xp + 160, y))
        app.processEvents()
        QTest.mouseRelease(tl, Qt.RightButton, Qt.NoModifier, QPoint(xp + 160, y))
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xp + 160, y))
        for _ in range(6):
            app.processEvents()
        r["items_after_cdr"] = _n(w)
        r["undo_delta_cdr"] = len(w.session.undo) - u1
        r["dup_mode_final"] = tl.duplicate_mode()
        ok = (r["multi"] == 2
              and r["items_after_menu_copy"] - n0 == 2 and r["undo_delta_menu_copy"] == 1
              and r["items_after_cdr"] - n1 == 2 and r["undo_delta_cdr"] == 1
              and r["dup_mode_final"] is False)
        r["bug"] = (not ok)
        res["⑥_group_duplicate"] = r
    finally:
        _shutdown(w)


# ==========================================================================
# ⑦ 多选的**五种退出路径**都清多选 + 恢复属性面板
# ==========================================================================
def t7_exit_paths():
    r = {}

    def _stopped(w, tl):
        return (len(tl.multi_selection()) == 0) and w.props_panel.isEnabled()

    # (a) 点"不在多选集里"的单个片段
    w, ly, it1, it2, it3, tl = _setup_three()
    try:
        _box_select(w, tl, 0.5, 24.0)                 # 选中 it1+it2
        r["a_multi_before"] = len(tl.multi_selection())
        y = _row_y(tl, 0)
        x3 = int((tl._x(float(it3.at_sec)) + tl._x(float(it3.end_sec))) / 2)
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x3, y))
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x3, y))
        for _ in range(4):
            app.processEvents()
        r["a_ok"] = _stopped(w, tl) and tl.sel[1] == it3.item_id
        # (b) 点空白
        _box_select(w, tl, 0.5, 24.0)
        xb = int(tl._x(39.0))
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xb, _row_y(tl, 0)))
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xb, _row_y(tl, 0)))
        for _ in range(4):
            app.processEvents()
        r["b_ok"] = _stopped(w, tl) and tl.sel == ("", "")
        # (c) 双击片段 ⇒ 清多选 + 进剪辑态
        _box_select(w, tl, 0.5, 24.0)
        xm = int((tl._x(float(it1.at_sec)) + tl._x(float(it1.end_sec))) / 2)
        QTest.mouseDClick(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xm, _row_y(tl, 0)))
        for _ in range(4):
            app.processEvents()
        r["c_ok"] = _stopped(w, tl) and (tl.edit_mode_item() is it1)
        # 退出剪辑态
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtCore import QEvent
        tl.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        app.processEvents()
        # (d) Esc 清多选
        _box_select(w, tl, 0.5, 24.0)
        r["d_multi_before"] = len(tl.multi_selection())
        tl.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        for _ in range(3):
            app.processEvents()
        r["d_ok"] = _stopped(w, tl)
        # (e) 换码表 ⇒ 清多选
        _box_select(w, tl, 0.5, 24.0)
        r["e_multi_before"] = len(tl.multi_selection())
        w.new_sheet("FIX10_switch", 1920, 1080)
        for _ in range(5):
            app.processEvents()
        r["e_ok"] = (len(tl.multi_selection()) == 0)
        ok = all(r.get(k) for k in ("a_ok", "b_ok", "c_ok", "d_ok", "e_ok"))
        r["bug"] = (not ok)
        res["⑦_exit_paths"] = r
    finally:
        _shutdown(w)


# ==========================================================================
for fn in (t0_box_pixels, t1_box_select_immediate, t2_long_press_pan, t3_blank_click_clears,
           t4_group_move, t5_click_collapses_to_single, t6_group_duplicate, t7_exit_paths):
    try:
        fn()
    except Exception:                              # noqa: BLE001
        import traceback
        res[fn.__name__ + "_CRASH"] = traceback.format_exc()

bugs = {k: v["bug"] for k, v in res.items() if isinstance(v, dict) and "bug" in v}
res["_summary_bugs"] = bugs
res["_verdict"] = "ALL_FIXED" if (bugs and not any(bugs.values())) else "BUGS_PRESENT"

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(res, f, ensure_ascii=False, indent=1)
print(json.dumps(res, ensure_ascii=False, indent=1))
print("DONE")
