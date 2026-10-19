# -*- coding: utf-8 -*-
"""音频采集与分析引擎
- 系统声音：WASAPI Loopback（soundcard 库）
- 麦克风 / 线路输入：普通录制设备
- 分析：FFT 频段能量、RMS 电平、节拍检测（谱通量+自相关 BPM 估计）、Drop 检测
- 行为模式指标：瞬态密度 / BPM 稳定性 / 节拍清晰度 / 频谱复杂度 / 能量趋势 / 人声占比 / 段落识别

⚠ 线程模型（重要）：
soundcard 库导入时会在【当前线程】一次性 CoInitializeEx(MTA)，且没有按线程初始化。
因此本模块把 soundcard 的导入、设备枚举、录音全部收敛到一条常驻后台线程（_worker_loop），
绝不能在 GUI 主线程触碰 soundcard，否则 Qt OleInitialize 失败（0x80010106），
系统文件对话框（IFileDialog 需要 COM STA）会直接卡死。
"""
import time
import queue
import threading
from collections import deque
import numpy as np

BLOCK = 1024


def _sc():
    """导入 soundcard（只能在音频后台线程调用一次）"""
    import soundcard as sc
    return sc


def _native_samplerate(mic):
    """读设备的 WASAPI 原生混音采样率（PKEY_AudioEngine_DeviceFormat）。

    只在音频后台线程调用（soundcard 的 COM 限制）。返回 int 采样率，失败返回 None。
    用于「自动采样率」：采设备原生采样率可避免 WASAPI 共享模式的 SRC 重采样——
    重采样会轻微抹平瞬态（低频冲击/kick 检测的核心信号），导致能量偏低
    （用户：开 NVIDIA 录制后能量偏低——录屏把系统混音器切成 48k，软件却固定采 44.1k）。"""
    try:
        import soundcard.mediafoundation as mf
        ppPropertyStore = mf._ffi.new('IPropertyStore **')
        ptr = mic._device_ptr()
        hr = ptr[0][0].lpVtbl.OpenPropertyStore(ptr[0], 0, ppPropertyStore)
        mf._com.release(ptr)
        mf._com.check_error(hr)
        propvariant = mf._PropVariant()
        PKEY = mf._ffi.new('PROPERTYKEY *',
            [[0xf19f064d, 0x82c, 0x4e27, [0xbc, 0x73, 0x68, 0x82, 0xa1, 0xbb, 0x8e, 0x4c]], 0])
        hr = ppPropertyStore[0][0].lpVtbl.GetValue(ppPropertyStore[0], PKEY, propvariant.ptr)
        mf._com.release(ppPropertyStore)
        mf._com.check_error(hr)
        pBlob = mf._ffi.cast('BLOB_PROPVARIANT *', propvariant.ptr)
        wf = mf._ffi.cast('WAVEFORMATEX *', pBlob[0].blob.pBlobData)
        return int(wf[0].nSamplesPerSec)
    except Exception:
        return None


def _band_energy(spec, freqs, lo, hi):
    m = (freqs >= lo) & (freqs < hi)
    if not m.any():
        return 0.0
    return float(np.mean(spec[m]))


class AudioState:
    """线程间共享的音频分析状态"""

    def __init__(self):
        self.lock = threading.Lock()
        self.level = 0.0            # RMS 0..1
        self.bass = 0.0             # 低频能量 0..1（平滑）
        self.mid = 0.0
        self.high = 0.0
        self.energy = 0.0           # 综合能量 0..1
        self.bpm = 0.0              # 平滑后的 BPM（**已锁定**，拍钟/切换节奏只用这个）
        # 显示用 BPM：未锁定阶段也把「票数够、众数够集中」的估计值暴露出来给界面显示。
        # 为什么分开（2026-09-24 用户报「BPM 不显示了，一直是 0」）：
        #   建锁门控要求众数∈[115,185]，于是 慢歌(<115)、极快曲(>185) 以及
        #   **被估成半速的歌**（实测 160→80.5、180→90.5、115→76.5）**永远显示不出 BPM**，
        #   界面只剩 0。但拍钟若跟着未锁定值走，一旦它是半速值，切换节奏会错一整倍 ——
        #   所以「显示」与「驱动拍钟」必须解耦：这个字段只给界面看，不参与任何节奏计算。
        self.bpm_display = 0.0
        self.bpm_locked = False     # bpm_display 是否已是锁定值（界面据此加「≈」前缀）
        self.beat_phase = 0.0       # 0..1 拍内相位
        self.beat_count = 0         # 总拍数
        self.bar_count = 0          # 4/4 小节数（按识别出的小节头起算）
        self.downbeat_off = 0       # 小节头相位：4/4 里第 1 拍是 beat_count%4 的哪一相
        self.downbeat_conf = 0.0    # 小节头识别置信度 0..1（0=证据不足，沿用旧相位）
        self.is_beat = False        # 本帧是否踩拍
        self.is_bar = False         # 本帧是否到小节
        self.drop = False           # 本帧是否 Drop（低频冲击）
        self.beat_active = False    # 最近 2 秒内是否有鼓点活动（宽松阈值）
        self.spectrum = [0.0] * 24  # 24 段对数频谱 0..1（后处理可视化特效数据源）
        self.ndi_feed = None        # 可选：NDI 音频喂入回调 callable(float32_stereo_interleaved, sr)
        self.ndi_audio_on = False   # NDI 音频是否启用：False 时采集循环**不做任何拷贝**，
                                    # 直接把 PCM 丢掉（原实现不管有没有开 NDI，每块都要
                                    # np.asarray + reshape(-1) + copy() 一次）
        # ---- 行为模式自动选择所需的实时指标 ----
        self.onset_density = 0.0    # 瞬态密度：每秒打击次数（近 4s 窗口）
        self.bpm_stab = 0.0         # BPM 稳定性 0..1
        self.beat_clarity = 0.0     # 节拍清晰度 0..1
        self.spec_complex = 0.0     # 频谱复杂度 0..1
        self.energy_trend = 0.0     # 能量趋势 -1..1（上升为正）
        self.vocalness = 0.0        # 人声占比估计 0..1（中频主导度）
        self.segment = "none"       # none/quiet/build/drop/peak/outro
        self.silent_sec = 0.0       # 连续静音秒数
        self.lv_db = -80.0          # 当前 RMS 电平（dBFS），供 Kv 主视觉图层的静音判定使用
        self.genre_tags = []        # 实时曲风英文标签（如 ['Hardcore','House']）
        self.genre_styles = []      # 实时曲风英文风格名
        self.recognized_song_id = -1  # 指纹识别确认的歌 id（-1=无/待机）
        self.recognized_offset = -1.0  # 该歌当前播放位置（秒，来自指纹匹配 offset；-1=无）
        self.running = False
        self.error = ""
        self.device_desc = ""
        # 能量各分项（只读调试用：能量诊断工具显示「能量是怎么算出来的」，主程序不读）
        self.dbg = {}

    def snapshot(self):
        with self.lock:
            return {
                "level": self.level, "bass": self.bass, "mid": self.mid,
                "high": self.high, "energy": self.energy, "bpm": self.bpm,
                "bpm_display": self.bpm_display, "bpm_locked": self.bpm_locked,
                "beat_phase": self.beat_phase, "beat_count": self.beat_count,
                "bar_count": self.bar_count, "downbeat_off": self.downbeat_off,
                "downbeat_conf": self.downbeat_conf, "drop": self.drop,
                "is_beat": self.is_beat, "is_bar": self.is_bar,
                "beat_active": self.beat_active,
                "onset_density": self.onset_density, "bpm_stab": self.bpm_stab,
                "beat_clarity": self.beat_clarity, "spec_complex": self.spec_complex,
                "energy_trend": self.energy_trend, "vocalness": self.vocalness,
                "segment": self.segment,
                "spectrum": list(self.spectrum),
                "silent_sec": self.silent_sec, "running": self.running,
                "lv_db": self.lv_db,
                "error": self.error, "device_desc": self.device_desc,
                "recognized_song_id": self.recognized_song_id,
                "recognized_offset": self.recognized_offset,
                "genre_tags": list(self.genre_tags),
                "dbg": dict(self.dbg),      # 能量分项（诊断工具用）
            }


class _Analyzer:
    """逐块音频分析：所有跨块持久状态集中在实例里，feed() 出错不会丢线程。"""

    def __init__(self, sr):
        self.sr = sr
        self.block_sec = BLOCK / sr
        self.energy_scale = 1.0   # 能量伽马校正（>1 压低人声段、鼓点段基本不动）
        self.win = np.hanning(2048)
        self.prev_spec = None
        self.flux_hist = np.zeros(max(16, int(round(10.0 / self.block_sec))), dtype=np.float32)
        self.flux_i = 0
        self.flux_ref = 0.0        # 慢衰减通量基准（半衰期≈15s）
        self.bpm_smooth = 0.0
        self.bpm_display = 0.0               # 显示用（未锁定也可有值），不参与拍钟
        self._bpm_votes = deque(maxlen=60)   # 60s 投票窗口，众数取 BPM（换歌静音清空）
        self._bpm_lock = False               # 锁定建立标志（主段稳定后锁定，breakdown 不扰动）
        self.prev_bass_spec = None         # kick 频段上一帧谱（低频通量用）
        self.bass_flux_hist = np.zeros(max(16, int(round(10.0 / self.block_sec))), dtype=np.float32)
        self.beat_interval = 0.5
        self.beat_count = 0
        self.last_beat_t = 0.0
        self.last_strong_t = 0.0
        self.last_onset_t = 0.0
        self.last_drop_t = 0.0
        self.smooth = {"bass": 0.0, "mid": 0.0, "high": 0.0, "energy": 0.0}
        self.wdb_base = -60.0        # 能量 v4：加权能量 dB 的慢速自适应基线（≈10s EMA）
        self.loud_peak = 0.35   # 慢速追踪的曲目响度峰值（自适应母带音量）
        # 行为模式指标
        self.onset_times = deque(maxlen=256)
        self.strike_times = deque(maxlen=64)   # 近4秒强拍时间戳（打击密度用，带去重）
        self.strike_strengths = deque(maxlen=64)  # 每次强拍的相对强度（flux/flux_ref），与 strike_times 一一对应
        self.e_loud_smooth = 0.0               # e_loud 中速平滑（τ≈1.2s，压秒级波动）
        self._kick_e_smooth = 0.0              # kick_e 持续打击平滑（τ≈0.8s EMA，抹钢琴偶发尖峰）
        self.bass_peak = 0.0                   # 低频能量的全曲慢峰（只升不降，换歌重置）——低频能量分量参考
        # 低频通量（kick 冲击）1s 累计 + 全曲慢峰——鼓点强度的正确度量。
        # 人声段有贝斯垫底（低频能量 bass 不低），但 kick 脉冲的「瞬时起伏」远大于贝斯的
        # 「缓慢起伏」（实测 2.8 倍）。窗口必须够短（1s）才能凸现 kick 脉冲——
        # 4s 窗口会把脉冲和贝斯起伏平均掉，区分度从 2.8 倍抹成 1.3 倍。
        self._bflux_recent = deque(maxlen=max(16, int(round(1.0 / self.block_sec))))
        self._bdelta_recent = deque(maxlen=max(16, int(round(1.0 / self.block_sec))))
        self.fluct_peak = 0.0                  # 低频「起伏变化量」1s 累计的全曲慢峰（只升不降）
        self.prev_bass_e = None                # 上一块的低频能量（起伏差分用）
        self.kick_peak = 0.0                   # 低频通量 1s 累计的慢峰（只升不降，换歌重置）
        self._peak_confirmed = False           # peak 可信度：是否被「显著更高的 kick」验证过
        self._peak_age = 0.0                   # 换歌/启动起未确认的秒数（90s 超时强制确认）
        self.bpm_samples = deque(maxlen=12)
        self.bpm_sample_t = 0.0
        self.beat_gaps = deque(maxlen=16)
        # ---- 小节头拍（downbeat）识别 ----
        # 4/4 里「哪一拍是第 1 拍」靠等分是不知道的（beat_count 从 0 起算，没有音乐含义），
        # 所以「每 4 拍一条小节线」可能整体偏 0~3 拍 → 素材切换踩在音乐的第 2/3/4 拍上。
        # 按拍相位（beat_count % 4）累计落拍证据，证据显著最高的相位就是小节头。
        self._db_sum = [0.0, 0.0, 0.0, 0.0]   # 各相位证据累计
        self._db_cnt = [0, 0, 0, 0]           # 各相位样本数
        self._downbeat_off = 0                # 当前认定的小节头相位
        self._db_conf = 0.0                   # 置信度（领先幅度归一）
        self._db_cand = -1                    # 上次评估的候选相位
        self._db_streak = 0                   # 候选连续命中的次数
        self._db_eval_beat = 0                # 上次评估时的 beat_count
        self._db_change_beat = -10 ** 9       # 上次真正改相位时的 beat_count（初值=从不）
        self._db_conf_ever = False            # 是否已经认准过一次（之后改相位要更大领先幅度）
        self._db_prev_cent = None             # 上一拍窗口的低频谱质心（低音换音检测）
        # 当前拍窗口内的累计量（拍钟有 ±0.3 拍相位误差，逐块取值会采错位置，取窗口峰值更稳）
        self._dbw_bass = 0.0
        self._dbw_bflux = 0.0
        self._dbw_csum = 0.0
        self._dbw_w = 0.0
        self._dbw_drop = False       # 本拍窗口内是否出现过 drop 突刺
        self._db_pending = None      # 待结算的拍相位（拍点后 0.3 拍结算）
        self._db_pending_due = 0.0
        self._db_bass_ref = 0.0      # 拍窗口低音峰值的慢衰减基准（比较各拍 kick 轻重）
        self.cx_smooth = 0.0
        self.energy_hist = deque(maxlen=192)
        self.voc_smooth = 0.0
        self.seg_state = {"quiet_since": None, "build_since": None, "last_drop": 0.0,
                          "was_high": False, "high_since": None, "label": "none",
                          "seg_until": 0.0, "prev_e": 0.0}

    def feed(self, data, st):
        """分析一个采集块并更新共享状态。任何异常由调用方兜底，不影响线程存活。"""
        now = time.perf_counter()
        sig = np.mean(data, axis=1) if data.shape[1] > 1 else data[:, 0]
        rms = float(np.sqrt(np.mean(sig ** 2)))

        # ---- FFT ----
        buf = np.zeros(2048, dtype=np.float32)
        n = min(len(sig), 2048)
        buf[:n] = sig[:n]
        spec = np.abs(np.fft.rfft(buf * self.win)) / 1024.0
        freqs = np.fft.rfftfreq(2048, 1.0 / self.sr)
        bass = _band_energy(spec, freqs, 20, 160)
        mid = _band_energy(spec, freqs, 160, 2000)
        high = _band_energy(spec, freqs, 2000, 12000)
        # 低频「起伏变化量」：相邻块低频能量的上升差分，1s 窗口累计。
        # drop 段的 kick 让低频一拍一拍地大起大落（起伏大且密），平稳垫底/pad 段起伏小。
        # 与 kick（频谱级通量）互补：录屏/压缩削瞬态通量时，能量级的起伏通常仍在。
        if self.prev_bass_e is not None:
            self._bdelta_recent.append(max(bass - self.prev_bass_e, 0.0))
        self.prev_bass_e = bass

        # ---- 24 段对数频谱（可视化特效数据源：径向频谱/音频反应变形）----
        edges = np.geomspace(2, len(spec) - 1, 25).astype(int)
        edges = np.unique(edges)
        band_vals = np.array([spec[edges[i]:max(edges[i] + 1, edges[i + 1])].mean()
                              for i in range(len(edges) - 1)], dtype=np.float32)
        # 滚动峰值归一（慢衰减，自动适配音量），再 EMA 平滑
        if not hasattr(self, "_spec_ref") or self._spec_ref is None:
            self._spec_ref = np.full(len(band_vals), 1e-6, dtype=np.float32)
        self._spec_ref = np.maximum(self._spec_ref * 0.995, band_vals)
        self._spec_ref = np.maximum(self._spec_ref, 1e-6)
        sn = np.clip(band_vals / self._spec_ref, 0.0, 1.0)
        if not hasattr(self, "_spec_sm") or self._spec_sm is None:
            self._spec_sm = sn.copy()
        else:
            k = min(len(sn), len(self._spec_sm))
            self._spec_sm[:k] = self._spec_sm[:k] * 0.6 + sn[:k] * 0.4
        self.spectrum = [round(float(v), 3) for v in self._spec_sm[:24]]
        while len(self.spectrum) < 24:
            self.spectrum.append(0.0)

        # ---- 谱通量 + 慢衰减基准 ----
        if self.prev_spec is None:
            self.prev_spec = spec
        flux = float(np.sum(np.maximum(spec - self.prev_spec, 0)))
        self.prev_spec = spec
        self.flux_hist[self.flux_i % len(self.flux_hist)] = flux
        self.flux_i += 1
        self.flux_ref = max(self.flux_ref * 0.999, flux)

        # ---- 低频(kick)通量：踢鼓节奏比全频稳定（全频易被旋律乐句周期带偏）----
        bass_mask = (freqs >= 20) & (freqs <= 200)
        bass_spec = spec[bass_mask]
        if self.prev_bass_spec is None:
            self.prev_bass_spec = bass_spec
        bflux = float(np.sum(np.maximum(bass_spec - self.prev_bass_spec, 0)))
        self.prev_bass_spec = bass_spec
        # kick 低频通量的慢基准（半衰期≈15s 同步自适应）。打击强度用 bflux/bflux_ref 衡量：
        # 区分度来自频段本身——breakdown 段没有 kick，bflux 接近 0；主段重 kick 的 bflux 大。
        # （v6.1 首版用全频 flux/flux_ref 做强度，方向反了：breakdown 的旋律 tick 反而比值更高。）
        self.bflux_ref = max(getattr(self, "bflux_ref", 0.0) * 0.999, bflux)
        self.bass_flux_hist[self.flux_i % len(self.bass_flux_hist)] = bflux
        # 低频通量 4s 累计（kick 冲击密度）及其只升不降慢峰
        self._bflux_recent.append(bflux)
        kick_density = float(sum(self._bflux_recent))
        # 低频「起伏变化量」：1s 窗口内低频能量的上升差分累计（与 kick 的频谱通量互补）
        fluct_density = float(sum(self._bdelta_recent)) if self._bdelta_recent else 0.0
        self.kick_peak = max(self.kick_peak, kick_density)

        # ---- BPM 估计 v6（每 ~1s）：kick 节奏优先 + 谐波梳状 + 60s 投票众数 ----
        if self.flux_i % int(self.sr // BLOCK) == 0:
            fps = self.sr / BLOCK
            bh = self.bass_flux_hist.copy()
            fh = self.flux_hist.copy()

            def _comb_score(h):
                h = h - np.mean(h)
                ac = np.correlate(h, h, "full")[len(h) - 1:]
                acn = ac / (ac[0] + 1e-9)
                lo = max(2, int(60 / 200 * fps))
                hi = min(int(60 / 60 * fps), len(acn) - 10)
                if hi <= lo + 2:
                    return 0.0, -1, acn
                best_lag, best_score = lo, -1.0
                for lag in range(lo, hi):
                    s = acn[lag]
                    if 2 * lag < len(acn):
                        s += 0.8 * acn[2 * lag]
                    if 3 * lag < len(acn):
                        s += 0.6 * acn[3 * lag]
                    if 4 * lag < len(acn):
                        s += 0.5 * acn[4 * lag]
                    if s > best_score:
                        best_score, best_lag = s, lag
                return best_score, best_lag, acn

            # kick 与全频各自估一个候选；kick 节奏清晰（梳状峰强）时优先 kick，
            # 否则（breakdown 无 kick）退回全频。解决全频通量在 2/3 半速处梳状分更高的系统性误检。
            kscore, klag, kac = _comb_score(bh)
            fscore, flag, fac = _comb_score(fh)
            if kscore > 0.9:      # kick 节奏清晰 → 用 kick
                lag, acn = klag, kac
            else:
                lag, acn = flag, fac
                # 半速修正：全频通量偏向「真实 BPM 的 1/2 或 2/3」（intro 半速 73↔146、
                # 主段 2/3 半速 96↔145、88↔132）。候选偏慢时依次检查 ×2、×1.5 处自相关，
                # 若也强则升上去。舞曲场景慢歌(<120)极少，误升风险低。
                b0 = 60.0 / (lag / fps)
                if b0 < 120:
                    for mult in (2.0, 1.5):
                        lagm = int(round(60.0 / (b0 * mult) * fps))
                        if 2 <= lagm < len(acn) and acn[lagm] > 0.6 * acn[lag]:
                            lag = lagm
                            break
            if lag > 0 and (kscore > 0.9 or fscore > 0):
                # 抛物线插值细化亚采样精度
                delta = 0.0
                if 1 <= lag < len(acn) - 1:
                    denom = acn[lag - 1] - 2 * acn[lag] + acn[lag + 1]
                    if abs(denom) > 1e-9:
                        delta = max(-0.5, min(0.5, 0.5 * (acn[lag - 1] - acn[lag + 1]) / denom))
                bpm = float(max(60.0, min(200.0, 60.0 / ((lag + delta) / fps))))
                # 60s 投票众数 + 两阶段锁定：一首歌 BPM 恒定。
                # 未锁定阶段：主段众数集中(conf≥0.45)且票数足(≥20)才建立锁定；
                # 已锁定阶段：只接受与锁定值 ±8 内的微调，breakdown/outro 的飞值（无 kick
                # 时全频被旋律带飞）一律忽略，避免「2:20 飞到 180」。静音换歌重置解锁。
                self._bpm_votes.append(bpm)
                vals = np.array(self._bpm_votes, dtype=np.float32)
                if len(vals) >= 6:
                    bins = np.arange(60.0, 201.0, 1.0)
                    hist, edges = np.histogram(vals, bins=bins)
                    peak = int(np.argmax(hist))
                    mode = float(edges[peak] + 0.5)
                    conf = float(hist[peak]) / len(vals)
                    if not self._bpm_lock:
                        # 锁定建立：舞曲常见区间 [115,185] + 众数集中 + 票数足。
                        # intro 的半速值(64/73)一致但错，落在区间外 → 不锁定，等主段正确值。
                        if 115 <= mode <= 185 and conf >= 0.45 and len(vals) >= 20:
                            self.bpm_smooth = mode
                            self._bpm_lock = True
                        elif conf >= 0.4 and self.bpm_smooth > 0:
                            self.bpm_smooth = self.bpm_smooth * 0.7 + mode * 0.3
                    else:
                        if abs(mode - self.bpm_smooth) <= 8:
                            self.bpm_smooth = self.bpm_smooth * 0.7 + mode * 0.3
                    # ---- 显示用 BPM（**不参与拍钟/切换节奏**）----
                    # 门控只影响「锁不锁」，但界面显示不该被门控连坐：慢歌、极快曲、
                    # 被估成半速的歌都要能看到一个数字。票数≥10 且 conf≥0.40 就发布，
                    # 并在未锁定时记 bpm_locked=False（界面显示成「≈ N」，与锁定值区分）。
                    if len(vals) >= 10 and conf >= 0.40:
                        if self._bpm_lock:
                            self.bpm_display = self.bpm_smooth
                        else:
                            self.bpm_display = mode

        # ---- 节拍相位 / 踩拍 / 鼓点活动 ----
        if self.bpm_smooth > 0:
            self.beat_interval = max(0.25, min(1.2, 60.0 / self.bpm_smooth))
        strong = flux > max(1e-4, self.flux_ref * 0.55)
        # 低频 kick 强拍：bflux（20-200Hz 通量）超过其慢基准。人声/旋律的高频瞬态
        # 会产生全频 flux（strong）但几乎不产生低频通量——打击感必须用 kick_strong 计数，
        # 否则人声段 strike_density 虚高、能量降不下来（In The Atmosphere 教训）。
        kick_strong = bflux > max(1e-4, self.bflux_ref * 0.55)
        if flux > max(3e-4, self.flux_ref * 0.35):
            self.last_onset_t = now
        beat_active = (now - self.last_onset_t) < 2.0
        # ---- 节拍钟：匀速推进 + 强拍锁相（PLL）----
        # 旧实现「检测到强拍就 last_beat_t = now」有两个硬伤：
        #   ① 拍点时间被 onset 检测的抖动带偏（每次 ±几十 ms，累积漂移）；
        #   ② 漏检强拍时拍钟完全停走，beat_count 与音乐越错越远 → 切换踩不到小节线。
        # 改为匀速节拍钟：按 beat_interval 累加推进（漏检也照常走拍），强拍只做小幅相位校正
        # （PLL，35% 步长，仅当强拍落在拍点 ±0.3 拍内才拉，offbeat 的 0.5 拍不拉）。
        # 这样 beat_count 稳定对应音乐拍数，「切换点就近对齐 4 拍小节线」才能真正落到小节上。
        gap = now - self.last_beat_t
        is_beat = False
        if self.bpm_smooth > 0 and self.last_beat_t > 0.0 and gap <= self.beat_interval * 3.0:
            # 匀速推进（一次可能跨多拍，逐拍补齐 beat_count）
            while now - self.last_beat_t >= self.beat_interval:
                self.last_beat_t += self.beat_interval
                self.beat_count += 1
                is_beat = True
            # 强拍锁相：把拍钟朝 onset 拉一点（小步，防抖）
            if strong:
                ph = (now - self.last_beat_t) / self.beat_interval
                err = ph if ph <= 0.5 else ph - 1.0     # -0.5~0.5：强拍相对最近拍点的相位偏差
                if abs(err) < 0.3:
                    self.last_beat_t += err * self.beat_interval * 0.35
                # 拍间隔微调：仅在 gap 接近当前间隔(0.8~1.3 倍)时吸收，防 offbeat 带偏
                if 0 < gap < 4.0 and self.beat_interval * 0.8 <= gap <= self.beat_interval * 1.3:
                    self.beat_interval = max(0.25, min(1.2, 0.85 * self.beat_interval + 0.15 * gap))
        else:
            # BPM 未建立 / 断档（静默、换歌、大间隙）：宽松自由推进（旧行为兜底，重置拍钟）
            if gap >= self.beat_interval and rms > 1e-4:
                self.last_beat_t = now
                self.beat_count += 1
                is_beat = True
            # 断档 = 换歌/长时间无鼓点：拍钟重新起拍，小节头相位也作废重找
            self._downbeat_reset()
        phase = (now - self.last_beat_t) / max(self.beat_interval, 1e-3)
        # 小节线按「识别出的小节头相位」起算，而不是拍钟起点（拍钟起点无音乐含义）
        is_bar = is_beat and ((self.beat_count - self._downbeat_off) % 4 == 0)

        # ---- 强踩拍间隔 → 节拍清晰度 ----
        beat_clarity = 0.0  # 缺省值：无强拍样本时也是有效数字（避免未绑定变量）
        if strong and self.last_strong_t > 0:
            g = now - self.last_strong_t
            if 0.15 < g < 3.0:
                self.beat_gaps.append(g)
        if strong:
            self.last_strong_t = now
        # 打击密度：用 kick_strong（低频通量=真 kick）计数，0.15s 去重。
        # 人声段全频 flux 有瞬态（strong）但低频无 kick（kick_strong 稀疏）→ 打击感低 → 能量低。
        if kick_strong:
            if not self.strike_times or now - self.strike_times[-1] > 0.15:
                self.strike_times.append(now)
                # 记录这次打击的 kick 强度（bflux 相对其慢基准，仅调试观测用）
                self.strike_strengths.append(min(4.0, bflux / max(self.bflux_ref, 1e-9)))
        while self.strike_times and now - self.strike_times[0] > 4.0:
            self.strike_times.popleft()
            if self.strike_strengths:
                self.strike_strengths.popleft()
        strike_density = len(self.strike_times) / 4.0   # 每秒强拍次数（0~6+，打击感）
        # 强度加权打击能量：每秒「强度总和」。重 kick 密集≈6，轻 tick≈2。
        weighted_strike = sum(self.strike_strengths) / 4.0
        if self.beat_gaps and self.bpm_smooth > 0:
            want = 60.0 / self.bpm_smooth
            good = sum(1 for g in self.beat_gaps if abs(g - want) < want * 0.3)
            beat_clarity = good / len(self.beat_gaps)

        # ---- Drop 检测：安静后低频突刺 ----
        t_now = time.time()
        is_drop = (bass > 0.12 and self.smooth["bass"] < 0.04
                   and t_now - self.last_drop_t > 4.0)
        if is_drop:
            self.last_drop_t = t_now

        # ---- 小节头拍（downbeat）识别：找出 4/4 里哪一拍是「第 1 拍」----
        self._downbeat_tick(is_beat, bass, bflux, bass_spec, is_drop)

        # ---- 平滑能量 ----
        sm = 0.25
        self.smooth["bass"] = self.smooth["bass"] * (1 - sm) + bass * sm
        self.smooth["mid"] = self.smooth["mid"] * (1 - sm) + mid * sm
        self.smooth["high"] = self.smooth["high"] * (1 - sm) + high * sm

        # 能量 v7.3：低频冲击(kick)主导 + 响度辅助。
        # 用户场景（线上 VJ 给别的 DJ 用）：无法要求每个 DJ 关响度均衡/压缩——它们会抹平响度
        # （人声段被增益到和 drop 一样响），但抹不平 kick 冲击的频谱特征。所以能量必须以
        # 低频通量（kick 冲击）为主导（70%），响度只做辅助（30%）。
        weighted = bass * 0.6 + mid * 0.25 + high * 0.15       # 低频主导
        wdb = 20.0 * float(np.log10(weighted + 1e-9))
        if rms > 1e-4:
            if self.wdb_base <= -59.9:
                self.wdb_base = wdb                      # 冷启动：锚定第一声
            else:
                tau = 80.0
                self.wdb_base += (wdb - self.wdb_base) * (1.0 - float(np.exp(-self.block_sec / tau)))
        rel = wdb - self.wdb_base
        if rel < 0:
            e_loud = 0.5 * (1.0 + rel / 6.0)
        else:
            e_loud = 0.5 + 0.5 * rel / 6.0
        e_loud = max(0.0, min(1.0, e_loud))
        # 低频冲击强度：kick_density（1s 低频通量累计）相对全曲慢峰（压缩鲁棒）。
        # 映射带 0.55 死区：实测鼓点段 kick_rel 0.71~0.84、钢琴/人声段 0.08~0.50（BW 钢琴
        # 和弦的低频瞬态会贡献 0.3~0.5，但远低于真 kick）——线性 min(rel*1.3) 把钢琴段
        # 映到 0.4~0.65 导致 breakdown 能量虚高（Black Warrior 1:50 钢琴段 0.8+）。
        # 锚定保护：VDJ 接歌混播无静音→换歌重置不触发，新歌常从 breakdown（钢琴/人声段）
        # 进入，kick_peak 被锚定在低值→后续 kick_rel 全部虚高。按「peak 可信度」处理：
        # 若 peak 从未被「显著更高的 kick（>1.18×peak，即真 drop 到来）」验证过、且未超 90s，
        # 视为「可能只见过 breakdown」→ 分母放大 1.54 压低 kick_rel（breakdown 保持低能量）；
        # 一旦真 drop 到来（kick 暴涨）或超时，立即确认，能量恢复满血。正常从头播的歌
        # 第一个 drop 就会确认，几乎不受影响（比时间一刀切的观察期精准）。
        self._peak_age += self.block_sec
        if not self._peak_confirmed and (kick_density > self.kick_peak * 1.18 or self._peak_age > 90.0):
            self._peak_confirmed = True
        peak_eff = max(self.kick_peak, 1e-6) * (1.0 if self._peak_confirmed else 1.54)
        kick_rel = kick_density / peak_eff
        # 映射带 0.50 死区：实测鼓点段 kick_rel 0.71~0.84、钢琴/人声段 0.08~0.50（BW 钢琴
        # 和弦的低频瞬态会贡献 0.3~0.5，但远低于真 kick）；Bunsen 类全程均匀 kick 的歌
        # rel 长期 0.55~0.75，拐点不能高过 0.5（否则全程高潮被误压成中能量）。
        kick_e = max(0.0, min(1.0, (kick_rel - 0.48) / 0.30))
        # 持续打击平滑（τ≈1.2s 对称 EMA）：真鼓点 kick_e 连续多秒高、钢琴/人声段的偶发低频
        # 敲击（钢琴低音伴奏的节奏敲击）是短促的——不平滑会被 0.7 权重放大成能量横跳
        # （BW 钢琴段 0.2↔0.6 反复、跨档滞回等不到稳定低值）。EMA 抹掉短促尖峰、保留持续
        # kick，drop 约 2.2s 到位（可接受）。
        tau_ke = 1.2
        self._kick_e_smooth += (kick_e - self._kick_e_smooth) * (1.0 - float(np.exp(-self.block_sec / tau_ke)))
        kick_e = self._kick_e_smooth
        # 低频能量分量（相对全曲峰值，只升不降，换歌重置）：drop 段 bass 高（0.7~0.9）、
        # 钢琴段低（0.3~0.4）、人声段中但被 vocal_factor 压。用于「录屏削瞬态」场景兜底——
        # NVIDIA 录制会抹平 kick 冲击（低频通量），但低频持续能量还在，用它区分 drop/钢琴。
        self.bass_peak = max(self.bass_peak, bass)
        bass_rel = bass / max(self.bass_peak, 1e-6)
        bass_factor = max(0.0, min(1.0, (bass_rel - 0.45) / 0.22))
        # 低频起伏变化量因子（用户：高能量时低频起伏大）：起伏量相对全曲慢峰（只升不降）。
        # 映射与 bass_factor 同构（拐点 0.45/斜率 0.22）——起伏和低频水平高度相关。
        self.fluct_peak = max(self.fluct_peak, fluct_density)
        fluct_rel = fluct_density / max(self.fluct_peak, 1e-9)
        fluct_factor = max(0.0, min(1.0, (fluct_rel - 0.45) / 0.22))
        # 门控（用户语义：低频值超过阈值后起伏才算高能量）：低频水平不足（bass_factor 低）时
        # 起伏量不参与——防止钢琴/人声段「起伏均匀但低频水平不高」被起伏量误抬。
        fluct_factor *= min(1.0, bass_factor * 2.0)
        # 能量 = 响度(40%) + max(低频冲击, 低频能量)(60%)。
        # 关键：kick 与 bass 取 max 而非固定加权——kick 强（正常 drop）用 kick；kick 被录屏/
        # 压缩削掉瞬态时（kick_rel 掉进死区），低频能量 bass 仍在、自动兜底。固定加权（如
        # 0.35/0.25）会两头不讨好：kick 权重低则 ITA 类「响度不突出、靠 kick 撑 drop」的歌被
        # 压到 0.5；bass 权重低则录屏场景兜不住。钢琴/人声段两者都低（bass_rel<拐点、kick<死区），
        # max 后仍为 0，不会误抬。
        loud_term = e_loud                                  # 响度项（0~1，融合前）
        ev_term = max(kick_e, bass_factor, fluct_factor)    # 低频证据项（0~1）
        e_loud = 0.4 * loud_term + 0.6 * ev_term
        # 人声惩罚：人声中频占比高 → 砍能量。响度均衡是宽频增益、不改变频谱形状，
        # 所以人声段中频占比始终高（压缩鲁棒的人声检测）。voc_smooth 为上一块的中频占比平滑值。
        # 惩罚系数 0.7 → 0.6（2026-09-24 用户要求「削弱一点人声惩罚」）：人声段能量会整体抬高
        # 约 5~15%（人声明显时抬得多），最大惩罚从 -70% 变 -60%。
        vocalness_now = min(1.0, self.voc_smooth * 1.6)
        vocal_factor = 1.0 - 0.6 * max(0.0, min(1.0, (vocalness_now - 0.25) / 0.35))
        e_loud *= vocal_factor
        # 调试观测字段（无副作用，排障用）
        self.dbg_rel = rel
        self.dbg_eloud = e_loud
        self.dbg_ws = kick_rel
        self.dbg_strike_n = vocal_factor
        # 能量各分项（供「能量诊断工具」显示；主程序不读，只放一份小字典进状态）
        try:
            st.dbg = {"loud": loud_term, "kick_e": kick_e, "bass_f": bass_factor,
                      "fluct_f": fluct_factor, "vocal_f": vocal_factor, "ev": ev_term,
                      "rel": rel, "kick_rel": kick_rel, "bass_rel": bass_rel, "pre": e_loud}
        except Exception:
            pass
        # 不对称平滑：升快（τ≈0.5s，drop 突增快速响应）、降慢（τ≈2.5s，压回落震荡）。
        tau_smooth = 0.5 if e_loud > self.e_loud_smooth else 2.5
        self.e_loud_smooth += (e_loud - self.e_loud_smooth) * (1.0 - float(np.exp(-self.block_sec / tau_smooth)))
        e = max(0.0, min(1.0, self.e_loud_smooth))
        # 非对称平滑（Attack 快升 / Release 慢降）+ 慢释放压震荡：
        prev_e = self.smooth["energy"]
        if e > prev_e:
            self.smooth["energy"] = prev_e + (e - prev_e) * 0.4    # attack：快速跟 kick 上升沿
        else:
            self.smooth["energy"] = prev_e + (e - prev_e) * 0.05   # release：慢回落，压忽高忽低
        # 能量伽马校正（0.5~2.0，默认 1.0）：现场系统低频增强会把人声段能量抬到接近鼓点段。
        # 伽马>1 压低中段（人声段）、几乎不动高端（鼓点段 0.8+），比全局乘数更符合需求。
        sc = getattr(self, "energy_scale", 1.0)
        E = min(1.0, max(0.0, self.smooth["energy"]) ** sc)

        # ---- 行为模式指标 ----
        # 瞬态密度 = 每秒强拍次数。之前用「beat_active 就 append」，鼓点密集时每块都 append，
        # 4 秒窗口被 maxlen 卡满 → 恒 64，归一后恒 1，模式打分完全失真。改用 strong_times（强拍）。
        onset_density = strike_density

        if now - self.bpm_sample_t > 1.0 and self.bpm_smooth > 0:
            self.bpm_samples.append(self.bpm_smooth)
            self.bpm_sample_t = now
        bpm_stab = 0.0
        if len(self.bpm_samples) >= 4:
            arr = np.array(self.bpm_samples)
            bpm_stab = max(0.0, 1.0 - float(arr.std()) / 8.0)

        psum = float(spec.sum()) + 1e-9
        pp = spec / psum
        H = float(-(pp * np.log(pp + 1e-12)).sum()) / float(np.log(len(pp)))
        self.cx_smooth = self.cx_smooth * 0.95 + H * 0.05
        spec_complex = min(1.0, self.cx_smooth * 1.3)

        self.energy_hist.append(E)
        energy_trend = 0.0
        if len(self.energy_hist) >= 48:
            arr = np.array(self.energy_hist)
            half = len(arr) // 2
            energy_trend = max(-1.0, min(1.0, float(arr[half:].mean() - arr[:half].mean()) * 6.0))

        v = mid / (bass + mid + high + 1e-6)
        self.voc_smooth = self.voc_smooth * 0.98 + v * 0.02
        vocalness = min(1.0, self.voc_smooth * 1.6)

        # ---- 段落识别（~4Hz）----
        if self.flux_i % 11 == 0:
            s = self.seg_state
            if beat_active and E - s["prev_e"] > 0.28 and bass > 0.3:
                s["last_drop"] = now
                s["was_high"] = True
            s["prev_e"] = E

            # 各状态条件——不满足就复位（否则 build_since 一旦置位就永久卡在 build）
            in_quiet = E < 0.18
            if in_quiet:
                s["quiet_since"] = s["quiet_since"] or now
            else:
                s["quiet_since"] = None
            in_build = energy_trend > 0.06 and E > 0.15
            if in_build:
                s["build_since"] = s["build_since"] or now
            else:
                s["build_since"] = None
            # peak 滞回：进入要 E>0.65，保持只要 E>0.45——防止能量在 0.6 附近震荡时
            # peak 标签反复丢失（HUD 段落显示抖动、模式打分 seg_w 抖动）
            if E > (0.45 if s["high_since"] else 0.65):
                s["high_since"] = s["high_since"] or now
                s["was_high"] = True
            else:
                s["high_since"] = None

            # 判定优先级：drop(冲击后6s) > outro(曾高能后回落) > peak(持续高能) > build > quiet > 保持上一状态
            if now - s["last_drop"] < 6.0:
                label = "drop"
            elif s["was_high"] and E < 0.3 and not beat_active:
                label = "outro"
                s["was_high"] = False
            elif s["high_since"] and now - s["high_since"] > 2.0:
                label = "peak"
            elif s["build_since"] and now - s["build_since"] > 2.0:
                label = "build"
            elif s["quiet_since"] and now - s["quiet_since"] > 2.0:
                label = "quiet"
            else:
                label = s.get("last_label") or "quiet"   # 避免 none 闪烁：保持上一状态
            s["last_label"] = label
            s["label"] = label
            s["seg_until"] = now + 1.5

        # ---- 写共享状态 ----
        with st.lock:
            # 电平表用 dB 刻度（-30dBFS→0、-6dBFS→满），与耳朵/系统音量表的起伏一致。
            # 旧版 rms*6 线性放大：现代母带 RMS 0.15~0.3 乘 6 即满格，电平表 90% 时间爆红。
            lv_db = 20.0 * float(np.log10(max(rms, 1e-5)))
            st.lv_db = lv_db
            st.level = max(0.0, min(1.0, (lv_db + 30.0) / 24.0))
            st.bass = min(1.0, self.smooth["bass"] * 8)
            st.mid = min(1.0, self.smooth["mid"] * 12)
            st.high = min(1.0, self.smooth["high"] * 16)
            st.energy = E
            st.bpm = self.bpm_smooth
            st.bpm_display = self.bpm_display
            st.bpm_locked = self._bpm_lock
            st.beat_phase = min(1.0, max(0.0, phase))
            st.beat_count = self.beat_count
            # 小节数按识别出的小节头起算（相位未知时 downbeat_off=0，等价于旧行为）
            st.bar_count = max(0, self.beat_count - self._downbeat_off) // 4
            st.downbeat_off = self._downbeat_off
            st.downbeat_conf = self._db_conf
            # 24 段频谱：一直没往共享状态写（只写在分析器自己身上），导致
            # 「径向频谱」「音频反应变形」两个后处理拿到的是全 0 数组 → 画面毫无反应，
            # 但界面上勾选框和强度条都正常，看起来像"有显示没效果"。
            st.spectrum = self.spectrum
            st.is_beat = is_beat
            st.is_bar = is_bar
            st.drop = is_drop
            st.beat_active = beat_active
            st.onset_density = onset_density
            st.bpm_stab = bpm_stab
            st.beat_clarity = beat_clarity
            st.spec_complex = spec_complex
            st.energy_trend = energy_trend
            st.vocalness = vocalness
            st.segment = self.seg_state["label"] if now < self.seg_state["seg_until"] else "none"
            # 短静音后重新有声 = 大概率换歌：重置能量分析状态（wdb_base/bass_peak 等残留
            # 上一首歌的基线/慢峰，会导致新歌能量失真——用户反馈切歌后能量一直 0.5+）。
            # 3s 阈值（电子乐很少 3s 全静音；搓碟/EQ 短暂静音 <3s 不会误触发）。
            if st.silent_sec > 3.0 and rms >= 1e-4:
                self.wdb_base = -60.0          # 重新冷启动锚定
                self.bass_peak = 0.0
                self._peak_confirmed = False   # 新歌 peak 重新观察（锚定保护重启）
                self._peak_age = 0.0
                self._kick_e_smooth = 0.0      # 持续打击平滑重置
                self.fluct_peak = 0.0          # 起伏变化量慢峰重置
                self.prev_bass_e = None
                self.e_loud_smooth = 0.0
                self.smooth["energy"] = 0.0
                self.smooth["bass"] = self.smooth["mid"] = self.smooth["high"] = 0.0
                self.flux_ref = 0.0
                self.bflux_ref = 0.0
                self.strike_times.clear()
                self.strike_strengths.clear()
                self._downbeat_reset()         # 新歌重新找小节头相位
            # 长静音（>10s）后重新有声 = 明确换歌：连 BPM 投票与锁定也清空（保守阈值防搓碟误清）
            if st.silent_sec > 10.0 and rms >= 1e-4:
                self._bpm_votes.clear()
                self._bpm_lock = False
                self.bpm_smooth = 0.0
                self.bpm_display = 0.0
            st.silent_sec = st.silent_sec + self.block_sec if rms < 1e-4 else 0.0
            st.running = True

    # ---------------- 小节头拍（downbeat）识别 ----------------
    DB_EVAL_BEATS = 16       # 每 16 拍评估一次候选相位
    DB_MIN_SAMPLES = 6       # 每个相位至少 6 个样本才够评估（≈24 拍）
    DB_MARGIN = 0.10         # 首次认准：冠军相位需领先亚军 10%
    DB_SWITCH_MARGIN = 0.20  # 已认准后改相位：需领先 20%
    DB_COOLDOWN = 128        # 改相位冷却（拍）≈1 分钟，防抖

    def _downbeat_reset(self):
        """清空小节头证据（换歌/断档时调用）。

        刻意**不清 `_downbeat_off`**：相位一旦认准，就用上次的结论继续对齐；
        清空证据只是让它在下一次评估前保持现状。这样「重新找」的过程不会把
        已经对好的小节线打回没有音乐含义的「拍钟起点」，整场演出不会突然错位。"""
        self._db_sum = [0.0, 0.0, 0.0, 0.0]
        self._db_cnt = [0, 0, 0, 0]
        self._db_conf = 0.0
        self._db_cand = -1
        self._db_streak = 0
        self._db_prev_cent = None
        self._db_eval_beat = 0
        self._dbw_bass = 0.0
        self._dbw_bflux = 0.0
        self._dbw_csum = 0.0
        self._dbw_w = 0.0
        self._dbw_drop = False
        self._db_pending = None

    def _downbeat_tick(self, is_beat, bass, bflux, bass_spec, is_drop):
        """按拍相位累计「这一拍像不像小节头」的证据，识别 4/4 里的第 1 拍。

        为什么要它：`beat_count` 从 0 起算，没有音乐含义，所以「每 4 拍一条小节线」
        可能整体偏 0~3 拍（固定偏移、不随时间漂移）——素材切换就踩在音乐的第 2/3/4 拍上。

        证据（全部用相对量，自动适配音量/母带/场地）：
          · 低音峰值   bass_win / bass_ref   —— 第 1 拍通常 kick 最重（主判据）
          · 低频通量   bflux_win / bflux_ref —— kick 冲击的锐度（辅助）
          · 低音换音   |低频谱质心跳变|       —— 4/4 里低音常在小节头换音（四踩均衡时的主要判据）
          · drop 命中  is_drop → +3          —— 段落转折点必是小节头（强锚点）
        四踩均衡、证据拉不开差距时**不提交**（保持当前相位 = 旧行为），绝不会乱翻。

        实现细节（两个坑都踩过）：
        ① 逐块「卡点取值」会被拍钟 ±0.3 拍的相位残差毁掉——采到的是 kick 的尾巴或提前量，
           各相位证据几乎无差别；所以按**拍窗口**取峰值统计。
        ② 结算必须**晚 0.3 拍**：拍钟可能略早于真实 kick，若在跨拍那一刻就结算，kick 起点
           会落进下一个窗口，证据整体差一拍（合成音源实测：认到没有 kick 的相位）。
           窗口取 [T_{k-1}+0.3拍, T_k+0.3拍)，则每个 kick 必然落在自己那一拍的窗口里
           （只要拍钟误差 <0.3 拍），前后两个 kick 都不会串进来。"""
        # 逐块累计到「当前拍窗口」
        if bass > self._dbw_bass:
            self._dbw_bass = bass
        if bflux > self._dbw_bflux:
            self._dbw_bflux = bflux
        if is_drop:
            self._dbw_drop = True
        tot = float(np.sum(bass_spec))
        if tot > 1e-9:
            cent = float(np.dot(np.arange(bass_spec.shape[0], dtype=np.float32), bass_spec) / tot)
            self._dbw_csum += cent * tot
            self._dbw_w += tot

        now = time.perf_counter()
        if is_beat:
            # 记下这一拍的相位，再多累计 0.3 拍后结算（等 kick 峰值进窗口）
            self._db_pending = self.beat_count % 4
            self._db_pending_due = now + 0.30 * self.beat_interval
        if self._db_pending is None or now < self._db_pending_due:
            return
        ph = self._db_pending
        self._db_pending = None
        cent_avg = (self._dbw_csum / self._dbw_w) if self._dbw_w > 1e-9 else None
        dpitch = 0.0
        if cent_avg is not None and self._db_prev_cent is not None:
            dpitch = abs(cent_avg - self._db_prev_cent) / max(1.0, self._db_prev_cent)
        if cent_avg is not None:
            self._db_prev_cent = cent_avg

        # 主判据 = 拍窗口的低音峰值（= 这一拍的 kick 轻重，第 1 拍通常最重且带低音换音）。
        # 之前用「全频通量」当主判据是错的：反拍的 hihat 是宽带噪声，通量比 kick 还大，
        # 会把证据整个带偏到反拍相位上（合成音源实测：认到没有 kick 的相位）。全频通量只
        # 保留很小权重（编配变化在低音都平的时候才有参考价值）。
        self._db_bass_ref = max(self._db_bass_ref * 0.99, self._dbw_bass)
        ev = (1.5 * min(2.0, self._dbw_bass / max(self._db_bass_ref, 1e-9))
              + 0.4 * min(2.0, self._dbw_bflux / max(self.bflux_ref, 1e-9))
              + 1.2 * min(2.0, dpitch))
        if self._dbw_drop:
            ev += 3.0
        self._db_sum[ph] += ev
        self._db_cnt[ph] += 1
        self._dbw_bass = 0.0
        self._dbw_bflux = 0.0
        self._dbw_csum = 0.0
        self._dbw_w = 0.0
        self._dbw_drop = False

        # 每 16 拍（且各相位样本够）评估一次
        if self.beat_count - self._db_eval_beat < self.DB_EVAL_BEATS:
            return
        if min(self._db_cnt) < self.DB_MIN_SAMPLES:
            return
        self._db_eval_beat = self.beat_count
        means = [self._db_sum[i] / self._db_cnt[i] for i in range(4)]
        order = sorted(range(4), key=lambda i: -means[i])
        best, second = order[0], order[1]
        margin = (means[best] - means[second]) / max(means[second], 1e-6)
        self._db_conf = max(0.0, min(1.0, margin / 0.25))

        # 连续两次评估都指向同一相位才认（抑制单次波动）
        if best == self._db_cand:
            self._db_streak += 1
        else:
            self._db_cand = best
            self._db_streak = 1
        # 首次认准只要领先 10%；已经认准过再改相位要领先 20%（防抖）
        need = self.DB_SWITCH_MARGIN if self._db_conf_ever else self.DB_MARGIN
        if self._db_streak < 2 or margin < need:
            return
        if best == self._downbeat_off:
            return
        if self.beat_count - self._db_change_beat < self.DB_COOLDOWN:
            return
        self._downbeat_off = best
        self._db_change_beat = self.beat_count
        self._db_conf_ever = True


class _AudioRing:
    """固定容量单声道环形缓冲（numpy 实现，零 Python 对象分配）。

    为什么不用 `deque` + `tolist()`（2026-09-24 优化）：
      原实现每块 `deque.extend(mono.tolist())` —— 每秒 48000 个 Python 对象；
      更贵的是**读取端**：`_recognize_loop` 每 0.5 秒要做
      `list(self._genre_buf)[-216000:]`，也就是把 38 万元素的 deque 整个转成 list
      再切片（`_genre_loop` 每 3 秒一次同样的复制）。实测这是 `autovj-audio`
      线程占 0.78 个核的主因。改成 numpy 环形缓冲后，写入是切片赋值、读取是
      连续切片拷贝，全程不产生 Python 对象。
    """

    def __init__(self, cap):
        self.cap = int(cap)
        self.buf = np.zeros(self.cap, dtype=np.float32)
        self.pos = 0        # 下一个写入位置
        self.filled = 0     # 已写入的有效样本数（上限 cap）

    def write(self, x):
        x = np.asarray(x, dtype=np.float32).ravel()
        n = len(x)
        if n <= 0:
            return
        if n >= self.cap:
            self.buf[:] = x[-self.cap:]
            self.pos = 0
            self.filled = self.cap
            return
        p = self.pos
        end = p + n
        if end <= self.cap:
            self.buf[p:end] = x
        else:
            k = self.cap - p
            self.buf[p:] = x[:k]
            self.buf[:n - k] = x[k:]
        self.pos = end % self.cap
        self.filled = min(self.cap, self.filled + n)

    def last(self, n):
        """最近 n 个样本，按时间先后顺序返回（新数组，调用方可安全持有）"""
        n = int(min(n, self.filled))
        if n <= 0:
            return np.zeros(0, dtype=np.float32)
        start = (self.pos - n) % self.cap
        if start + n <= self.cap:
            return self.buf[start:start + n].copy()
        k = self.cap - start
        out = np.empty(n, dtype=np.float32)
        out[:k] = self.buf[start:]
        out[k:] = self.buf[:n - k]
        return out


class AudioEngine:
    """常驻音频线程封装：导入/枚举/采集都在该线程完成。"""

    def __init__(self):
        self.state = AudioState()
        self._gen = 0            # 采集代际编号：start/stop 递增，旧采集自动失效
        self.energy_scale = 1.0  # 能量伽马校正，运行时实时透传给采集分析器
        self._jobs = queue.Queue()
        # 曲风识别：环形缓冲 + 后台线程（每 3 秒用最近 2 秒音频跑一次 Discogs-EffNet）
        # 用 numpy 环形缓冲（原来 deque + tolist，读取端每 0.5 秒复制 38 万元素 → 0.78 核）
        self._genre_buf = _AudioRing(48000 * 8)
        self._genre_sr = 48000
        self._genre_lock = threading.Lock()
        self._genre_stop = threading.Event()
        self._genre_thread = threading.Thread(
            target=self._genre_loop, daemon=True, name="autovj-genre")
        self._genre_thread.start()
        # 现场指纹识别：指纹库 + 音乐电池引擎（main 用 set_recognizer 启用）
        self._fp_db = None
        self._recognizer_engine = None
        self._recognize_thread = threading.Thread(
            target=self._recognize_loop, daemon=True, name="autovj-recognize")
        self._recognize_thread.start()
        self._worker = threading.Thread(
            target=self._worker_loop, daemon=True, name="autovj-audio")
        self._worker.start()
        # 设备枚举单独一条线程：采集线程长期忙于 record()，枚举任务排在它后面会饿死
        self._dev_jobs = queue.Queue()
        self._dev_worker = threading.Thread(
            target=self._dev_loop, daemon=True, name="autovj-audio-dev")
        self._dev_worker.start()

    def _dev_loop(self):
        sc = None
        try:
            sc = _sc()
        except Exception:
            pass
        while True:
            job = self._dev_jobs.get()
            if job is None:
                break
            try:
                job(sc)
            except Exception:
                pass

    def _worker_loop(self):
        sc = None
        import warnings
        warnings.filterwarnings("ignore", message="data discontinuity in recording")
        try:
            sc = _sc()  # COM(MTA) 初始化只发生在本线程
        except Exception as e:
            with self.state.lock:
                self.state.error = f"soundcard 导入失败：{e}"
        while True:
            job = self._jobs.get()
            if job is None:
                break
            try:
                job(sc)
            except Exception as e:
                with self.state.lock:
                    self.state.error = str(e)

    def _submit(self, job):
        self._jobs.put(job)

    # ---------------- 设备枚举（阻塞，供后台扫描线程调用） ----------------
    def list_devices(self, timeout=8.0):
        """返回 {'system': [..], 'mic': [..]}。走独立的设备线程，不受采集占用影响。"""
        result = {}
        done = threading.Event()

        def job(sc):
            out = {"system": [], "mic": []}
            if sc is not None:
                for m in sc.all_microphones(include_loopback=True):
                    if m.isloopback:
                        out["system"].append(m.name)
                    else:
                        out["mic"].append(m.name)
            result.update(out)
            done.set()

        self._dev_jobs.put(job)
        done.wait(timeout)
        return result or {"system": [], "mic": []}

    # ---------------- 采集生命周期 ----------------
    def start(self, source_type="system", device_name="", mono=True, sr=48000):
        """切换设备/重启采集。用代际编号替代共享停止标志：
        旧实现 stop() 后立刻 clear()，采集循环（约21ms检查一次）可能没看到停止标志，
        导致新任务永远排队、设备切不过去（一直采集最初选的设备）。"""
        self._gen += 1                      # 让当前采集失效
        gen = self._gen
        with self.state.lock:
            self.state.running = False
        self._submit(lambda sc: self._run(sc, source_type, device_name, mono, sr, gen))

    def stop(self):
        self._gen += 1                      # 当前采集在下次检查时退出
        with self.state.lock:
            self.state.running = False

    def shutdown(self):
        self._gen += 1
        self._genre_stop.set()
        try:
            self._jobs.put(None)
        except Exception:
            pass

    def _genre_loop(self):
        import time as _t
        from audio_genre import get_genre_classifier, GenreTracker, style_name
        try:
            gc = get_genre_classifier()
        except Exception:
            return
        tracker = GenreTracker()
        while not self._genre_stop.is_set():
            _t.sleep(3.0)
            if not self.state.running:
                continue
            with self._genre_lock:
                if self._genre_buf.filled < 16000:
                    continue
                # classify 内部只取最近 need 个样本，给 8 秒足够，省一次整段拷贝
                buf = self._genre_buf.last(int(self._genre_sr * 8))
            try:
                r = gc.classify(buf, self._genre_sr)
                tracker.push(r["styles"])
                stable = tracker.stable()
                if stable:
                    with self.state.lock:
                        self.state.genre_styles = [stable]
                        self.state.genre_tags = [style_name(stable)]
                # 证据不足时不更新，保持上一次结果（避免闪烁）
            except Exception:
                pass

    def set_recognizer(self, fp_db):
        """启用现场识别（main 在曲库就绪后调用）。fp_db=None 禁用。"""
        self._fp_db = fp_db
        if fp_db is not None and self._recognizer_engine is None:
            from charge_engine import ChargeBarEngine
            self._recognizer_engine = ChargeBarEngine()

    def _recognize_loop(self):
        """现场识别：每 0.5s 取最近 4.5s 音频 → 指纹匹配 → 电池引擎确认 → 发布 song_id"""
        import time as _t
        import numpy as _np
        from charge_engine import FpResult
        while not self._genre_stop.is_set():
            _t.sleep(0.5)
            fp_db = self._fp_db
            eng = self._recognizer_engine
            if fp_db is None or eng is None or not self.state.running:
                continue
            # 静音超时 → 回待机（清空识别）
            with self.state.lock:
                silent = self.state.silent_sec
            if silent > 10.0:
                eng.reset()
                with self.state.lock:
                    self.state.recognized_song_id = -1
                    self.state.recognized_offset = -1.0
                continue
            with self._genre_lock:
                if self._genre_buf.filled < 16000:
                    continue
                # 只要最近 4.5 秒（指纹匹配窗口）；numpy 切片拷贝，不再 list(deque) 全量复制
                buf = self._genre_buf.last(int(self._genre_sr * 4.5))
            try:
                sig = _np.asarray(buf, dtype=_np.float32)
                sid, votes, off = fp_db.match(sig, self._genre_sr)
                eng.tick(FpResult(matched=(votes > 0), song_id=sid,
                                  aligned_votes=votes, offset_sec=off), _t.time())
                with self.state.lock:
                    self.state.recognized_song_id = eng.current_song_id
                    if sid == eng.current_song_id and votes > 0:
                        # off 是最近 4.5s 窗口起始在歌里的秒数，窗口末尾≈当前播放秒
                        self.state.recognized_offset = off + 4.5
                    elif eng.current_song_id < 0:
                        self.state.recognized_offset = -1.0
            except Exception:
                pass

    # ---------------- 采集（运行在音频线程） ----------------
    def _run(self, sc, source_type, device_name, mono, sr, gen):
        st = self.state
        if sc is None:
            with st.lock:
                st.error = "soundcard 不可用，无法采集音频"
            return
        if self._gen != gen:
            return   # 过期任务（用户又切换了设备），直接跳过
        try:
            mics = sc.all_microphones(include_loopback=True)
            mic = None
            if source_type == "system":
                loop = [m for m in mics if m.isloopback]
                if not loop:
                    raise RuntimeError("找不到可采集的系统声音（Loopback）设备")
                mic = loop[0]
                if device_name:
                    for m in loop:
                        if device_name in m.name:
                            mic = m
                            break
            else:
                cands = [m for m in mics if not m.isloopback]
                if not cands:
                    raise RuntimeError("找不到麦克风/线路输入设备")
                mic = cands[0]
                if device_name:
                    for m in cands:
                        if device_name in m.name:
                            mic = m
                            break
            if self._gen != gen:
                return   # 打开设备期间又被切换
            if not sr:   # 0 = 自动：跟随设备原生采样率，避免 WASAPI 重采样抹平瞬态
                sr = _native_samplerate(mic) or 48000
            with st.lock:
                st.device_desc = mic.name
                st.error = ""
                st.running = True
            self._capture_loop(mic, mono, sr, gen)
        except Exception as e:
            with st.lock:
                st.error = str(e)
                st.running = False

    def _capture_loop(self, mic, mono, sr, gen):
        st = self.state
        analyzer = _Analyzer(sr)
        # 连续读块失败计数：设备被拔掉时 record() 会不停抛异常，
        # 旧写法「单块出错就跳过、继续循环」会让采集线程一直占着 worker，
        # 后续「切换设备」的任务永远排在它后面 → 表现为改音源毫无反应。
        # 连续失败若干次就主动退出循环，让 worker 空出来接手切换请求。
        err_streak = 0
        with mic.recorder(samplerate=sr, channels=2) as rec:
            self._genre_sr = sr
            while self._gen == gen:   # 只在自己这一代有效；切换设备后立即退出
                try:
                    # 实时透传能量伽马校正（用户调滑块后下一块立即生效，不能在启动时只拷贝一次）
                    analyzer.energy_scale = self.energy_scale
                    data = rec.record(numframes=BLOCK)
                    if data is None or len(data) == 0:
                        continue
                    err_streak = 0
                    if mono:
                        analyzer.feed(data[:, :1], st)
                    else:
                        analyzer.feed(data, st)
                    # NDI 音频（音画同步）：把采集到的立体声 PCM 喂给 NDI 发送器
                    # 未启用 NDI 时**直接跳过**，省掉每块的 asarray/reshape/copy（原来无条件执行）
                    try:
                        ndi = st.ndi_feed
                        if ndi is not None and st.ndi_audio_on:
                            arr = _np.asarray(data, dtype=_np.float32)
                            if arr.ndim == 1:
                                arr = _np.column_stack([arr, arr])
                            elif arr.shape[1] == 1:
                                arr = _np.repeat(arr, 2, axis=1)
                            ndi(arr.reshape(-1).copy(), sr)
                    except Exception:
                        pass
                    # 喂曲风环形缓冲（左声道 float32）——numpy 环形缓冲，零对象分配
                    try:
                        mono_sig = data[:, 0] if data.ndim == 2 else data
                        with self._genre_lock:
                            self._genre_buf.write(mono_sig)
                    except Exception:
                        pass
                except Exception as e:
                    # 单块出错不杀死采集线程：记录并继续；但连续失败要放弃
                    err_streak += 1
                    with st.lock:
                        if err_streak >= 5:
                            st.error = ("音频设备可能已断开或不可用，已停止采集。"
                                        "请到「音源」重新选择设备后点应用。")
                            st.running = False
                        else:
                            st.error = f"分析块错误（已跳过）: {e}"
                    if err_streak >= 5:
                        break   # 主动退出，把 worker 让给「切换设备」的任务
                    time.sleep(0.02)
        # 采集结束（切换设备/停止）：复位运行标志
        with st.lock:
            if self._gen != gen:
                st.running = False
