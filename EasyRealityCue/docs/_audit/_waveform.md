# 真实彩色波形（数据层）实现报告

**新增文件**
- `src/ercue/audio/waveform.py`（数据层，零 Qt）
- `tools/_waveform_verify.py`（可复跑自测脚本）

**未改动**：`ui/timeline_widget.py`（按派单要求没碰，接线由主理人后续统一安排）；`src/**` 其它文件。

**复核结论**：`compute()` 端到端 **0.80 s / 3 分钟素材**（判据 ≤5 s）；`compileall -q src/` 零错误；
`tools/selftest.py` **7/7**；`tests/`（audio / compositor / media / timeline / ui_smoke）**全绿**；
`tools/_waveform_verify.py` **5/5 判据全过**。

---

## 一、算法说明（**实际**怎么算的）

整条链路：`decode → 分列 → 三频段能量 → 幅度 → 配色`，全部自研，基于通用 DSP 原理，未复制任何第三方源码。

### 1) 解码 → 单声道 16 kHz（`_decode_mono16k`，waveform.py:178-228）

用 PyAV 打开文件，把第一条音频流经 `av.AudioResampler(format="fltp", layout="mono", rate=16000)`
重采样成 **单声道 16 kHz float32 一维数组**（`fr.to_ndarray()` 出来是 `(1, N)`，取 `[0]` 展平后拼接）。

- **为什么是 16 kHz**：画波形不需要全带宽，降采样能成倍省算力；16 kHz 的 Nyquist=8 kHz
  足够覆盖我们关心的三频段（上限取 7.8 kHz，留出重采样边缘）。
- **时长一律按真实解码样本数换算**：`duration_sec = N / 16000`（waveform.py:342）。
  ⚠ 绝不用容器/别处推来的近似值 —— 用户正在反馈"时长被拉长"，这里必须老实。
- **`metadata_errors="replace"` 不能省**（waveform.py:190）：GBK 元数据的 `.mov` 会让
  `av.open` 直接抛 `UnicodeDecodeError`（同 `media/frames.py` 的既有教训）。
- 失败一律 `return None` + `log`，**绝不抛到调用方**。

### 2) 分列（`_plan_columns`，waveform.py:231-239）

默认 **每秒 200 列**（`COLUMNS_PER_SEC`，约 5 ms/列）。每列对应一段**等长且不重叠**的 samples；
末尾不足一列的余数丢弃（`win = n // ncol`，`ncol = n // win`，保证 `win*ncol <= n`）。

- **为什么不重叠**：重叠会让波形更平滑，但列内计算量成倍增加，肉眼在看结构上几乎无差别，不划算。

### 3) 三频段能量（`_band_energy_columns`，waveform.py:242-271）—— **整块 FFT 带通**

把每块（默认 120 s）时域信号做一次 `np.fft.rfft`，用频域掩码切出低/中/高三个频带，
`np.fft.irfft` 变回时域，最后在每列样本窗内取均方（= 该列该频段的能量）。

- **为什么是"整块 FFT 带通"而非"每列各自 FFT"**：每列只有 80 个样本（5 ms），
  其自身 FFT 的频率分辨率 ~200 Hz，**根本分不开 100 Hz 与 1 kHz**；整块 FFT 的分辨率是
  `sr/块长`（毫秒级频率精度），能干净切频带，再回时域按短窗测能量，既准又稳。
- **为什么分块**：一次对整轨（几分钟 → 数百万点）做 `rfft`，内存与耗时都不划算。
  按 `CHUNK_SEC` 分块、**块间留 `OVERLAP_SEC` 上下文**、只取块内非边缘列的能量，
  把峰值内存压到几十 MB，又几乎无边界伪影。
- 频带：低 `(20, 200)`→R、中 `(200, 2000)`→G、高 `(2000, 7800)`→B（`BAND_*`）。
  下限 20 Hz 去直流；上限 7800 Hz 留 Nyquist 边缘。

### 4) 幅度（`_amp_columns`，waveform.py:286-299）

```
rms  = sqrt(mean(x²))          # 段落密度 / 持续能量
peak = max(|x|)                # 保住瞬态（鼓点的那一下）
amp  = sqrt(rms * peak)        # 几何平均
```

- **为什么几何平均**：单用 RMS 会漏掉稀疏鼓点（被平均掉），单用 peak 会让整轨都是满高，几何平均兼顾两者。
- 全局归一用 **99 分位**（`AMP_PERCENTILE`）而非最大值：抗"单个尖峰把整轨压平"。
- **弱段也看得清**：对全局归一后的幅度做 1 s（`MOVAVG_SEC`）移动平均得"局部能量包络"，
  再 `sqrt(全局归一 × 局部归一)` 做最终幅度 —— 强段不被削平、弱段也不因整体很响被压成一条线。

### 5) 配色（`_colors`，waveform.py:302-321）

```
逐频段整轨均值 e_band / mean_band   # 让"某段落某频段相对突出"显出来
→ 逐列占比 (r+g+b≈1)                # 天然避免"整体很响 ⇒ 一片亮色墙"
→ c = c ** GAMMA                     # 温和强调，拉开频段对比
```

- **为什么要逐频段整轨归一**：不这样，鼓/贝斯永远压住高频、画面清一色红 —— 这是"看结构"的关键一步。
- **为什么要逐列占比归一**：天然规避"整个副歌都是一片亮色墙"，保持颜色分布有意义。
- `GM_FLOOR_REL` 下限保护（waveform.py:310-312）：某频段整轨近乎无能量时其均值趋近 0，
  直接相除会把数值噪声归一成"大占比"；故把每频段均值下限钉在「最强频段均值 × 1e-3」，
  使该频段占比自然趋近 0。
- 静音列（三段能量全 0）→ 中性灰 `1/3`。

### 6) 缓存（waveform.py:123-162、353-405）

- **位置**：复用项目既有约定 `ercue.config.app_base_dir()/cache/waveform`
  （即 `%LOCALAPPDATA%\EasyRealityCue\cache\waveform`）；取不到则退到系统临时目录。
- **键**：`sha1(绝对路径 | mtime | size | 版本 | 列数)[:24]`，落 `.npz`（`np.savez_compressed`）。
- ⚠ **列数进键**：不同列数是**不同的数据**，混用会画错（比只按 path+mtime+size 更安全）。
- `load_or_compute()` 命中缓存**直接返回、不再解码**；`force=True` / `use_cache=False` 强制重算。
- 写入走 `.tmp.npz` + `os.replace` 原子替换，避免半截文件。

---

## 二、参数取值与理由

| 参数 | 取值 | 理由 |
|---|---|---|
| `ANALYSIS_SR` | 16000 | 降采样省算力；Nyquist=8k 足够覆盖三频段上限 7.8k |
| `COLUMNS_PER_SEC` | 200 | 约 5 ms/列，结构看得清；再多是浪费（画不出来） |
| `GAMMA` | **2.4** | **温和**强调。不用更大：γ 过大会把微小占比推成纯色，相邻列像彩色噪点乱跳、久看刺眼 |
| `AMP_PERCENTILE` | 99.0 | 全局归一参考分位，抗单尖峰压平整轨 |
| `MOVAVG_SEC` | 1.0 | 局部能量包络窗 |
| `LOCAL_FLOOR` | 0.10 | 局部归一的分母下限，防弱段被无限放大 |
| `BAND_LOW` | (20, 200) | 低音/底鼓 → R；下限 20 Hz 去直流 |
| `BAND_MID` | (200, 2000) | 人声/主旋律 → G |
| `BAND_HIGH` | (2000, 7800) | 镲片/齿音 → B；上限 7.8k 留边缘 |
| `CHUNK_SEC` | 120.0 | FFT 分块长度；平衡内存与块数 |
| `OVERLAP_SEC` | 0.5 | 块间上下文，只用于消带通边界伪影 |
| `GM_FLOOR_REL` | 1e-3 | 逐频段均值的相对下限，防"把数值噪声归一成大占比" |
| `CACHE_VERSION` | 1 | 缓存格式版本；算法改动时递增即整体失效 |

> 关于派单提到的"γ 别调大"：已按要求取 **2.4**，并在 docstring 与上表写明"过大会变纯色/噪点"的理由。
> 派单提到的"局部高能量区域平滑（我实现了一个变体）"：本实现用 **1 s 移动平均包络 + 几何平均**
> 做局部对比自适应（`_amp_columns`），而非逐帧扫描 sustained 区，效果等价且更简单、O(n)。

---

## 三、判据结果（可复跑：`tools/_waveform_verify.py`）

跑法：

```
<项目根>\EasyRealityCue\venv\Scripts\python.exe  tools/_waveform_verify.py
```

夹具用 `stdlib wave` + numpy 合成，落 `tempfile.mkdtemp()`；缓存目录被改写到临时目录（不污染用户目录）。
**实测 5/5 全过**：

### 判据 1 — 频段响应 ✅

| 输入纯音 | r | g | b | 主导通道 |
|---|---|---|---|---|
| 100 Hz | **1.000** | 0.000 | 0.000 | r ✅ |
| 1 kHz | 0.000 | **1.000** | 0.000 | g ✅ |
| 6 kHz | 0.000 | 0.000 | **1.000** | b ✅ |

（纯音下非对应频段能量近 0，逐列占比自然趋近纯色 —— 正是期望行为；判据要求"显著高于其它"，实得 1.000 vs 0.000。）

### 判据 2 — 瞬态保留 ✅

安静段（0.2~1.8 s）amp 中位数 **0.062**（> 0，弱段可见）；
4 个鼓点列 amp **均 = 1.000**（均值 1.000）—— 远高于安静段，证明 `sqrt(RMS×peak)` 保住了瞬态。

### 判据 3 — 时长正确 ✅

10 秒素材 ⇒ `duration_sec = 10.0000`（判据 ±0.1），列数 2000。

### 判据 4 — 缓存生效 ✅

| | 耗时 | 进度回调次数 |
|---|---|---|
| 首次 `load_or_compute` | 0.1031 s | 135（含解码阶段） |
| 缓存命中 | **0.0086 s** | 1（只有末值 `1.0`） |
| `force=True` | 重算 | 135（绕缓存） |

缓存命中**无解码阶段回调** ⇒ 证明**未重新解码**；且第二次数据与首次一致。

### 判据 5 — 性能（3 分钟）✅

夹具 3 分钟 WAV（31.8 MB）：

```
端到端 compute = 0.80 s   （判据 ≤ 5 s）
列数 = 36000   时长 = 180.00 s
安静段 amp = 0.189   副歌段 amp = 0.904（比值 4.78）  ⇒ 结构可见
```

### 判据 6 — 编译 / 回归 ✅

- `python -m compileall -q src/` ⇒ **零错误**。
- `tools/_waveform_verify.py` ⇒ 5/5 全过。
- `tools/selftest.py` ⇒ **7/7 全绿**。
- `tests/`（test_audio / test_compositor / test_media / test_timeline / test_ui_smoke）逐个跑 ⇒ **全绿**（本机无 pytest，测试文件自带 `main()`）。
- 模块自检 `PYTHONPATH=src python -m ercue.audio.waveform` ⇒ `列数=600 时长=3.000s 首次=0.328s 缓存命中=0.009s`。

### 附加边界鲁棒性（自查，非派单判据）

| 场景 | 结果 |
|---|---|
| 不存在的路径 | `compute → None`（不崩） |
| 非音频文件（.txt） | `compute → None`（解码失败仅记日志） |
| 极短音频（0.02 s） | `columns=4, duration=0.0200`，正常返回 |
| 显式 `columns=7` | `columns=7`（尊重调用方指定） |
| `progress` 回调自身抛异常 | 计算不受影响、不崩（`_call` 吞异常） |

---

## 四、与派单要求的对应

| 派单要求 | 落实 |
|---|---|
| 新文件 `src/ercue/audio/waveform.py`，只有数据层、不 import PySide6 | ✅ 全文无 Qt import（仅注释里提到"零 Qt"） |
| `WaveformData(columns/amp/r/g/b/duration_sec)` | ✅ waveform.py:92-117，另附 `as_lists()` / `color_at()` |
| `compute(path, columns=None, progress=None) -> WaveformData\|None` | ✅ waveform.py:327 |
| `cache_path_for(...)` / `load_or_compute(...)` | ✅ waveform.py:153 / 389 |
| PyAV 解码到降采样单声道；时长用真实值 | ✅ 16 kHz；`duration=N/sr` |
| 每秒 ~200 列 | ✅ `COLUMNS_PER_SEC=200` |
| `amp = sqrt(RMS×peak)` | ✅ waveform.py:291 |
| 三频段 低→R / 中→G / 高→B | ✅ `BAND_*` |
| 温和强调 γ≈2.4（不要更大） | ✅ `GAMMA=2.4` |
| 逐频段整轨归一（占比和≈1），不要把响轨吹成亮色墙 | ✅ `_colors` + `GM_FLOOR_REL` + 逐列占比 |
| 缓存 key = path+mtime+size 哈希，复用既有缓存约定，`.npz` | ✅ `sha1(...)` + `app_base_dir()/cache/waveform` + `np.savez_compressed` |
| 3 分钟端到端 ≤5 s | ✅ 0.80 s |
| `progress` 回调 0..1 | ✅ 0.0 / 解码 0.02~0.48 / 频段 0.50~0.90 / 1.0 |
| **只加** `waveform.py` + `_waveform_verify.py`；**不动** `ui/timeline_widget.py` | ✅ `git status` 仅新增 `src/ercue/audio/waveform.py`（tools/ 未纳管） |
| 算法自研，不复制第三方源码 | ✅ 全部基于通用 DSP 原理自行实现 |

---

## 五、给接线方（UI 层）的用法说明

本模块**不做线程调度、不碰 UI**。UI 侧建议：

```python
from ercue.audio import waveform as wf

# 在后台线程内调用（同步、耗时可忽略；3 分钟 ~0.8s，缓存命中 ~10ms）
data = wf.load_or_compute(audio_path, progress=lambda p: emit_signal(p))
if data is not None:
    # data.columns / data.amp[i] / data.color_at(i) / data.duration_sec
    # 时长以 data.duration_sec 为准（真实时长）
    ...
```

- 列密度用默认 200/s 即可；如需更粗/更细，传 `columns=`（会进缓存键，互不污染）。
- 想强制重算传 `force=True`。
