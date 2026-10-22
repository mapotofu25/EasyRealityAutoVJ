# -*- coding: utf-8 -*-
"""抓图核对：播放高亮的金边到底画出来没有（离屏 + 真引擎）。

用法：venv/Scripts/python.exe -u tools/_hl_grab.py
产出：_hl_rowN.png（每行素材格的抓图，放大 2 倍另存一份）
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, _HERE)

import cv2                                                       # noqa: E402
from PySide6.QtWidgets import QApplication                       # noqa: E402

app = QApplication([])
import ui_main                                                   # noqa: E402


class _MB:
    @staticmethod
    def warning(*a, **k):
        pass

    @staticmethod
    def information(*a, **k):
        pass


ui_main.QMessageBox = _MB
w = ui_main.MainWindow()
w.save_cfg = lambda *a, **k: None
w.save_layers = lambda *a, **k: None
w.cfg.save = lambda *a, **k: None
w.show()
app.processEvents()
w.toggle_run()
print("已启动引擎，跑 8 秒…")
t0 = time.perf_counter()
while time.perf_counter() - t0 < 8.0:
    app.processEvents()
    time.sleep(0.02)

print()
for i, (hdr, grid) in enumerate(w.layers_panel.rows):
    if grid.count() == 0:
        continue
    im = grid.grab().toImage()
    n = 0
    for x in range(0, im.width(), 2):
        for y in range(0, im.height(), 2):
            c = im.pixelColor(x, y)
            if c.red() > 200 and 150 < c.green() < 235 and c.blue() < 95:
                n += 1
    print("行 %d  grab %dx%d  金黄像素=%d" % (i, im.width(), im.height(), n))
    p1 = os.path.join(_ROOT, "_hl_row%d.png" % i)
    im.save(p1)
    big = cv2.imread(p1)
    if big is not None:
        big = cv2.resize(big, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_NEAREST)
        ok, buf = cv2.imencode(".png", big)
        with open(os.path.join(_ROOT, "_hl_row%d_big.png" % i), "wb") as f:
            f.write(buf.tobytes())
print("已存 _hl_rowN.png / _hl_rowN_big.png")
