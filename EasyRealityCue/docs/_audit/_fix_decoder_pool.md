# 视频预览卡顿（第三轮）· 有界解码器池 —— 同素材多层不同入点不再互相 seek

**范围**：只改 `src/ercue/media/frames.py` + `src/ercue/ui/preview.py`
（+ 新增 `tools/_fix_decoder_pool_verify.py`、微调 `tools/_fix_preview_perf2_verify.py`）。
**未动** `src/ercue/audio/waveform.py`、`src/ercue/ui/timeline_widget.py`（判据⑥已核）；**未触碰** `P:\ERAVJ\AutoVJ\`。

**结论（一句话）**：本轮报告 §四 那条遗留限制——「同一素材切几段、分放多层（VJ 典型用法），各层入点不同
却挤在一个解码器上互相 `seek`，三层场景只有 ~15 fps」——是用户"预览还是非常卡"的**另一半原因**。
本次把它做成**有界解码器池**：`_src` 的键从 `path` 变成 **`(path, lane)`**（lane = 图层/播放线），
同素材不同入点**各持独立解码器**，同素材同入点（叠层）**仍共用**；池用 **每路径上限 3 + 全局上限 6 + LRU**
严格封顶，池满且都在用时**退化为"别名共用"**（绝不无上限新建、也不逐出重建）。
**目标场景 3 层异入点 15.3 → 32.2 fps**（改前 18.1 → 改后 29.1 fps，0/10/20s）；
"5 层同素材异入点"极端场景**解码器数恒 ≤ 3**（内存有界）。

跑法：
```
P:\ERAVJ\EasyRealityCue\venv\Scripts\python.exe  tools/_fix_decoder_pool_verify.py    # 判据 ①~⑥ 复跑
```
证据留存：`docs/_audit/_fix_decoder_pool_verify.txt`（本轮判据原始输出）。

---

## 一、根因与设计目标

### 1.1 现象 / 根因
架构纪律 A1「**每路径复用同一个解码器**」在"单层 / 多层同入点"时是最优的（省内存、省打开）。
但 **VJ 的典型用法是"同一个素材切几段、分放多层"**（背景铺一段、前景放另一段）。
此时各层的 `play_sec` 不同，却共享同一个容器 ⇒ `frame_play` 的"顺序解码前进"条件（前进 ≤ 1.0s、
且要求"只比当前帧前进"）**反复不成立** ⇒ 每拍退化成"回退/大跳 ⇒ seek"，**N 层就是每拍 N 次 seek**，
彼此把对方的解码位置冲掉 ⇒ 三层场景实测 **15 fps**（`Balls visual@110 (19).mov`, 4K, 960px 画布）。

### 1.2 设计目标（对齐任务书硬要求）
1. **池必须有界**：每路径上限 + 全局上限，**LRU** 淘汰；4K 解码器吃内存 ⇒ 先**实测**再定上限。
2. **同素材不同入点 ⇒ 不互相干扰**（各持解码器）；**同素材同入点 ⇒ 可共用**（那是同一条播放线）。
3. **绝不放任资源增长**：池满且都在用时**必须有明确策略**（不许"无上限新建"）。
4. **关闭 / 切轨全部释放**（不泄漏解码器或句柄，沿用 `CueFrameSource.close()` 语义）。
5. **既有行为不退化**：单层、多层同素材同入点、上一轮的 `out_size` 缩放路径全部保持。

---

## 二、池的设计

### 2.1 键 = `(path, lane)`（而不是 `(path, 入点)`）
```python
def _key(path, lane): return (path, lane)      # lane=None ⇒ (path, None) ⇒ 每路径一个解码器
```
**lane 由预览按"图层"给出**（`PreviewCanvas._build_lane_map` → `lane = lyr_id`）。理由：
- 合成器契约只认 **`(clip_id, play_sec)`**（`compositor.compose(size, states, frame_lookup)`），
  **根本拿不到"哪一层"**；而 `_compose` 手里有 `states`（含 `lyr_id`），于是由它造一张
  **`(clip_id, round(play_sec,3)) → lane`** 表交给取帧回调。
- 为什么不直接用 **`(path, 入点)`**：入点会随用户在时间轴上拖动**连续变化**（`src_in_sec` 是可编辑的），
  用它做键 ⇒ 拖动时**每次微调都新建一个解码器**，正好踩中本项目最忌讳的"资源无限增长"。
  用**图层标识**做键则天然有界（图层数是确定的），且与用户心智一致（"这一层就是这条播放线"）。
- **同素材不同入点** ⇒ 不同 `play_sec` ⇒ 不同键 ⇒ 不同 lane ⇒ **各持解码器**（本任务目标）。
- **同素材同入点（叠层）** ⇒ 同一键 ⇒ 同一 lane ⇒ **仍共用**（同一条播放线，共用更省）。
- **与合成器逐字对齐**：合成器会把 `play_sec` 钳到 `src_out_sec`（A2），故 `_build_lane_map`
  **同时登记"原始键"与"钳位后的键"**，保证回调一定命中。

### 2.2 上限（实测内存后定死）
```python
POOL_MAX_PER_PATH = 3    # 同一素材最多的并发解码器数
POOL_MAX_TOTAL    = 6    # 全局上限（多素材时的安全阀）
POOL_HOT_SEC      = 0.5  # "热"判据：最近这么多秒被取过帧的解码器不许逐出
```
- **per-path = 3**：目标场景是"同一素材 3 层、各层入点不同"，要 **≥3** 才不互相逐出；
  再多一层就**别名共用**（见 §2.3），**绝不无上限新建**。
- **total = 6**：多素材时有上限。实测 4K 解码器 **≈ 94 MB/个**（见 §三），
  per-path 3 ⇒ 目标场景 **≈ 280 MB**；total 6 ⇒ 4K 最坏 **≈ 560 MB + 基线**，对桌面预览可接受。

### 2.3 淘汰策略：LRU，但**池满且都热时退化为"别名共用"**（★ 关键）
`_make_room(path, now)` 的决策（**全部有界**）：
1. 该路径未到 per-path、且全局未到 total ⇒ **新建**；
2. 否则挑**冷**的（`now - used > POOL_HOT_SEC`）**逐出并立即 `container.close()`**（把内存真正还回去）；
3. 若**同路径**全都在用（都热）⇒ **别名共用**（把最冷那个 `_Source` 的引用给新键，**绝不重建**）；
4. 全局满、都热、且是**跨素材** ⇒ 只能逐出（**跨素材共用会取错画面，绝不允许**）。

配套的不变量：
- `_close_key` 在真正 `container.close()` 前**检查该 `_Source` 是否仍被别的 lane 别名引用**
  （仍被引用 ⇒ 只摘键、不关容器）——否则会把别人正在用的解码器关掉。
- `decoder_count()` / 池上限一律按 **去重的 `_Source`（`id(src)`）** 计，
  `lane_count()` 才是键数（别名共用时 `lane_count > decoder_count`，是**预期**而非 bug）。

> **为什么需要 `POOL_HOT_SEC`（别名共用）**：见 §四 的调优故事——一开始我按朴素 LRU 硬逐出，
> 5 层场景**每拍逐出→下一拍又重建**，从 12 fps 掉到 **4.1 fps**（重建比共用贵得多）。

### 2.4 生命周期（不泄漏）
- `CueFrameSource.close()/release(path)`：`release(None)` 释放**全部键**（含所有 lane）；
  `release(path)` 释放该路径的**所有 lane**。`_close_key` 逐个判别名后关闭。
- `PreviewCanvas.set_runtime(...)`：**换曲目/切轨时先 `src.release(None)`**（`set_runtime` 只在用户
  操作时调用，不在每拍，故释放代价可忽略）；下次取帧按需懒开。
- `PreviewCanvas.set_active(False)`：沿用既有的 `src.close()`（离开页面即关全部解码器）。
- 本类**完全同步、不起后台线程**，故 `close()` 之后不留线程/句柄。

### 2.5 `lane=None` 默认：既有行为零变化
`lane` 是可选参数、默认 `None` ⇒ 键 `(path, None)` ⇒ **每路径一个解码器**。
缩略图 / 入出点预览 / `tests/test_media.py` 等**既有调用方零改动、零影响**。

---

## 三、内存实测（Windows `ctypes`：`GetProcessMemoryInfo` / `GetProcessHandleCount`）

> ⚠ 这两个 API 在 Python `ctypes` 里**必须显式设 `argtypes`/`restype`**（`wt.HANDLE`/`wt.DWORD`/`wt.BOOL`），
> 否则**静默返回 0**（第一次量到"0 MB / 0 句柄"就是这个坑）。

增量测量：`CueFrameSource()` 逐个 `frame_play(p, sec, out_size=(1440,810), lane="L%d")`。

| 内容 | 内存增量 | 句柄增量 |
|---|---|---|
| 基线（解释器 + PySide6 + Qt + numpy 已加载） | 73 MB | 391 |
| 第 1 个 **4K** 解码器（预热一帧 1.5×） | **+105 MB** | +21 |
| 第 2 个 4K 解码器（边际） | **+94 MB** | +18 |
| 第 3 个 4K 解码器（边际） | **+94 MB** | +18 |
| ⇒ **每个 4K 预热解码器** | **≈ 94 MB** | **≈ 18 句柄** |
| 每个 **1080p** 预热解码器 | **≈ 27~32 MB** | ≈ 18 句柄 |
| 仅建容器（未解码）4K / 1080p | ≈ 2.9 / 0.7 MB | +1 |
| `close()` 之后 | 回到 86 MB（+13 MB 是 Python 分配器不还 OS，不是泄漏） | 回到 394 ≈ 基线 |

**内存上界的结论**：
- 每解码器的绝大部分开销是 **ffmpeg 解码缓冲 + 线程池栈**（4K ≈ 33MB 一帧 × 若干缓冲 + 2 线程），
  不是容器本身（容器才几 MB）。
- **per-path = 3 ⇒ 同素材 4K 目标场景 ≈ 280 MB**（实测 5 层场景进程**峰值 479 MB**，含 73MB 基线）；
  **total = 6 ⇒ 4K 全 6 个 ≈ 560 MB + 基线**（最坏，且只在多素材同时活跃时短暂出现）。
- 若团队认为 479 MB 峰值偏高，**调参旋钮**就是 `POOL_MAX_PER_PATH / POOL_MAX_TOTAL` 两个常量
  （见 §六 备选方案）。

---

## 四、调优故事：朴素 LRU 硬逐出会"每拍重建"，反而更慢

| 策略（5 层同素材异入点） | 实测 fps | 说明 |
|---|---|---|
| **朴素 LRU 硬逐出**（池满就关最旧、下一拍再建） | **4.1** | ❌ 每拍"逐出→重建"，重建比共用贵得多（还狂开文件） |
| **别名共用**（池满且都热 ⇒ 多 lane 指向同一 `_Source`） | **13.3** | ✅ 画面正确，只是回到"一起 seek"的老代价，但绝不重建 |
| 加 `POOL_HOT_SEC=0.5s` + `_close_key` 判别名后（最终） | **12.4~13.3** | ✅ 与 3 层场景一致，**无退化、无越界** |

⇒ 结论：**"池满都在用"时正确的退化方向是"共用"，不是"逐出重建"**。
只有**确实冷掉**（composition 变了、这一层不再取帧）的解码器才关闭、把内存还回去。

---

## 五、判据结果（`tools/_fix_decoder_pool_verify.py` → **7/7 全绿 ✓**）

> **"改前/改后"口径**：为**隔离出"有界池"的贡献**，两者都用**上一轮出货的取帧配置**
> （`out_size = 画布×1.5`、`thread_type=AUTO`、`thread_count=2`），**唯一差别**是
> "改前"**禁用 lane**（所有层 lane=None ⇒ 池退化为**每路径一个解码器** = 老行为），"改后"**启用 lane**。

### ① 目标场景：同一素材 3 层、各层入点不同 ⇒ ≥ 24 fps ✓
| 场景（`Balls visual@110 (19).mov` 4K，960px 画布） | 改前 | **改后** | ≥24 |
|---|---|---|---|
| **3 层，入点 0 / 10 / 20 s**（任务书原话） | 18.1 | **29.1** | ✓ |
| 3 层，入点 0 / 0.7 / 1.4 s（更密 seek，复现"当前 15.5"） | **15.3** | **32.2** | ✓ |

改后 `decoder_count=3、lane_count=3、evictions=0` ⇒ 三层**各持一个独立解码器**、互不 seek。

### ② 无退化 ✓
| 场景 | 改前 | 改后 |
|---|---|---|
| 1 层 4K | 31.7 | **31.6** |
| 3 层不同素材 | 32.2 | **32.2** |
| 3 层同素材**同**入点（叠层） | 31.4 | **32.2** |

三者都不低于上一轮的数字（≈31~32 fps），且 `≥24`。

### ③ 内存有界：同一素材 5 层异入点（极端）✓
- `decoder_count = 3`（**= `POOL_MAX_PER_PATH` 上限**，total 上限 6 也未越），`lane_count = 5`，
  `evictions = 0`（走的是**别名共用**，没有逐出重建）。
- 进程内存 **92 → 峰值 479 MB**（含 ~73 MB 基线；3 个 4K 解码器 + 5 层帧缓冲峰值）。

### ④ 无泄漏：反复切轨 20 次 ✓
- 切轨 20 次（每次换入点组合、`set_runtime` + `_compose`）：
  `decoder_count` **max = 2（≤3）**；句柄 **base 433 → after 433（不增长）**；
  `shutdown()` 后 **decoder_count = 0**、句柄回到 397（≤基线）。
- 内存 **277 → 277 MB**（不增长）。

### ⑤ 静态闸门 + 全量测试 ✓
`compileall -q src/` 零错误 · `tools/selftest.py` **7/7** · `tests/` 全量
（`test_media` / `test_compositor` / `test_timeline` / `test_audio` / `test_ui_smoke` 退出码全 0）·
`tools/_fix_timeline_verify.py` **6/6** · `tools/_fix_wave_ui_verify.py` **6/6** ·
`tools/_fix_preview_perf2_verify.py` **6/6**。

### ⑥ 未越界 ✓
`audio/waveform.py` 与 `ui/timeline_widget.py` **不含**任何 Task G 符号
（`POOL_MAX_PER_PATH` / `POOL_MAX_TOTAL` / `POOL_HOT_SEC` / `_make_room` / `_build_lane_map` / `lane_count`），
且两者 mtime 均**早于** `frames.py`（本次改动的文件）。

---

## 六、权衡与备选方案（供团队决策）

本方案（`(path, lane)` 键 + per-path 3 / total 6 + 别名共用）**已达标**，无需替换。若团队希望进一步压内存：

| 方案 | 优点 | 代价 | 我的建议 |
|---|---|---|---|
| **A. 本次方案**：每层独立池，共享上限 3/6，池满共用 | 实现简单、语义清晰；目标场景 29~32 fps；内存 ~280MB | 极端 5 层峰值 ~479MB | ✅ **已采用** |
| B. 入点差异 **> 阈值** 才独立（否则共用） | 解码器更少、内存更省 | 阈值难调；"刚过阈值"的层每拍在"独立↔共用"间抖动；复杂度高 | 不推荐 |
| C. 每层一条**独立小池**（不复用上限） | 层间彻底隔离 | **总量无全局上界** ⇒ 违反"绝不无限增长"红线 | ❌ 不采用 |
| D. 调小上限（如 per-path 2 / total 4） | 内存峰值降到 ~380MB | 3 层目标场景会退化为"2 独立 + 1 共用"（fps 略降） | 团队若在意内存可改常量，一行的事 |

**结论**：采用 A；B/C/D 仅作记录。若后续用户报"内存太高"，把 `POOL_MAX_PER_PATH=3/POOL_MAX_TOTAL=6`
调成 `2/4` 即可（纯常量调整，不动逻辑）。

---

## 七、改动清单

| 文件 | 改动 |
|---|---|
| `src/ercue/media/frames.py` | 新增常量 `POOL_MAX_PER_PATH=3` / `POOL_MAX_TOTAL=6` / `POOL_HOT_SEC=0.5`；`_src` 键改 **`(path, lane)`**，新增 `_used`（LRU）、`open_attempts`/`evictions` 计数；新增 `_key/_close_key/_pool_sources/_make_room`（有界+LRU+别名共用）；`_get_source(path, lane)`；`frame/frame_exact/frame_play` 增 `lane=`；`release` 支持释放某路径全部 lane；诊断增 `decoder_count`(按 `id(src)` 去重)/`lane_count`/`cached_keys`；`selftest` 加 lane/池安全性；docstring 增 **A7** 纪律 |
| `src/ercue/ui/preview.py` | `set_runtime` 先 `src.release(None)`（换轨不泄漏）；新增 `self._lane_of` 与 `@staticmethod _build_lane_map(states)`（`(clip_id, play_sec) → lane=lyr_id`，含 `src_out_sec` 钳位键）；`_frame_lookup` 带上 `lane`；`_compose` 每拍重建 lane 表；docstring 增 **A7** |
| `tools/_fix_decoder_pool_verify.py` | **新增**：判据 ①~⑥ 复跑闸门（含 ctypes 内存/句柄测量、"改前"用实例级禁 lane 模拟） |
| `tools/_fix_preview_perf2_verify.py` | t2 的"3 层异入点"由"已知老限制"改为**按出货配置断言 ≥24（有界池已修复）** |

**未改**：`audio/waveform.py`、`ui/timeline_widget.py`、`render/compositor.py`、`model/*`、`P:\ERAVJ\AutoVJ\`。
