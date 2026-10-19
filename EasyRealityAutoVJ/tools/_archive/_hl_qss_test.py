# -*- coding: utf-8 -*-
"""关键对照：把主题 QSS 挂在 **app 级**（真实运行时的挂法）时，
哪一档改法能让 item.setBackground() 生效。

变体：
  A 完整 QSS
  B 只删基态 ::item（当前改法）
  C 删全部 QListWidget::item* 规则
  D 不挂任何 QSS（对照）
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from PySide6.QtCore import QSize                     # noqa: E402
from PySide6.QtGui import QColor                      # noqa: E402
from PySide6.QtWidgets import (QApplication, QListWidget,  # noqa: E402
                               QListWidgetItem)

app = QApplication([])
import theme                                          # noqa: E402

FULL = theme._qss("dark")


def drop(qss, which):
    out = []
    for line in qss.splitlines():
        s = line.strip()
        if s.startswith("QListWidget::item"):
            if which == "all":
                continue
            if which == "base" and re.match(r"^QListWidget::item\s*\{", s):
                continue
        out.append(line)
    return "\n".join(out)


VARIANTS = [
    ("A 完整 QSS（改之前）", FULL),
    ("B 只删基态 ::item", drop(FULL, "base")),
    ("C 删全部 ::item*", drop(FULL, "all")),
    ("D 不挂 QSS", None),
]


def probe():
    lw = QListWidget()
    lw.setIconSize(QSize(72, 40))
    lw.setGridSize(QSize(80, 50))
    lw.setViewMode(QListWidget.IconMode)
    lw.resize(200, 60)
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


for name, qss in VARIANTS:
    app.setStyleSheet(qss or "")
    app.processEvents()
    c = probe()
    ok = c.red() > 200 and c.green() > 150
    print("%-22s #%02x%02x%02x  %s" % (name, c.red(), c.green(), c.blue(),
                                       "✓ 底色生效" if ok else "✗ 被吃"))
