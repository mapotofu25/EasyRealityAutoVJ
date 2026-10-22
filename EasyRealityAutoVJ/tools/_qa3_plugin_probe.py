# -*- coding: utf-8 -*-
"""探测：普通 Qt Widgets 应用（本项目的用法）会加载 plugins/generic/qtuiotouchplugin 吗？
QT_DEBUG_PLUGINS=1 的输出会被外层脚本重定向后 grep。"""
import os
import sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QMainWindow, QComboBox, QCheckBox
from PySide6.QtCore import QTimer
app = QApplication.instance() or QApplication([])
w = QMainWindow()
cb = QComboBox(w); cb.addItems(["a", "b"])
ck = QCheckBox("x", w)
w.show()
QTimer.singleShot(1500, app.quit)
app.exec()
print("RAN_OK generic_loaded=%s" % ("qtuiotouchplugin" in repr(os.environ)))
