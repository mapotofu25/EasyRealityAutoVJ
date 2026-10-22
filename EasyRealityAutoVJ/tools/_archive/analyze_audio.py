# -*- coding: utf-8 -*-
"""离线音频 BPM/能量分析工具（测试用）
用法: python tools/analyze_audio.py <音频文件> [时长秒]
用与软件内相同的 BPM 检测算法分析音乐文件（flac/mp3/wav），验证节拍检测效果。
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import numpy as np
import soundfile as sf


def analyze(path, max_sec=60):
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    if data.shape[0] > sr * max_sec:
        data = data[: sr * max_sec]
    sig = np.mean(data, axis=1)
    block = 1024
    n_blocks = len(sig) // block
    flux = np.zeros(n_blocks, dtype=np.float32)
    rms = np.zeros(n_blocks, dtype=np.float32)
    prev = None
    win = np.hanning(2048)
    for i in range(n_blocks):
        chunk = sig[i * block:(i + 1) * block]
        rms[i] = np.sqrt(np.mean(chunk ** 2))
        buf = np.zeros(2048, dtype=np.float32)
        buf[:block] = chunk
        spec = np.abs(np.fft.rfft(buf * win))
        if prev is not None:
            flux[i] = float(np.sum(np.maximum(spec - prev, 0)))
        prev = spec

    # 自相关 BPM
    h = flux - flux.mean()
    ac = np.correlate(h, h, "full")[len(h) - 1:]
    fps = sr / block
    lo, hi = int(60 / 180 * fps), min(int(60 / 60 * fps), len(ac) - 1)
    seg = ac[lo:hi]
    lag = int(np.argmax(seg)) + lo
    bpm = 60.0 / (lag / fps)
    while bpm < 70:
        bpm *= 2
    while bpm > 180:
        bpm /= 2

    print(f"文件: {os.path.basename(path)}")
    print(f"时长: {len(sig)/sr:.1f}s  采样率: {sr}")
    print(f"估计 BPM: {bpm:.1f}")
    print(f"平均电平 RMS: {rms.mean():.4f}  峰值: {rms.max():.4f}")
    e = np.sort(flux)
    print(f"能量分布: 低10%={e[:len(e)//10].mean():.0f}  中位={np.median(e):.0f}  高10%={e[-len(e)//10:].mean():.0f}")


if __name__ == "__main__":
    analyze(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 60)
