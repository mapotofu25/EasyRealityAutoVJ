# -*- coding: utf-8 -*-
"""离屏截取主界面，用于 UI 布局评审（本地工具，不进仓库）。

用法：venv\Scripts\python.exe tools\_ui_shot.py [宽 高]
产物：P:\AutoVJ\_ui_shot\*.png
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"P:\AutoVJ\src")

from PySide6.QtWidgets import QApplication          # noqa: E402
from PySide6.QtCore import QTimer                    # noqa: E402

OUT = r"P:\AutoVJ\_ui_shot"
W = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
H = int(sys.argv[2]) if len(sys.argv) > 2 else 900

app = QApplication(sys.argv)
import ui_main                                        # noqa: E402

win = ui_main.MainWindow()
win.resize(W, H)
win.show()

steps = []


def save(widget, name):
    os.makedirs(OUT, exist_ok=True)
    pm = widget.grab()
    p = os.path.join(OUT, name + ".png")
    pm.save(p)
    print("  %-24s %dx%d  →  %s" % (name, pm.width(), pm.height(), p))


def run():
    # 1) 整体主窗
    save(win, "01_main")
    # 2) 右侧设置面板（完整高度）
    try:
        sp = win.settings
        sp.adjustSize()
        save(sp, "02_settings")
        for t, sec in getattr(sp, "_sections", {}).items():
            print("     节:", t)
    except Exception as e:                                     # noqa: BLE001
        print("  设置面板截图失败:", e)
    # 3) 预览面板
    try:
        save(win.preview_panel, "03_preview")
    except Exception as e:                                     # noqa: BLE001
        print("  预览面板截图失败:", e)
    # 4) 素材库（中央区）
    for attr in ("library_panel", "media_panel", "center", "tabs"):
        w = getattr(win, attr, None)
        if w is not None:
            try:
                save(w, "04_" + attr)
            except Exception as e:                             # noqa: BLE001
                print("  %s 截图失败: %s" % (attr, e))
    app.quit()


QTimer.singleShot(6000, run)      # 等界面稳定（模型加载/缩略图等）
app.exec()
print("完成")
