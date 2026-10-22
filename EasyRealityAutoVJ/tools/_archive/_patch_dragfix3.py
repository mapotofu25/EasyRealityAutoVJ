# -*- coding: utf-8 -*-
"""从根上修拖放：给 _ClipList 写自己的 startDrag，保证拖出去的 mime 一定带 clip_id。

为什么：PySide6 里 `QListWidget` 真正参与拖拽的是**内部模型**的 mimeData，
重写视图的 `mimeData()` 不一定被采用 ⇒ 时间轴看到的是 Qt 的默认 mime
⇒ `hasFormat(CLIP_MIME)` 为假 ⇒ **整条拖放被拒**（用户报"素材拖不进层"）。
`startDrag()` 是视图自己的方法，一定被调用 ⇒ 在这里塞我们的 mime 最可靠。
"""
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
p = os.path.join(ROOT, "src", "cue_editor.py")
ls = io.open(p, encoding="utf-8").read().split("\n")

# ① 补 QDrag 导入
for i, l in enumerate(ls):
    if l.startswith("from PySide6.QtGui import ") and "QColor" in l:
        ls[i] = l.rstrip() + "\nfrom PySide6.QtGui import QDrag"
        break
else:
    raise SystemExit("找不到 QtGui 导入行")

# ② 给 _ClipList 加 startDrag
i = None
for k, l in enumerate(ls):
    if l.startswith("class _ClipList(QListWidget):"):
        i = k
        break
assert i is not None, "找不到 _ClipList"
# 插在类的 mimeData 方法之前
j = None
for k in range(i, min(i + 30, len(ls))):
    if ls[k].strip().startswith("def mimeData(self, items):"):
        j = k
        break
assert j is not None, "找不到 mimeData"
BLK = '''    def startDrag(self, actions):
        """★ 让拖出去的 mime **一定**带我们的 clip_id。

        QListWidget 的拖拽走的是内部模型的 mimeData，视图上重写的 `mimeData()`
        不保证被调用 ⇒ 时间轴那边 `hasFormat(CLIP_MIME)` 会为假、整条拖放被拒
        （用户 2026-09-29 报"素材拖不进层"）。`startDrag()` 是视图自己的方法，
        一定被调用，所以在这里自己造 QDrag 最可靠。
        """
        it = self.currentItem()
        if it is None:
            return
        cid = str(it.data(Qt.UserRole) or "")
        if not cid:
            return
        md = QMimeData()
        md.setData(CLIP_MIME, cid.encode("utf-8"))
        md.setText(cid)
        drag = QDrag(self)
        drag.setMimeData(md)
        drag.exec(Qt.CopyAction)

'''
ls[j:j] = BLK.split("\n")

io.open(p, "w", encoding="utf-8", newline="").write("\n".join(ls))
print("已给 _ClipList 加 startDrag + 导入 QDrag")
