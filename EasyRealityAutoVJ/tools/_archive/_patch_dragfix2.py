# -*- coding: utf-8 -*-
"""修拖放（用行定位，不靠长锚点）：加「从拖拽源当前选中项取 clip_id」的兜底。"""
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
p = os.path.join(ROOT, "src", "cue_editor.py")
ls = io.open(p, encoding="utf-8").read().split("\n")

HELPER = '''    @staticmethod
    def _clip_from_event(e):
        """从拖放事件里取 clip_id（**两级兜底**）。

        ★ 用户 2026-09-29 报「素材拖不进层」：PySide6 里 QListWidget 真正参与拖拽的是
          它**内部模型**的 mimeData，我们重写的 `_ClipList.mimeData` 不一定被采用
          ⇒ 只看 `hasFormat(CLIP_MIME)` 会**整条拒收**，表现就是"拖不进去"。
          所以再加一级：直接从**拖拽源控件**的当前选中项读 UserRole。
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
            it = (src.currentItem() if (src is not None and hasattr(src, "currentItem"))
                  else None)
            if it is not None:
                return str(it.data(Qt.UserRole) or "")
        except Exception:                                          # noqa: BLE001
            pass
        return ""

'''

# ① 在 dragEnterEvent 之前插 helper，并让两个判定改用 helper
i_enter = None
for i, l in enumerate(ls):
    if l.strip().startswith("def dragEnterEvent(self, e):") and i > 40:
        i_enter = i
        break
assert i_enter, "找不到 dragEnterEvent"
ls[i_enter:i_enter] = HELPER.split("\n")
# 重新定位并替换两处判定
n_rep = 0
for i, l in enumerate(ls):
    if l.strip() == "if e.mimeData().hasFormat(CLIP_MIME):" and i > i_enter:
        indent = l[:len(l) - len(l.lstrip())]
        ls[i] = indent + "if self._clip_from_event(e):"
        n_rep += 1
print("dragEnter/dragMove 判定已改：%d 处" % n_rep)

# ② dropEvent：把「mime 判定 + 解码」整段换成 helper
i_if = None
for i, l in enumerate(ls):
    if l.strip() == "if not e.mimeData().hasFormat(CLIP_MIME):" and i > i_enter:
        i_if = i
        break
assert i_if, "找不到 dropEvent 的 mime 判定"
j = None
for i in range(i_if, min(i_if + 12, len(ls))):
    if "cid = bytes(e.mimeData().data(CLIP_MIME))" in ls[i]:
        j = i
        break
assert j, "找不到 cid 解码行"
end = j
while end < len(ls) and ls[end].strip() != "return":
    end += 1
end += 1                      # 连那个 return 一起去掉
indent = ls[j][:len(ls[j]) - len(ls[j].lstrip())]
ls[i_if:end] = [indent + "cid = self._clip_from_event(e)",
                indent + "if not cid:",
                indent + "    return"]
print("dropEvent 已改用 helper（替换 %d 行）" % (end - i_if))

io.open(p, "w", encoding="utf-8", newline="").write("\n".join(ls))
print("已写入 cue_editor.py")
