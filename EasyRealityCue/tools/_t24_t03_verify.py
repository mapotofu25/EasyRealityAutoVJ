# -*- coding: utf-8 -*-
"""★ T03（24 条）真窗口实测 —— 时间轴新交互（#6/#9/#10/#13/#14/#18）。

跑法：<Cue>\\venv\\Scripts\\python.exe -u <Cue>\\tools\\_t24_t03_verify.py [OUT_JSON]

★ 真 `QApplication`（offscreen 但**真走 `paintEvent()`/`grab()`**）+ `QSignalSpy` + 逐像素差。

⚠ 为什么**不依赖 `MainWindow`**：T03 的改动**全在 `timeline_widget.py`**（纯控件、无 `_interactive`
  短路分支），而 `ui/window.py` 正由 T02 独占改（`window.py` 处于中间态时会拖垮）⇒ 这里直接搭
  **独立 `CueTimeline`** 走真绘制，等价且更稳。判据逐条打印，末尾 `T03_OK` / `T03_FAIL`。
"""
import json
import os
import sys

CUE = r"P:\ERAVJ\EasyRealityCue"
sys.path.insert(0, os.path.join(CUE, "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ERCUE_NO_AUTOSAVE"] = "1"

import logging                                    # noqa: E402
logging.disable(logging.CRITICAL)

import numpy as np                                 # noqa: E402
from PySide6.QtCore import QPoint, Qt              # noqa: E402
from PySide6.QtGui import QContextMenuEvent, QImage, QKeyEvent  # noqa: E402
from PySide6.QtTest import QSignalSpy, QTest       # noqa: E402
from PySide6.QtWidgets import QApplication         # noqa: E402

from ercue.model.cuesheet import (CompLayer, CueItem, CueSheet,   # noqa: E402
                                  Identity, MediaRef, TrackEntry)
from ercue.ui import theme                         # noqa: E402
from ercue.ui.timeline_widget import CueTimeline   # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(CUE, "tools", "_t24_t03_verify.json")
res = {}
app = QApplication.instance() or QApplication([])

_THIS = os.path.abspath(__file__)
tl = CueTimeline()
tl.resize(1100, 260)
te = TrackEntry(track_key="k", identity=Identity(title="T", artist="A",
                                                 duration_sec=300.0, bpm=0.0))
te.identity.ensure_meta_id()
ly = CompLayer(lyr_id="L1", name="L1", z=0)
ly.items.append(CueItem(item_id="i1", clip_id="c1", at_sec=4.0, end_sec=14.0,
                        src_in_sec=0.0, src_out_sec=10.0, speed=1.0).normalized())
ly.items.append(CueItem(item_id="i2", clip_id="c1", at_sec=20.0, end_sec=30.0,
                        src_in_sec=0.0, src_out_sec=10.0, speed=1.0).normalized())
te.composition.layers.append(ly)
sheet = CueSheet(title="T03")
sheet.media.append(MediaRef(clip_id="c1", name="c1.mp4", duration_sec=10.0))
sheet.tracks.append(te)
tl.set_track(te)
tl.set_sheet(sheet)
tl.set_media_paths({"c1": _THIS})
tl.set_view(0.0, 40.0)
tl.show()
for _ in range(6):
    app.processEvents()


def _rgb(img):
    if hasattr(img, "toImage"):
        img = img.toImage()
    im = img.convertToFormat(QImage.Format_ARGB32)
    ww, hh = im.width(), im.height()
    bpl = im.bytesPerLine()
    a = np.frombuffer(bytes(im.constBits()), dtype=np.uint8, count=bpl * hh).reshape(hh, bpl)
    return a[:, :ww * 4].reshape(hh, ww, 4)[:, :, :3].astype(np.int16)


def _count_near(img, hexcol, tol=26):
    """统计与 `hexcol`（如 '#5DCAA5'）接近的像素数。

    ⚠ ARGB32 在内存里是 **BGRA** 字节序 ⇒ 取出的 `[:, :, :3]` 是 **(B, G, R)**，不是 RGB。
    """
    a = _rgb(img)
    c = np.array([int(hexcol[5:7], 16), int(hexcol[3:5], 16), int(hexcol[1:3], 16)], np.int16)
    return int((np.abs(a - c).max(axis=2) <= tol).sum())


y = int(theme.RULER_H + tl.row_h / 2)

# ---- #6 + 第五轮⑥：标尺**点击**定位、**拖动**连续 seek（松手定稿）；都不进平移 ----
sp_seek = QSignalSpy(tl.seek_requested)
v0 = tl.view0
QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(12.0)), 5))
QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(12.0)), 5))
app.processEvents()
click_last = (round(float(sp_seek.at(sp_seek.count() - 1)[0]), 2) if sp_seek.count() else None)
n_click = sp_seek.count()
QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(5.0)), 5))
QTest.mouseMove(tl, QPoint(int(tl._x(9.0)), 5))
QTest.mouseMove(tl, QPoint(int(tl._x(15.0)), 5))
QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(15.0)), 5))
app.processEvents()
drag_last = (round(float(sp_seek.at(sp_seek.count() - 1)[0]), 2) if sp_seek.count() else None)
res["#6_seek"] = {"click_last": click_last,
                  "click_ok": (click_last is not None and abs(click_last - 12.0) < 0.5),
                  "drag_emits": sp_seek.count() - n_click, "drag_last": drag_last,
                  "drag_ok": (drag_last is not None and abs(drag_last - 15.0) < 0.5),
                  "no_pan": (tl._pan is None), "view_unchanged": abs(tl.view0 - v0) < 1e-9}

# ---- #9 剪辑态 hover 把手 ⇒ SizeHorCursor（真鼠标 move + 真光标） ----
xmid1 = int((tl._x(4.0) + tl._x(14.0)) / 2)
QTest.mouseDClick(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid1, y))
app.processEvents()
QTest.mouseMove(tl, QPoint(int(tl._x(4.0)) + 2, y))
app.processEvents()
cur_handle = tl.cursor().shape()
QTest.mouseMove(tl, QPoint(xmid1, y))
app.processEvents()
cur_mid = tl.cursor().shape()
res["#9_cursor"] = {"handle_is_SizeHor": cur_handle == Qt.SizeHorCursor,
                    "mid_is_Arrow": cur_mid == Qt.ArrowCursor}
tl.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))

# ---- #10 点空白 ⇒ picked("","")（且真抓帧不抛异常） ----
tl.set_selected("L1", "i1")
sp_pick = QSignalSpy(tl.picked)
blank_x = int((tl._x(14.0) + tl._x(20.0)) / 2)
_before = _rgb(tl.grab())
QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(blank_x, y))
QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(blank_x, y))
app.processEvents()
_dur = _rgb(tl.grab())
res["#10_blank_clear"] = {"sel_empty": tl.sel == ("", ""),
                          "picked_empty": any(tuple(sp_pick.at(i)) == ("", "")
                                              for i in range(sp_pick.count())),
                          "paint_ok": bool(np.abs(_before - _dur).max() >= 0)}

# ---- #13 空白右键 ⇒ context_menu("","",gpos) + sec_at_global ----
sp_menu = QSignalSpy(tl.context_menu)
tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse,
                                      QPoint(blank_x, y), QPoint(33, 44)))
app.processEvents()
got = [tuple(sp_menu.at(i)) for i in range(sp_menu.count())]
res["#13_blank_menu"] = {"count": len(got) == 1,
                         "item_empty": bool(got and got[0][0] == "" and got[0][1] == "")}
s = tl.sec_at_global(tl.mapToGlobal(QPoint(blank_x, y)))
res["#13_sec_at_global_delta"] = round(abs(s - tl._sec(blank_x)), 4)

# ---- #14 吸附：开 → 拖到邻居 20 附近 ⇒ 吸 20（真画出导引线；邻居不动） ----
tl.set_snap(True)
base = _count_near(tl.grab(), theme.c("accent2"))
move_x = int(tl._x(19.9) + xmid1 - tl._x(4.0))
QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid1, y))
QTest.mouseMove(tl, QPoint(move_x, y))
app.processEvents()
tgt = tl._drag.get("target") if tl._drag else None
with_guide = _count_near(tl.grab(), theme.c("accent2"))
QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(move_x, y))
app.processEvents()
res["#14_snap"] = {"snapped_to_20": (tgt is not None and abs(tgt - 20.0) < 1e-6),
                   "target": (round(float(tgt), 3) if tgt is not None else None),
                   "guide_px_increase": with_guide - base,
                   "neighbor_untouched": (abs(float(ly.items[1].at_sec) - 20.0) < 1e-9)}
tl.set_snap(False)

# ---- #18 再制状态机（真窗口） ----
tl.set_selected("", "")                                # 先清选中（确保走"未选中⇒菜单"分支）
sp_dup = QSignalSpy(tl.duplicate_requested)
sp_menu2 = QSignalSpy(tl.context_menu)
tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(xmid1, y), QPoint(5, 5)))
app.processEvents()
menu_when_unselected = sp_menu2.count()
sel_after_menu = tl.sel
tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(xmid1, y), QPoint(6, 6)))
app.processEvents()
res["#18_dup"] = {"menu_when_unselected": menu_when_unselected,
                  "sel_after_menu": list(sel_after_menu),
                  "dup_on_selected": sp_dup.count(),
                  "dup_mode": tl.duplicate_mode(),
                  "cursor_DragCopy": tl.cursor().shape() == Qt.DragCopyCursor}
tl.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
app.processEvents()
res["#18_esc_exit_mode"] = tl.duplicate_mode()

# ---- 整体：反复真抓帧不抛异常 ----
ok_paint = True
try:
    for _ in range(3):
        tl.grab()
        app.processEvents()
except Exception:                                      # noqa: BLE001
    ok_paint = False
res["real_paint_ok"] = ok_paint

verdict_ok = (
    res["#6_seek"]["click_ok"] and res["#6_seek"]["drag_emits"] >= 2
    and res["#6_seek"]["drag_ok"]
    and res["#6_seek"]["no_pan"] and res["#6_seek"]["view_unchanged"]
    and res["#9_cursor"]["handle_is_SizeHor"] and res["#9_cursor"]["mid_is_Arrow"]
    and res["#10_blank_clear"]["sel_empty"] and res["#10_blank_clear"]["picked_empty"]
    and res["#13_blank_menu"]["count"] and res["#13_blank_menu"]["item_empty"]
    and res["#13_sec_at_global_delta"] < 1e-3
    and res["#14_snap"]["snapped_to_20"] and res["#14_snap"]["guide_px_increase"] > 0
    and res["#14_snap"]["neighbor_untouched"]
    and res["#18_dup"]["menu_when_unselected"] == 1
    and res["#18_dup"]["dup_on_selected"] == 1 and res["#18_dup"]["dup_mode"]
    and res["#18_dup"]["cursor_DragCopy"] and not res["#18_esc_exit_mode"]
    and res["real_paint_ok"]
)
res["verdict"] = "T03_OK" if verdict_ok else "T03_FAIL"

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(res, f, ensure_ascii=False, indent=1)
print(json.dumps(res, ensure_ascii=False, indent=1))
print("DONE")
