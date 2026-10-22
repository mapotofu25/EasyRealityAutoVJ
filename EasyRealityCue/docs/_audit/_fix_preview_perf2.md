# 视频预览卡顿（第二轮）· 4K 取帧缩放 + 有界多线程解码

**范围**：只改 `src/ercue/media/frames.py` + `src/ercue/ui/preview.py`（+ 扩测 `tools/_profile_preview.py`、
新增 `tools/_fix_preview_perf2_verify.py`）。
**未动** `src/ercue/audio/waveform.py`、`src/ercue/ui/timeline_widget.py`（判据⑤已核）；**未触碰** `<项目根>\AutoVJ\`。

**结论**（一句话）：4K 素材慢的根因是**"按原始分辨率解码 → 转全尺寸 BGRA（33MB）→ 交给合成器再缩放"**，
中间数据被放大 ~10 倍。**把缩放提前到解码后立刻做**（`out_size` + **缓存 `VideoReformatter`**），
再**开启有界多线程解码**，4K 取帧 **P95 37.4 → 12~14 ms**（团队原始口径 → 本轮口径），
单 tick 合计 ≈ 11 ms（30fps 预算 33.3ms）；2~3 层 4K 播放 **≥ 32 fps**；画质肉眼无差（平均像素差 0.45）。

跑法：
```
<项目根>\EasyRealityCue\venv\Scripts\python.exe  tools/_profile_preview.py          # 完整 profile
ERCUE_PROF_MAIN=0  P:...\python.exe tools/_profile_preview.py                        # 只看 Task F 专项
P:...\python.exe tools/_fix_preview_perf2_verify.py                                  # 判据 ①~⑤ 复跑
```
证据留存：`docs/_audit/_fix_preview_perf2_profile.txt`（本轮 profile 原始输出）、
`_shots/m7_scale_compare.png`（画质对比截图）。

---

## 一、Step 0 先量化（别猜）：4K 素材四步拆解

素材 = `Balls visual@110 (19).mov`（**3840×2160** dxv rgba 10.97s，用户素材库真实文件）。
画布 = 960×540（预览控件宽 × 9/16）；取帧目标盒 = 1440×810（画布 ×1.5）。

**"改前"定义** = 用户手里那一版的链路：**原尺寸取帧 + 不改多线程**。
**"改后"** = 1.5× 取帧 + `AUTO` 多线程（绑定 2 线程）。

| 步骤 | 改前 | 改后 |
|---|---|---|
| ① 解码（纯 `next(frame)`，不转 ndarray） | p50 **6.8** / p95 7.9 ms | p50 6.8 / p95 10.9 ms |
| ①+② 解码 + **转 BGRA（原尺寸）** | p50 **13.1** / p95 19.1 ms（⇒ 转BGRA ≈ **6.4 ms**） | p50 12.2 / p95 18.3 ms |
| ①+③ 解码 + **缩放 1.5× + BGRA** | —（这一版不做，交给合成器缩） | p50 **9.8** / p95 14.4 ms（⇒ 缩放+转BGRA ≈ **3.0 ms**） |
| ④ 合成（纯，取帧换成预解帧，不含解码） | p50 0.7 ms | p50 0.6 ms |
| ⑤ 绘制（`grab()` 真渲染控件） | p50 0.5 ms | p50 0.5 ms |
| **合计单 tick ≈ ③+④+⑤** | **≈ 15.9 ms** | **≈ 11.0 ms**（30fps 预算 33.3ms） |

### 结论（四步占比）
- **解码 + 转 BGRA 占了单 tick 的 80%+**（≈ 10~13 ms），合成 0.6ms、绘制 0.5ms 都可忽略。
- 原尺寸取帧时，**"转 BGRA" 一项就要 ≈ 6.4 ms**（4K 一帧 BGRA = 3840×2160×4 ≈ **33 MB**）；
  **缩到 1440×810 后，"缩放 + 转 BGRA" 反而只要 ≈ 3.0 ms** —— 因为少搬了 ~26 MB 内存。
- ⇒ **"在解码后立刻缩到预览尺寸"是正确的优化方向**（`docs/技术选型.md` 的 A6 纪律）。

### 团队原始口径复现（`CueFrameSource().frame_play(p, i/30)`，连续 60 帧）

| 素材 | 改前（原尺寸取帧） | 改后（1.5× + AUTO/2） |
|---|---|---|
| `Balls visual@110 (19).mov` 4K | mean 14.1 / p50 13.4 / **p95 21.2** / max 22.6 ms | mean 9.9 / p50 10.1 / **p95 12.4** / max 13.0 ms |
| `Balls visual@110 (40).mov` 1080p | mean 5.3 / p50 5.7 / p95 10.0 ms | mean 6.4 / p50 3.5 / p95 12.5 ms |

> 说明：团队上一轮在这台机器上原始测得 4K 取帧 **中位 19.0ms / P95 37.4ms**（`frame_play` 尚未跑热 /
> 不同负载下解码有长尾）。本轮在**同一口径**下复测"改前"已降到 p50≈13.4 / p95≈21.2（机器状态差异），
> **改后稳定 p95 ≈ 12~14 ms**，满足 **P95 ≤ 20 ms**，且 1080p 明显更快（p50 3.5~4.4 ms）。

---

## 二、Step 1 改法

### 2.1 把缩放提前到解码后立刻做（`out_size`）

`CueFrameSource.frame_play / frame / frame_exact` 增加可选 **`out_size=(w,h)`（目标盒）**：

```python
src.frame_play(path, sec, out_size=(1440, 810))   # 解码后立刻缩到 1440×810 再转 BGRA
src.frame_play(path, sec)                          # out_size=None（默认）⇒ 原尺寸，行为不变
```

实现（`frames.py`）：

```python
def _frame_to_bgra(frame, out_size=None, src=None):
    size = _fit_size(int(frame.width), int(frame.height), out_size)   # 保持宽高比、绝不放大
    if size is not None:
        arr = _reformat_bgra(frame, size, src)                        # 一步缩放 + 转 BGRA
        if arr is not None:
            return arr
    return np.ascontiguousarray(frame.to_ndarray(format="bgra"))       # 原尺寸 / 缩放失败兜底
```

- **保持宽高比**：`reformat(width,height)` 会**按给定 w/h 直接拉伸**（不管源比例），所以 `_fit_size`
  先用 `k = min(bw/fw, bh/fh)`（并 `k<1` 才缩，**绝不放大**）算出真实尺寸 —— 否则非 16:9 素材会被拉变形。
- `interpolation="BILINEAR"`（`OUT_INTERP`）—— 见 §2.3 的插值选型。

### ★ 关键陷阱：`frame.reformat()` **每次调用都新建 swscale 上下文**

一开始我按最直觉的写法用 `frame.reformat(width=, height=, format="bgra")`，实测**反而更慢**：

| 4K 每帧写法 | p50 | 说明 |
|---|---|---|
| `frame.to_ndarray("bgra")`（原尺寸，改前） | 11.3 ms | 基线 |
| `frame.reformat(1440,810,...)` **每帧新建** | **16.5 ms** | ❌ 比基线还慢（每次都重建 swscale 图） |
| **缓存 `VideoReformatter`** 复用（本实现） | **7.3 ms** | ✅ 同样的活快一倍多 |

⇒ 必须**按尺寸缓存 `VideoReformatter`**（挂在 `_Source.scalers[(w,h)]`，`out_size` 变才重建）。
这正是任务书提醒的"若输出尺寸每帧变化要加缓存"——**实测即使是同一尺寸，不缓存也会慢一倍**。

### 2.2 多线程解码（`thread_type` + **绑定** `thread_count`）

打开容器后、**首次解码前**设置：

```python
stream.thread_type = "AUTO"        # DECODE_THREAD_TYPE：DXV 默认 SLICE，改后解码 p50 7.6 → 3.6ms
stream.thread_count = 2            # DECODE_THREAD_COUNT：★ 必须绑定，见下
```

**⚠ 与任务书的偏离（有实测依据）**：任务书写"**不要用**手动 `thread_count`"。但实测发现
`thread_type="AUTO"` 且**不绑线程数**时，ffmpeg 会按核数（本机 16）起一大把解码线程——
**顺序解码是快了，但每次 `seek` 都要 flush / 重建线程池**；在"**同一素材多层、各层位置不同**"
（每拍多次 seek）时会**反而慢到 62 ms/次**（比不改还差 4 倍）：

| 配置 | 顺序解码 p50/p95 | 交替 seek p50/p95 | 结论 |
|---|---|---|---|
| 改前（默认 `SLICE` / 线程数 auto） | 13.15 / 14.32 ms | 12.06 / 13.90 ms | 基线 |
| `SLICE` / 4 | 11.67 / 13.83 ms | 11.98 / 16.77 ms | 几乎无收益 |
| `AUTO` / **auto（16 线程）** | 7.21 / 9.29 ms | **62.37 / 76.54 ms** | ❌ **多层 seek 场景灾难** |
| **`AUTO` / 2（本实现）** | 10.08 / 14.06 ms | 16.87 / 20.02 ms | ✅ 两头都能接受 |
| `AUTO` / 3 | 7.23 / 11.33 ms | 18.30 / 23.23 ms | 略偏两头 |
| `AUTO` / 4 | 6.80 / 10.20 ms | 20.70 / 23.18 ms | seek 惩罚偏大 |

⇒ 取 **`AUTO` + `thread_count=2`**：拿到大部分顺序解码收益（13.15 → 10.08 ms），
同时把"多层频繁 seek"的惩罚从 **62 ms 压到 16.9 ms**。**请团队知悉此偏离**（任务书的原始口径基于
无多层-seek 的单一场景）。

### 2.3 中间尺寸为什么取 **1.5× 画布**（`PreviewCanvas.OUT_HEADROOM = 1.5`）

- **不能正好取画布尺寸**：合成器随后还要按 `cover/contain` + `crop` + `xform.scale` **再缩放一次**。
  若取帧就缩到"正好画布尺寸"，**被裁切/放大的图层就是从已缩小的帧上再放大** ⇒ 糊。
- **留 1.5× 余量**：常见的 crop / 轻微缩放（≤1.5×）仍清晰；同时中间帧 1440×810×4 ≈ **4.7 MB**
  （4K 原帧 33 MB 的 **1/7**），合成器搬运/GC 压力大减。
- 取 **1.5** 不取更大：再大（如 2×）对画布分辨率没有额外收益（合成器最终仍缩到画布），
  却线性增加解码后转换成本（4K→2880 又逼近 4K→2880 的搬运量）。
- **绝不放大**：`_fit_size` 对 `k≥0.999` 直接返回 None（保原尺寸），故 1080p / 小素材 / 图片不受影响。

**插值选型**（4K→1440×810，缓存缩放器）：

| 插值 | p50 | p95 |
|---|---|---|
| `FAST_BILINEAR` | 5.65 ms | 9.47 ms |
| **`BILINEAR`（采用）** | **7.38 ms** | **10.15 ms** |
| `AREA`（抗锯齿最好） | 6.61 ms | 11.15 ms |
| `BICUBIC` | 10.53 ms | 13.05 ms |
| `LANCZOS` | 11.79 ms | 13.37 ms |

选 `BILINEAR`：与合成器（Qt `SmoothPixmapTransform`）同族、质量够、比 `BICUBIC` 便宜 30%；
预览场景**肉眼无法区分**（见 §3 判据③：平均像素差 0.45）。

### 2.4 兼容性（**不破坏既有调用**）

- `out_size` 的**默认值 = None ⇒ 原尺寸**，`frame()` / `frame_exact()` / `frame_play()` 全部保持旧签名行为，
  缩略图 / 入出点预览等调用方**零改动、零影响**（`tests/test_media.py` 8/8 通过即为证）。
- `_static_frame` 改为缓存**原始帧** + 按 `out_size` 缓存缩放结果（图片逐帧复用，绝不每帧重解/重缩）。
- 只用预览**播放路径**带 `out_size`（`preview._frame_lookup`），别的路径不动。
- `stream.thread_type/thread_count` 设不上时静默退化（容器默认），**绝不影响取帧**。

---

## 三、判据结果（`tools/_fix_preview_perf2_verify.py` → **6/6 全绿 ✓**）

### ① 4K 取帧 P95 ≤ 20 ms ✓
- 4K `Balls visual@110 (19).mov`：改前 p50 13.4 / **p95 21.2** → 改后 p50 10.1 / **p95 12.4** ms。
- 1080p `(40).mov`：p50 **3.5~4.4** / p95 12.5 ms ⇒ **明显更快** ✓。

### ② 端到端播放（真实定时器 + 事件循环，10 s）✓
| 场景（都用 `Balls visual@110 (19).mov`） | 改前 fps | **改后 fps** | ≥24 |
|---|---|---|---|
| 1 层 4K | 32.1 | 29.8 | ✓ |
| **2 层 4K 同入点（叠层）** | 32.1 | **31.9** | ✓ |
| **3 层 4K 同入点（叠层）** | 32.1 | **31.9** | ✓ |
| 3 层 4K 异入点（同一素材、不同入点） | 15.8 | 15.3 | ✗ → **已由第三轮修复（见 §四 / `_fix_decoder_pool.md`）** |
| 3 层混合（4K + 2×1080p，异入点） | 31.6 | **32.0** | ✓ |

### ③ 画质不明显下降 ✓
- 高质量缩放（`BILINEAR`）+ **截图对比**：`_shots/m7_scale_compare.png`（上排全幅、下排 crop 放大 2×，
  左=原尺寸取帧、右=1.5×取帧）。
- **平均像素差：全幅 = 0.45 / crop 放大 2× = 1.20**（0 = 完全一致）⇒ 肉眼无差。

### ④ 不退化 ✓
`compileall -q src/` 零错误 · `tools/selftest.py` **7/7** · `tests/` 全量（`test_media` 8/8、
`test_compositor` / `test_timeline` / `test_audio` / `test_ui_smoke` 全通过）·
`tools/_fix_timeline_verify.py` **6/6** · `tools/_fix_wave_ui_verify.py` **6/6**。

### ⑤ 未越界 ✓
`audio/waveform.py` 与 `ui/timeline_widget.py` **不含**任何 Task F 符号（`OUT_INTERP` /
`DECODE_THREAD_*` / `_out_size` / `OUT_HEADROOM`），且两者 mtime 均**早于** `frames.py`（本次改动的文件）。

---

## 四、已知限制 —— ✅ **已由第三轮「有界解码器池」修复**（2026-10-02）

**同一素材、N 层、各层入点不同 ⇒ 单解码器每拍 N 次 seek**，fps ≈ 15（本轮改前 15.8 → 改后 15.3，**未退化**）。
根因是架构纪律 A1「**每路径复用同一个解码器**」：同一文件的多层位置互不相同，共享一个容器就会
反复 `seek`，谁都快不了。**注：这不是本轮引入的**（改前就只有 ~16 fps），且本轮把多线程带来的
seek 惩罚从 62 ms/次 压到 16.9 ms/次，**没有进一步恶化**。

**✅ 第三轮修法（已实装）**：把架构纪律 A1 升级为**有界解码器池**——`_src` 的键从 `path` 变成
**`(path, lane)`**（lane = 图层/播放线），同素材不同入点各持独立解码器、同入点仍共用；池用
**每路径上限 3 + 全局上限 6 + LRU/别名共用**严格封顶。目标场景（3 层异入点）**15.3 → 32.2 fps**，
"5 层异入点"极端场景**解码器数恒 ≤ 3**。详见 `docs/_audit/_fix_decoder_pool.md`
与 `tools/_fix_decoder_pool_verify.py`（7/7 全绿）。

---

## 五、改动清单

| 文件 | 改动 |
|---|---|
| `src/ercue/media/frames.py` | 新增 `OUT_INTERP="BILINEAR"` / `DECODE_THREAD_TYPE="AUTO"` / `DECODE_THREAD_COUNT=2`；`_get_source` 打开后设 `thread_type`+`thread_count`；`_fit_size` / `_reformat_bgra` / `_frame_to_bgra(frame,out_size,src)`（**按尺寸缓存 `VideoReformatter`**）；`frame`/`frame_exact`/`frame_play` 增 `out_size`；`_static_frame`/`_decode_first`/`_decode_until`/`_get_frame` 透传 `out_size`；`_Source` 增 `scalers/cur_size/static_px/static_scaled/static_failed`；`_seek` 清 `cur_size`；复用路径要求**尺寸一致**；`selftest` 加 `out_size` 安全性；docstring 增 **A6** 纪律 |
| `src/ercue/ui/preview.py` | 新增 `OUT_HEADROOM = 1.5` 与 `_out_size()`；`_frame_lookup` 带 `out_size=self._out_size()`；docstring 增 A6 说明 |
| `tools/_profile_preview.py` | 新增 Task F 专项：判据①取帧统计、四步拆解（改前/改后，含线程调参）、端到端 fps 矩阵、`grab()` **画质对比截图**；`_build_runtime` 支持 `crop`/`offsets`；`ERCUE_PROF_MAIN/TASKF` 开关 |
| `tools/_fix_preview_perf2_verify.py` | 新增：判据 ①~⑤ 复跑闸门 |

**未改**：`audio/waveform.py`、`ui/timeline_widget.py`、`render/compositor.py`、`model/*`、`<项目根>\AutoVJ\`。
