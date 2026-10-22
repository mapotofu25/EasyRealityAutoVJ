# -*- coding: utf-8 -*-
"""端到端验证：真 MainWindow + 真 ClipRow，设置"正在播放"后抓图看有没有金黄底。"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, _HERE)

import cv2                                                  # noqa: E402
import numpy as np                                          # noqa: E402
from PySide6.QtCore import Qt                              # noqa: E402
from PySide6.QtWidgets import QApplication                  # noqa: E402

app = QApplication([])
import media_manager                                        # noqa: E402
import ui_main                                              # noqa: E402

d = tempfile.mkdtemp(prefix="hlreal_")
paths = []
for i, col in enumerate([(40, 40, 40), (200, 60, 60)]):
    p = os.path.join(d, "m%d.png" % i)
    img = np.zeros((48, 72, 3), np.uint8)
    img[:, :] = col
    ok, buf = cv2.imencode(".png", img)
    with open(p, "wb") as f:
        f.write(buf.tobytes())
    paths.append(p)


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

eng = w.engine
lay = eng.layers[0]
lay.clips = [media_manager.MediaItem(p) for p in paths]
lay.visible = True
lay.cur = 0                       # 第 1 个素材"正在播放"（它在可视区内，便于抓图）
w.layers_panel.rebuild()
app.processEvents()

rows = w.layers_panel.rows
print("图层行数 =", len(rows))
grid = rows[0][1]
print("该行素材格数 =", grid.count())
print("lay.clips[0] is item0.data =",
      grid.item(0).data(Qt.UserRole) is lay.clips[0])
print("刷新前 item0 背景 =", grid.item(0).background().color().getRgb())

grid.resize(420, 72)
app.processEvents()
w.layers_panel.refresh_playing()
app.processEvents()
print("刷新后 item0 背景 =", grid.item(0).background().color().getRgb(), "| 可视矩形", grid.visualItemRect(grid.item(0)))

def gold_of(widget, tag):
    im = widget.grab().toImage()
    n = 0
    for x in range(2, im.width() - 2, 2):
        for y in range(2, im.height() - 2, 2):
            c = im.pixelColor(x, y)
            if c.red() > 200 and 150 < c.green() < 235 and c.blue() < 90:
                n += 1
    print('   %-22s %dx%d 金黄像素=%d' % (tag, im.width(), im.height(), n))
    return n


gold = 0
gold += gold_of(grid, 'ClipRow')
gold += gold_of(w.layers_panel, 'layers_panel')
gold += gold_of(w, 'MainWindow')
print('   grid 尺寸', grid.size())
