# -*- coding: utf-8 -*-
"""Beat This! 离线**小节检测**（纯本地，不依赖 VirtualDJ）。

用途：给**没有 VDJ 数据**的曲目补「小节相位」（哪一拍是小节头）。
模型放 `assets/models/beat_this/small0_fixed.onnx`（9.9 MB），由 `build_exe.py` 的
`--add-data assets/models` 自动进发行包。

⚠ 它**只给小节级（4 拍）**：`beatgrid.phrase_phase()` 仍要判「两个小节里哪一个是乐句头」那 1 bit。
⚠ **零新依赖**：只用 numpy / onnxruntime（onnxruntime 早已随包，本地 AI 曲风识别在用）。

实测（2026-09-28，21 首，small0_fixed.onnx）：
    · 小节相位 21/21 判对（偏置 ≤0.25 拍；其中 2 首是 1/4 拍的**稳定亚拍偏置**，非选错小节）
    · 窗口 180/90/60/30 秒 → 单首 onnx 4.14 / 2.35 / 1.76 / 1.17 秒，**四档相位完全一样**
      ⇒ 默认 60 秒；检出拍数不足（前段无鼓）时自动延长到 180 秒重跑
    · ⚠ 网上流传的第三方 small0.onnx（10.4MB）**是坏的**（只出 71 个 beat）；
      本模块用的 `small0_fixed.onnx` 是从官方 `small0.ckpt` 自转的（ORT 复现 torch 误差 4e-6）

前处理两个坑（照抄官方，别改）：
    · mel **`norm=None`**（**不**做 Slaney 面积归一化）
    · STFT 幅度 **÷√1024**（`normalized="frame_length"`）
"""
from __future__ import annotations

import os
import sys
import threading
import time

import numpy as np

# ---- 前处理参数（官方 beat_this/preprocessing.py 的 LogMelSpect 默认值）----
SR = 22050
N_FFT = 1024
HOP = 441
F_MIN = 30.0
F_MAX = 11000.0
N_MELS = 128
LOG_MULT = 1000.0
FPS = SR / HOP                      # 50.0
SLANEY_NORM = False                 # ⚠ 必须 False

# ---- 推理参数（官方 inference.py）----
CHUNK = 1500                        # 30 秒
BORDER = 6                          # 边界丢弃帧数

# ---- 自适应窗口 ----
WIN_DEFAULT = 60.0                  # 默认分析窗口（秒）
WIN_MAX = 180.0                     # 拍数不足时延长到
MIN_BEATS = 16                      # 少于这个拍数认为"这段没鼓"，需要延长

_MODEL_REL = ("assets", "models", "beat_this", "small0_fixed.onnx")

_LOCK = threading.Lock()            # 串行化推理（扫描是并行的，别让多线程一起抢 CPU）
_SESS = None                        # 延迟创建
_SESS_ERR = ""
_BT_LOCK = threading.Lock()


# ======================================================================
# 模型位置 / 可用性
# ======================================================================
def model_path() -> str:
    """模型绝对路径（兼容 PyInstaller：资源在 sys._MEIPASS）。"""
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *_MODEL_REL)


def available() -> tuple[bool, str]:
    """模型能不能用。返回 (ok, 说明)。**探测与使用分开**（项目铁律）。"""
    p = model_path()
    if not os.path.isfile(p):
        return False, "模型缺失：%s" % p
    try:
        import onnxruntime                                     # noqa: F401
    except Exception as e:                                     # noqa: BLE001
        return False, "onnxruntime 不可用：%s" % e
    return True, p


def set_threads(n: int):
    """设置推理线程数（下一会话生效；演出中应设小一些）。"""
    global _THREADS, _SESS
    n = max(1, int(n))
    with _BT_LOCK:
        if n != _THREADS:
            _THREADS = n
            _SESS = None            # 重建会话以应用线程数


_THREADS = max(1, min(4, (os.cpu_count() or 4) // 2))


def _session():
    """延迟创建 + 复用（onnxruntime 会话较贵，别每首新建）。"""
    global _SESS, _SESS_ERR
    with _BT_LOCK:
        if _SESS is not None:
            return _SESS
        if _SESS_ERR:
            return None
        try:
            import onnxruntime as ort
            so = ort.SessionOptions()
            so.intra_op_num_threads = _THREADS
            so.inter_op_num_threads = 1
            # 关掉池线程自旋 —— 否则空闲时也在烧核（audio_genre 踩过同样的坑）
            try:
                so.add_session_config_entry("session.intra_op.allow_spinning", "0")
                so.add_session_config_entry("session.inter_op.allow_spinning", "0")
            except Exception:                                   # noqa: BLE001
                pass
            _SESS = ort.InferenceSession(model_path(), sess_options=so,
                                         providers=["CPUExecutionProvider"])
        except Exception as e:                                  # noqa: BLE001
            _SESS_ERR = "%s: %s" % (type(e).__name__, str(e)[:160])
            return None
        return _SESS


# ======================================================================
# log-mel（纯 numpy 复刻 torchaudio）
# ======================================================================
def _hz_to_mel(hz):
    f_sp = 200.0 / 3.0
    mels = hz / f_sp
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0
    return np.where(hz >= min_log_hz,
                    min_log_mel + np.log(np.maximum(hz, 1e-12) / min_log_hz) / logstep, mels)


def _mel_to_hz(mel):
    f_sp = 200.0 / 3.0
    freqs = f_sp * mel
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0
    return np.where(mel >= min_log_mel,
                    min_log_hz * np.exp(logstep * (mel - min_log_mel)), freqs)


_FB = None
_WIN = None
_FB_LOCK = threading.Lock()


def _bank():
    global _FB, _WIN
    with _FB_LOCK:
        if _FB is not None:
            return _FB, _WIN
        freqs = np.linspace(0.0, SR // 2, N_FFT // 2 + 1)
        m_pts = np.linspace(_hz_to_mel(F_MIN), _hz_to_mel(F_MAX), N_MELS + 2)
        f_pts = _mel_to_hz(m_pts)
        f_diff = f_pts[1:] - f_pts[:-1]
        slopes = f_pts[None, :] - freqs[:, None]
        down = -slopes[:, :-2] / f_diff[:-1]
        up = slopes[:, 2:] / f_diff[1:]
        fb = np.clip(np.minimum(down, up), 0.0, None)
        if SLANEY_NORM:                     # ⚠ 默认不归一化
            enorm = 2.0 / (f_pts[2:N_MELS + 2] - f_pts[:N_MELS])
            fb = fb * enorm[None, :]
        _FB = fb.astype(np.float32)
        _WIN = (0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(N_FFT) / N_FFT)).astype(np.float32)
        return _FB, _WIN


def logmel(mono):
    fb, win = _bank()
    pad = N_FFT // 2
    x = np.pad(np.asarray(mono, np.float32), (pad, pad), mode="reflect")
    n = (len(x) - N_FFT) // HOP + 1
    if n <= 0:
        return np.zeros((0, N_MELS), np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(x, N_FFT)[::HOP][:n]
    s = np.abs(np.fft.rfft(frames * win, axis=1)).astype(np.float32)
    s /= np.sqrt(float(N_FFT))              # ⚠ normalized="frame_length"
    return np.log1p(LOG_MULT * (s @ fb)).astype(np.float32)


def _resample_to_22k(mono, sr):
    if int(sr) == SR:
        return np.asarray(mono, np.float32)
    n_out = max(1, int(len(mono) * SR / float(sr)))
    idx = np.linspace(0.0, len(mono) - 1.0, n_out)
    i0 = np.floor(idx).astype(np.int64)
    i1 = np.minimum(i0 + 1, len(mono) - 1)
    frac = (idx - i0).astype(np.float32)
    return (mono[i0] * (1.0 - frac) + mono[i1] * frac).astype(np.float32)


# ======================================================================
# 推理 + 后处理
# ======================================================================
def _frames(sess, spect):
    in_name = sess.get_inputs()[0].name
    out_names = [o.name for o in sess.get_outputs()]
    t = spect.shape[0]

    def run(ch):
        b, d = sess.run(out_names, {in_name: ch[None, :, :].astype(np.float32)})
        return b[0].astype(np.float64), d[0].astype(np.float64)

    if t <= CHUNK:
        s = np.zeros((CHUNK, N_MELS), np.float32)
        s[:t] = spect
        b, d = run(s)
        return b[:t], d[:t]
    starts = list(range(-BORDER, t - BORDER, CHUNK - 2 * BORDER))
    if t > CHUNK - 2 * BORDER:
        starts[-1] = t - (CHUNK - BORDER)
    pb = np.full(t, -1000.0)
    pd = np.full(t, -1000.0)
    for st in reversed(starts):             # keep_first
        lo, hi = max(st, 0), min(st + CHUNK, t)
        seg = spect[lo:hi]
        left, right = max(0, -st), max(0, min(BORDER, st + CHUNK - t))
        if left or right:
            seg = np.pad(seg, ((left, right), (0, 0)))
        b, d = run(seg)
        pb[st + BORDER:st + CHUNK - BORDER] = b[BORDER:-BORDER]
        pd[st + BORDER:st + CHUNK - BORDER] = d[BORDER:-BORDER]
    return pb, pd


def _max_pool1d(v, k=7, p=3):
    vp = np.pad(v, (p, p), constant_values=-np.inf)
    n = len(v)
    out = np.empty(n)
    # 向量化（比逐点 for 快得多，且这里是离线路径的最后一步）
    idx = np.arange(n)[:, None] + np.arange(k)[None, :]
    out[:] = vp[idx].max(axis=1)
    return out


def _dedup(peaks, width=1):
    res = []
    if len(peaks) == 0:
        return np.array(res, dtype=np.float64)
    p, c = int(peaks[0]), 1
    for p2 in peaks[1:]:
        p2 = int(p2)
        if p2 - p <= width:
            c += 1
            p += (p2 - p) / c
        else:
            res.append(p)
            p, c = p2, 1
    res.append(p)
    return np.array(res, dtype=np.float64)


def _postprocess(beat_log, down_log):
    bl = _max_pool1d(beat_log)
    dl = _max_pool1d(down_log)
    beats = (bl == beat_log) & (beat_log > 0.0)
    downs = (dl == down_log) & (down_log > 0.0)
    bt = _dedup(np.flatnonzero(beats), 1) / FPS
    dt = _dedup(np.flatnonzero(downs), 1) / FPS
    if len(bt) > 0 and len(dt) > 0:          # downbeat 吸到最近 beat
        for i, d in enumerate(dt):
            dt[i] = bt[np.argmin(np.abs(bt - d))]
        dt = np.unique(dt)
    return bt, dt


def _implied_bpm(times):
    """由拍点估 BPM。

    ⚠ 不能直接 median(IBI)：50fps 下 IBI 量化到整数帧（128BPM 真实 23.44 帧，
    逐拍在 23/24 之间跳，median 会给出 ±2% 的假误差）⇒ 用**内点 IBI 的均值**；
    并先剔除"多检拍"造成的离群 IBI。
    """
    t = np.asarray(times, np.float64)
    if len(t) < 4:
        return 0.0
    d = np.diff(t)
    med = float(np.median(d))
    if med <= 0:
        return 0.0
    inl = d[(d > 0.6 * med) & (d < 1.6 * med)]
    if len(inl) >= 3:
        return 60.0 / float(np.mean(inl))
    slope = float(np.polyfit(np.arange(len(t), dtype=np.float64), t, 1)[0])
    return 60.0 / slope if slope > 0 else 0.0


def _fit_bar(dt, bar0):
    """**抗离群**的最小二乘拟合，只返回小节长度 bar（秒）。

    ⚠ 为什么不用 BT 的全局 BPM：它有 0.2~0.8% 误差，乘 60 秒窗口的几十个小节后
      会累积成 0.2~1.2 拍的相位偏置（实测 Sexy Edge：VDJ 155.09 vs BT 156.36 ⇒ 偏 0.80 拍）。
    ⚠ 为什么不用"相邻 downbeat 间距的中位数"：那对多检的 downbeat 也敏感
      （实测 Crescent Horizon 被带成 192 BPM）。
    ⚠ 为什么要迭代剔除：模型偶尔**多检/漏检**一个 downbeat（实测 Leviathan 60 秒里
      给出 43 个小节，正常只有 33 个）⇒ 小节序号 n 错配，直线被拽偏。
      做法：拟合 → 按残差剔除离群 → 用剩下的重拟合，最多 3 轮。
    """
    dt = np.asarray(dt, np.float64)
    if len(dt) < 3:
        return bar0
    n = np.round((dt - dt[0]) / bar0)
    bar = bar0
    for _ in range(3):
        try:
            coef, *_ = np.linalg.lstsq(
                np.vstack([np.ones_like(n), n]).T, dt, rcond=None)
        except Exception:                                       # noqa: BLE001
            break
        a_c, bar_c = float(coef[0]), float(coef[1])
        if not (0.85 * bar0 < bar_c < 1.15 * bar0):
            break                       # 偏离太多说明 n 错配，放弃这次拟合
        resid = dt - (a_c + n * bar_c)
        keep = np.abs(resid) < 0.30 * bar_c
        nk = int(keep.sum())
        if 3 <= nk < len(dt):
            try:
                coef2, *_ = np.linalg.lstsq(
                    np.vstack([np.ones(nk), n[keep]]).T, dt[keep], rcond=None)
                a2, bar2 = float(coef2[0]), float(coef2[1])
                if 0.85 * bar0 < bar2 < 1.15 * bar0:
                    bar_c = bar2
            except Exception:                                   # noqa: BLE001
                pass
        bar = bar_c
        n = np.round((dt - a_c) / bar)      # 用新的 bar 重算小节序号
    return bar


def _one_pass(sess, mono):
    spect = logmel(mono)
    if spect.shape[0] < 64:
        return None
    b, d = _frames(sess, spect)
    bt, dt = _postprocess(b, d)
    if len(bt) < 4 or len(dt) < 2:
        return None
    bpm = _implied_bpm(bt)
    if bpm <= 0:
        return None
    return {"bpm": bpm, "beats": bt, "downbeats": dt}


def analyze(mono, sr, maxsec=WIN_DEFAULT):
    """从**已解码**单声道信号估小节头。

    返回 dict 或 None（不可用时）。
      {"bpm", "anchor"(秒，某个小节头), "beat_len", "n_beats", "n_bars", "win"}
    ⚠ 自适应：默认 60 秒；若检出拍数 < MIN_BEATS，自动延长到 WIN_MAX 重跑
      （照顾"开头是无鼓 intro"的曲子）。
    """
    ok, _why = available()
    if not ok:
        return None
    sess = _session()
    if sess is None:
        return None
    sr = int(sr)
    n = min(len(mono), int(maxsec * sr)) if maxsec else len(mono)
    wins = [maxsec, WIN_MAX] if maxsec and maxsec < WIN_MAX else [maxsec]
    last = None
    for w in wins:
        m = mono[:min(len(mono), int(w * sr))] if w else mono
        x = _resample_to_22k(m, sr)
        with _LOCK:
            r = _one_pass(sess, x)
        if r is None:
            continue
        last = r
        if len(r["beats"]) >= MIN_BEATS:
            break
    if last is None:
        return None

    beats = np.asarray(last["beats"], np.float64)
    dt = np.asarray(last["downbeats"], np.float64)

    # ★★ 定锚点的正确姿势（连续踩了三个坑才收敛，别改）：
    #   ① 不要用 BT 的**全局 BPM** 去展开小节序号 —— 它有 0.2~0.8% 误差，
    #      乘 60 秒窗口的几十个小节后累积成 0.2~1.2 拍的相位偏置
    #      （实测 Sexy Edge：VDJ 155.09 vs BT 156.36 ⇒ 相位偏 0.80 拍）。
    #   ② 不要拿 **downbeat 做直线拟合** —— BT 偶尔多检/漏检 downbeat
    #      （实测 Leviathan 60 秒里给出 43 个小节，正常只有 33 个），拟合会被拽偏。
    #   ③ 正解：**小节长度只用 downbeat 自己的相邻间距取中位数**（对离群稳健、
    #      且不依赖任何全局 BPM）；相位再把每个 downbeat 折进一个 bar 取中位数。
    #   实测 BT 的 downbeat 落在 VDJ 拍线上只差 ±0.02 拍 ⇒ 拍网本身是极准的，
    #   唯一不确定的只是"4 个拍里哪一个是小节头"。
    bpm0 = float(last["bpm"])
    bar = 4.0 * (60.0 / bpm0)
    cand = _fit_bar(dt, bar)                    # ① 小节长度：抗离群 LS（准）
    if 0.2 < cand / 4.0 < 1.5:                  # 对应 BPM 40~300
        bar = cand
    bl = bar / 4.0
    bpm = 60.0 / bl

    res = dt - np.round((dt - dt[0]) / bar) * bar
    anchor = float(np.median(res))
    # 平移到 [0, bar)：保持 "anchor 就是某个小节头" 的语义
    if anchor < 0.0:
        anchor += bar * float(np.ceil(-anchor / bar))
    elif anchor >= bar:
        anchor -= bar * float(np.floor(anchor / bar))

    return {"bpm": round(bpm, 4), "beat_len": round(bl, 6),
            "anchor": round(anchor, 4), "n_beats": int(len(beats)),
            "n_bars": int(len(dt)), "win": float(maxsec),
            "bpm_bt": round(bpm0, 3)}


def analyze_file(path, maxsec=WIN_DEFAULT):
    """独立调试用：自己读文件。"""
    import soundfile as sf
    info = sf.info(path)
    data, sr0 = sf.read(path, dtype="float32", always_2d=True,
                        frames=int(maxsec * info.samplerate))
    mono = data[:, 0].copy()          # ★ 用左声道（mean 遇反相素材会抵消低频）
    del data
    return analyze(mono, int(sr0), maxsec=maxsec)


def selftest():
    """自检：模型在不在、log-mel 形状对不对、一次前向能不能跑通。

    ⚠ **不要拿合成信号要求"必须检出小节"** —— 合成信号不是音乐，模型给不出结果是正常的
      （踩过一次，看着像失败其实管线没坏）。所以这里只验证管线连通与形状。
    """
    ok, why = available()
    print("[phrase_bt] available:", ok, why)
    if not ok:
        return False
    sess = _session()
    if sess is None:
        print("[phrase_bt] 会话创建失败:", _SESS_ERR)
        return False
    x = np.zeros(SR * 8, np.float32)        # 8 秒静音，只验证形状
    sp = logmel(x)
    print("[phrase_bt] log-mel 形状:", sp.shape, "（期望约 (400, 128)）")
    with _LOCK:
        b, d = _frames(sess, sp)
    print("[phrase_bt] 前向输出:", b.shape, d.shape,
          "| 输入名=%s 输出=%s" % (sess.get_inputs()[0].name,
                                   [o.name for o in sess.get_outputs()]))
    return sp.shape[1] == N_MELS and len(b) == sp.shape[0]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="")
    ap.add_argument("--maxsec", type=float, default=WIN_DEFAULT)
    a = ap.parse_args()
    if a.file:
        t0 = time.perf_counter()
        print(analyze_file(a.file, maxsec=a.maxsec), "耗时 %.2fs" % (time.perf_counter() - t0))
    else:
        selftest()
