# -*- coding: utf-8 -*-
"""音色分段（独立于能量分段）。

设计（2026-11-16，用户：「分开来嘛 用音色分段 能量还是我们之前的算法」）：
  · 这里**只负责找边界**：用 log-mel → MFCC(13) 的「左右各 4 秒音色差」曲线，
    取显著峰值、按 8 拍线吸附，得到段落边界。
  · 能量、档位（drop/中段/过渡）仍由 `sections.segments_from_edges()` 用老口径算，
    两边互不影响，便于 A/B。
  · 只在 `ERLIB_SEG_MODE=timbre` 时由扫描器调用；默认仍是老的能量分段。
"""
import os

import numpy as np

import phrase_bt as pb

MAXSEC = 180.0
N_MFCC = 13
FPS = pb.SR / float(pb.HOP) / 8.0      # log-mel 50 fps，8 帧平均 ⇒ 6.25 fps
WIN_SEC = 4.0

# 峰值门槛：z 值（全曲标准化后），可用环境变量覆盖
THRESH = float(os.environ.get("ERLIB_TIMBRE_THRESH", "1.40"))
MIN_BEATS = 16.0                       # 段最短 2 个八拍
MAX_BEATS = 64.0                       # 与「段长 ≤64 拍」硬规则一致
EDGE_GUARD = 1.0                       # 曲首/曲尾 1 秒内不取边界


def _mfcc(mono, sr):
    x = pb._resample_to_22k(mono[: int(MAXSEC * sr)], sr)
    lm = pb.logmel(x)
    n = (lm.shape[0] // 8) * 8
    if n < 8 * 8:
        return None
    lm = lm[:n].reshape(n // 8, 8, lm.shape[1]).mean(axis=1)
    L = lm.shape[1]
    k = np.arange(1, N_MFCC + 1)[:, None]
    m = np.arange(L)[None, :]
    dct = np.cos(np.pi * k * (2 * m + 1) / (2.0 * L)) * np.sqrt(2.0 / L)
    mf = lm @ dct.T
    return (mf - mf.mean(axis=0)) / (mf.std(axis=0) + 1e-8)


def novelty(mono, sr):
    """返回 (z, fps)；z 是「音色变化强度」的全曲标准化曲线。"""
    F = _mfcc(mono, sr)
    if F is None:
        return None, FPS
    T = F.shape[0]
    win = max(4, int(round(WIN_SEC * FPS)))
    cs = np.vstack([np.zeros(F.shape[1]), F.cumsum(0)])
    idx = np.arange(T)
    a = np.clip(idx - win, 0, T)
    c = np.clip(idx + win, 0, T)
    left = (cs[idx] - cs[a]) / np.maximum(1, (idx - a))[:, None]
    right = (cs[c] - cs[idx]) / np.maximum(1, (c - idx))[:, None]
    ln = np.linalg.norm(left, axis=1) + 1e-8
    rn = np.linalg.norm(right, axis=1) + 1e-8
    d = 1.0 - (left * right).sum(1) / (ln * rn)
    z = (d - d.mean()) / (d.std() + 1e-8)
    return z, FPS


def edges(mono, sr, grid, dur=0.0):
    """音色边界（秒），已吸附到八拍线并满足 16~64 拍段长。"""
    z, fps = novelty(mono, sr)
    if z is None or z.size < 8:
        return None
    ga = float((grid or {}).get("anchor") or 0.0)
    gbl = float((grid or {}).get("beat_len") or 0.0)
    if gbl <= 1e-6:
        return None
    dur_eff = float(dur) if dur and dur > 0 else z.size / fps

    # 1) 平滑 + 局部极大（间隔 ≥ MIN_BEATS 拍）
    k = max(1, int(round(0.4 * fps)))
    ker = np.ones(k) / k
    zs = np.convolve(z, ker, mode="same")
    min_gap = max(1, int(round(MIN_BEATS * gbl * fps)))
    peaks = []
    for i in range(1, zs.size - 1):
        if zs[i] < THRESH:
            continue
        if zs[i] < zs[i - 1] or zs[i] < zs[i + 1]:
            continue
        t = i / fps
        if t < EDGE_GUARD or t > dur_eff - EDGE_GUARD:
            continue
        if peaks and (t - peaks[-1]) < min_gap / fps:
            if zs[i] > zs[int(round(peaks[-1] * fps))]:
                peaks[-1] = t
            continue
        peaks.append(t)

    # 2) 吸附到八拍线
    b8 = 8.0 * gbl
    snapped = []
    for t in peaks:
        s = ga + round((t - ga) / b8) * b8
        if s <= EDGE_GUARD or s >= dur_eff - EDGE_GUARD:
            continue
        if snapped and s - snapped[-1] < MIN_BEATS * gbl - 1e-6:
            continue
        snapped.append(float(s))

    out = [0.0] + snapped + [dur_eff]
    # 3) 段长上限：> MAX_BEATS 的段在段内最强音色峰处再切（递归一次即可）
    res = [out[0]]
    for t1 in out[1:]:
        t0 = res[-1]
        while (t1 - t0) > MAX_BEATS * gbl + 1e-6:
            lo = int(max(0, (t0 + MIN_BEATS * gbl) * fps))
            hi = int(min(zs.size - 1, (t0 + (MAX_BEATS - MIN_BEATS) * gbl) * fps))
            if hi <= lo:
                break
            j = lo + int(np.argmax(zs[lo:hi + 1]))
            c = ga + round(((j / fps) - ga) / b8) * b8
            if not (t0 + 1e-3 < c < t1 - 1e-3):
                break
            res.append(float(c))
            t0 = float(c)
        res.append(float(t1))
    return res
