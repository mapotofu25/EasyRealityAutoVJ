# -*- coding: utf-8 -*-
"""追加今日日志（第二轮）。"""
import io
import os

P = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")),
                 "WorkBuddy", "自动VJ软件", ".workbuddy", "memory", "2026-09-29.md")

ADD = '''

---

## ★★ 00:40~ 第二轮（用户 4 条）

### ① 「绑定到/新建图层/绑定 这个东西怎么还在这」
从**顶栏撤掉**，挪进「1 码表包」页（分组框「这张码表用在哪个图层」）。
⚠ `cb_layer` 控件**留着但隐藏**（`_refresh` 要填它、`_bind` 要读它）；可见的是同内容的
`cb_layer_view`，`_refresh` 里同步、`_bind` 读它。

### ② 「素材怎么添加到层上面 没做？」
做了两条路（都走同一个 `_append_cue()`）：
- **双击「3 素材」里的素材 = 在播放头位置给当前层加一个 cue**
- **把素材拖到时间轴某一层上** ⇒ 落在拖放那一拍（按当前吸附档位对齐）
  · 私有 mime `CLIP_MIME`（`_ClipList.mimeData` 只放 clip_id）＋ `CueTimeline.dropEvent`
  · 素材列表右键 = 在文件管理器里定位 / 从码表里去掉

### ③ ★★ 「码表编排是一个独立的系统，和演出预览没关系；码表预览的是**歌和画面对不对得上**」
用户纠正得对：以前编辑器的"预览"画的是 `engine.latest_frame()`（= 演出输出那一路），
**要先建图层、绑码表、起位置源**才能看到画面 —— 全是编排阶段不该有的前置条件。
新增 **`src/cue_preview.py`**：
- `CuePreviewCanvas`：**自己合成**（读 `CueRuntime.update(pos)` 解出的激活片段，
  按 xform / crop / opacity / blend / trans_p 画到自己的画布上），**完全不碰引擎**。
- `CueAudio`：**PyAV 解码 + soundcard 播放**（专用线程 + 有界队列 24 块，满了丢）。
  ⚠ `PySide6.QtMultimedia` 在本机**没有后端插件**，放不出声 ⇒ 只能走这条路。
- ▶ = 播这首歌的音频 + 播放头**跟着音频走**（`_play_tick` 优先用 `audio.position()`；
  没音频才退回挂钟推进）⇒ 能"边听边看"对不对得上。
- **不需要图层 / 不需要位置源**就能试（编排阶段就该这样）。
- 位置换算用**离线网格锚点**（`_audio_anchor_sec()`）：音乐真正的小节/乐句头才会落在 ◆ 线上。
- `cfg["cue"]["track_audio"]` 记住「导入音频…」选的路径；找不到就按文件名/曲名去曲库找。

### ④ 「CPU 占用会越来越高」
写了 `tools/_cpu_growth_probe.py`（真实 MainWindow 跑起来，定时采样 CPU/内存/线程/QObject/QImage/
引擎容器/日志行）。离线跑 150 秒：**线程 23→23、QObject 660→660、QImage 36→38（噪声）** ——
**没有增长**。⚠ 读数第一版 `ctypes` 写错（CPU/RSS 都是 0）⇒ 改用 `os.times()`。
⚠ 用户任务管理器截图里 **`EasyRealityAutoVJ.exe (2)` = 开了两个实例**（这本身就会 CPU 翻倍），
已提醒他确认是不是残留实例。

### ★ 又踩了同一个坑（MEMORY 里明明写着）
`adds` 的插入文本里**又写了一遍锚点行** ⇒ 生成两个连续 `def _back_to_show` ⇒ IndentationError。
**这条规矩必须当机械动作执行：插入块的最后一行绝不能是锚点本身。**
'''
t = io.open(P, encoding="utf-8").read()
if "00:40~ 第二轮" not in t:
    io.open(P, "w", encoding="utf-8", newline="").write(t.rstrip() + ADD)
    print("已追加")
else:
    print("已存在")
