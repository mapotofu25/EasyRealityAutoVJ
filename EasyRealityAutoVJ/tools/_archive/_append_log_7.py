# -*- coding: utf-8 -*-
"""记录本轮的关键教训：① 容器层解码器缓存；② 高亮别挂帧流；③ 性能诊断方法。"""
import io
import os

MEM = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")),
                   "WorkBuddy", "自动VJ软件", ".workbuddy", "memory")

# ---- ① 日志（append）----
ADD = """

---

## 第六轮：码表性能爆表（CPU 12 核 / 磁盘 500MB/s）—— 真根因与修法

用户现场用 `tools/_perf_watch.py` 量到：**CPU 1130~1580%（≈12~16 核）、磁盘持续读 500~750MB/s**、
内存 1.0~1.2GB（不涨）、GPU 12~42% / 显存 1.2GB / 25~86W。之前只有 10~30%。

### ★★ 真根因（两处叠加，第一处才是主因）
1. **`_get_player()` 的通用 LRU 上限 8，容器层永远保护不了任何解码器。**
   淘汰时靠 `cur_path = lay.clips[lay.cur].path` 保护"正在播的那个"，
   而**码表图层是容器、`lay.clips` 是空的** ⇒ `cur_path == ""` ⇒ **谁都保护不了**
   ⇒ 每帧把要用的素材**新建一遍再淘汰**（开容器 = 反复读盘）⇒ 12 核 + 500MB/s。
   **修**：码表图层**单独一份 `lay._cue_players` 缓存**，容量 `max(32, 素材数+16)`，
   只淘汰"当前没活跃"的（`cs_states` 里 active 的绝不淘汰）；卸载码表时关掉它。
2. 我在上一轮加的**独立预览**：25fps + 无条件重算 + 取不到帧就每帧重建播放器。
   **修**：10fps、**只在位置变了或正在播放时重算**、取帧失败退避 2 秒、离开页面关解码器。

### ★ 为什么"播放一下就降不下来"
编辑器点 ▶ 会把**模拟位置源**推给引擎（有可见码表图层时）⇒ 引擎开始合成码表图层，
而位置源**不会自己停**（`is_playing` 保持 True）⇒ 即使停止播放，引擎仍在每帧churn解码器。
⇒ 排查这类"降不下来"的问题，要同时看**编辑器侧**和**引擎侧**两条渲染路径。

### ★ 另一个独立 bug：播放高亮挂在帧流上
图层里"正在用的素材"的金黄底，原来只在 `_update_preview()`（每 6 帧）里刷 ⇒
**换页 / 关预览 / 引擎不产帧时整个不动**。**修**：独立 1 秒定时器
（`self._hl_timer` → `layers_panel.refresh_playing`），与帧流解耦。

### 性能诊断工具（留着常用）
`tools/_perf_watch.py [秒] [间隔]` —— 直接对着**正在运行的实例**打表：
CPU% / 内存MB / **磁盘读MB/s** / 写MB/s / GPU(util,显存,功耗)。
★ 判据经验：**"CPU 高 + 磁盘读持续几百 MB/s" = 在反复开容器**（解码器被反复新建），
而不是算法慢；只 CPU 高、磁盘静 = 纯计算问题。

### 仍未做（上轮审查清单里的高优先项，继续排）
`at_sec` 全为 0（导出跨机按秒会漂）、素材入/出点未接解码器 seek、
码表片段被全局 BPM 变速污染、认歌切曲目丢位置/拿错序号、预览不显示夹在码表层之间的普通图层、
入场/出场只有淡入·硬切真实现。
"""

p = os.path.join(MEM, "2026-09-29.md")
t = io.open(p, encoding="utf-8").read()
if "第六轮" not in t:
    io.open(p, "w", encoding="utf-8", newline="").write(t.rstrip() + ADD)
    print("日志已追加")

# ---- ② MEMORY.md：加两条铁律（同时压掉等量旧文，保持在上限内）----
mp = os.path.join(MEM, "MEMORY.md")
m = io.open(mp, encoding="utf-8").read()
PAIRS = [
    ("- ★★ **`dist/` 被占住（WinError 32）**",
     "- ★★ **容器层（码表图层）的播放器缓存绝不能走通用 LRU-8**：`_get_player` 淘汰时靠 "
     "`lay.clips[lay.cur].path` 保护在用解码器，而容器层 `clips` 是空的 ⇒ **谁都保护不了** ⇒ "
     "每帧新建+淘汰解码器 = **CPU 12 核 / 磁盘 500MB/s**。容器层必须用**自己的缓存**"
     "（`lay._cue_players`，容量按素材数）且只淘汰「非活跃」的。\n"
     "- ★ **自绘/预览类定时器**：绝不无条件每帧重算；只在"输入变了或正在播放"时算；"
     "取不到数据要**退避**（绝不每帧重建播放器）；离开界面就关解码器。\n"
     "- ★ **UI 状态刷新别挂在帧流上**（如播放高亮原挂在 `_update_preview`）⇒ 换页/关预览就不动，"
     "要用独立定时器。\n"
     "- ★ **性能判据**：「CPU 高 **+ 磁盘持续读几百 MB/s**」= 在反复开容器（解码器被反复新建）；"
     "只 CPU 高而磁盘静 = 纯计算。工具：`tools/_perf_watch.py`（对着运行中的实例打表）。\n"
     "- ★★ **`dist/` 被占住（WinError 32）**"),
]
for a, b in PAIRS:
    if m.count(a) == 1:
        m = m.replace(a, b)
    else:
        print("(未命中，跳过)", a[:30])
io.open(mp, "w", encoding="utf-8", newline="").write(m)
print("MEMORY.md 现为 %d 字节" % len(io.open(mp, encoding="utf-8").read().encode("utf-8")))
