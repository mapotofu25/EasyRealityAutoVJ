# -*- coding: utf-8 -*-
"""定位真凶：到底是「app 级 QSS 里的 ::item 规则」还是「控件自己有一份 QSS」让
item.setBackground() 失效？（ClipRow 自己有 `#clipRow{...}` 样式表。）

变体：
  E app 级完整 QSS + 控件级 `#clipRow{...}`
  F 只有控件级 `#clipRow{...}`（无 app QSS）
  G app 级 QSS 里改成 `#clipRow{...}`（控件级不再设）+ 完整 ::item 规则
  H 什么都没有
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from PySide6.QtCore import QSize                      # noqa: E402
from PySide6.QtGui import QColor                       # noqa: E402
from PySide6.QtWidgets import (QApplication, QListWidget,  # noqa: E402
                               QListWidgetItem)

app = QApplication([])
import theme                                           # noqa: E402

FULL = theme._qss("dark")
ROW = "#clipRow{background:%s;border:1px solid %s;}" % (theme.V("row"), theme.V("border"))


def probe(app_qss, widget_qss):
    app.setStyleSheet(app_qss or "")
    app.processEvents()
    lw = QListWidget()
    lw.setObjectName("clipRow")
    lw.setIconSize(QSize(72, 40))
    lw.setGridSize(QSize(80, 50))
    lw.setViewMode(QListWidget.IconMode)
    lw.resize(200, 60)
    if widget_qss:
        lw.setStyleSheet(widget_qss)
    it = QListWidgetItem("")
    it.setSizeHint(QSize(76, 46))
    lw.addItem(it)
    it.setBackground(QColor(255, 205, 0, 235))
    lw.show()
    app.processEvents()
    img = lw.grab().toImage()
    best = None
    for x in range(6, 74, 4):
        for y in range(6, 46, 4):
            c = img.pixelColor(x, y)
            if best is None or c.red() > best.red():
                best = c
    lw.close()
    return best


CASES = [
    ("E app级完整 + 控件级 #clipRow", FULL, ROW),
    ("F 只有控件级 #clipRow", None, ROW),
    ("G app级(含 #clipRow) + 无控件级", FULL + "\n" + ROW, None),
    ("H 都没有", None, None),
]

for name, aq, wq in CASES:
    c = probe(aq, wq)
    ok = c.red() > 200 and c.green() > 150
    print("%-32s #%02x%02x%02x  %s" % (name, c.red(), c.green(), c.blue(),
                                       "✓ 底色生效" if ok else "✗ 被吃"))
