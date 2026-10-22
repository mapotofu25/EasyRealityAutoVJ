# -*- coding: utf-8 -*-
"""把「码表」小节整体重写（合并重复、压到上限以内）。"""
import io
import os

P = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")),
                 "WorkBuddy", "自动VJ软件", ".workbuddy", "memory", "MEMORY.md")

NEW_CUE = """## 码表 / 时间码（`.ercue`）
- 包 = zip（`cuesheet.json` + 可选 `media/`）；`format_version`/`min_app_version`/`requires_features`
  三件套（**只提示不阻断**）。模块：`cuesheet.py`（模型+IO）、`cuesheet_runtime.py`（解释器）、
  `position_source.py`（probe/start 分离，VdjSource 纯 Python OSC）、`cue_editor.py`（编排界面）。
- **码表图层 = 容器**：`Layer.is_cuesheet` + 子层替身 `_CueSubLayer`（`clips` 用真实 `MediaItem`）。
  ⚠ `VideoPlayer._active` 默认 False，必须 `_cue_set_active()` 每帧激活。
  - 图层头**青绿** + **码表徽标 `btnCue`**（未绑定=橙虚线警示；已绑定=码表名，点一下进编排界面）；
    容器**隐藏 α/锁/混合**（对容器无意义）。★ **Solo 对它也生效**（旧写法在 solo 判断前就 `continue`）。
  - ★★ **不受画面效果影响**（同 Kv，用户明确要求）：`engine._cuesheet_on()` ⇒ `_apply_postfx` 整条跳过、
    `_apply_color` 把 `kv_on` 顶到 1.0。
- ★ **编排界面是 PR 式**：上 = 左标签页（①曲目 ②码表包·素材 ③片段属性）+ 右**节目监视器** `CuePreview`
  （`latest_frame()`、8fps、可见才刷）+ **播放条**；下 = **时间轴全宽**。
  - **曲目 = 绑定音乐**（`_ti` + `_track()`，不再 `tracks[0]`）；**顶栏「新建…」**起空码表（不落盘、
    标「（未保存）」）；② 页**「＋ 素材…」**把素材加进码表（以前没这入口）；
    **切曲目 = 立刻重建运行时**（现场换歌就是换曲目）；「导入音频…」用 mutagen 读标签/时长。
  - **播放条** `⏮/▶⏸/循环/位置滑杆`：▶ 按曲目 BPM 实时推进（33ms，`dt` 上限 0.25s），自动起「模拟」源；
    位置统一走 `_set_pos()`。⚠ 没做音频试听（QtMultimedia 这个 venv **没后端插件**）。
  - **时间轴操作（PR 式）**：滚轮上下 / Shift 左右 / **Ctrl 以鼠标为锚缩放** / Alt 行高 /
    中键或空白处拖动平移；标尺自适应 `_ruler_plan(pxb)`（数字 ≥30px 才画、竖线 ≥2px）。
- ⚠ **指纹认歌 → 自动切曲目：还没接**（`_bind` 传的是手选那首；引擎不读 `recognized_song_id`；
  界面「指纹 id」恒空）。位置只能来自位置源（没位置源 ⇒ 不接管）。
- 内嵌素材**解在包旁边 `<包名>.media/`**（不可写才退回 app_base_dir）。**用户要求：东西不要放 C 盘**。
- `probe()` 必须绑 `0.0.0.0`；示例码表必须**自包含**（`embed=True`）。
- 设计文档：项目根 `_码表功能设计.md`。待办：`src_in/src_out`（播放器无 `seek()`）、VDJ 实测。
"""

t = io.open(P, encoding="utf-8").read()
before = len(t.encode("utf-8"))
i = t.find("\n## 码表 / 时间码")
assert i > 0
j = t.find("\n## 输出（Spout / NDI）")
assert j > i
t = t[:i + 1] + NEW_CUE + t[j + 1:]
io.open(P, "w", encoding="utf-8", newline="").write(t)
print("MEMORY.md  %d -> %d 字节" % (before, len(t.encode("utf-8"))))
