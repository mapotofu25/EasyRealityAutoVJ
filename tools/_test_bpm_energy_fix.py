# -*- coding: utf-8 -*-
r"""回归脚本：验证实时音频分析两个 bug 已修复（P0-8 / P0-9）。

① **BPM 减半**（P0-8）：合成 4-on-the-floor（kick 每拍 + offbeat bass）的
   128/160/180 BPM 信号喂进 `_Analyzer`，断言锁定 BPM 落在真值附近（不再出现
   64/80/90 的减半值）。修复前：kick 梳状优先分支**跳过**半速修正 → 160→80.4、
   180→72.1（不锁定）。修复后：半速修正对 kick/全频两分支都生效 → 158.5/177.5（锁定）。

② **Hardstyle/Hardcore Drop 能量偏低**（P0-9）：合成「密集低频 kick + offbeat bass、
   整体响度 -7.7 dBFS」的 Drop，与「持续中频、-14.9 dBFS」的钢琴 breakdown 对比，
   断言 Drop 能量 > breakdown 且 Drop 能量 ≥ 0.6（不再系统性偏低）。
   修复前：`abs_ev` 先 `20*log10(rms)` 再 EMA（对 dB 取平均），脉冲型 kick 的
   log 均值远低于均值的 log ⇒ abs_f≈0.36；修复后：先平滑**功率**再转 dB ⇒ abs_f≈0.77。

用法：venv\\Scripts\\python.exe -u tools/_test_bpm_energy_fix.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import numpy as np

import audio_engine as AE


def _synth_kick(sr, bpm, secs, offbeat=0.5, f0=55.0, noise=0.25):
    """合成 4-on-the-floor：kick 每拍 + offbeat bass(offbeat) + offbeat 高频噪声。"""
    n = int(sr * secs)
    sig = np.zeros(n, dtype=np.float32)
    beat = 60.0 / bpm
    klen = int(sr * 0.12)
    kt = np.arange(klen) / sr
    kick = (np.sin(2 * np.pi * f0 * kt) * np.exp(-kt * 40.0)).astype(np.float32)
    rng = np.random.default_rng(1)

    def place(pos_sec, amp):
        j = int(pos_sec * sr)
        e = min(n, j + klen)
        if e > j:
            sig[j:e] += amp * kick[:e - j]

    t = 0.0
    while t < secs:
        place(t, 1.0)
        if offbeat > 0:
            place(t + beat / 2, offbeat)
        j = int((t + beat / 2) * sr)
        e = min(n, j + int(sr * 0.02))
        if e > j and noise > 0:
            sig[j:e] += noise * rng.standard_normal(e - j).astype(np.float32)
        t += beat
    return sig / (np.abs(sig).max() + 1e-6) * 0.5


def _feed_all(an, st, sig, sr=48000, block=1024):
    for k in range(0, len(sig) - block + 1, block):
        an.feed(sig[k:k + block].reshape(-1, 1).astype(np.float32), st)


def _synth_drop(sr, secs, bpm=150.0):
    """Hardstyle drop：密集低频 kick + offbeat bass，整体 RMS 拉到 -7.7 dBFS。"""
    n = int(sr * secs)
    sig = np.zeros(n, dtype=np.float32)
    beat = 60.0 / bpm
    klen = int(sr * 0.15)
    kt = np.arange(klen) / sr
    kick = (np.sin(2 * np.pi * 50.0 * kt) * np.exp(-kt * 25.0)).astype(np.float32)
    bass = (np.sin(2 * np.pi * 55.0 * kt) * np.exp(-kt * 8.0)).astype(np.float32)
    rng = np.random.default_rng(7)

    def place(pos_sec, arr, amp):
        j = int(pos_sec * sr)
        e = min(n, j + klen)
        if e > j:
            sig[j:e] += amp * arr[:e - j]

    t = 0.0
    while t < secs:
        place(t, kick, 1.0)
        place(t + beat / 2, bass, 0.8)
        t += beat
    sig += 0.08 * rng.standard_normal(n).astype(np.float32)
    sig = sig / (np.abs(sig).max() + 1e-6)
    return _norm_rms_db(sig, -7.7)


def _synth_piano(sr, secs):
    """钢琴 breakdown：持续中频和弦，整体 RMS 拉到 -14.9 dBFS。"""
    n = int(sr * secs)
    t = np.arange(n) / sr
    sig = np.zeros(n, dtype=np.float32)
    for f in (262.0, 330.0, 392.0):
        sig += 0.15 * np.sin(2 * np.pi * f * t).astype(np.float32)
    sig += 0.05 * np.random.default_rng(7).standard_normal(n).astype(np.float32)
    sig = sig / (np.abs(sig).max() + 1e-6)
    return _norm_rms_db(sig, -14.9)


def _norm_rms_db(sig, db):
    target = 10.0 ** (db / 20.0)
    cur = float(np.sqrt(np.mean(np.asarray(sig, dtype=np.float64) ** 2))) + 1e-9
    return (sig * (target / cur)).astype(np.float32)


def _run_energy(kind, secs=12.0):
    sr = 48000
    an = AE._Analyzer(sr, block=1024)
    st = AE.AudioState()
    sig = _synth_drop(sr, secs) if kind == "drop" else _synth_piano(sr, secs)
    _feed_all(an, st, sig)
    return float(st.energy), float((st.dbg or {}).get("abs_f", 0.0))


def test_bpm():
    sr = 48000
    print("== ① BPM 减半回归 ==")
    for bpm in (128.0, 160.0, 180.0):
        an = AE._Analyzer(sr, block=1024)
        st = AE.AudioState()
        sig = _synth_kick(sr, bpm, 35.0)
        _feed_all(an, st, sig)
        got = float(an.bpm_smooth)
        assert an._bpm_lock, "BPM %d 未锁定（bpm_smooth=%.1f）" % (bpm, got)
        tol = max(5.0, bpm * 0.05)
        assert abs(got - bpm) <= tol, "BPM %d 被估成 %.1f（疑似减半，未修复）" % (bpm, got)
        print("  BPM %d → %.1f ✓（锁定）" % (bpm, got))
    # 慢歌不误升（修复不应把真慢歌翻倍）
    for bpm in (90.0, 100.0):
        an = AE._Analyzer(sr, block=1024)
        st = AE.AudioState()
        sig = _synth_kick(sr, bpm, 35.0, offbeat=0.0)
        _feed_all(an, st, sig)
        disp = float(an.bpm_display)
        assert abs(disp - bpm) <= max(5.0, bpm * 0.06), \
            "慢歌 BPM %d 被误估成 %.1f（误升）" % (bpm, disp)
        print("  慢歌 BPM %d → %.1f ✓（未误升）" % (bpm, disp))


def test_energy():
    print("== ② Hardstyle Drop 能量回归 ==")
    drop_e, drop_abs = _run_energy("drop")
    piano_e, piano_abs = _run_energy("piano")
    assert drop_abs > 0.6, "绝对响度兜底 abs_f=%.3f（修复前≈0.36，说明仍在 log 域平均）" % drop_abs
    assert drop_e > piano_e, "Drop 能量 %.3f 应高于钢琴 breakdown %.3f" % (drop_e, piano_e)
    assert drop_e >= 0.6, "Drop 能量 %.3f 系统性偏低（应 ≥0.6）" % drop_e
    assert piano_e < 0.5, "钢琴 breakdown 被误抬到 %.3f（不应抬高）" % piano_e
    print("  Drop 能量=%.3f（abs_f=%.3f） vs 钢琴=%.3f ✓" % (drop_e, drop_abs, piano_e))


def main():
    test_bpm()
    test_energy()
    print("\n[test_bpm_energy_fix] 全部通过 ✓")


if __name__ == "__main__":
    main()
