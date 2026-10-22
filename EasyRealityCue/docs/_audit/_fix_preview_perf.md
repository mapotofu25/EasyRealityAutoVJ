# 视频预览卡顿 · profile + 修复报告

**范围**：只改 `src/ercue/ui/preview.py` + `src/ercue/media/frames.py`（**未动** `audio/waveform.py`，
**未触碰** `<项目根>\AutoVJ\`）。
**自测脚本**：`tools/_profile_preview.py`（真实素材 + 真实 runtime + 真实事件循环）。
**结论**：**卡顿主因是 `preview.FPS = 10` 硬封顶**（渲染管线实测能跑 70~82 fps）；**次因**是
`frames._decode_until` **无界顺序解码**（一次前进近 1 秒 → P95 = 211 ms 卡主线程）。
两处修完：**端到端交付 fps 10.3~10.7 → 30.4~31.4**；单 tick P95 **≤ 20 ms**；
`compileall` 零错误；`tests/` **全绿**；`_fix_timeline_verify.py`（6/6）、`_fix_wave_ui_verify.py`（6/6）、
`tools/selftest.py`（7/7）全绿。

跑法：
```
<项目根>\EasyRealityCue\venv\Scripts\python.exe  tools/_profile_preview.py
```

---

## 一、先测（Step 0）：每个环节的耗时数字

用**用户素材库的真实 DXV**（1920×1080@30fps）+ 真实 `CueRuntime` + 真实 `compose()`。
画布尺寸 = 预览控件宽 × 9/16。`_compose` = 取帧 + 合成。

### 1 层 / 3 层 DXV（含取帧 vs 纯合成）

| 预览宽 | 画布 | 1 层 DXV `_compose` | **3 层 DXV `_compose`** | **3 层 DXV 纯合成（无取帧）** | 3 层 4K `_compose` |
|---|---|---|---|---|---|
| 960 | 960×540 | 7.0 / p95 7.8 ms | 16.9 / **p95 19.3** ms | 1.8 ms | 32.7 / p95 38.7 ms |
| 1280 | 1280×720 | 7.2 / p95 7.8 ms | 18.0 / **p95 20.1** ms | 3.2 ms | 32.9 / p95 35.8 ms |
| 1600 | 1600×900 | 7.1 / p95 7.6 ms | 17.5 / **p95 19.1** ms | 4.6 ms | 33.9 / p95 37.8 ms |

（4K 素材 = `Balls visual@110`，3840×2160@30fps rgba。）

### 关键拆解
- **纯合成只有 1.8~4.6 ms** ⇒ **合成器不是瓶颈**，画布尺寸也不是主因（960→1600 只涨 ~3 ms）。
- 3 层 DXV `_compose` ≈ 17~18 ms，其中 **~15 ms 是逐层取帧（解码）** ⇒ **解码是 `_compose` 里的主成本**。
- `frame_play()` 单次：前进 1 帧 ≈ 5 ms；**一次前进 1.0 s（最坏）= mean 76 / P95 211 ms** ← ★ 顿卡元凶；
  回退 seek ≈ 34~39 ms。
- 控件整幅绘制（`grab` 渲染）≈ 1~2 ms ⇒ 绘制可忽略。

### 当前实际能达到的 fps
| | 值 |
|---|---|
| 管线吞吐（连续跑 `_tick`，3 层 DXV） | **70~82 fps** |
| **端到端交付 fps（真实定时器 + 真实事件循环）** | **10.3~10.7 fps** ← 用户实际看到的 |

### 瓶颈判定
1. **主因：`preview.FPS = 10` 硬封顶。** 管线能跑 70~82 fps，却被人为限到 10 fps ⇒ 用户看到的就是
   10 fps 的"卡顿 / 非常慢"。（`A3` 当年设 10fps 是为"静止不烧 CPU"，但把**播放**也压到 10fps。）
2. **次因：`frames._decode_until` 无界顺序解码 + 逐帧 `to_ndarray`。** `PLAY_FWD_MAX_SEC=1.0` 且无帧数上限，
   "一次前进接近 1 秒"就会顺序解上百帧、并对**每一帧**都做 `to_ndarray("bgra")`（4K 一帧 ~33MB），
   实测 **P95 = 211 ms** ⇒ 主线程被卡住（掉帧 / 拖动 scrub 时的顿卡）。

---

## 二、按瓶颈修（Step 1）

### 修 1（主因）：预览帧率 10 → 30，且**不引入堆积**
`src/ercue/ui/preview.py`：
- `FPS = 10` → **`FPS = 30`**（匹配 30fps 素材；管线实测有 ~2× 余量）。
- 定时器改 **单次触发 + 自校正**：`_tick` 处理完再挂下一拍，间隔 = `标称间隔 − 本拍耗时` ⇒
  周期稳定在标称值，**慢帧自动拉长、绝不堆积/重入**（旧固定周期定时器慢帧后会连发补拍）。
- **静止时不再挂下一拍**（`if not dirty and not playing: return`）⇒ 保持 `A3` 的"静止 CPU≈0"。
- 标称间隔单独存 `self._interval_ms`（`QTimer.start(msec)` 会改写 `interval()`，不能回读）。

> ⚠ 踩过的坑：一开始用"单次触发 + 固定 `interval` 再挂"，实际周期 = 本拍耗时 + 间隔 ⇒ 只有 ~17 fps。
> 用**自校正**（间隔 − 耗时）后回到 30 fps（实测 31.4）。

### 修 2（次因）：有界顺序解码 + 只转返回帧
`src/ercue/media/frames.py` 的 `_decode_until`：
- 新增 `PLAY_MAX_STEP_FRAMES=8` / `PLAY_STEP_BUDGET_SEC=0.012`（**前进/播放补帧**路径）与
  `PLAY_SEEK_BUDGET_SEC=0.040`（**定位/seek** 路径）；超限就用"最近一帧"顶着、下一拍继续追
  （**绝不返回黑帧**）。前进路径双上限（帧数 + 时间），定位路径只设时间上限（保证定位尽量精确）。
- **只对"要返回的那一帧"做 `to_ndarray`**，中间帧一律不转（4K 省掉大量 ~33MB 转换）。
- 退化：仍只在"回退 / 大前跳 / 尚未定位"时 seek；同位置重复取帧仍**复用上一帧**（不跳帧）。

> 修完 `frame_play`：**一次前进 1.0s 的 P95 = 211 ms → 18.4 ms**；回退 seek 38.7 → 19.1 ms。
> 保留"连续前进 / 同位置不跳帧 / 拖动 scrub 平滑"全部语义（`test_media` 与 `_fix_timeline_verify` 全过）。

### 未改（有数据支撑）
- **合成器 `render/compositor.py` 不动**：纯合成仅 1.8~4.6 ms，不是瓶颈。
- **画布尺寸不改**：960→1600 只差 ~3 ms，改小反而糊。

---

## 三、判据结果

| # | 判据 | 目标 | 实测 | 结论 |
|---|---|---|---|---|
| 1 | 播放流畅度（用户素材库 DXV，3 层） | 实际 fps ≥ 24 | **30.4 ~ 31.4 fps**（改前 10.3~10.7） | ✓ |
| 2 | 不引入卡顿（单 tick 最坏 P95） | ≤ 40 ms | **19.1 ~ 20.1 ms**（3 层 DXV）；3 层 4K 亦 ≤ 38.7 ms | ✓ |
| 3 | 不退化 | 行为不变 | `tests/` 全绿；`_fix_timeline_verify.py` 6/6；`_fix_wave_ui_verify.py` 6/6；`selftest` 7/7 | ✓ |
| 4 | 编译 | 零错误 | `compileall -q src/` 零错误 | ✓ |

### 真实 MainWindow 端到端复测（最强证据）
用**真实主窗口 + 真实播放传输**（`toggle_play()` → `window._play_timer` 33ms 驱动位置 + 预览重组），
3 层真实 DXV、预览控件 1280×720、播放 5 秒：
```
MainWindow 真实播放（3 层 DXV，1280 预览）: 全程 30.6 fps；最后 2s 稳态 30.8 fps；位置=4.96s
```
⇒ 位置 5 秒走 4.96 s（**实时**），预览稳定 **30.8 fps**。

### 改前 / 改后对比（同一测试机、同一真实 DXV）
| 指标 | 改前 | 改后 |
|---|---|---|
| 端到端交付 fps（3 层 DXV，1280） | **10.7** | **31.4** |
| `frame_play` 一次前进 1.0s（P95） | **211 ms** | **18.4 ms** |
| `frame_play` 回退 seek（P95） | 38.7 ms | 19.1 ms |
| 单 tick `_compose` P95（3 层 DXV） | 20.6 ms | 20.1 ms（不变，本就够快） |

---

## 四、全量验证表

| 闸门 | 命令 | 结果 |
|---|---|---|
| 编译 | `python -m compileall -q src/` | 零错误 |
| 模型自检 | `python tools/selftest.py` | **7/7** |
| 素材层（含 `frame_play`） | `python tests/test_media.py` | **8/8** |
| UI 冒烟 | `QT_QPA_PLATFORM=offscreen python tests/test_ui_smoke.py` | **全部通过** |
| 时间轴/合成/音频 | `test_timeline.py` / `test_compositor.py` / `test_audio.py` | **全部通过** |
| 时间轴 5 条修复回归 | `python tools/_fix_timeline_verify.py` | **6/6**（含 ①②③ 逆向） |
| 波形接线回归 | `python tools/_fix_wave_ui_verify.py` | **6/6** |
| 预览 profile | `python tools/_profile_preview.py` | 见上（改前/改后） |

---

## 五、改动清单（精确）

`src/ercue/ui/preview.py`
- `FPS` 10 → 30；模块 docstring / 类 docstring 同步更新（"10fps 封顶" → "播放 30fps 单次触发"）。
- `__init__`：新增 `self._active`、`self._interval_ms`；`_t` 设 `setSingleShot(True)`。
- 新增 `_arm(delay_ms=None)`；`set_pos` / `set_playing` 变化时 `_arm()`；`set_active` 用 `_active` 控制。
- `_tick`：compose 后按"标称间隔 − 本拍耗时"自校正再挂下一拍（仅播放中）。

`src/ercue/media/frames.py`
- 新增常量 `PLAY_MAX_STEP_FRAMES` / `PLAY_STEP_BUDGET_SEC` / `PLAY_SEEK_BUDGET_SEC`。
- `frame_play`：前进路径与 seek 路径分别调用有界的 `_decode_until`。
- `_decode_until(src, target, max_frames=None, budget_sec=None)`：加预算；**只转返回帧**。

`tools/_profile_preview.py`（新增）：可复跑的 profile（真实素材 DXV/4K/小 mp4 × 多宽度；含拆解与交付 fps）。
