# -*- coding: utf-8 -*-
"""回归测试：图层高亮**只在过渡进行中**才点亮"上一个素材"。

用户 2026-10-01 反馈「不在逐拍为什么有两个框」：引擎过渡结束后不会把 `prev_idx`
重置成 -1，所以只要无条件把它算进去，就会永远有两个金框。

判据：`trans_active=False` ⇒ 1 个金框；`trans_active=True` ⇒ 2 个金框。
用法：venv/Scripts/python.exe -u tools/_test_hl_prev.py
"""
import os
import sys
import shutil
import atexit
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

import cv2                                                       # noqa: E402
import numpy as np                                               # noqa: E402
from PySide6.QtCore import Qt                                    # noqa: E402
from PySide6.QtWidgets import QApplication                       # noqa: E402

app = QApplication([])
import media_manager                                             # noqa: E402
import ui_main                                                   # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


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

d = tempfile.mkdtemp(prefix="autovj_hlprev_")
atexit.register(shutil.rmtree, d, True)     # 新铁律：mkdtemp 必须清理（退出/ sys.exit 时回收）
paths = []
for i, col in enumerate([(30, 30, 30), (200, 60, 60)]):
    p = os.path.join(d, "m%d.png" % i)
    img = np.zeros((48, 72, 3), np.uint8)
    img[:, :] = col
    ok, buf = cv2.imencode(".png", img)
    with open(p, "wb") as f:
        f.write(buf.tobytes())
    paths.append(p)

lay = w.engine.layers[0]
lay.clips = [media_manager.MediaItem(p) for p in paths]
lay.visible = True
lay.cur = 0
lay.prev_idx = 1
lay.trans_active = False
w.layers_panel.rebuild()
app.processEvents()
grid = w.layers_panel.rows[0][1]


def gold_count():
    w.layers_panel.refresh_playing()
    app.processEvents()
    n = 0
    for k in range(grid.count()):
        if grid.item(k).background().color().getRgb()[:3] == (255, 205, 0):
            n += 1
    return n


n1 = gold_count()
check("非过渡状态：只有 1 个金框（cur）", n1 == 1, "实测 %d 个" % n1)

lay.trans_active = True
n2 = gold_count()
check("过渡进行中：2 个金框（cur + prev）", n2 == 2, "实测 %d 个" % n2)

lay.trans_active = False
lay.prev_idx = -1
n3 = gold_count()
check("prev_idx=-1 时仍然 1 个", n3 == 1, "实测 %d 个" % n3)

print()
print("结果：%s" % ("全部通过 ✓" if not FAILS else "失败 %d 项 ✗ %s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
