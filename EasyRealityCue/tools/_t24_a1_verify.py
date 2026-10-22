# -*- coding: utf-8 -*-
"""★ A1 复现/回归（24 条 §A1）：双击裁剪时，**按下把手（未移动）**那一帧时间轴不得变空。

跑法：<Cue>\\venv\\Scripts\\python.exe -u <Cue>\\tools\\_t24_a1_verify.py

真 `QApplication` + 真 `grab()`（**必须真走 paintEvent**）。
判定：按下后抓帧**不得抛异常**、画面与"未按下"**逐像素一致**（旧 bug：`_trim["preview"]=None`
⇒ `_paint_clip` 抛 `TypeError` 逃出 `paintEvent` ⇒ 整条时间轴不画，松手才回来）。
"""
import os
import sys

CUE = r"<项目根>\EasyRealityCue"
sys.path.insert(0, os.path.join(CUE, "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np                                 # noqa: E402
from PySide6.QtCore import QPoint, Qt              # noqa: E402
from PySide6.QtGui import QImage                   # noqa: E402
from PySide6.QtTest import QTest                   # noqa: E402
from PySide6.QtWidgets import QApplication         # noqa: E402

from ercue.model.cuesheet import (CompLayer, CueItem, CueSheet,   # noqa: E402
                                  Identity, MediaRef, TrackEntry)
from ercue.ui import theme                         # noqa: E402
from ercue.ui.timeline_widget import CueTimeline   # noqa: E402

app = QApplication.instance() or QApplication([])
_THIS = os.path.abspath(__file__)
tl = CueTimeline()
tl.resize(1000, 260)
te = TrackEntry(track_key="k", identity=Identity(title="T", artist="A",
                                                 duration_sec=300.0, bpm=0.0))
te.identity.ensure_meta_id()
ly = CompLayer(lyr_id="L1", name="L1", z=0)
ly.items.append(CueItem(item_id="i1", clip_id="c1", at_sec=4.0, end_sec=14.0,
                        src_in_sec=2.0, src_out_sec=12.0, speed=1.0).normalized())
te.composition.layers.append(ly)
sheet = CueSheet(title="A1")
sheet.media.append(MediaRef(clip_id="c1", name="c1.mp4", duration_sec=100.0))
sheet.tracks.append(te)
tl.set_track(te)
tl.set_sheet(sheet)
tl.set_media_paths({"c1": _THIS})
tl.set_view(0.0, 30.0)
tl.show()
for _ in range(4):
    app.processEvents()

y = int(theme.RULER_H + tl.row_h / 2)
it = ly.items[0]
xmid = int((tl._x(it.at_sec) + tl._x(it.end_sec)) / 2)


def _np(img):
    im = img.toImage().convertToFormat(QImage.Format_ARGB32)
    w, h = im.width(), im.height()
    bpl = im.bytesPerLine()
    a = np.frombuffer(bytes(im.constBits()), dtype=np.uint8, count=bpl * h).reshape(h, bpl)
    return a[:, :w * 4].reshape(h, w, 4)[:, :, :3].astype(np.int16)


QTest.mouseDClick(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid, y))
app.processEvents()
assert tl.edit_mode_item() is it, "双击应进剪辑态"
normal = _np(tl.grab())

err = None
during = None
QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(it.at_sec)) + 2, y))
app.processEvents()
try:
    during = _np(tl.grab())                        # ★ 按下后立刻抓帧（旧 bug：这里抛 TypeError）
except Exception as ex:                            # noqa: BLE001
    err = "%s: %s" % (type(ex).__name__, ex)
QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(it.at_sec)) + 2, y))
app.processEvents()

diff = int(np.abs(normal - during).max()) if during is not None else -1
verdict = (err is None and diff == 0)
print("A1: 按下把手（未移动）抓帧异常=%s  与未按下逐像素 diff=%s" % (err, diff))
print("verdict:", "A1_OK" if verdict else "A1_FAIL")
raise SystemExit(0 if verdict else 1)
