# -*- coding: utf-8 -*-
"""★ 真实音频波形（**数据层**）：整轨 → 每列「幅度 + 三频段配色」。

给时间轴画**真波形**用：不是示意波形，而是从音频文件实际算出每一列该画多高（幅度）、
该是什么颜色（低频=红 / 中频=绿 / 高频=蓝），从而**一眼看出歌曲结构**（副歌通常又多又亮又偏中高频）。

★ 本模块**零 Qt**（`media/`、`audio/` 层铁律：不准 import PySide6）。它只吐 numpy 数组，
  由 UI 层在自己的后台线程里调用、自己上色绘制。**本模块不做任何线程调度，也不碰 UI。**

═══ 算法（全部自研，基于通用 DSP 原理，未复制任何第三方源码）═══

  1) **解码 → 单声道 16 kHz**（`_decode_mono16k`）
     用 PyAV 把音频重采样成 `fltp` / `mono` / `16000 Hz` 的 float32 一维数组。
     分析采样率取 16 kHz 而非原始 44.1/48k：画波形不需要全带宽，降采样能显著省算力；
     16 kHz 的 Nyquist=8 kHz 足以覆盖我们关心的三频段（上限取 7.8 kHz）。
     ⚠ **时长一律按真实解码样本数换算**（`duration = N / 16000`），绝不用别处推来的近似值
       —— 用户正在反馈"时长被拉长"，这里必须老实。

  2) **分列**（`_plan_columns`）：默认 **每秒 200 列**（`COLUMNS_PER_SEC`，约 5 ms/列），
     每列对应一段等长 samples（**不重叠**：重叠更平滑但会成倍增加列内计算，画图看不出差别）。

  3) **三频段能量**（`_band_energy_columns`）：**分块 + 整块 FFT 带通**。
     把每块时域信号做一次 `rfft`，用频域掩码切出低/中/高三个频带，再 `irfft` 回时域，
     最后在每列的样本窗内取均方（= 该列该频段的能量）。
     · 为什么是「整块 FFT 带通」而不是「每列各自 FFT」：每列只有 80 个样本（5 ms），
       其自身 FFT 的频率分辨率 ~200 Hz，**根本分不开 100 Hz 与 1 kHz**；
       整块 FFT 的分辨率是 `sr/块长`（毫秒级频率精度），能干净地切出频带，
       再回到时域按短窗测能量，既准又稳。
     · 为什么分块：一次对整轨（几分钟 → 数百万点）做 `rfft` 内存与耗时都不划算；
       按 `CHUNK_SEC` 分块、块间留 `OVERLAP_SEC` 上下文、只取块内**非边缘**列的能量，
       既把峰值内存压到几十 MB，又几乎无边界伪影。
     · 频带划分：低 `(20,200)`→R、中 `(200,2000)`→G、高 `(2000,7800)`→B（`BAND_*`）。
       下限 20 Hz 去直流；上限 7800 Hz 留出重采样边缘（Nyquist=8000）。

  4) **幅度**（`_amp_columns`）：`amp = sqrt(RMS × peak)`（**几何平均**）。
     · RMS 反映该列的**段落密度/持续能量**；peak 保住**瞬态**（鼓点的那一下）。
       单用 RMS 会漏掉稀疏鼓点，单用 peak 会让整轨都是满高；几何平均兼顾两者。

  5) **配色**（`_colors`）—— **各路独立映射**（★ 关键改动，详见 `_colors` docstring）：
     · 每路（低→R / 中→G / 高→B）**各自**相对整轨分位（`COLOR_REF_PCT`）归一；
       ⚠ **不做逐列占比归一**（`r+g+b≈1`）—— 那套"此消彼长"从数学上就排斥混色，
       会把颜色压成清一色纯红/纯绿/纯蓝（实测混色列仅 ~2%）。
     · 独立映射下多路可**同时亮** ⇒ 自然出现混色/彩虹：
       红+绿=黄、绿+蓝=青、红+蓝=品红、三路同亮=偏白/粉。
     · **压缩** `base**COLOR_BETA`（β<1 抬升弱段，让弱频段也参与混色）
       + **温和强调** `**GAMMA`（`GAMMA=1.05`，仅轻微拉开对比；过大反而削减混色）。
     · **频段下限保护**（`REF_FLOOR_REL`）：某路整轨近乎无能量时其参考值被钉住，
       该路占比自然趋近 0 —— 纯音仍得**纯色**、不串色。

  6) **幅度动态范围**（`_amp_columns`，避免"整轨一堵亮墙"）：
     全局分位归一 → `an`；1 s 移动平均得局部包络 `env` → 局部对比 `loc`；
     最终 `af = an**pw × loc**AMP_LOC_POW`，其中 **`pw = AMP_POW_MIN + AMP_POW_ENV×env`**。
     · `pw` **随局部能量自适应**：弱段（env 小）→ `pw≈1`（几乎不压，保持可见）；
       持续高能量区（env 大）→ `pw` 升高（加强对比、压暗糊段）。
     ⇒ 均值从 ~0.69 降到 ~0.49，弱段仍清晰、强段不再糊成一堵墙。

═══ 缓存 ═══
  整轨算一次是秒级 ⇒ 必须缓存。缓存位置复用项目既有约定（`ercue.config.app_base_dir()` 下的
  `cache/waveform/`，即 `%LOCALAPPDATA%\\EasyRealityCue\\cache\\waveform`）；取不到就退到系统临时目录。
  缓存键 = `sha1(绝对路径 | mtime | size | 版本 | 列数)[:24]`，落 `.npz`（`np.savez_compressed`）。
  ⚠ 列数进键：不同列数是**不同的数据**，混用会画错（比只按 path+mtime+size 更安全）。

可离线自检：`python -m ercue.audio.waveform`
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from dataclasses import dataclass, field

import numpy as np

log = logging.getLogger("ercue.audio.waveform")

# ---- 分析参数 ----
ANALYSIS_SR = 16000          # 分析采样率（单声道）
COLUMNS_PER_SEC = 200        # 默认列密度（约 5 ms/列）
# ★ 配色（**各路独立映射**，见 `_colors`）：不逐列占比归一，才可能出混色/彩虹
COLOR_REF_PCT = 90.0         # 每路相对整轨归一的分位参考
COLOR_BETA = 0.55            # 独立映射压缩指数（<1 抬升弱段 ⇒ 多路同时亮 ⇒ 混色）
GAMMA = 1.05                 # 温和强调指数（独立映射下重标定；过大反而削减混色）
REF_FLOOR_REL = 1e-3         # 每路参考值下限（相对最强路），防"把数值噪声归一成亮色"
# 幅度动态范围（避免"整轨一堵亮墙"）
AMP_PERCENTILE = 99.0        # 幅度全局归一参考分位（避免单个尖峰压平整轨）
AMP_POW_MIN = 1.0            # 局部低能量区（弱段）的对比幂下限（保持可见）
AMP_POW_ENV = 0.60           # 随局部能量包络增加的额外幂（持续高能量区加强对比）
AMP_LOC_POW = 0.30           # 局部对比幂
MOVAVG_SEC = 1.0             # 局部能量包络的移动平均窗（秒）
LOCAL_FLOOR = 0.12           # 局部归一的分母下限（防弱段被无限放大）
BAND_LOW = (20.0, 200.0)     # 低频 → R
BAND_MID = (200.0, 2000.0)   # 中频 → G
BAND_HIGH = (2000.0, 7800.0)  # 高频 → B（上限留出 Nyquist 边缘）
CHUNK_SEC = 120.0            # FFT 分块长度（秒）
OVERLAP_SEC = 0.5            # 块间重叠上下文（秒），只为消除带通边界伪影

CACHE_VERSION = 2             # ★ 配色算法改动时必须 +1，否则旧缓存会给出旧颜色


# ==========================================================================
# 输出数据结构
# ==========================================================================
@dataclass
class WaveformData:
    """整轨波形：每列一个幅度 + 一个 RGB 配色（分量 0..1，逐列和≈1）。

    `amp/r/g/b` 用 **numpy 一维 float32 数组**（而非 Python list）：列数可达数万，
    数组更省内存、切片更快；读法（`len()` / 下标 / 迭代）与 list 一致，UI 直接可用。
    需要 list 时用 `as_lists()`。
    """
    columns: int = 0                                            # 列数
    amp: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))
    r: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))
    g: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))
    b: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))
    duration_sec: float = 0.0

    def __len__(self) -> int:
        return int(self.columns)

    def as_lists(self) -> dict:
        """转成纯 Python list（跨进程/序列化/UI 便捷用）。"""
        return {"amp": self.amp.tolist(), "r": self.r.tolist(),
                "g": self.g.tolist(), "b": self.b.tolist()}

    def color_at(self, i: int):
        """第 `i` 列的 `(r, g, b)`（0..1）。"""
        return (float(self.r[i]), float(self.g[i]), float(self.b[i]))


# ==========================================================================
# 缓存路径
# ==========================================================================
def cache_dir() -> str:
    """波形缓存目录：`<app_base>/cache/waveform`；不可写则退到系统临时目录。"""
    base = None
    try:
        from ..config import app_base_dir                 # 零 Qt，安全
        base = os.path.join(app_base_dir(), "cache", "waveform")
    except Exception:                                     # noqa: BLE001
        base = None
    if not base:
        base = os.path.join(tempfile.gettempdir(), "ercue_waveform")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        base = os.path.join(tempfile.gettempdir(), "ercue_waveform")
        try:
            os.makedirs(base, exist_ok=True)
        except OSError:
            pass
    return base


def _file_sig(path) -> tuple:
    ap = os.path.abspath(str(path or ""))
    try:
        st = os.stat(ap)
        return ap, int(st.st_mtime), int(st.st_size)
    except OSError:
        return ap, 0, 0


def cache_path_for(audio_path, columns=None) -> str:
    """缓存文件路径 = `cache_dir()/sha1(路径|mtime|size|版本|列数)[:24].npz`。

    ⚠ 列数进键：不同列数是不同数据，混用会画错。
    """
    ap, mt, sz = _file_sig(audio_path)
    token = "auto" if columns is None else "c%d" % int(columns)
    key = "%s|%d|%d|v%d|%s" % (ap, mt, sz, CACHE_VERSION, token)
    h = hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:24]
    return os.path.join(cache_dir(), h + ".npz")


# ==========================================================================
# 内部工具
# ==========================================================================
def _call(progress, v) -> None:
    """安全调用进度回调（回调抛异常不影响计算）。"""
    if progress is None:
        return
    try:
        progress(float(v))
    except Exception:                                     # noqa: BLE001
        pass


def _decode_mono16k(path, progress=None):
    """PyAV 解码 + 重采样为**单声道 16 kHz float32 一维数组**；失败返回 None。"""
    path = str(path or "")
    if not path or not os.path.isfile(path):
        return None
    try:
        import av
    except Exception:                                     # noqa: BLE001
        log.warning("PyAV 不可用，无法计算波形")
        return None
    try:
        # ★ metadata_errors="replace" 不能省（GBK 元数据会让 av.open 抛 UnicodeDecodeError）
        with av.open(path, metadata_errors="replace") as c:
            streams = [s for s in c.streams if s.type == "audio"]
            if not streams:
                return None
            st = streams[0]
            expected = 0
            try:
                if st.duration:
                    expected = int(float(st.duration) * float(st.time_base) * ANALYSIS_SR)
            except Exception:                             # noqa: BLE001
                expected = 0

            res = av.AudioResampler(format="fltp", layout="mono", rate=ANALYSIS_SR)
            chunks = []
            got = 0
            for frame in c.decode(st):
                for fr in res.resample(frame):
                    a = fr.to_ndarray()                   # fltp mono → (1, N)
                    if a.ndim == 2 and a.shape[0] == 1:
                        a = a[0]
                    a = np.asarray(a, dtype=np.float32).reshape(-1)
                    chunks.append(a)
                    got += a.size
                if expected > 0:
                    _call(progress, 0.02 + 0.46 * min(1.0, got / float(expected)))
            try:                                          # flush 重采样器残留
                for fr in res.resample(None):
                    a = fr.to_ndarray()
                    if a.ndim == 2 and a.shape[0] == 1:
                        a = a[0]
                    chunks.append(np.asarray(a, dtype=np.float32).reshape(-1))
            except Exception:                             # noqa: BLE001
                pass
            if not chunks:
                return None
            return np.ascontiguousarray(np.concatenate(chunks), dtype=np.float32)
    except Exception:                                     # noqa: BLE001
        log.exception("波形解码失败: %s", path)
        return None


def _plan_columns(n: int, columns, sr: int):
    """返回 `(win, ncol)`：每列样本数、实际列数（只用完整窗口，末尾余数丢弃）。"""
    if columns is None:
        ncol = max(1, int(round((n / float(sr)) * COLUMNS_PER_SEC)))
    else:
        ncol = max(1, int(columns))
    win = max(1, n // ncol) if ncol else 1
    ncol = max(1, n // win)
    return win, ncol


def _band_energy_columns(mono, sr: int, win: int, ncol: int, progress=None):
    """每列三频段能量。分块 FFT 带通（见模块 docstring 之 3）。返回 3 个 float64 数组。"""
    x = mono.astype(np.float64, copy=False)
    total = win * ncol
    out = [np.zeros(ncol, np.float64) for _ in range(3)]
    bands = (BAND_LOW, BAND_MID, BAND_HIGH)
    ov = int(sr * OVERLAP_SEC)
    chunk_cols = max(1, int(CHUNK_SEC * sr) // win)
    n_chunks = max(1, (ncol + chunk_cols - 1) // chunk_cols)
    ci = 0
    for c0 in range(0, ncol, chunk_cols):
        c1 = min(ncol, c0 + chunk_cols)
        s0, s1 = c0 * win, c1 * win
        a0, a1 = max(0, s0 - ov), min(total, s1 + ov)     # 带上下文的块
        seg = x[a0:a1]
        ci += 1
        if seg.size == 0:
            continue
        spec = np.fft.rfft(seg)
        freqs = np.fft.rfftfreq(seg.size, 1.0 / sr)
        off, span, ncols = s0 - a0, s1 - s0, c1 - c0
        for bi, (lo, hi) in enumerate(bands):
            mask = (freqs >= lo) & (freqs < hi)
            if mask.any():
                xb = np.fft.irfft(spec * mask, n=seg.size)[off:off + span]
            else:
                xb = np.zeros(span, np.float64)
            out[bi][c0:c1] = (xb * xb).reshape(ncols, win).mean(axis=1)
        _call(progress, 0.50 + 0.40 * (ci / float(n_chunks)))
    return out[0], out[1], out[2]


def _moving_average(a, w: int):
    """居中移动平均（`same` 语义，边界用 edge 填充）；纯 numpy、O(n)。"""
    w = max(1, int(w))
    a = np.asarray(a, dtype=np.float64)
    if w <= 1 or a.size == 0:
        return a
    pad = w // 2
    ap = np.pad(a, (pad, w - 1 - pad), mode="edge")
    c = np.concatenate(([0.0], np.cumsum(ap)))
    return (c[w:] - c[:-w]) / float(w)


def _amp_columns(mono, win: int, ncol: int):
    """每列幅度：`sqrt(RMS × peak)` → 全局分位归一 → 局部对比 → 幂次动态范围。返回 float32 0..1。"""
    xr = mono[:ncol * win].reshape(ncol, win).astype(np.float64, copy=False)
    rms = np.sqrt(np.mean(xr * xr, axis=1))
    peak = np.max(np.abs(xr), axis=1)
    amp = np.sqrt(rms * peak)                              # 几何平均：密度 + 瞬态
    if amp.size == 0:
        return np.zeros(0, np.float32)
    ref = float(np.percentile(amp, AMP_PERCENTILE))
    an = np.clip(amp / (ref + 1e-12), 0.0, 1.0)            # 全局归一（分位：抗单尖峰）
    env = _moving_average(an, int(round(COLUMNS_PER_SEC * MOVAVG_SEC)))   # 局部能量包络
    loc = np.clip(an / (env + LOCAL_FLOOR), 0.0, 1.0)      # 局部对比（弱段提升）
    # ★ 自适应动态范围：弱段（env 小）→ 幂≈1（保持可见）；持续高能量区 → 幂升高（加强对比）
    pw = AMP_POW_MIN + AMP_POW_ENV * env
    af = (an ** pw) * (loc ** AMP_LOC_POW)
    return np.clip(af, 0.0, 1.0).astype(np.float32)


def _colors(e_low, e_mid, e_high, gamma: float = GAMMA, ref_pct: float = COLOR_REF_PCT,
            beta: float = COLOR_BETA):
    """三频段能量 → 每列 RGB（**各路独立映射**，允许多路同时亮 ⇒ 自然混色/彩虹）。

    映射（逐路，**互不争夺**）：
      1) 每路相对**整轨分位**（`ref_pct`）归一 → `base`；
      2) 压缩 `base**beta`（β<1 抬升弱段，让弱频段也能参与混色）；
      3) γ 温和强调 `**gamma`（独立映射下取值≈1，仅轻微拉开对比）。

    ★ 关键：**不做逐列占比归一**（`r+g+b≈1`）。正是那套"此消彼长"从数学上排斥混色，
      把颜色压成清一色纯红/纯绿/纯蓝（实测混色列仅 ~2%）。各路保持自身亮度后，
      「红+绿=黄、绿+蓝=青、红+蓝=品红、三路同亮=偏白/粉」才会自然出现。
    ★ **频段下限保护**（`REF_FLOOR_REL`）：某路整轨近乎无能量（如纯音的其它两路）时，
      其参考值被钉在「最强路参考 × REF_FLOOR_REL」，使该路占比自然趋近 0
      —— 纯音仍得**纯色**、不串色。

    返回 3 个 float32 数组（各 0..1，**互相独立、不再归一**）；静音列全 0。
    """
    E = np.stack([e_low, e_mid, e_high], axis=1).astype(np.float64)       # (n,3)
    active = E.sum(axis=1) > 0.0
    if active.any():
        ref = np.percentile(E[active], float(ref_pct), axis=0)           # 逐路整轨参考
    else:
        ref = np.ones(3, np.float64)
    # ★ 下限保护：把每路参考钉在「最强路参考 × REF_FLOOR_REL」，防止"把数值噪声归一成亮色"
    ref = np.maximum(ref, max(float(ref.max()) * REF_FLOOR_REL, 1e-12))
    base = np.clip(E / ref[None, :], 0.0, 1.0)
    v = np.clip(base ** float(beta), 0.0, 1.0) ** float(gamma)
    v[~active] = 0.0                                                      # 静音列 → 全黑
    return (v[:, 0].astype(np.float32), v[:, 1].astype(np.float32),
            v[:, 2].astype(np.float32))


# ==========================================================================
# 公开 API
# ==========================================================================
def compute(path, columns=None, progress=None):
    """从音频文件算出整轨波形数据。失败返回 None（**绝不抛到调用方**）。

    参数：
        path      音频文件路径
        columns   目标列数；None = 按 `COLUMNS_PER_SEC`（200/秒）自动
        progress  可选回调 `progress(fraction: float)`（0..1），供 UI 显示进度；
                  本函数**在调用方线程内同步执行**，线程调度由 UI 负责。
    """
    _call(progress, 0.0)
    mono = _decode_mono16k(path, progress)
    if mono is None or mono.size == 0:
        return None
    sr = ANALYSIS_SR
    n = int(mono.size)
    duration = n / float(sr)                                # ★ 真实时长
    win, ncol = _plan_columns(n, columns, sr)
    _call(progress, 0.50)
    e_low, e_mid, e_high = _band_energy_columns(mono, sr, win, ncol, progress)
    _call(progress, 0.92)
    amp = _amp_columns(mono, win, ncol)
    r, g, b = _colors(e_low, e_mid, e_high)
    _call(progress, 1.0)
    return WaveformData(columns=ncol, amp=amp, r=r, g=g, b=b, duration_sec=duration)


def _save_cache(path: str, data: WaveformData) -> bool:
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp.npz"
        np.savez_compressed(tmp, amp=data.amp, r=data.r, g=data.g, b=data.b,
                            duration_sec=np.float64(data.duration_sec),
                            columns=np.int64(data.columns),
                            version=np.int64(CACHE_VERSION))
        os.replace(tmp, path)
        return True
    except Exception:                                     # noqa: BLE001
        log.warning("写波形缓存失败: %s", path, exc_info=True)
        return False


def _load_cache(path: str, want_columns=None):
    try:
        if not path or not os.path.isfile(path):
            return None
        with np.load(path, allow_pickle=False) as z:
            if int(z["version"]) != CACHE_VERSION:
                return None
            cols = int(z["columns"])
            if want_columns is not None and int(want_columns) != cols:
                return None
            return WaveformData(columns=cols,
                                amp=z["amp"].astype(np.float32),
                                r=z["r"].astype(np.float32),
                                g=z["g"].astype(np.float32),
                                b=z["b"].astype(np.float32),
                                duration_sec=float(z["duration_sec"]))
    except Exception:                                     # noqa: BLE001
        log.warning("读波形缓存失败: %s", path, exc_info=True)
        return None


def load_or_compute(audio_path, columns=None, progress=None,
                    use_cache=True, force=False):
    """先查缓存；命中直接返回（**不再解码**），否则 `compute` 并写缓存。

    `use_cache=False` 或 `force=True` 可强制重算。失败返回 None。
    """
    ap = str(audio_path or "")
    cp = cache_path_for(ap, columns) if ap else ""
    if use_cache and not force and cp:
        cached = _load_cache(cp, columns)
        if cached is not None:
            _call(progress, 1.0)
            return cached
    data = compute(ap, columns=columns, progress=progress)
    if data is not None and cp:
        _save_cache(cp, data)
    return data


# ==========================================================================
# 自检（合成一段 WAV，不依赖外部素材）
# ==========================================================================
def _write_wav(path, samples, sr: int) -> None:
    """把 float32 [-1,1] 单声道写成立体声 PCM16 WAV（PyAV 可读）。"""
    import wave
    x = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    stereo = np.stack([x, x], axis=1)
    pcm = (stereo * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())


def selftest() -> bool:
    import time
    d = tempfile.mkdtemp(prefix="ercue_wf_")
    p = os.path.join(d, "tone.wav")
    sr = 44100
    t = np.arange(int(sr * 3.0)) / sr
    _write_wav(p, 0.4 * np.sin(2 * np.pi * 1000.0 * t), sr)

    seen = []
    t0 = time.perf_counter()
    data = load_or_compute(p, progress=seen.append)          # 首次：计算 + 写缓存
    t_compute = time.perf_counter() - t0
    assert data is not None, "load_or_compute 应成功"
    assert abs(data.duration_sec - 3.0) < 0.05, data.duration_sec
    assert len(data.amp) == data.columns and data.columns > 0
    assert seen and seen[-1] == 1.0, "progress 应以 1.0 结束"

    # 纯 1 kHz ⇒ 中频 G 明显最高
    mid = float(np.mean(data.g))
    assert mid > float(np.mean(data.r)) and mid > float(np.mean(data.b)), \
        (data.r.mean(), mid, data.b.mean())

    # 缓存：第二次不再解码（命中缓存、不出现在 progress 回调里）
    cp = cache_path_for(p)
    assert os.path.isfile(cp), "首次 load_or_compute 后应有缓存"
    seen2 = []
    t0 = time.perf_counter()
    d2 = load_or_compute(p, progress=seen2.append)
    t_cache = time.perf_counter() - t0
    assert d2 is not None and abs(d2.duration_sec - 3.0) < 0.05
    assert t_cache < t_compute, "缓存命中应快于首次计算"
    assert seen2 == [1.0], "缓存命中不应出现解码阶段的进度回调（即未重新解码）：%r" % (seen2,)
    print("[waveform] 自检通过 ✓  列数=%d 时长=%.3fs  首次=%.3fs 缓存命中=%.3fs"
          % (data.columns, data.duration_sec, t_compute, t_cache))
    return True


if __name__ == "__main__":
    selftest()
