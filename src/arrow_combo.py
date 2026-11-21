# -*- coding: utf-8 -*-
"""带稳定右侧小箭头的下拉框。

Qt 默认的 `QComboBox::down-arrow` 在自定义 QSS / 深色主题下经常被画没，
用户看到的就是"一个没有箭头的框"。这里直接在下拉框右侧居中画一个小三角，
并用 QSS 关掉默认箭头，保证单屏/多屏的配置下拉都有同一个箭头。
"""
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QPainter, QPolygon
from PySide6.QtWidgets import QComboBox


class ArrowComboBox(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        # 默认箭头关掉，统一由 paintEvent 画；drop-down 区域仍保留点击。
        self.setStyleSheet(
            "QComboBox::drop-down{border:none;width:18px;}"
            "QComboBox::down-arrow{image:none;}")

    def paintEvent(self, ev):
        super().paintEvent(ev)
        try:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing, True)
            col = self.palette().color(self.foregroundRole())
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            w, h = 8, 5
            x = self.width() - 15
            y = (self.height() - h) // 2
            p.drawPolygon(QPolygon([QPoint(x, y), QPoint(x + w, y),
                                    QPoint(x + w // 2, y + h)]))
            p.end()
        except Exception:                                          # noqa: BLE001
            pass
