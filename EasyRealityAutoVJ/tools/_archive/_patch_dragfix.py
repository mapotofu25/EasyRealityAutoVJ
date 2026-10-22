# -*- coding: utf-8 -*-
"""修「编排界面里素材拖不进层」：不要只依赖自定义 mime。

原因：`_ClipList.mimeData()` 在 PySide6 里不一定被 Qt 的拖拽流程采用
（QListWidget 内部模型的 mimeData 才是真正被调用的那个）⇒
`dragEnterEvent` 的 `hasFormat(CLIP_MIME)` 为假 ⇒ **整条拖放被拒绝**，看起来就是"拖不进去"。

修法：拖入时除了看 mime，**再兜底从"拖拽源控件当前选中项"取 clip_id**（`e.source()`），
这条不依赖任何 mime 约定，必通。
"""
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
p = os.path.join(ROOT, "src", "cue_editor.py")
t = io.open(p, encoding="utf-8").read()
orig = t


def rep(old, new, cnt=1):
    global t
    got = t.count(old)
    if got != cnt:
        raise SystemExit("期望 %d 处实得 %d\n---\n%s" % (cnt, got, old[:240]))
    t = t.replace(old, new)


# ① dragEnter / dragMove：不只看 mime，也看拖拽源里有没有可用的选中项
rep('''    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(CLIP_MIME):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if e.mimeData().hasFormat(CLIP_MIME):
            e.acceptProposedAction()
''',
    '''    @staticmethod
    def _clip_from_event(e):
        """从拖放事件里取出 clip_id。

        ★ 两级兜底（2026-09-29 用户报"素材拖不进层"）：
          ① 自定义 mime（`_ClipList.mimeData` 放的）；
          ② **兜底**：直接从**拖拽源控件**（`e.source()`）的当前选中项取 UserRole。
             因为 PySide6 里 QListWidget 真正参与拖拽的是它内部模型的 mimeData，
             我们重写的那个不一定被采用 ⇒ 只看 mime 会**整条拒收**、表现就是"拖不进去"。
        """
        try:
            if e.mimeData().hasFormat(CLIP_MIME):
                cid = bytes(e.mimeData().data(CLIP_MIME)).decode("utf-8", "replace")
                if cid:
                    return cid
        except Exception:                                          # noqa: BLE001
            pass
        try:
            src = e.source()
            it = src.currentItem() if (src is not None and hasattr(src, "currentItem")) else None
            if it is not None:
                return str(it.data(Qt.UserRole) or "")
        except Exception:                                          # noqa: BLE001
            pass
        return ""

    def dragEnterEvent(self, e):
        if self._clip_from_event(e):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if self._clip_from_event(e):
            e.acceptProposedAction()
''')

# ② dropEvent：同样用两级兜底
rep('''        if not e.mimeData().hasFormat(CLIP_MIME):
            return
        try:
            cid = bytes(e.mimeData().data(CLIP_MIME)).decode("utf-8", "replace")
        except Exception:                                          # noqa: BLE001
            return
''',
    '''        cid = self._clip_from_event(e)
''')

io.open(p, "w", encoding="utf-8", newline="").write(t)
print("cue_editor.py 已写入（%+d 字节）" % (len(t) - len(orig)))
