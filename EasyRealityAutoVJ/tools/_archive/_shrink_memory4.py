# -*- coding: utf-8 -*-
"""最后一档精简（不经过 shell，避免反引号被吃掉）。"""
import io
import os

P = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")),
                 "WorkBuddy", "自动VJ软件", ".workbuddy", "memory", "MEMORY.md")

PAIRS = [
    ("- ⚠★ **不能把没接上的功能写进用户文档/界面**：曾把「演出时自动按认歌切曲目」写进界面和手册，"
     "实际没接 ⇒ 必须先查代码确认，再决定写「已支持」还是「还没接」。",
     "- ⚠★ **没接上的功能绝不能写进界面/文档**（曾把「自动按认歌切曲目」写进 UI 和手册，其实没接）"
     "⇒ 先查代码再落笔。"),
    ("- ⚠★ **写补丁/测试的锚点要够长**：`adds` 的插入文本里**别再写一遍锚点行**（会生成两个连续 `def`）；"
     "短锚点会匹配到别的方法（`self._drag = None` 在 `mouseReleaseEvent` 里也有）⇒ **改完必须跑真测试**"
     "（编译过 ≠ 对）。",
     "- ⚠★ **补丁锚点要够长**：`adds` 文本里**别再写一遍锚点行**（会生成两个连续 `def`）；"
     "短锚点会命中别的方法 ⇒ **改完必须跑真测试**（编译过 ≠ 对）。"),
    ("  ⚠ **附件名不允许非 ASCII**；大文件用 `http.client` 流式；含中文 `.bat` 显式 `utf-8` + `chcp 65001`。",
     "  ⚠ **附件名不允许非 ASCII**；大文件用 `http.client` 流式。"),
]

t = io.open(P, encoding="utf-8").read()
before = len(t.encode("utf-8"))
for a, b in PAIRS:
    if t.count(a) != 1:
        print("!! 未命中（跳过）:", a[:44])
        continue
    t = t.replace(a, b)
io.open(P, "w", encoding="utf-8", newline="").write(t)
print("MEMORY.md  %d -> %d 字节" % (before, len(t.encode("utf-8"))))
