# -*- coding: utf-8 -*-
"""生成"音乐变速"测试素材（SOLA 时间伸缩，**保持音高** = DJ 软件 keylock 语义）。

为什么不用 ffmpeg atempo：本机 ffmpeg 是 `--disable-filters` 的纯音频裁剪版，
**没有 atempo**（只有 aformat/anull/atrim/format/setpts/trim/aresample）。
所以这里用 numpy 自己实现 SOLA（波形相似叠加）：

  · 帧长 N=2048 / 合成跳 Hs=1024（Hann 50% 叠加，和恒为 1）
  · 分析跳 Ha = Hs × rate（rate>1 ⇒ 压缩时间 = 加速）
  · 每帧在 ±D 内按**互相关**挑最相似的落点，避免相位跳变

用法：
  python tools/_mk_tempo.py                       # 默认揽月 180s 起 190s
  python tools/_mk_tempo.py --src x.flac --start 180 --dur 190 --rates 1.1 0.9
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tools", "_tempo")
DEFAULT_SRC = r"Y:/EasyRealityAutoVJ/码表素材/揽月 - Dreamira.flac"


def sola(x, rate, N=2048, D=384):
    """x: (n, ch) float32 → 长度 ≈ n/rate 的同音高变速信号。"""
    Hs = N // 2
    Ha = Hs * rate
    win = np.hanning(N + 1)[:N].astype(np.float32)     # 周期 Hann，50% 叠加和 = 1
    mon = x.mean(axis=1) if x.ndim > 1 else x           # 互相关用单声道
    n_frames = int(np.ceil((len(x) - N) / Ha)) + 1
    out = np.zeros((n_frames * Hs + N, x.shape[1] if x.ndim > 1 else 1),
                   dtype=np.float32)
    for k in range(n_frames):
        base = int(round(k * Ha))
        if base + N > len(x):
            break
        if k > 0:
            o = k * Hs
            ref = out[o:o + Hs].mean(axis=1)
            lo = max(0, base - D)
            hi = min(len(x), base + D + Hs)
            seg = mon[lo:hi]
            if len(seg) >= Hs and float(ref.dot(ref)) > 1e-6:
                corr = np.correlate(seg, ref, mode="valid")   # 每个落点的相似度
                base = lo + int(np.argmax(corr))
        o = k * Hs
        out[o:o + N] += x[base:base + N] * win[:, None]
    n_out = int(round(len(x) / rate))
    return np.ascontiguousarray(out[:n_out])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--start", type=float, default=180.0)
    ap.add_argument("--dur", type=float, default=190.0)
    ap.add_argument("--rates", type=float, nargs="+", default=[1.1, 0.9])
    a = ap.parse_args()

    import soundfile as sf
    os.makedirs(OUT, exist_ok=True)
    info = sf.info(a.src)
    print("源：%s  sr=%d ch=%d 时长=%.1fs" % (os.path.basename(a.src), info.samplerate,
                                            info.channels, info.duration))
    i0 = int(a.start * info.samplerate)
    n = int(a.dur * info.samplerate)
    with sf.SoundFile(a.src) as f:
        f.seek(i0)
        seg = f.read(n, dtype="float32", always_2d=True)
    print("取 [%.0fs, %.0fs) = %d 样本" % (a.start, a.start + a.dur, len(seg)))

    for r in a.rates:
        y = sola(seg, r)
        p = os.path.join(OUT, "lanyue_%.2f.wav" % r)
        sf.write(p, y, info.samplerate, subtype="PCM_16")
        print("  rate=%.2f → %s  %.1fs（原始 %.1fs，应≈%.1fs）"
              % (r, os.path.basename(p), len(y) / info.samplerate,
                 len(seg) / info.samplerate, len(seg) / info.samplerate / r))
    print("完成 →", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
