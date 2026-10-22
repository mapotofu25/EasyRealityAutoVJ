# ERAVJ 侧码表实装方案（2026-10-11）

> 目标：**DJ 在 EasyRealityCue 里编排 → 导出 `.ercue` → 演出机 ERAVJ 导入 → 演出时按码表驱动画面**
> 前提（已实证）：`.ercue` 格式双向兼容 ✓；旧模型零外部依赖 ✓；旧运行时是纯逻辑（自述"不碰 Qt/引擎/IO"）✓

---

## 一、为什么要做

ERAVJ 在 2026-09-30 把码表**整体移除**（用户当时的判断是"所有问题都是码表加的"）。
现在有了**独立的编排工具**（EasyRealityCue，不带演出引擎），码表可以以**干净的方式**回到演出端：
编排与演出解耦，出问题不再互相牵连。

备份 `P:\ERAVJ\AutoVJ_码表备份_20260930\` 保留了被裁掉的全部代码（git `1c9ac6e`）。

---

## 二、移植清单

### 照搬（零改动或极小改动）
| 文件 | 行数 | 说明 |
|---|---|---|
| `cuesheet.py` | 658 | 数据模型 + `.ercue` 读写（**只有标准库依赖**） |
| `cuesheet_runtime.py` | 319 | 运行时解释器（纯逻辑，返回 `list[LayerPlayState]`） |

⚠ 移植时**必须与 EasyRealityCue 侧的同名模块保持字段一致**（两侧都有 `cuesheet.py`，格式契约在这）。
建议加一个**契约测试**：用一份固定的 `.ercue` 样例，比对两侧读出的字段值。

### 恢复集成点（需适配当前 engine）
备份 `engine.py` 里码表相关 **119 处引用**，核心是：
- `_CueSubLayer`（284 行起，码表专用的子图层）
- `set_cue_sheet(sheet, media_paths, track, ...)` / `clear_cue_sheet()`
- `_cuesheet_tick(snap)` —— **每帧**调用，位置在 `_tick_locked` 内、合成之前
- `_cuesheet_on()` / `cuesheet_wanted()` / `cuesheet_status()`
- `_cuesheet_autoswitch(snap)`（认歌自动切曲目）
- `_cue_pos_from_fingerprint(snap, beat_len)`（**指纹定位**，`MIN_VOTES=40` / `MAX_AGE=3.0`）
- `_cue_beat_len(snap)`
- 后处理链跳过（`if self._cuesheet_on() > 0.0: skip`）

⚠ **当前 engine 已比备份版改了很多**（含 2026-10-10 那轮修复：抢锁快照、Spout、异常限流等）⇒
移植时**逐点适配**，不要整段贴。

### 不移植
- `cue_editor.py`（2456）/ `cue_preview.py`（412）—— 由 EasyRealityCue 取代
- `position_source.py`（640，含 VirtualDJ OSC）—— **阶段 2 再评估**
- ui_main 的第 3 编排界面 —— 只要"导入 + 选曲目"

---

## 三、位置来源（关键设计）

旧实现的设计已经定过（`engine.py:2463-2470` 注释）：

> ★ 位置由**指纹认歌**给（录音回环 → 指纹算出"放到第几秒"）；**设备（VirtualDJ）只在指纹给不出时作后备**。

⇒ **阶段 1 只做指纹定位**：
- ERAVJ 的指纹库（86 万条，2026-10-10 刚修过列序问题）**现成可用**
- 需要：`_cue_pos_from_fingerprint` 的 `MIN_VOTES` / `MAX_AGE` 判据调好
- **不需要** `position_source.py` 那 640 行

---

## 四、四条对齐项（实装前必须定死）

| # | 项 | 定案 | 状态 |
|---|---|---|---|
| 1 | 画面几何 | **两端都用演出端判据**：xform 全默认 ⇒ cover(`max`)；有变换 ⇒ contain(`min*us`) | ✅ EasyRealityCue 侧已改（A4） |
| 2 | fade 曲线 | α²（`engine.py:2835`） | ✅ EasyRealityCue 侧已改（A5） |
| 3 | 能力表 | `trans_glitch`/`trans_zoom` 进 `SUPPORTED`（两端引擎都能渲染） | ✅ EasyRealityCue 侧已改（A3） |
| 4 | 素材传递 | 见下 | ⏳ 待定 |

### 素材传递（唯一还需设计的）
三种素材形态：
- **不内嵌**（默认）：包只有几 KB，素材在磁盘 ⇒ 演出机需要能访问同样的路径（或手动指定目录）
- **内嵌**（`embed=True`）：素材打进 zip ⇒ 演出机导入时需解压（EasyRealityCue 侧已修 `extract_media` 接线）

**建议**：ERAVJ 导入时**照抄同一逻辑** —— 包内有 `media/` ⇒ 解压到 `<包路径>.media/` 再挂载。
这样"内嵌包"在两端行为一致，用户拿到一个文件就能换机演出。

---

## 五、分阶段

### 阶段 1：最小可用（建议先做）
1. 移植模型 + 运行时（~1000 行）
2. 恢复 engine 集成点（~400 行，含适配）
3. ui_main 导入入口（~150 行）：菜单/按钮 → 选 `.ercue` → 挂载 → 状态栏显示"码表：X / 曲目 Y / 位置来源"
4. 素材传递（~80 行）
5. **端到端验证**：编排一张真码表 → 导出 → ERAVJ 导入 → 认歌定位 → 画面按拍切换

### 阶段 2：完整（按需）
- 设备位置源（VirtualDJ OSC，640 行）
- 码表图层的可视化管理（哪些图层接管、Kv 交互）
- 指纹定位的鲁棒性调优（`MIN_VOTES`）

---

## 六、风险与对策

| 风险 | 对策 |
|---|---|
| 移植时适配出错，污染现有演出逻辑 | `_cuesheet_tick` 的"不接管三条件"必须原样保留（无码表/无位置/图层不可见 ⇒ 完全退回普通行为）；**先做无码表回归测试**，确保不装码表时行为逐值不变 |
| 指纹定位不准 ⇒ 画面与音乐对不上 | 复用 2026-09-29 的既有判据；上线前用真实演出曲目验证；`_cue_jumped`（大跳变）逻辑保证 DJ 打 loop 时能跟上 |
| 两端字段漂移 | 加**契约测试**（同一份 `.ercue`，两侧读出字段比对） |
| 演出中断风险 | 与 2026-10-10 那轮同样的纪律：**改热路径要实测**、GUI 不碰引擎锁（码表状态也应走 `ui_snapshot()` 快照） |
