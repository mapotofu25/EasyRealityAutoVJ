# -*- coding: utf-8 -*-
"""★ 第六轮修复实测（① CDR 式再制流程 / ③ 拖把手改长短生效）—— **真窗口** + 状态断言。

跑法：<Cue>\\venv\\Scripts\\python.exe -u <Cue>\\tools\\_fix6_verify.py [OUT_JSON]

真 `QApplication`（offscreen）+ `MainWindow` + `QSignalSpy` 式记录 + 模型落值断言。
每项给 `bug`（true=问题仍在）/明细；并给总体 `_verdict`。
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

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(CUE, "tools", "_fix6_verify.json")
res = {}
app = QApplication.instance() or QApplication([])


def _make_mp4(path, frames=100, fps=10, w=320, h=180):
    """10s 素材（10fps × 100 帧）。"""
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


tmp = tempfile.mkdtemp(prefix="ercue_fix6_")
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


def _setup(view_span=40.0, clip_end=None):
    """1 层 1 片段（4s 起，10s 素材 ⇒ end=14s）。"""
    w = _new_window()
    w.new_sheet("FIX6", 1920, 1080)
    ids = w.add_media_paths([mp4])
    w.add_track("曲", "a", 300.0, 120.0)
    ly = w._layers()[0]
    it = w.add_cue(ly.lyr_id, ids[0], 4.0)
    if clip_end is not None:
        it.end_sec = float(clip_end)
    w._rebuild_runtime()
    tl = w.timeline
    tl.resize(1100, 260)
    tl.set_view(0.0, view_span)
    for _ in range(6):
        app.processEvents()
    return w, ly, it, tl


def _row_y(tl, row=0):
    return int(theme.RULER_H + tl.row_h * row + tl.row_h / 2)


def _n_items(w):
    return sum(len(l.items) for l in w._layers())


def _shutdown(w):
    try:
        w.shutdown()
    except Exception:                              # noqa: BLE001
        pass


# ==========================================================================
# ① CDR 式再制流程
# ==========================================================================
def t1_cdr_duplicate():
    w, ly, it, tl = _setup()
    try:
        tl.set_selected(ly.lyr_id, it.item_id)
        app.processEvents()
        y = _row_y(tl)
        at0 = float(it.at_sec)                    # 4.0
        end0 = float(it.end_sec)                  # 14.0
        n0 = _n_items(w)
        moved, dups = [], []
        tl.moved.connect(lambda *a: moved.append(a))
        tl.duplicate_requested.connect(lambda *a: dups.append(a))

        xmid = int((tl._x(at0) + tl._x(end0)) / 2)
        grab = xmid - tl._x(at0)

        # 1) 左键按在片段上
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid, y))
        # 2) 拖到 8.0
        x8 = int(tl._x(8.0) + grab)
        QTest.mouseMove(tl, QPoint(x8, y))
        for _ in range(2):
            app.processEvents()
        r = {"press_dup_mode": tl.duplicate_mode(), "after_drag_dup_mode": tl.duplicate_mode(),
             "dups_after_drag": len(dups)}
        # 3) 按右键 ⇒ 进复制模式（**不产份**）
        tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(x8, y), QPoint(1, 1)))
        for _ in range(2):
            app.processEvents()
        r["after_rightclick_dup_mode"] = tl.duplicate_mode()
        r["after_rightclick_dups"] = len(dups)
        r["cursor_dragcopy"] = (tl.cursor().shape() == Qt.DragCopyCursor)
        # 4) 拖到 12.0
        x12 = int(tl._x(12.0) + grab)
        QTest.mouseMove(tl, QPoint(x12, y))
        for _ in range(2):
            app.processEvents()
        r["during_at_unchanged"] = (abs(float(it.at_sec) - at0) < 1e-9)
        # 5) 松右键（无动作）
        QTest.mouseRelease(tl, Qt.RightButton, Qt.NoModifier, QPoint(x12, y))
        for _ in range(2):
            app.processEvents()
        r["after_rightrelease_dup_mode"] = tl.duplicate_mode()
        # 6) 松左键 ⇒ 提交
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x12, y))
        for _ in range(5):
            app.processEvents()

        new_items = [x for l in w._layers() for x in l.items if x.item_id != it.item_id]
        r.update({
            "orig_at": round(float(it.at_sec), 3),
            "orig_end": round(float(it.end_sec), 3),
            "moved_emits": len(moved),
            "dup_emits": len(dups),
            "items_before": n0, "items_after": _n_items(w),
            "new_item_at": (round(float(new_items[0].at_sec), 3) if new_items else None),
            "dup_mode_final": tl.duplicate_mode(),
            "cursor_arrow_final": (tl.cursor().shape() == Qt.ArrowCursor),
        })
        ok = (abs(r["orig_at"] - 4.0) < 1e-9 and r["moved_emits"] == 0
              and r["items_after"] - r["items_before"] == 1
              and r["new_item_at"] is not None and abs(r["new_item_at"] - 12.0) < 0.35
              and r["dup_mode_final"] is False and r["cursor_arrow_final"]
              and r["after_rightclick_dups"] == 0 and r["after_rightclick_dup_mode"]
              and r["after_rightrelease_dup_mode"])
        r["bug"] = (not ok)
        res["①_cdr_duplicate"] = r
    finally:
        _shutdown(w)


# ==========================================================================
# ①-b 旧语义（未拖动的普通右键 ⇒ 立即复制一份，且自动取消）
# ==========================================================================
def t1b_legacy_duplicate_autocancel():
    w, ly, it, tl = _setup()
    try:
        tl.set_selected(ly.lyr_id, it.item_id)
        app.processEvents()
        y = _row_y(tl)
        n0 = _n_items(w)
        xmid = int((tl._x(float(it.at_sec)) + tl._x(float(it.end_sec))) / 2)
        tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(xmid, y), QPoint(2, 2)))
        for _ in range(4):
            app.processEvents()
        r = {"items_before": n0, "items_after": _n_items(w),
             "dup_mode_after": tl.duplicate_mode(),
             "cursor_arrow": (tl.cursor().shape() == Qt.ArrowCursor)}
        # 再右键 ⇒ 再复制一份，仍不残留复制模式
        #（注意：首次复制后 `_copy_item` 会把选中切到副本 ⇒ 这里**重新选中原件**再右键，
        #  且点在原片段独占区域（5s 处），避免命中副本）
        tl.set_selected(ly.lyr_id, it.item_id)
        app.processEvents()
        x5 = int(tl._x(5.0))
        tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(x5, y), QPoint(3, 3)))
        for _ in range(4):
            app.processEvents()
        r["items_after2"] = _n_items(w)
        r["dup_mode_after2"] = tl.duplicate_mode()
        ok = (r["items_after"] - r["items_before"] == 1 and r["dup_mode_after"] is False
              and r["cursor_arrow"] and r["items_after2"] - r["items_after"] == 1
              and r["dup_mode_after2"] is False)
        r["bug"] = (not ok)
        res["①b_legacy_duplicate_autocancel"] = r
    finally:
        _shutdown(w)


# ==========================================================================
# ③ 剪辑态拖右把手 ⇒ end 真的变小 + 可 Ctrl+Z 撤销
# ==========================================================================
def t3_trim_right_handle():
    w, ly, it, tl = _setup()
    try:
        y = _row_y(tl)
        xmid = int((tl._x(float(it.at_sec)) + tl._x(float(it.end_sec))) / 2)
        QTest.mouseDClick(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid, y))
        for _ in range(4):
            app.processEvents()
        r = {"edit_mode": (tl.edit_mode_item() is it)}
        end0 = float(it.end_sec)
        xr = int(tl._x(float(it.end_sec))) - 2
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xr, y))
        QTest.mouseMove(tl, QPoint(xr - 100, y))
        for _ in range(3):
            app.processEvents()
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xr - 100, y))
        for _ in range(5):
            app.processEvents()
        end1 = float(it.end_sec)
        r.update({"end_before": round(end0, 3), "end_after": round(end1, 3),
                  "end_shrank": (end1 < end0 - 0.3)})
        # Ctrl+Z 撤销
        w.undo()
        for _ in range(5):
            app.processEvents()
        # ⚠ 撤销会**重建模型对象** ⇒ 必须按 item_id 重新取"活的"片段再读（不能读旧引用）
        it_u = next((x for l in w._layers() for x in l.items if x.item_id == it.item_id), None)
        end2 = float(it_u.end_sec) if it_u is not None else float("nan")
        r["end_after_undo"] = round(end2, 3)
        r["undo_restored"] = (it_u is not None and abs(end2 - end0) < 0.05)
        ok = r["edit_mode"] and r["end_shrank"] and r["undo_restored"]
        r["bug"] = (not ok)
        res["③_trim_right_handle"] = r
    finally:
        _shutdown(w)


# ==========================================================================
# ③-b 根因场景：片段右端**在视口外** ⇒ 视口缘把手必须可抓（trim 而非 move）
# ==========================================================================
def t3b_trim_offscreen_edge():
    w, ly, it, tl = _setup(view_span=40.0, clip_end=120.0)   # 片段 4~120s，视口 0~40s
    try:
        y = _row_y(tl)
        W = tl.width()
        r = {"x_of_end_off": round(tl._x(float(it.end_sec)), 1), "W": W,
             "edge_offscreen": (tl._x(float(it.end_sec)) > W)}
        xmid = int((tl._x(4.0) + tl._x(30.0)) / 2)
        QTest.mouseDClick(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid, y))
        for _ in range(4):
            app.processEvents()
        r["edit_mode"] = (tl.edit_mode_item() is it)
        at0, end0 = float(it.at_sec), float(it.end_sec)
        moved, tris = [], []
        tl.moved.connect(lambda *a: moved.append(a))
        x_handle = W - 3
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x_handle, y))
        QTest.mouseMove(tl, QPoint(x_handle - 100, y))
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x_handle - 100, y))
        for _ in range(5):
            app.processEvents()
        end1 = float(it.end_sec)
        r.update({"moved_emits": len(moved), "at_after": round(float(it.at_sec), 3),
                  "end_after": round(end1, 3), "end_changed": (abs(end1 - end0) > 0.3),
                  "at_unchanged": (abs(float(it.at_sec) - at0) < 1e-6)})
        ok = (r["edit_mode"] and r["edge_offscreen"] and r["moved_emits"] == 0
              and r["end_changed"] and r["at_unchanged"])
        r["bug"] = (not ok)
        res["③b_trim_offscreen_edge"] = r
    finally:
        _shutdown(w)


# ==========================================================================
for fn in (t1_cdr_duplicate, t1b_legacy_duplicate_autocancel,
           t3_trim_right_handle, t3b_trim_offscreen_edge):
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
