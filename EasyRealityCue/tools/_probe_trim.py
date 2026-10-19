# -*- coding: utf-8 -*-
"""临时探针 4：③ 根因候选 —— 片段边缘超出视口时，绘制的把手在视口边，但 _hit 用真实边缘。"""
import os
import sys
import tempfile

CUE = r"P:\ERAVJ\EasyRealityCue"
sys.path.insert(0, os.path.join(CUE, "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ERCUE_NO_AUTOSAVE"] = "1"

import logging                                    # noqa: E402
logging.disable(logging.CRITICAL)

import numpy as np                                 # noqa: E402
from PySide6.QtCore import QPoint, Qt              # noqa: E402
from PySide6.QtTest import QTest                   # noqa: E402
from PySide6.QtWidgets import QApplication         # noqa: E402


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


tmp = tempfile.mkdtemp(prefix="ercue_probe4_")
mp4 = os.path.join(tmp, "clip10s.mp4")
_make_mp4(mp4)

from ercue.ui.window import MainWindow             # noqa: E402
from ercue.ui import theme                          # noqa: E402

app = QApplication.instance() or QApplication([])

w = MainWindow(restore_workspace=False, autosave=False)
w.thumbs_enabled = False
w._interactive = False
w.setAttribute(Qt.WA_DontShowOnScreen, True)
w.resize(1280, 860)
w.show()
for _ in range(6):
    app.processEvents()
w.new_sheet("P", 1920, 1080)
ids = w.add_media_paths([mp4])
w.add_track("曲", "a", 300.0, 120.0)
ly = w._layers()[0]
it = w.add_cue(ly.lyr_id, ids[0], 4.0)
it.end_sec = 120.0        # ★ 片段很长（4~120s）⇒ 右端远超视口
w._rebuild_runtime()
tl = w.timeline
tl.resize(1100, 260)
tl.set_view(0.0, 40.0)    # 视口只显示 0~40s ⇒ 片段右端(120s)在视口外
for _ in range(6):
    app.processEvents()

W = tl.width()
print("viewport span=%.0f clip=%.0f~%.0f  x(4)=%.1f x(120)=%.1f  W=%d GUTTER=%d" %
      (tl.view_span, float(it.at_sec), float(it.end_sec), tl._x(4.0), tl._x(120.0),
       W, theme.GUTTER))

y = int(theme.RULER_H + tl.row_h / 2)
xmid = int((tl._x(4.0) + tl._x(30.0)) / 2)
QTest.mouseDClick(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid, y))
for _ in range(4):
    app.processEvents()
print("edit_mode =", tl.edit_mode_item() is it)

tris, movs = [], []
res = []
tl.trimmed.connect(lambda *a: tris.append(a))
tl.moved.connect(lambda *a: movs.append(a))
tl.resized.connect(lambda *a: res.append(a))

# 用户看到"把手"在视口右缘（W-6..W）⇒ 去抓它
xr_visible = W - 3
QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xr_visible, y))
QTest.mouseMove(tl, QPoint(xr_visible - 100, y))
QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xr_visible - 100, y))
for _ in range(5):
    app.processEvents()
print("grab visible right handle: trim=%d move=%d resize=%d  at=%.2f end=%.2f" %
      (len(tris), len(movs), len(res), float(it.at_sec), float(it.end_sec)))

try:
    w.shutdown()
except Exception:
    pass
print("PROBE4_DONE")
