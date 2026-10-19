# -*- coding: utf-8 -*-
"""NDI 音频「持续嘶嘶杂音」根因取证。

目标：抓 WASAPI loopback 原始 PCM，回答三个问题：
  1. 采集侧实际交付速率 vs 48k（是否有 ~1% 亏空）
  2. 每个块内部是否有**不连续**（跳变）——这是"嘶嘶"最直接的证据
  3. 块与块之间是否有重复/跳帧（deque 丢弃 / 时间戳错位）

⚠ 本脚本**只读**，不改任何配置；跑完自行退出。
用法：venv\\Scripts\\python.exe tools\\_ndi_audio_probe.py [秒数]
"""
import sys
import time

import numpy as np

SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0


def main():
    import soundcard as sc

    mics = sc.all_microphones(include_loopback=True)
    loop = [m for m in mics if m.isloopback]
    if not loop:
        print("找不到 loopback 设备")
        return 2
    # 与 config.json 一致：扬声器 (2- Realtek High Definition Audio)
    mic = None
    want = "Realtek"
    for m in loop:
        if want.lower() in m.name.lower():
            mic = m
            break
    if mic is None:
        mic = loop[0]
    print("设备: %s" % mic.name)

    # 原生采样率
    try:
        from audio_engine import _native_samplerate
        sr = _native_samplerate(mic) or 48000
    except Exception as e:                                     # noqa: BLE001
        print("取原生采样率失败：%s" % e)
        sr = 48000
    print("原生采样率: %d" % sr)

    BLOCK = 1024
    t0 = time.perf_counter()
    blocks = 0
    samples = 0
    # 统计
    disc_max = 0.0            # 块内相邻样本最大跳变
    disc_cnt = 0             # 跳变 > 0.5 的次数
    seam_max = 0.0            # 块边界跳变
    seam_hist = []
    rms_min, rms_max = 9.9, 0.0
    dup_cnt = 0              # 与上一块完全相同（重复）
    nan_cnt = 0
    prev_tail = None
    prev_block = None
    peaks = []

    with mic.recorder(samplerate=sr, channels=2) as rec:
        while time.perf_counter() - t0 < SECS:
            data = rec.record(numframes=BLOCK)
            if data is None or len(data) == 0:
                continue
            d = np.asarray(data, dtype=np.float32)
            if d.ndim == 1:
                d = np.column_stack([d, d])
            if not np.isfinite(d).all():
                nan_cnt += 1
                d = np.nan_to_num(d)
            blocks += 1
            samples += d.shape[0]
            # 块内跳变（左声道）
            x = d[:, 0]
            if x.size > 1:
                dj = np.abs(np.diff(x))
                m = float(dj.max())
                if m > disc_max:
                    disc_max = m
                disc_cnt += int((dj > 0.5).sum())
            r = float(np.sqrt(np.mean(x * x)))
            rms_min = min(rms_min, r)
            rms_max = max(rms_max, r)
            peaks.append(float(np.abs(x).max()))
            # 块边界
            if prev_tail is not None:
                s = abs(float(x[0]) - float(prev_tail))
                seam_hist.append(s)
                if s > seam_max:
                    seam_max = s
            prev_tail = float(x[-1])
            # 重复块检测
            if prev_block is not None and prev_block.shape == d.shape:
                if np.array_equal(prev_block, d):
                    dup_cnt += 1
            prev_block = d

    dt = time.perf_counter() - t0
    print("-" * 56)
    print("时长        : %.2f s" % dt)
    print("块数        : %d  (%.1f 块/s)" % (blocks, blocks / dt))
    print("样本数      : %d  (%.0f 样本/s，理想 %d)" % (samples, samples / dt, sr))
    print("速率比      : %.4f  (1.0 = 完全实时)" % ((samples / dt) / sr))
    print("亏空        : %+.2f %%   ← 负数=采集比实时慢" % (((samples / dt) / sr - 1.0) * 100))
    print("块内最大跳变: %.4f   (>0.5 次数 %d)" % (disc_max, disc_cnt))
    print("块边界跳变  : max=%.4f  均值=%.4f" % (seam_max, (sum(seam_hist) / len(seam_hist)) if seam_hist else 0))
    print("RMS 范围    : %.6f ~ %.6f" % (rms_min, rms_max))
    print("峰值范围    : %.4f ~ %.4f" % (min(peaks or [0]), max(peaks or [0])))
    print("重复块数    : %d" % dup_cnt)
    print("非有限块数  : %d" % nan_cnt)
    print("-" * 56)
    # 结论
    ratio = (samples / dt) / sr
    if abs(ratio - 1.0) > 0.003:
        print("★ 采集速率偏离实时 >0.3%%：%.2f%%  ← 时间轴漂移的主要来源" % ((ratio - 1) * 100))
    if disc_cnt > 0 or disc_max > 0.5:
        print("★ 块内存在跳变 >0.5：%d 次 —— 数据本身就有突变（嘶嘶的直接嫌疑）" % disc_cnt)
    if seam_max > 0.5:
        print("★ 块边界跳变 >0.5 —— 块拼接处不连续（爆音/嘶嘶嫌疑）")
    if dup_cnt > 0:
        print("★ 存在 %d 个完全重复块 —— 采集层在重发同一段（deque/驱动重复）" % dup_cnt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
