# -*- coding: utf-8 -*-
"""真实彩色波形（数据层）验证 `src/ercue/audio/waveform.py`。**只读被测源码。**

跑法：
    P:\\ERAVJ\\EasyRealityCue\\venv\\Scripts\\python.exe  tools/_waveform_verify.py

判据：
  1) 频段响应：纯 100 Hz / 1 kHz / 6 kHz ⇒ 对应通道显著高于其它两通道（给出实测数值）
  2) 瞬态保留："安静段 + 强鼓点" ⇒ 鼓点列 amp 明显高于安静段
  3) 时长正确：10 秒音频 ⇒ duration_sec ≈ 10（±0.1）
  4) 缓存生效：第二次调用不再重新解码（耗时 + 进度回调对比）
  5) 性能：3 分钟音频端到端 compute 耗时（≤ 5 s 判据）
  6) 混色占比 + 颜色多样性：对《揽月》 ⇒ 三通道同时>0.25 的列占比 ≥30%；8×8×8 色格数 ≥60
  7) 视觉留档：渲染《揽月》波形 PNG（改动前 / 后）到 `_shots/`

夹具全部用 `stdlib wave` + numpy 合成，落 `tempfile.mkdtemp()`；缓存目录被改写到临时目录（不污染用户目录）。
《揽月》相关判据若素材不存在则**跳过**（不判失败）。
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import time
import traceback
import wave
import zlib

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import numpy as np                                                   # noqa: E402

import ercue.audio.waveform as wf                                    # noqa: E402

_TMP = tempfile.mkdtemp(prefix="wf_verify_")
_FAILS = []


def _need(cond, msg):
    print("    · %s %s" % ("OK " if cond else "✗  ", msg))
    if not cond:
        _FAILS.append(msg)


def _write_wav(path, samples, sr=44100):
    """float32 [-1,1] 单声道 → 立体声 PCM16 WAV。"""
    x = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    stereo = np.stack([x, x], axis=1)
    pcm = (stereo * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())


def _tone(freq, dur, sr=44100, amp=0.35):
    t = np.arange(int(sr * dur)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


# 缓存目录改写到临时目录（隔离，不碰用户 %LOCALAPPDATA%）
_CACHE = os.path.join(_TMP, "cache")
os.makedirs(_CACHE, exist_ok=True)
wf.cache_dir = lambda: _CACHE                                        # noqa: E731

# 真实曲（与用户截图同一首）；不存在则相关判据跳过
FLAC = r"Y:\EasyRealityAutoVJ\码表素材\揽月 - Dreamira.flac"
_SHOTS = os.path.join(_ROOT, "_shots")


# --------------------------------------------------------------------------
# 渲染辅助（纯 stdlib zlib 写 PNG，**不依赖 Qt/PIL**）
# --------------------------------------------------------------------------
def _write_png(path, rgb, w, h):
    """把 `(h, w, 3)` uint8 数组写成 8-bit RGB PNG（stdlib zlib）。"""
    raw = bytearray()
    for y in range(h):
        raw.append(0)                                                # 每行 filter=0
        raw += rgb[y].tobytes()

    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data +
                struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)             # color type 2 = RGB
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
           chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def _render_waveform_png(path, amp, r, g, b, width=None, height=420):
    """渲染整轨彩色波形（**垂直居中对称柱**）：高度=amp，颜色=(r,g,b)。

    ★ 每个像素取该段内 **amp 最大的那一列** 的颜色（代表列），而**不做均值** ——
      否则相邻的纯色列被平均后也被"混"成彩色，会掩盖"纯色 vs 混色"的真实差异。
    """
    n = len(amp)
    width = int(width or min(2000, n))
    k = int(np.ceil(n / float(width)))
    pad = width * k - n
    A = np.pad(np.asarray(amp, np.float64), (0, pad), "edge").reshape(width, k)
    R = np.pad(np.asarray(r, np.float64), (0, pad), "edge").reshape(width, k)
    G = np.pad(np.asarray(g, np.float64), (0, pad), "edge").reshape(width, k)
    B = np.pad(np.asarray(b, np.float64), (0, pad), "edge").reshape(width, k)
    pick = np.argmax(A, axis=1)
    xi = np.arange(width)
    Aa, Rr, Gg, Bb = A[xi, pick], R[xi, pick], G[xi, pick], B[xi, pick]
    img = np.zeros((height, width, 3), np.uint8)
    img[:, :] = (12, 12, 16)                                         # 近黑背景
    mid = height // 2
    half = height // 2 - 2
    for x in range(width):
        hh = int(round(float(Aa[x]) * half))
        if hh <= 0:
            continue
        col = (np.clip([Rr[x], Gg[x], Bb[x]], 0.0, 1.0) * 255.0).astype(np.uint8)
        img[mid - hh:mid + hh + 1, x] = col
    _write_png(path, img, width, height)


# 改动前的旧算法（仅用于"前后对比"渲染，不复用生产代码）
def _old_colors(E, gamma=2.4):
    act = E.sum(1) > 0
    gm = E[act].mean(0)
    gm = np.maximum(gm, max(gm.max() * 1e-3, 1e-12))
    rel = E / gm
    sh = rel / np.maximum(rel.sum(1, keepdims=True), 1e-12)
    em = sh ** gamma
    out = em / np.maximum(em.sum(1, keepdims=True), 1e-12)
    out[~act] = 1.0 / 3.0
    return out[:, 0].astype(np.float32), out[:, 1].astype(np.float32), out[:, 2].astype(np.float32)


def _old_amp(mono, win, ncol, floor=0.10):
    xr = mono[:ncol * win].reshape(ncol, win).astype(np.float64)
    rms = np.sqrt(np.mean(xr * xr, 1))
    pk = np.max(np.abs(xr), 1)
    amp = np.sqrt(rms * pk)
    an = np.clip(amp / (float(np.percentile(amp, 99.0)) + 1e-12), 0, 1)
    env = wf._moving_average(an, int(round(wf.COLUMNS_PER_SEC * 1.0)))
    loc = np.clip(an / (env + floor), 0, 1)
    return np.clip(np.sqrt(an * loc), 0, 1).astype(np.float32)


# --------------------------------------------------------------------------
# 1) 频段响应
# --------------------------------------------------------------------------
def t1_band_response():
    cases = [("低频 100 Hz", 100.0, "r"), ("中频 1 kHz", 1000.0, "g"), ("高频 6 kHz", 6000.0, "b")]
    for name, freq, chan in cases:
        p = os.path.join(_TMP, "tone_%d.wav" % int(freq))
        _write_wav(p, _tone(freq, 3.0))
        d = wf.compute(p)
        assert d is not None, name
        # 取中段求均值，避开两端（整块 FFT 起始处无左上下文）
        a, b = int(d.columns * 0.2), int(d.columns * 0.8)
        means = {"r": float(np.mean(d.r[a:b])), "g": float(np.mean(d.g[a:b])),
                 "b": float(np.mean(d.b[a:b]))}
        dominant = max(means, key=means.get)
        other = [v for k, v in means.items() if k != chan]
        print("    %-12s r=%.3f g=%.3f b=%.3f  → 主导=%s" %
              (name, means["r"], means["g"], means["b"], dominant))
        _need(dominant == chan, "%s ⇒ 主导通道应为 %s" % (name, chan.upper()))
        _need(means[chan] > max(other) + 0.15, "%s ⇒ 主导通道应**显著**高于其它（%.3f vs %.3f）"
              % (name, means[chan], max(other)))


# --------------------------------------------------------------------------
# 2) 瞬态保留
# --------------------------------------------------------------------------
def t2_transient():
    sr = 44100
    dur = 4.0
    t = np.arange(int(sr * dur)) / sr
    x = (0.012 * np.sin(2 * np.pi * 180.0 * t)).astype(np.float32)    # 安静段：极轻
    x += (0.004 * np.random.default_rng(1).standard_normal(x.size)).astype(np.float32)
    # 2.0s 起，每 0.5s 一个强鼓点（指数衰减的低频瞬态）
    drum_times = np.arange(2.0, 3.6, 0.5)
    for dt in drum_times:
        i0 = int(dt * sr)
        n = int(0.18 * sr)
        tau = np.arange(n) / sr
        burst = (0.95 * np.exp(-28.0 * tau) * np.sin(2 * np.pi * 80.0 * tau)).astype(np.float32)
        x[i0:i0 + n] = np.clip(x[i0:i0 + n] + burst, -1.0, 1.0)
    p = os.path.join(_TMP, "drums.wav")
    _write_wav(p, x, sr)

    d = wf.compute(p)
    assert d is not None
    cols = d.columns
    cps = cols / dur
    quiet = d.amp[int(0.2 * cps):int(1.8 * cps)]                      # 0.2~1.8s 安静段
    drum_idx = [int(dt * cps) for dt in drum_times]
    drum = np.array([float(np.max(d.amp[max(0, i - 3):i + 14])) for i in drum_idx])  # 鼓点列
    print("    安静段 amp 中位数=%.3f   鼓点列 amp=%s（均值 %.3f）"
          % (float(np.median(quiet)), np.round(drum, 3), float(np.mean(drum))))
    _need(float(np.median(quiet)) > 0.0, "安静段 amp 应 > 0（弱段也可见）")
    _need(float(np.mean(drum)) > 3.0 * float(np.median(quiet)) + 0.05,
          "鼓点列 amp 应明显高于安静段（≥3×）")


# --------------------------------------------------------------------------
# 3) 时长正确
# --------------------------------------------------------------------------
def t3_duration():
    p = os.path.join(_TMP, "ten.wav")
    _write_wav(p, _tone(440.0, 10.0))
    d = wf.compute(p)
    assert d is not None
    print("    10 秒素材 ⇒ duration_sec=%.4f  列数=%d" % (d.duration_sec, d.columns))
    _need(abs(d.duration_sec - 10.0) <= 0.1, "duration_sec 应 ≈ 10（±0.1），实得 %.4f" % d.duration_sec)


# --------------------------------------------------------------------------
# 4) 缓存生效
# --------------------------------------------------------------------------
def t4_cache():
    p = os.path.join(_TMP, "cache_src.wav")
    _write_wav(p, _tone(1000.0, 12.0))
    cp = wf.cache_path_for(p)
    if os.path.isfile(cp):
        os.remove(cp)

    seen1 = []
    t0 = time.perf_counter()
    d1 = wf.load_or_compute(p, progress=seen1.append)
    t_first = time.perf_counter() - t0
    assert d1 is not None
    _need(os.path.isfile(cp), "首次 load_or_compute 应写出缓存：%s" % os.path.basename(cp))
    _need(len(seen1) > 1, "首次应出现解码阶段进度回调（%d 次）" % len(seen1))

    seen2 = []
    t0 = time.perf_counter()
    d2 = wf.load_or_compute(p, progress=seen2.append)                # 命中缓存
    t_cached = time.perf_counter() - t0
    assert d2 is not None
    print("    首次=%.4fs（回调 %d 次）  缓存命中=%.4fs（回调 %d 次）"
          % (t_first, len(seen1), t_cached, len(seen2)))
    _need(t_cached < t_first, "缓存命中应快于首次计算")
    _need(seen2 == [1.0], "缓存命中不应出现解码阶段回调（即未重新解码）：%r" % (seen2,))
    _need(d2.columns == d1.columns and abs(d2.duration_sec - d1.duration_sec) < 1e-9,
          "缓存数据应与首次一致")

    # force=True 应绕过缓存重算
    seen3 = []
    t0 = time.perf_counter()
    _ = wf.load_or_compute(p, progress=seen3.append, force=True)
    t_force = time.perf_counter() - t0
    _need(len(seen3) > 1 and t_force >= t_cached, "force=True 应绕过缓存重算（回调 %d 次）" % len(seen3))


# --------------------------------------------------------------------------
# 5) 性能：3 分钟
# --------------------------------------------------------------------------
def t5_perf():
    sr = 44100
    dur = 180.0
    t = np.arange(int(sr * dur)) / sr
    rng = np.random.default_rng(7)
    x = (0.05 * np.sin(2 * np.pi * 200.0 * t)).astype(np.float32)     # 前 90s：安静
    half = x.size // 2
    x[half:] = (0.4 * np.sin(2 * np.pi * 900.0 * t[half:])             # 后 90s：副歌（中+高频）
                + 0.2 * np.sin(2 * np.pi * 6000.0 * t[half:])
                + 0.15 * rng.standard_normal(x.size - half)).astype(np.float32)
    p = os.path.join(_TMP, "threemin.wav")
    _write_wav(p, x, sr)
    print("    夹具 3 分钟 WAV：%.1f MB" % (os.path.getsize(p) / 1e6))

    t0 = time.perf_counter()
    d = wf.compute(p)                                                # 端到端（解码 + 全部分析）
    dt = time.perf_counter() - t0
    assert d is not None
    print("    端到端 compute=%.2fs  列数=%d  时长=%.2fs（目标 ≤5s）" % (dt, d.columns, d.duration_sec))
    _need(dt <= 5.0, "3 分钟音频 compute 应 ≤ 5s，实得 %.2fs" % dt)
    _need(abs(d.duration_sec - 180.0) <= 0.3, "3 分钟时长应 ≈180s，实得 %.2f" % d.duration_sec)

    # 结构可见性：副歌段（后半）的相对能量应高于安静段（前半）的幅度
    cps = d.columns / dur
    quiet_amp = float(np.median(d.amp[int(20 * cps):int(80 * cps)]))
    chorus_amp = float(np.median(d.amp[int(110 * cps):int(170 * cps)]))
    print("    通道均值：安静段 amp=%.3f  副歌段 amp=%.3f（比值 %.2f）"
          % (quiet_amp, chorus_amp, chorus_amp / max(1e-9, quiet_amp)))
    _need(chorus_amp > quiet_amp, "副歌段幅度应高于安静段（结构可见）")


# --------------------------------------------------------------------------
# 6) 混色占比 + 颜色多样性（真实曲《揽月》）
# --------------------------------------------------------------------------
def t6_mixing_and_diversity():
    if not os.path.isfile(FLAC):
        print("    ! 素材不存在，跳过：%s" % FLAC)
        return
    t0 = time.perf_counter()
    d = wf.compute(FLAC)
    dt = time.perf_counter() - t0
    assert d is not None
    r, g, b = d.r, d.g, d.b
    mix = float(np.mean((r > 0.25) & (g > 0.25) & (b > 0.25)))
    dm = np.argmax(np.stack([r, g, b], 1), 1)
    q = ((np.clip(r, 0, .999) * 8).astype(int) * 64 +
         (np.clip(g, 0, .999) * 8).astype(int) * 8 +
         (np.clip(b, 0, .999) * 8).astype(int))
    cells = int(len(np.unique(q)))
    print("    列数=%d 时长=%.1fs compute=%.2fs" % (d.columns, d.duration_sec, dt))
    print("    r=%.2f~%.2f(%.2f)  g=%.2f~%.2f(%.2f)  b=%.2f~%.2f(%.2f)"
          % (r.min(), r.max(), r.mean(), g.min(), g.max(), g.mean(), b.min(), b.max(), b.mean()))
    print("    ★ 三通道同时>0.25 占比 = %.1f%%（改动前基线 ~1.7%%）" % (100 * mix))
    print("    ★ 8×8×8 色格数 = %d（改动前基线 64）" % cells)
    print("    R/G/B 主导 = %.0f%% / %.0f%% / %.0f%%" %
          (100 * np.mean(dm == 0), 100 * np.mean(dm == 1), 100 * np.mean(dm == 2)))
    _need(mix >= 0.30, "三通道同时>0.25 的列占比应 ≥30%%，实得 %.1f%%" % (100 * mix))
    _need(cells >= 60, "8×8×8 色格数应 ≥60，实得 %d" % cells)


# --------------------------------------------------------------------------
# 7) 视觉留档：渲染《揽月》波形 PNG（改动前 / 后）
# --------------------------------------------------------------------------
def t7_render_png():
    if not os.path.isfile(FLAC):
        print("    ! 素材不存在，跳过：%s" % FLAC)
        return
    os.makedirs(_SHOTS, exist_ok=True)
    mono = wf._decode_mono16k(FLAC)
    assert mono is not None
    win, ncol = wf._plan_columns(mono.size, None, wf.ANALYSIS_SR)
    lo, mi, hi = wf._band_energy_columns(mono, wf.ANALYSIS_SR, win, ncol, None)
    E = np.stack([lo, mi, hi], 1).astype(np.float64)

    # 改动后（当前算法）
    r, g, b = wf._colors(lo, mi, hi)
    amp = wf._amp_columns(mono, win, ncol)
    p_after = os.path.join(_SHOTS, "waveform_tune_after.png")
    _render_waveform_png(p_after, amp, r, g, b)
    # 改动前（旧算法）
    oa = _old_amp(mono, win, ncol)
    ocr, ocg, ocb = _old_colors(E)
    p_before = os.path.join(_SHOTS, "waveform_tune_before.png")
    _render_waveform_png(p_before, oa, ocr, ocg, ocb)

    # 1:1 局部放大（1 像素 = 1 列，无色块平均 ⇒ 直观看出"纯色 vs 混色"）
    seg = slice(20000, 22400)
    zw = seg.stop - seg.start
    p_za = os.path.join(_SHOTS, "waveform_tune_zoom_after.png")
    _render_waveform_png(p_za, amp[seg], r[seg], g[seg], b[seg], width=zw)
    p_zb = os.path.join(_SHOTS, "waveform_tune_zoom_before.png")
    _render_waveform_png(p_zb, oa[seg], ocr[seg], ocg[seg], ocb[seg], width=zw)

    print("    amp 均值：改动前 %.3f → 改动后 %.3f" % (oa.mean(), amp.mean()))
    for p in (p_before, p_after, p_zb, p_za):
        print("    已渲染：%s" % os.path.relpath(p, _ROOT))
    _need(os.path.isfile(p_after) and os.path.getsize(p_after) > 1000,
          "应生成改动后波形 PNG（垂直居中对称柱，高度=amp、颜色=(r,g,b)）")


# --------------------------------------------------------------------------
def main():
    tests = [("1 频段响应（100Hz/1kHz/6kHz）", t1_band_response),
             ("2 瞬态保留（安静+鼓点）", t2_transient),
             ("3 时长正确（10s）", t3_duration),
             ("4 缓存生效", t4_cache),
             ("5 性能（3 分钟）", t5_perf),
             ("6 混色占比 + 颜色多样性（揽月）", t6_mixing_and_diversity),
             ("7 视觉留档 PNG（揽月 前/后）", t7_render_png)]
    for name, fn in tests:
        print("\n=== %s ===" % name)
        try:
            fn()
        except Exception:                                            # noqa: BLE001
            _FAILS.append(name)
            print("    ✗ 异常:\n" + traceback.format_exc())
    print("\n" + "=" * 64)
    if _FAILS:
        print("结果：%d 项失败 ✗" % len(_FAILS))
        for m in _FAILS:
            print("  ✗ " + m)
        return 1
    print("结果：波形数据层 7 项判据全部通过 ✓   夹具目录 %s" % _TMP)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
