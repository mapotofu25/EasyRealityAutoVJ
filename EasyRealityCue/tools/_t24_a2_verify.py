# -*- coding: utf-8 -*-
"""★ A2 复现/回归（24 条 §A2）：拖动视频素材时，其**素材音轨波形必须跟着块一起走**。

跑法：<Cue>\\venv\\Scripts\\python.exe -u <Cue>\\tools\\_t24_a2_verify.py

真 `QApplication` + 真 `grab()` + 逐像素：注入"单尖峰"绿色假波形，量尖峰列在**拖动中**的横移，
与"块的横移（`_preview_dx`）"比对。旧 bug：波形锚在绝对时间轴 ⇒ 尖峰不动（shift≈0）。
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

from ercue.audio.waveform import WaveformData      # noqa: E402
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
                        src_in_sec=0.0, src_out_sec=10.0, speed=1.0).normalized())
te.composition.layers.append(ly)
sheet = CueSheet(title="A2")
sheet.media.append(MediaRef(clip_id="c1", name="c1.mp4", duration_sec=60.0))
sheet.tracks.append(te)
tl.set_track(te)
tl.set_sheet(sheet)
tl.set_media_paths({"c1": _THIS})

n = 300
amp = np.full(n, 0.02, dtype=np.float32)
amp[30] = 1.0                                       # 尖峰落在可见源窗口内（源 6.0s）
wf = WaveformData(columns=n, amp=amp, r=np.zeros(n, np.float32),
                  g=np.ones(n, np.float32), b=np.zeros(n, np.float32), duration_sec=60.0)
tl.set_clip_waves({"c1": wf})
tl.set_show_clip_audio(True)
tl.set_view(0.0, 30.0)
tl.show()
for _ in range(4):
    app.processEvents()

y = int(theme.RULER_H + tl.row_h / 2)


def _np(img):
    im = img.toImage().convertToFormat(QImage.Format_ARGB32)
    w, h = im.width(), im.height()
    bpl = im.bytesPerLine()
    a = np.frombuffer(bytes(im.constBits()), dtype=np.uint8, count=bpl * h).reshape(h, bpl)
    return a[:, :w * 4].reshape(h, w, 4)[:, :, :3].astype(np.int16)


def spike_x(img):
    a = _np(img)
    band = a[theme.RULER_H:theme.RULER_H + int(tl.row_h), :, :]
    g, rr, bb = band[:, :, 1], band[:, :, 0], band[:, :, 2]
    green = (g > 60) & (g > rr + 30) & (g > bb + 30)
    counts = green.sum(axis=0)
    return int(np.argmax(counts)) if counts.max() > 0 else -1


x0 = spike_x(tl.grab())
cx = int((tl._x(4.0) + tl._x(14.0)) / 2)
QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(cx, y))
QTest.mouseMove(tl, QPoint(cx + 150, y))
app.processEvents()
dx = float(tl._preview_dx)
x1 = spike_x(tl.grab())
QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(cx + 150, y))
app.processEvents()

shift = (x1 - x0) if (x0 >= 0 and x1 >= 0) else None
verdict = shift is not None and abs(shift - dx) <= max(6.0, abs(dx) * 0.25)
print("A2: 尖峰 %s→%s，块移动 %.1fpx，波形移动 %spx" % (x0, x1, dx, shift))
print("verdict:", "A2_OK" if verdict else "A2_FAIL")
raise SystemExit(0 if verdict else 1)
