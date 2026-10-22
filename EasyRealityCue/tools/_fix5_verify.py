# -*- coding: utf-8 -*-
"""★ 第五轮修复实测（①吸附开关 ②再制接线 ③再制不动原件 ⑥标尺 seek）—— 真窗口 + spy。

跑法：<Cue>\\venv\\Scripts\\python.exe -u <Cue>\\tools\\_fix5_verify.py [OUT_JSON]

真 `QApplication`（offscreen）+ `MainWindow` + `QSignalSpy`、状态/坐标断言。
每项给 `bug`（true=问题仍在）/`fixed` 判定；BEFORE/AFTER 各跑一次对比。
"""
import json
import os
import sys
import tempfile

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

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(CUE, "tools", "_fix5_verify.json")
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
        img[:, (i * 12) % w:((i * 12) % w) + 24, 1] = 255
        for pk in st.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
            c.mux(pk)
    for pk in st.encode():
        c.mux(pk)
    c.close()


tmp = tempfile.mkdtemp(prefix="ercue_fix5_")
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


def _setup():
    """1 层 1 片段（4s~14s，10s 素材）。"""
    w = _new_window()
    w.new_sheet("FIX5", 1920, 1080)
    ids = w.add_media_paths([mp4])
    w.add_track("曲", "a", 300.0, 120.0)
    ly = w._layers()[0]
    it = w.add_cue(ly.lyr_id, ids[0], 4.0)
    it.end_sec = 14.0
    w._rebuild_runtime()
    tl = w.timeline
    tl.resize(1100, 260)
    tl.set_view(0.0, 40.0)
    for _ in range(6):
        app.processEvents()
    return w, ly, it, tl


def _shutdown(w):
    try:
        w.shutdown()
    except Exception:                                  # noqa: BLE001
        pass


# ==========================================================================
# ① 自动吸附开关
# ==========================================================================
def t1_snap_switch():
    w, ly, it, tl = _setup()
    try:
        r = {}
        chk = getattr(w, "chk_snap", None)
        r["switch_exists"] = chk is not None
        r["switch_visible"] = bool(chk is not None and chk.isVisibleTo(w))
        if chk is not None:
            chk.setChecked(True)
            app.processEvents()
            r["on_enables_timeline"] = bool(tl.snap_enabled())
            r["cfg_persisted_true"] = bool(w.cfg.get("snap", None) is True)
            chk.setChecked(False)
            app.processEvents()
            r["off_disables_timeline"] = (tl.snap_enabled() is False)
            r["cfg_persisted_false"] = bool(w.cfg.get("snap", None) is False)
        r["bug"] = not (r["switch_exists"] and r.get("on_enables_timeline")
                        and r.get("off_disables_timeline"))
        res["①_snap_switch"] = r
    finally:
        _shutdown(w)


# ==========================================================================
# ② 再制接线（右键已选中 ⇒ 落点产副本）
# ==========================================================================
def t2_duplicate_wired():
    w, ly, it, tl = _setup()
    try:
        y = int(theme.RULER_H + tl.row_h / 2)
        tl.set_selected(ly.lyr_id, it.item_id)
        app.processEvents()
        n0 = sum(len(l.items) for l in w._layers())
        at0 = float(it.at_sec)
        undo0 = len(w.session.undo)
        xmid = int((tl._x(at0) + tl._x(float(it.end_sec))) / 2)
        tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse,
                                              QPoint(xmid, y), QPoint(9, 9)))
        for _ in range(4):
            app.processEvents()
        n1 = sum(len(l.items) for l in w._layers())
        new_items = [x for l in w._layers() for x in l.items if x.item_id != it.item_id]
        r = {"items_before": n0, "items_after": n1, "added": n1 - n0,
             "dup_mode": tl.duplicate_mode(),
             "undo_delta": len(w.session.undo) - undo0,
             "orig_at_unchanged": abs(float(it.at_sec) - at0) < 1e-9,
             "new_at": (round(float(new_items[0].at_sec), 2) if new_items else None)}
        r["bug"] = (n1 - n0) != 1
        res["②_duplicate_wired"] = r
    finally:
        _shutdown(w)


# ==========================================================================
# ③ 再制不得挪动原件（含"拖动中右键"）
# ==========================================================================
def t3_duplicate_keeps_original():
    w, ly, it, tl = _setup()
    try:
        y = int(theme.RULER_H + tl.row_h / 2)
        tl.set_selected(ly.lyr_id, it.item_id)
        app.processEvents()
        at0 = float(it.at_sec)
        end0 = float(it.end_sec)
        xmid = int((tl._x(at0) + tl._x(end0)) / 2)
        # 用户序列：左键按下 → 拖动（块跟手）→ **再按右键** → 松开左键
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid, y))
        QTest.mouseMove(tl, QPoint(xmid + 150, y))
        for _ in range(3):
            app.processEvents()
        tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse,
                                              QPoint(xmid + 150, y), QPoint(9, 9)))
        for _ in range(3):
            app.processEvents()
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid + 150, y))
        for _ in range(4):
            app.processEvents()
        r = {"orig_at_before": round(at0, 3), "orig_at_after": round(float(it.at_sec), 3),
             "orig_moved": abs(float(it.at_sec) - at0) > 1e-9,
             "items": sum(len(l.items) for l in w._layers())}
        r["bug"] = bool(r["orig_moved"])
        res["③_duplicate_keeps_original"] = r
    finally:
        _shutdown(w)


# ==========================================================================
# ⑥ 标尺：点击 + 拖动 ⇒ seek 被 window 收到并执行
# ==========================================================================
def t6_ruler_seek():
    w, ly, it, tl = _setup()
    try:
        # 点击
        w._set_pos(0.0)
        app.processEvents()
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(12.0)), 5))
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(12.0)), 5))
        for _ in range(4):
            app.processEvents()
        click_pos = float(w._pos)
        # 拖动（标尺上按住左键移动 ⇒ 连续 seek；松手定稿）
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(5.0)), 5))
        QTest.mouseMove(tl, QPoint(int(tl._x(10.0)), 5))
        QTest.mouseMove(tl, QPoint(int(tl._x(15.0)), 5))
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(15.0)), 5))
        for _ in range(4):
            app.processEvents()
        drag_pos = float(w._pos)
        r = {"click_pos": round(click_pos, 2), "click_ok": abs(click_pos - 12.0) < 0.5,
             "drag_pos": round(drag_pos, 2), "drag_ok": abs(drag_pos - 15.0) < 0.5,
             "playhead": (round(float(tl.playhead), 2) if tl.playhead is not None else None)}
        r["bug"] = not (r["click_ok"] and r["drag_ok"])
        res["⑥_ruler_seek"] = r
    finally:
        _shutdown(w)


# ==========================================================================
for fn in (t1_snap_switch, t2_duplicate_wired, t3_duplicate_keeps_original, t6_ruler_seek):
    try:
        fn()
    except Exception as ex:                            # noqa: BLE001
        import traceback
        res[fn.__name__ + "_CRASH"] = traceback.format_exc()

bugs = {k: v["bug"] for k, v in res.items() if isinstance(v, dict) and "bug" in v}
res["_summary_bugs"] = bugs
res["_verdict"] = "ALL_FIXED" if (bugs and not any(bugs.values())) else "BUGS_PRESENT"

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(res, f, ensure_ascii=False, indent=1)
print(json.dumps(res, ensure_ascii=False, indent=1))
print("DONE")
