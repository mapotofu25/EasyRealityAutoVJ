# -*- coding: utf-8 -*-
"""Beat This! (small0.onnx) 独立验证探针 —— 只读分析，不改动 src/。

流程（全部用已有依赖：numpy / soundfile / onnxruntime，**不装 librosa/scipy/torch**）：
    音频(任意采样率) --重采样到 22050 单声道--> log-mel 谱(numpy, 复刻 torchaudio)
        --> small0.onnx (onnxruntime) --> beat / downbeat logits(逐帧)
        --> minimal 后处理(峰值拾取) --> beat / downbeat 时间戳(秒)

评测：以 VirtualDJ 数据库的锚点(BPM/Phase)为「参考答案」，
    比较 Beat This 预测的 downbeat 与 VDJ 小节线(anchor + 4n*beat_len)的误差/相位；
    并与本项目 src/beatgrid.py 的现有方法做对照。

用法：
    python tools/_beat_this_probe.py                # 默认 5 首
    python tools/_beat_this_probe.py --n 8          # 前 8 首
    python tools/_beat_this_probe.py --maxsec 180
输出：同时打印并写入 P:\\AutoVJ\\_beat_this_probe.txt
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import beatgrid as bg                       # noqa: E402  本项目现有方法

MODEL = os.path.join(HERE, "_beat_this_models", "final0_cpp.onnx")
DIR = "W:/临时结界相关/2of/tofu"
OUT = os.path.join(ROOT, "_beat_this_probe.txt")

# ---- Beat This! 前处理参数（来自官方 beat_this/preprocessing.py 的 LogMelSpect 默认值）----
SR = 22050
N_FFT = 1024
HOP = 441
F_MIN = 30.0
F_MAX = 11000.0
N_MELS = 128
LOG_MULT = 1000.0
FPS = SR / HOP                  # = 50.0
# torchaudio.transforms.MelSpectrogram 的 norm 默认是 None，而 beat_this 没有传 norm
# ⇒ **不做 Slaney 面积归一化**（这一点极易踩坑：很多资料误以为默认是 slaney）。
SLANEY_NORM = False

# ---- 模型推理参数（来自官方 beat_this/inference.py）----
CHUNK = 1500                    # 30 s
BORDER = 6                      # 6 帧边界丢弃


# ======================================================================
# 1. log-mel 谱（纯 numpy，逐位复刻 torchaudio.transforms.MelSpectrogram）
#    mel_scale=slaney, norm=slaney, normalized="frame_length", power=1
# ======================================================================
def _hz_to_mel(hz):
    """Slaney mel（与 torchaudio._hz_to_mel / librosa 一致）。"""
    f_min, f_sp = 0.0, 200.0 / 3.0
    mels = (hz - f_min) / f_sp
    min_log_hz = 1000.0
    min_log_mel = (min_log_hz - f_min) / f_sp
    logstep = np.log(6.4) / 27.0
    return np.where(hz >= min_log_hz,
                    min_log_mel + np.log(np.maximum(hz, 1e-12) / min_log_hz) / logstep,
                    mels)


def _mel_to_hz(mel):
    f_min, f_sp = 0.0, 200.0 / 3.0
    freqs = f_min + f_sp * mel
    min_log_hz = 1000.0
    min_log_mel = (min_log_hz - f_min) / f_sp
    logstep = np.log(6.4) / 27.0
    return np.where(mel >= min_log_mel,
                    min_log_hz * np.exp(logstep * (mel - min_log_mel)),
                    freqs)


def mel_filterbank():
    """(n_fft//2+1, n_mels) 三角滤波组。

    ⚠ torchaudio 的 `melscale_fbanks` 只有在 norm="slaney" 时才做面积归一化；
      beat_this 的 LogMelSpect **没传 norm**，MelSpectrogram 的默认值也是 None，
      因此这里默认 **不** 做 Slaney 归一化（SLANEY_NORM=False）。
    """
    all_freqs = np.linspace(0.0, SR // 2, N_FFT // 2 + 1)
    m_pts = np.linspace(_hz_to_mel(F_MIN), _hz_to_mel(F_MAX), N_MELS + 2)
    f_pts = _mel_to_hz(m_pts)                          # (n_mels+2,)
    f_diff = f_pts[1:] - f_pts[:-1]                    # (n_mels+1,)
    slopes = f_pts[None, :] - all_freqs[:, None]       # (nfreq, n_mels+2)
    down = -slopes[:, :-2] / f_diff[:-1]
    up = slopes[:, 2:] / f_diff[1:]
    fb = np.clip(np.minimum(down, up), 0.0, None)
    if SLANEY_NORM:
        enorm = 2.0 / (f_pts[2:N_MELS + 2] - f_pts[:N_MELS])
        fb = fb * enorm[None, :]
    return fb.astype(np.float32)


_FB = None
_WIN = None


def logmel(mono):
    """mono: float32 @22050 → (frames, 128) log1p(1000*mel)。"""
    global _FB, _WIN
    if _FB is None:
        _FB = mel_filterbank()
        _WIN = (0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(N_FFT) / N_FFT)).astype(np.float32)
    pad = N_FFT // 2
    x = np.pad(mono, (pad, pad), mode="reflect")
    n_frames = (len(x) - N_FFT) // HOP + 1
    if n_frames <= 0:
        return np.zeros((0, N_MELS), np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(x, N_FFT)[::HOP][:n_frames]
    S = np.abs(np.fft.rfft(frames * _WIN, axis=1)).astype(np.float32)
    S /= np.sqrt(float(N_FFT))                         # normalized="frame_length"
    mel = S @ _FB
    return np.log1p(LOG_MULT * mel).astype(np.float32)


# ======================================================================
# 2. 读音频 → 22050 单声道
# ======================================================================
def load_mono(path, maxsec=None):
    import soundfile as sf
    info = sf.info(path)
    sr0 = int(info.samplerate)
    frames = -1 if maxsec is None else int(maxsec * sr0)
    data, _ = sf.read(path, dtype="float32", always_2d=True, frames=frames)
    mono = data.mean(axis=1).astype(np.float32)
    del data
    if sr0 != SR:
        n_out = int(len(mono) * SR / sr0)
        idx = np.linspace(0.0, len(mono) - 1.0, n_out)
        i0 = np.floor(idx).astype(np.int64)
        i1 = np.minimum(i0 + 1, len(mono) - 1)
        frac = (idx - i0).astype(np.float32)
        mono = (mono[i0] * (1.0 - frac) + mono[i1] * frac).astype(np.float32)
    return mono


# ======================================================================
# 3. ONNX 推理（分块 + 聚合，keep_first）
# ======================================================================
class BeatThis:
    def __init__(self, model_path=MODEL):
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, (os.cpu_count() or 4))
        self.sess = ort.InferenceSession(
            model_path, sess_options=so, providers=["CPUExecutionProvider"])
        self.in_name = self.sess.get_inputs()[0].name
        self.out_names = [o.name for o in self.sess.get_outputs()]

    def _run_chunk(self, chunk):
        x = chunk[None, :, :].astype(np.float32)
        beat, down = self.sess.run(self.out_names, {self.in_name: x})
        return beat[0].astype(np.float64), down[0].astype(np.float64)

    def frames(self, spect):
        """spect:(T,128) → (beat_logits[T], downbeat_logits[T])。"""
        T = spect.shape[0]
        if T <= CHUNK:
            pad = CHUNK - T
            s = np.zeros((CHUNK, N_MELS), np.float32)
            s[:T] = spect
            b, d = self._run_chunk(s)
            return b[:T], d[:T]
        starts = list(range(-BORDER, T - BORDER, CHUNK - 2 * BORDER))
        if T > CHUNK - 2 * BORDER:
            starts[-1] = T - (CHUNK - BORDER)
        chunks, starts2 = [], []
        for st in starts:
            lo, hi = max(st, 0), min(st + CHUNK, T)
            seg = spect[lo:hi]
            left = max(0, -st)
            right = max(0, min(BORDER, st + CHUNK - T))
            if left or right:
                seg = np.pad(seg, ((left, right), (0, 0)))
            chunks.append(seg)
            starts2.append(st)
        pb = np.full(T, -1000.0)
        pd = np.full(T, -1000.0)
        for st, ch in zip(reversed(starts2), reversed(chunks)):   # keep_first
            b, d = self._run_chunk(ch)
            b = b[BORDER:-BORDER]
            d = d[BORDER:-BORDER]
            pb[st + BORDER:st + CHUNK - BORDER] = b
            pd[st + BORDER:st + CHUNK - BORDER] = d
        return pb, pd


# ======================================================================
# 4. minimal 后处理（复刻官方 Postprocessor.postp_minimal）
# ======================================================================
def _max_pool1d(v, k=7, p=3):
    vp = np.pad(v, (p, p), constant_values=-np.inf)
    out = np.empty(len(v))
    for i in range(len(v)):
        out[i] = vp[i:i + k].max()
    return out


def _dedup(peaks, width=1):
    res = []
    if len(peaks) == 0:
        return np.array(res)
    p = int(peaks[0])
    c = 1
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


def postprocess(beat_log, down_log):
    beats = _max_pool1d(beat_log) == beat_log
    beats = beats & (beat_log > 0.0)
    downs = _max_pool1d(down_log) == down_log
    downs = downs & (down_log > 0.0)
    bf = np.flatnonzero(beats)
    df = np.flatnonzero(downs)
    bf = _dedup(bf, 1)
    df = _dedup(df, 1)
    bt = bf / FPS
    dt = df / FPS
    if len(bt) > 0:                    # downbeat 移到最近 beat
        for i, d in enumerate(dt):
            dt[i] = bt[np.argmin(np.abs(bt - d))]
    dt = np.unique(dt)
    return bt, dt


# ======================================================================
# 5. 一个完整预测：音频 → beat/downbeat 秒
# ======================================================================
def predict(model, path, maxsec=None):
    t0 = time.time()
    mono = load_mono(path, maxsec=maxsec)
    t1 = time.time()
    spect = logmel(mono)
    t2 = time.time()
    b, d = model.frames(spect)
    t3 = time.time()
    bt, dt = postprocess(b, d)
    t4 = time.time()
    info = {"load_s": t1 - t0, "mel_s": t2 - t1, "onnx_s": t3 - t2,
            "post_s": t4 - t3, "dur_s": len(mono) / SR, "n_frames": spect.shape[0]}
    return bt, dt, info


# ======================================================================
# 6. 评测工具
# ======================================================================
def vdj_barlines(entry, bl_override=None):
    bl = bl_override or entry["beat_len"]
    a = entry["anchor"]
    return bl, a


def implied_bpm(times):
    """由拍点序列稳健估计 BPM。

    ⚠ 两个坑：
      1) 直接用 median(IBI)：50fps 下 IBI 量化到整数帧，128 BPM 的真实拍长
         0.46875s=23.44 帧，逐拍在 23/24 帧间跳，median 会取到 125 或 130.4，
         凭空产生 ±2% 误差 → 改用**内点 IBI 的均值**（0.46/0.48 交替时均值≈0.46875）。
      2) 模型偶尔插入 1-2 个「多检拍」（IBI 骤降到 ~4 帧），会污染最小二乘斜率
         → 先按 IBI∈[0.6,1.6]·中位 剔除离群，再估计。
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


def octave_ratio(a, b):
    """a/b 归约到最接近的 {1, 2, 0.5, 3, 2/3, 4, 1/4} 里的比值（含原始比值）。"""
    if b <= 0:
        return 0.0, ""
    r = a / b
    cands = {1.0: "1x", 2.0: "2x", 0.5: "0.5x", 3.0: "3x",
             2 / 3: "2/3x", 4.0: "4x", 0.25: "1/4x",
             1.5: "1.5x", 3 / 2: "1.5x"}
    best = min(cands, key=lambda c: abs(np.log(r / c)))
    return r, cands[best]


def alignment(pred, bl, a):
    """pred 时间戳 → 相对 VDJ 网格线(a + n*bl)的误差（秒）。返回 (signed_med_s, abs_med_ms, med_beats)。"""
    pred = np.asarray(pred, np.float64)
    if len(pred) == 0:
        return None
    n = np.round((pred - a) / bl)
    lines = a + n * bl
    err = pred - lines                     # 秒，最近的网格线
    med = float(np.median(err))
    return med, float(np.median(np.abs(err))) * 1000.0, med / bl


def align_frac(pred, bl, a, tol_s=0.030):
    """pred 落在 VDJ 网格线 ±tol 内的时间戳占比（评判对齐是否真的干净）。"""
    pred = np.asarray(pred, np.float64)
    if len(pred) == 0:
        return 0.0
    n = np.round((pred - a) / bl)
    err = np.abs(pred - (a + n * bl))
    return float((err <= tol_s).mean())


# ======================================================================
# 7. 主流程
# ======================================================================
class Tee:
    def __init__(self, *fhs):
        self.fhs = fhs

    def write(self, s):
        for f in self.fhs:
            try:
                f.write(s)
            except Exception:
                pass

    def flush(self):
        for f in self.fhs:
            try:
                f.flush()
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--maxsec", type=float, default=180.0)
    ap.add_argument("--model", default=MODEL)
    args = ap.parse_args()

    if not os.path.isfile(args.model):
        print("模型不存在: %s" % args.model)
        return 1

    vdj = bg.load_vdj()
    print("VDJ 库: %d 首（已分析 %d）" % (len(vdj), vdj.analyzed))

    files = sorted(f for f in os.listdir(DIR) if f.lower().endswith(".flac"))
    # 优先用团队已验证过的 5 首，其余按名字补足
    prefer = [
        "Rolling Contact - Outburst Oxygen (Sharp Remaster).flac",
        "Rolling Contact - The Lost (135 Extended).flac",
        "Rolling Contact - Crescent Horizon (Rebuild).flac",
        "Rolling Contact - Dust.flac",
        "Rolling Contact - Endless & Limitless.flac",
    ]
    ordered = [f for f in prefer if f in files] + [f for f in files if f not in prefer]
    chosen = ordered[:args.n]

    model = BeatThis(args.model)
    print("模型: %s | 输入名=%s | 输出=%s" % (
        os.path.basename(args.model), model.in_name, model.out_names))
    print("分析窗口: 前 %.0f 秒 | 采样率 %d | 帧率 %.1f fps" % (args.maxsec, SR, FPS))
    print()

    rows = []
    for fn in chosen:
        path = os.path.join(DIR, fn)
        entry = bg.vdj_lookup(vdj, path)
        short = fn.replace("Rolling Contact - ", "")[:40]
        print("=" * 92)
        print("# %s" % short)
        print("=" * 92)
        if entry is None:
            print("  [!] VDJ 无匹配记录，跳过")
            continue
        bl, a = entry["beat_len"], entry["anchor"]
        vbpm = entry["bpm"]

        bt, dt, info = predict(model, path, maxsec=args.maxsec)
        bbpm = implied_bpm(bt)
        ratio, tag = octave_ratio(bbpm, vbpm)

        # --- Beat This downbeat vs VDJ 小节线 ---
        db = alignment(dt, 4 * bl, a)          # 相对小节线
        btline = alignment(bt, bl, a)          # 相对拍线
        dfrac = align_frac(dt, 4 * bl, a, 0.030)
        bfrac = align_frac(bt, bl, a, 0.030)
        exp_beats = args.maxsec * vbpm / 60.0
        exp_bars = exp_beats / 4.0
        print("  参考(VDJ): BPM %.3f  拍长 %.6fs  锚点 %.3fs  时长 %.1fs"
              % (vbpm, bl, a, entry["dur"]))
        print("  BeatThis : beats=%d (期望~%.0f)  downbeats=%d (期望~%.0f)  估计BPM %.3f (%s, 比值%.3f)"
              % (len(bt), exp_beats, len(dt), exp_bars, bbpm, tag, ratio))
        print("  耗时: 读%.2fs mel%.2fs onnx%.2fs 后处理%.2fs | 帧数 %d (%.1fs 音频)"
              % (info["load_s"], info["mel_s"], info["onnx_s"], info["post_s"],
                 info["n_frames"], info["dur_s"]))
        if db:
            print("  → downbeat vs 小节线: 绝对中位 %.1f ms | 拍位偏移 %+.3f 拍 | 落在±30ms内占比 %.0f%%"
                  % (db[1], db[2], dfrac * 100))
        else:
            print("  → downbeat: 无输出")
        if btline:
            print("  → beat    vs 拍线  : 绝对中位 %.1f ms | 拍位偏移 %+.3f 拍 | 落在±30ms内占比 %.0f%%"
                  % (btline[1], btline[2], bfrac * 100))

        # --- 我们现有方法（冷启动，无 VDJ 提示）---
        ta = time.time()
        ours = bg.analyze_track(path, maxsec=args.maxsec)
        ours_t = time.time() - ta
        o_period = 60.0 / ours["bpm"] if ours["bpm"] > 0 else 0.0
        o_bpm_ratio, o_tag = octave_ratio(ours["bpm_auto"], vbpm)
        o_phase = None
        if o_period > 0:
            o_phase = ((ours["anchor"] - a) / bl) % 4.0   # 相对 VDJ 锚点的拍相位(0=对齐)
            o_phase = min(o_phase, 4.0 - o_phase)
        print("  现有法(冷): BPM %.3f (auto %.3f, %s) 八拍头 %.3fs phrase %d contrast %.2f 耗时%.2fs"
              % (ours["bpm"], ours["bpm_auto"], o_tag, ours["anchor"],
                 ours["phrase"], ours["contrast"], ours_t))
        if o_phase is not None:
            print("  → 八拍头 vs VDJ锚点 相位: %.3f 拍 (0=对齐; 0.5=偏半拍即错拍)"
                  % o_phase)

        # --- 我们现有方法（暖启动：用 VDJ 的 BPM+锚点，只判八拍相位）---
        tw = time.time()
        ow = bg.analyze_track(path, maxsec=args.maxsec, bpm_hint=vbpm, anchor_hint=a)
        ow_t = time.time() - tw
        print("  现有法(暖): phrase %d contrast %.2f 八拍头 %.3fs 耗时%.2fs"
              % (ow["phrase"], ow["contrast"], ow["anchor"], ow_t))
        print()

        rows.append({
            "short": short, "vbpm": vbpm, "bbpm": bbpm, "tag": tag,
            "db_abs_ms": db[1] if db else None, "db_signed_ms": db[0] * 1000 if db else None,
            "db_beats": db[2] if db else None, "dfrac": dfrac,
            "bt_abs_ms": btline[1] if btline else None, "bfrac": bfrac,
            "n_beats": len(bt), "n_bars": len(dt),
            "exp_beats": exp_beats, "exp_bars": exp_bars,
            "ours_bpm": ours["bpm_auto"], "ours_tag": o_tag,
            "ours_phase": o_phase, "ours_phrase": ours["phrase"],
            "ours_contrast": ours["contrast"],
            "warm_phrase": ow["phrase"], "warm_contrast": ow["contrast"],
            "onnx_s": info["onnx_s"], "mel_s": info["mel_s"],
        })

    # ---------------- 汇总表 ----------------
    print("=" * 100)
    print("汇总：Beat This(%s) vs VDJ 参考（downbeat 对齐 = 判「第几拍是小节头」的相位）"
          % os.path.basename(args.model))
    print("=" * 100)
    print("%-24s %7s %7s %5s %6s %7s %8s %8s %7s"
          % ("曲目", "VDJ BPM", "BT BPM", "比值", "beats", "bars", "DB绝对ms", "DB拍偏", "<30ms"))
    print("-" * 100)
    for r in rows:
        print("%-24s %7.2f %7.2f %5s %6s %7s %8s %8s %6.0f%%"
              % (r["short"], r["vbpm"], r["bbpm"], r["tag"],
                 "%d/%.0f" % (r["n_beats"], r["exp_beats"]),
                 "%d/%.0f" % (r["n_bars"], r["exp_bars"]),
                 ("%.1f" % r["db_abs_ms"]) if r["db_abs_ms"] is not None else "-",
                 ("%+.3f" % r["db_beats"]) if r["db_beats"] is not None else "-",
                 r["dfrac"] * 100))

    print()
    print("=" * 92)
    print("汇总：现有方法(冷启动) 对照")
    print("=" * 92)
    print("%-26s %9s %5s %10s %9s %9s"
          % ("曲目", "自算BPM", "比值", "相位(拍)", "八拍phrase", "contrast"))
    print("-" * 92)
    for r in rows:
        print("%-26s %9.2f %5s %10s %9d %9.2f"
              % (r["short"], r["ours_bpm"], r["ours_tag"],
                 ("%.3f" % r["ours_phase"]) if r["ours_phase"] is not None else "-",
                 r["ours_phrase"], r["ours_contrast"]))

    # ---------------- 速度汇总 ----------------
    if rows:
        tot = sum(r["onnx_s"] + r["mel_s"] for r in rows)
        n = len(rows)
        print()
        print("平均单首(%.0fs窗口): mel %.2fs + onnx %.2fs = %.2fs（不含读盘）"
              % (args.maxsec,
                 sum(r["mel_s"] for r in rows) / n,
                 sum(r["onnx_s"] for r in rows) / n, tot / n))
    return 0


if __name__ == "__main__":
    fh = open(OUT, "w", encoding="utf-8")
    orig = sys.stdout
    sys.stdout = Tee(orig, fh)
    try:
        rc = main()
    finally:
        sys.stdout = orig
        fh.close()
        print("全文已写入 %s" % OUT)
    sys.exit(rc)
