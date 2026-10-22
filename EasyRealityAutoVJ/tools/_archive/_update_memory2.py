# -*- coding: utf-8 -*-
"""更新 MEMORY.md（加这轮的长期教训 + 精简，保证不超过注入上限）。"""
import io
import os

P = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")),
                 "WorkBuddy", "自动VJ软件", ".workbuddy", "memory", "MEMORY.md")

t = io.open(P, encoding="utf-8").read()
before = len(t.encode("utf-8"))

# ① 工作方式：补两条长期教训
old = "- ⚠ **多行中文正文一律用 Write 工具落文件**"
new = ("- ⚠★ **不能把没接上的功能写进用户文档/界面**：曾把「演出时自动按认歌切曲目」写进界面和手册，"
       "实际没接 ⇒ 必须先查代码确认，再决定写「已支持」还是「还没接」。\n"
       "- ⚠★ **写补丁/测试的锚点要够长**：`adds` 的插入文本里**别再写一遍锚点行**（会生成两个连续 `def`）；"
       "短锚点会匹配到别的方法（`self._drag = None` 在 `mouseReleaseEvent` 里也有）"
       "⇒ **改完必须跑真测试**（编译过 ≠ 对）。\n"
       "- ⚠ **多行中文正文一律用 Write 工具落文件**")
assert t.count(old) == 1, t.count(old)
t = t.replace(old, new)

# ② 码表小节：补这轮功能，同时精简措辞
old2 = "- 设计文档：项目根 `_码表功能设计.md`。待办：`src_in/src_out`（播放器无 `seek()`）、VDJ 实测。"
new2 = ("- ★ **多曲目**：一张码表按曲目组织；**顶栏「新建…」**起空码表（不落盘，标「（未保存）」），"
        "② 标签页**「＋ 素材…」**把素材加进码表（以前没这个入口）；切曲目走 `_pick_track` ⇒ "
        "**立刻重建运行时**（现场换歌就是换曲目）。\n"
        "- ⚠ **指纹认歌 → 自动切曲目：还没接**（`_bind` 传的是手选那首；引擎不读 `recognized_song_id`；"
        "界面「指纹 id」恒空）。位置只能来自位置源（没位置源 ⇒ 不接管）。\n"
        "- ★ **播放条**：`⏮/▶⏸/循环/位置滑杆`，▶ = 按曲目 BPM 实时推进（33ms，`dt` 上限 0.25s），"
        "自动起「模拟」源；位置统一走 `_set_pos()`。⚠ 没做音频试听（QtMultimedia 这个 venv **没后端插件**）。\n"
        "- ★ **时间轴 PR 式操作**：滚轮上下 / Shift 左右 / **Ctrl 以鼠标为锚缩放** / Alt 行高 / "
        "中键或空白处拖动平移；行高与 `v_off` 参与 `_hit`。\n"
        "- ★ **Solo 对码表图层生效**（旧写法在 solo 判断前就 `continue`，别的层按 S 时它照样盖着）。\n"
        "- 设计文档：项目根 `_码表功能设计.md`。待办：`src_in/src_out`（播放器无 `seek()`）、VDJ 实测。")
assert t.count(old2) == 1, t.count(old2)
t = t.replace(old2, new2)

io.open(P, "w", encoding="utf-8", newline="").write(t)
after = len(t.encode("utf-8"))
print("MEMORY.md  %d -> %d 字节" % (before, after))
