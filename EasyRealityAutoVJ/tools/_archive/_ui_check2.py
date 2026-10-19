# -*- coding: utf-8 -*-
"""核对「逐拍交替参数」是否随模式显隐（本地检查脚本）"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"P:/AutoVJ/src")
from PySide6.QtWidgets import QApplication          # noqa: E402

app = QApplication([])
import ui_main                                       # noqa: E402

w = ui_main.MainWindow()
w.resize(1500, 900)
w.show()
app.processEvents()
sp = w.settings

L = []


def state(tag):
    L.append("  %-22s mode=%d  逐拍参数可见=%s  整段盒子可见=%s"
             % (tag, sp.mode_combo.currentIndex(),
                sp.beat_pick.isVisible(), sp._beat_box.isVisible()))


state("初始（自动模式）")
for idx, name in ((1, "常规切"), (2, "快切"), (3, "逐拍交替"), (0, "回自动")):
    sp.mode_combo.setCurrentIndex(idx)
    app.processEvents()
    state("切到 " + name)

# 截两张图：自动模式 / 逐拍模式
os.makedirs(r"P:/AutoVJ/_ui_shot", exist_ok=True)
for idx, name in ((0, "10_settings_auto"), (3, "11_settings_beat")):
    sp.mode_combo.setCurrentIndex(idx)
    app.processEvents()
    sp.adjustSize()
    sp.grab().save(r"P:/AutoVJ/_ui_shot/%s.png" % name)
    L.append("  已截图 %s.png" % name)

L.append("")
L.append("设置分区 = %s" % list(sp._sections.keys()))
open(r"P:/AutoVJ/_ui_check2.txt", "w", encoding="utf-8").write("\n".join(L))
print("ok")
