# -*- coding: utf-8 -*-
"""让"正在播放"的素材格**一眼可见**。

实测（tools/_hl_diag.py + 现场截图）：路径比对修好之后，高亮其实是**正常工作**的
（图层 2/3 各 2 格亮着 —— 正在播的 + 过渡中的上一个）。问题是**看不出来**：
缩略图的 iconSize = (CELL_W-8) × (CELL_H-10)，几乎铺满格子，金黄底只在四周露出
约 4px 一圈 —— 满屏深色/彩色缩略图里根本注意不到（用户 2026-09-30 反馈"没修好"）。

改法：保留金黄底（透明 PNG 会透出来），**再在缩略图之上画一道 3px 金边**
（自绘 delegate），任何素材上都醒目。用数据 role 传递"在播"标记，不依赖背景刷子。
"""
import io
import os
import sys

ROOT = r"P:\AutoVJ"
PAT = []


def rep(rel, old, new, cnt=1):
    PAT.append((rel, old, new, cnt))


# 1) 补导入
rep("src/panels.py",
    "from PySide6.QtCore import Qt, QSize, Signal, QMimeData, QTimer, QPoint",
    "from PySide6.QtCore import Qt, QSize, Signal, QMimeData, QTimer, QPoint, QRectF")
rep("src/panels.py",
    "    QFileDialog, QMessageBox, QPlainTextEdit,\n",
    "    QFileDialog, QMessageBox, QPlainTextEdit, QStyledItemDelegate,\n")

# 2) 加 PLAY_ROLE 与 delegate（放在 ClipRow 之前）
rep("src/panels.py",
    '''class ClipRow(QListWidget):
    """一个图层的素材行：横向排列、内部拖动排序、可接收素材库拖入"""
''',
    '''# "这个格子正在播"的标记 role（给 _PlayBorderDelegate 画金边用）
PLAY_ROLE = Qt.UserRole + 7


class _PlayBorderDelegate(QStyledItemDelegate):
    """在"正在播放"的素材格上，**压在缩略图之上**再画一道金边。

    为什么光靠 `item.setBackground` 不够（用户 2026-09-30 说"没修好"）：
    缩略图几乎铺满整格，金黄底只在四周露出 ~4px，一屏深色素材里根本看不见。
    金边画在最后 ⇒ 无论素材多亮多花，都能一眼认出哪一格在播。
    """

    def paint(self, painter, opt, idx):
        super().paint(painter, opt, idx)
        if not idx.data(PLAY_ROLE):
            return
        painter.save()
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            r = QRectF(opt.rect).adjusted(1.0, 1.0, -1.5, -1.5)
            pen = QPen(QColor(255, 205, 0, 255))
            pen.setWidth(3)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(r, 5, 5)
        finally:
            painter.restore()


class ClipRow(QListWidget):
    """一个图层的素材行：横向排列、内部拖动排序、可接收素材库拖入"""
''')

# 3) ClipRow 装上 delegate
rep("src/panels.py",
    """        self.setObjectName("clipRow")
""",
    """        self.setObjectName("clipRow")
        # ★ 播放高亮的金边靠它画（见 _PlayBorderDelegate）
        self.setItemDelegate(_PlayBorderDelegate(self))
""")

# 4) refresh_playing 打标记
rep("src/panels.py",
    """                if _key(pth) and _key(pth) in playing:
                    it.setBackground(QColor(255, 205, 0, 235))   # 正在播放：金黄打底
                    it.setForeground(QColor(25, 25, 25))
""",
    """                if _key(pth) and _key(pth) in playing:
                    it.setBackground(QColor(255, 205, 0, 235))   # 正在播放：金黄打底
                    it.setData(PLAY_ROLE, True)                  # ↑ delegate 再压一道金边
                    it.setForeground(QColor(25, 25, 25))
""")
rep("src/panels.py",
    """                else:
                    it.setBackground(QColor(0, 0, 0, 0))
                    it.setForeground(QColor(230, 230, 230))
""",
    """                else:
                    it.setBackground(QColor(0, 0, 0, 0))
                    it.setData(PLAY_ROLE, False)
                    it.setForeground(QColor(230, 230, 230))
""")


def main():
    for i, (rel, old, new, cnt) in enumerate(PAT, 1):
        s = io.open(os.path.join(ROOT, rel), encoding="utf-8").read()
        n = s.count(old)
        if n != cnt:
            print("✗ 第 %d 处命中 %d 次（应为 %d）：%s" % (i, n, cnt, old.strip()[:70]))
            return 1
    for rel, old, new, cnt in PAT:
        p = os.path.join(ROOT, rel)
        s = io.open(p, encoding="utf-8").read()
        io.open(p, "w", encoding="utf-8", newline="\n").write(s.replace(old, new))
    print("✓ panels.py 已改（%d 处）" % len(PAT))
    import py_compile
    py_compile.compile(os.path.join(ROOT, "src", "panels.py"), doraise=True)
    print("语法 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
