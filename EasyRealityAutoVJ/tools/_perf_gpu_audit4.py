# -*- coding: utf-8 -*-
"""四次取样：DXV 真实包 LZ 解包、GL 解码口径、BeatThis ONNX 推理、整首歌分析。"""
import os
import statistics
import sys
import time

import numpy as np
import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.chdir(ROOT)


def bench(name, fn, n=30, warm=3, freq=None):
    for _ in range(warm):
        fn()
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000)
    med = statistics.median(ts)
    line = "%-52s %8.3f ms  p90 %8.3f" % (name, med, sorted(ts)[int(n * 0.9) - 1])
    if freq:
        line += "  %5.2f 核@%dfps" % (med * freq / 1000, freq)
    print(line, flush=True)


print("=== DXV 逐帧 LZ 解包（生产同路径：demux 包 → frame_blocks） ===")
import dxvnative  # noqa: E402
import av  # noqa: E402

p = r"Y:\EasyRealityAutoVJ\素材库\0 (6).mov"
if os.path.exists(p):
    c = av.open(p, metadata_errors="replace")
    st = c.streams.video[0]
    w, h = int(st.width), int(st.height)
    pkts = []
    for pk in c.demux(st):
        if pk.size and len(pkts) < 200:
            pkts.append(bytes(pk))
    c.close()
    print("  %s  %dx%d  取 %d 包" % (os.path.basename(p), w, h, len(pkts)))
    i = {"i": 0}

    def one():
        i["i"] = (i["i"] + 1) % len(pkts)
        return dxvnative.frame_blocks(pkts[i["i"]], w, h)

    bench("dxvnative.frame_blocks 1080p DXT1", one, n=60, freq=60)
    r = one()
    print("  BC 块 = %.2f MB/帧，fmt=%s" % (len(r[1]) / 1e6, r[0]))
    bench("  bytes()+from_buffer_copy 额外拷贝", lambda: bytes(pkts[0]), n=60, freq=60)
else:
    print("  找不到素材，跳过")

print("\n=== GL 解码口径（含 worker 内部 arr.copy） ===")
import glctx  # noqa: E402

if glctx.preinit():
    wk = glctx.GLWorker.get()
    if wk.ensure():
        bw, bh = 512, 288
        blocks = bytes(os.urandom(bw // 4 * (bh // 4) * 8))
        fn = wk.decode
        bench("GLWorker.decode DXT1 -> 1920x1080 BGRA", lambda: fn(
            blocks, bw, bh, 1, 1920, 1080, bgra=True), n=30, warm=3, freq=60)
        dec = wk._dec
        print("  最后一次回读 = %.3f ms，stats=%s"
              % (dec.last_readback * 1000, wk.stats()))
        bench("对照：同尺寸 arr.copy()", lambda: np.empty((1080, 1920, 4), np.uint8).copy(),
              n=30, freq=60)
    else:
        print("  worker 未就绪：", wk.last_error)
else:
    print("  glfw 不可用")

print("\n=== BeatThis (ONNX) 推理：低频（每首导入一次） ===")
import phrase_bt as PB  # noqa: E402
import soundfile as sf  # noqa: E402

flac = os.path.join(ROOT, "_beat_audio", "Rolling Contact - Dust.flac")
if os.path.exists(flac):
    x, sr = sf.read(flac, dtype="float32", always_2d=True)
    mono = x.mean(axis=1)
    print("  音频 %.1fs @%d" % (len(mono) / sr, sr))
    bench("phrase_bt.analyze(60s 窗口)", lambda: PB.analyze(mono, sr, maxsec=60.0), n=3, warm=1)
    bench("  logmel(60s)", lambda: PB.logmel(PB._resample_to_22k(mono[:int(60 * sr)], sr)),
          n=3, warm=1)
    import beatgrid as BG  # noqa: E402
    m22 = BG.resample(mono, sr)[0]
    bench("beatgrid.features(180s @22k)", lambda: BG.features(m22[:int(180 * 22050)], 22050), n=3)
    bench("beatgrid.analyze_signal(180s)", lambda: BG.analyze_signal(m22[:int(180 * 22050)], 22050),
          n=2, warm=1)
else:
    print("  找不到测试音频")

print("\n=== 音频指纹「整首建库」成本（每次导入/扫库） ===")
import fp as FP  # noqa: E402
if os.path.exists(flac):
    seg = cv2.resize(mono[:int(30 * sr)].reshape(-1, 1), (1, int(30 * 44100)),
                     interpolation=cv2.INTER_AREA).ravel().astype(np.float32)
    bench("fingerprint_audio(30s@44.1k)", lambda: FP.fingerprint_audio(seg, 44100), n=3, warm=1)
