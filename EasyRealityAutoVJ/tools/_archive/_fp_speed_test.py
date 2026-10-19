# -*- coding: utf-8 -*-
"""临时实测：指纹匹配对「变速」的鲁棒性（只读分析，不改 src）。

验证两件事：
  A) 不做 keylock（重采样）：时间轴 ×r 且音高 ×r  —— 类似黑胶/关掉 master tempo。
  B) 近似 keylock：只把幅度谱的时间轴拉伸 ×r（音高/频谱不变）—— 隔离 fp.py 里
     哈希三元组 (f1,f2,dt) 中 **dt 的单位与量化** 对时间缩放是否敏感。

跑： P:\\AutoVJ\\venv\\Scripts\\python.exe tools\\_fp_speed_test.py
"""
import os
import sys
import time
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import fp  # noqa: E402

SR = 44100
AUDIO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "_beat_audio",
                     "Rolling Contact - Outburst Oxygen (Sharp Remaster).flac")


def load(path, start=30.0, dur=150.0):
    import soundfile as sf
    info = sf.info(path)
    sr0 = int(info.samplerate)
    a, _ = sf.read(path, dtype="float32", always_2d=True,
                   start=int(start * sr0), frames=int(dur * sr0))
    mono = a.mean(axis=1)
    if sr0 != SR:
        x = np.linspace(0, len(mono) - 1, int(len(mono) * SR / sr0))
        mono = np.interp(x, np.arange(len(mono)), mono).astype(np.float32)
    return np.ascontiguousarray(mono, dtype=np.float32)


def build_db(sig):
    t0 = time.perf_counter()
    hs = fp.fingerprint_audio(sig, SR)
    dt = time.perf_counter() - t0
    db = defaultdict(list)
    for h, off in hs:
        db[h].append(off)
    return db, len(hs), dt


def match_hashes(db, qhashes):
    votes = defaultdict(int)
    for h, qoff in qhashes:
        for dboff in db.get(h, ()):
            votes[dboff - qoff] += 1
    if not votes:
        return 0, None
    delta, cnt = max(votes.items(), key=lambda kv: kv[1])
    return cnt, delta


def resample_time(x, r):
    """时间轴 ×r（r>1=变快）：取 len/r 个样本，重采样回去 → 时长变短、音高也变高"""
    n = max(2, int(round(len(x) / r)))
    idx = np.linspace(0, len(x) - 1, n)
    return np.interp(idx, np.arange(len(x)), x).astype(np.float32)


def timescale_mag(spec, r):
    """只缩放幅度谱时间轴（近似 keylock：频谱不变、时间轴 ×r）"""
    F, T = spec.shape
    Tn = max(2, int(round(T / r)))
    idx = np.clip((np.arange(Tn) * r).astype(np.int64), 0, T - 1)
    return spec[:, idx]


def main():
    print("音频:", os.path.basename(AUDIO))
    song = load(AUDIO, start=30.0, dur=150.0)
    print("载入 %.1fs @ %dHz" % (len(song) / SR, SR))

    db, n_hash, dt_build = build_db(song)
    print("建库：%d 条哈希，耗时 %.2fs" % (n_hash, dt_build))

    # ---- 基准：原速 4.5s 窗口 ----
    def window(t, dur=4.5):
        i0 = int(t * SR)
        return song[i0:i0 + int(dur * SR)]

    base = window(90.0)
    # 直接对窗口算 query 哈希
    t0 = time.perf_counter()
    qh = fp.fingerprint_audio(base, SR)
    dt_q = time.perf_counter() - t0
    cnt, delta = match_hashes(db, qh)
    exp_delta = int(90.0 * SR / fp.HOP_SIZE)  # 数据库里窗口起点对应的帧号
    print("\n[基准 原速] query 哈希 %d 条 (%.2fs)；命中票 %d，delta=%s (期望≈%d)"
          % (len(qh), dt_q, cnt, delta, exp_delta))

    # ---- A) 非 keylock：时间+音高一起变 ----
    print("\n[A) 重采样(时间×r, 音高×r) —— 关 keylock]")
    for r in (1.02, 1.05, 1.10):
        seg = window(90.0, dur=4.5 * r)      # 取 4.5*r 秒，压成 4.5 秒
        win = resample_time(seg, r)[:int(4.5 * SR)]
        qh = fp.fingerprint_audio(win, SR)
        cnt, delta = match_hashes(db, qh)
        print("  r=%.2f  哈希 %d，命中票 %d（基准 151）→ %s"
              % (r, len(qh), cnt, "OK" if cnt > 20 else "**匹配失败**"))

    # ---- B) 近似 keylock：只缩时间轴（频谱不变）----
    # ★ 用「固定 4.5s 挂钟采集窗口」建模：真实 _recognize_loop 取最近 4.5s 音频。
    #   变速 r 倍后，这 4.5s 挂钟里装的是 4.5*r 秒原曲内容 → 取 4.5*r 秒原曲，
    #   算谱后把时间轴压回 T/r 帧（音高不变），再匹配。
    print("\n[B) 幅度谱时间轴×r（近似 keylock，频谱不变；固定 4.5s 挂钟窗口）")
    spec0 = fp._spectrogram(base)
    peaks0 = fp._find_peaks(spec0)
    h0 = fp._hashes(peaks0)
    cnt0, delta0 = match_hashes(db, h0)
    print("  基准(r=1.0): 峰值 %d，哈希 %d，命中票 %d，delta=%d(期望≈%d)"
          % (len(peaks0), len(h0), cnt0, delta0, exp_delta))
    for r in (1.02, 1.05, 1.10):
        seg_r = window(90.0, dur=4.5 * r)          # 4.5*r 秒原曲
        spec_r = timescale_mag(fp._spectrogram(seg_r), r)
        peaks_r = fp._find_peaks(spec_r)
        h_r = fp._hashes(peaks_r)
        cnt, delta = match_hashes(db, h_r)
        raw = sum(1 for h, _ in h_r if h in db)
        print("  r=%.2f  峰值 %d，哈希 %d，能撞库 %d (%.0f%%)，最佳对齐票 %d，delta=%s(期望≈%d)"
              % (r, len(peaks_r), len(h_r), raw,
                 100.0 * raw / max(1, len(h_r)), cnt, delta, exp_delta))

    # ---- 关键：dt 量化桶在时间缩放下的漂移 ----
    print("\n[dt 哈希桶漂移分析] DT_QUANT=%d, HOP=%d (%.1fms/帧), MAX_DT=%d"
          % (fp.DT_QUANT, fp.HOP_SIZE, 1000.0 * fp.HOP_SIZE / SR, fp.MAX_HASH_TIME_DELTA))
    for dt in (20, 40, 80, 120, 160, 200):
        row = []
        for r in (1.05, 1.10):
            b_orig = dt // fp.DT_QUANT
            b_new = int(dt * r) // fp.DT_QUANT
            row.append("r=%.2f 桶 %d→%d %s" % (r, b_orig, b_new,
                                               "同" if b_orig == b_new else "变"))
        print("  dt=%3d帧(%5.0fms): %s" % (dt, 1000.0 * dt * fp.HOP_SIZE / SR, " | ".join(row)))

    # ---- 频率量化在「关 keylock」下的漂移 ----
    print("\n[频率哈希桶漂移分析] FREQ_QUANT=%d（关 keylock 时音高×r → bin×r）"
          % fp.FREQ_QUANT)
    for fbin in (100, 400, 900, 1800):
        row = []
        for r in (1.05, 1.10):
            b_orig = fbin // fp.FREQ_QUANT
            b_new = int(fbin * r) // fp.FREQ_QUANT
            row.append("r=%.2f %d→%d%s" % (r, b_orig, b_new,
                                           "同" if b_orig == b_new else "变"))
        print("  f_bin=%4d: %s" % (fbin, " | ".join(row)))


if __name__ == "__main__":
    main()
