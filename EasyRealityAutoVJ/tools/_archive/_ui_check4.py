# -*- coding: utf-8 -*-
"""核对：勾选框选中态是否「内缩一圈」；画面效果标签页是否还有多余横线"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"P:/AutoVJ/src")
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QCheckBox   # noqa: E402
from PySide6.QtCore import Qt                                                 # noqa: E402

app = QApplication([])
import theme                                                                  # noqa: E402
import ui_main                                                                # noqa: E402

theme.apply(app, "dark")

w = ui_main.MainWindow()
w.show()
app.processEvents()

OUT = r"P:/AutoVJ/_ui_shot"
os.makedirs(OUT, exist_ok=True)

# 1) 画面效果标签页（看还有没有横线）
tabs = w.settings._fx_tabs
tabs.parent().adjustSize()
pm = tabs.grab()
pm.save(os.path.join(OUT, "20_fx_tabs.png"))
print("fx_tabs", pm.width(), pm.height())
# 放大 3 倍便于肉眼看清
pm.scaled(pm.width() * 3, pm.height() * 3, Qt.KeepAspectRatio,
          Qt.FastTransformation).save(os.path.join(OUT, "21_fx_tabs_x3.png"))

# 2) 勾选框：选中 / 未选中 并排，放大 6 倍
box = QWidget()
v = QVBoxLayout(box)
c1 = QCheckBox("已勾选"); c1.setChecked(True)
c2 = QCheckBox("未勾选")
v.addWidget(c1); v.addWidget(c2)
box.adjustSize()
pm2 = box.grab()
pm2.scaled(pm2.width() * 6, pm2.height() * 6, Qt.KeepAspectRatio,
           Qt.FastTransformation).save(os.path.join(OUT, "22_checkbox_x6.png"))
print("checkbox", pm2.width(), pm2.height())
print("ok")
