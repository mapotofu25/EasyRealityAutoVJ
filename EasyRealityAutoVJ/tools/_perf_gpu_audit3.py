# -*- coding: utf-8 -*-
"""三次取样：确认"跨步拷贝"是主因，并量出替代写法 / DXV LZ 解包 / GL 解码现状。"""
import glob
import os
import statistics
import sys
import time

import numpy as np
import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.chdir(ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage, QGuiApplication  # noqa: E402

app = QGuiApplication.instance() or QGuiApplication([])


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


W, H = 1920, 1080
rng = np.random.RandomState(2)
raw = (rng.rand(H, W, 4) * 255).astype(np.uint8)
canvas = QImage(raw.data, W, H, 4 * W, QImage.Format_ARGB32).copy()
view4 = np.frombuffer(canvas.bits(), np.uint8, count=canvas.bytesPerLine() * H).reshape(H, W, 4)
scr = np.empty((H, W, 4), np.uint8)

print("--- A. 4 通道跨步视图能否直接喂 cv2 ---")
bench("np.copyto(scr, view4) 1080p", lambda: np.copyto(scr, view4), n=30, freq=60)
bench("view4.copy() 1080p", lambda: view4.copy(), n=30, freq=60)
bench("cv2.cvtColor(view4, BGRA2BGR)", lambda: cv2.cvtColor(view4, cv2.COLOR_BGRA2BGR),
      n=30, freq=60)
lut1 = np.arange(256, dtype=np.uint8)
lut4 = np.tile(lut1.reshape(1, 256, 1), (1, 1, 4))
bench("cv2.LUT(view4, lut4) 4ch 原地", lambda: cv2.LUT(view4, lut4, dst=view4), n=30, freq=60)
bench("cv2.LUT(scr, lut4) 4ch 连续", lambda: cv2.LUT(scr, lut4), n=30, freq=60)
bench("cv2.GaussianBlur(view4,(0,0),4)", lambda: cv2.GaussianBlur(view4, (0, 0), 4), n=20, freq=60)
bench("cv2.addWeighted(scr, view4)", lambda: cv2.addWeighted(scr, 1.0, view4, 1.0, 0), n=20, freq=60)
bench("cv2.resize(view4 -> 1/4)", lambda: cv2.resize(view4, (W // 4, H // 4)), n=20, freq=60)
bench("cv2.resize(view4 -> 全幅，无缩放)", lambda: cv2.resize(view4, (W, H)), n=20, freq=60)

print("\n--- B. 2 倍 buffer 互转（把 4ch 当 3ch 存不下，这里只作对照） ---")
bgr = np.ascontiguousarray(view4[:, :, :3])
print("  ascontiguousarray 3ch：见上一脚本 ~9.8ms")

print("\n--- C. DXV：LZ 解包（native）+ 软解对照 ---")
try:
    import dxvnative
    print("  dxvnative.available =", dxvnative.available())
except Exception as e:  # noqa: BLE001
    print("  dxvnative 不可用：", e)
    dxvnative = None

LIB = r"Y:\EasyRealityAutoVJ\素材库"


def find_dxv(limit=6):
    out = []
    if not os.path.isdir(LIB):
        return out
    for dirpath, _dirs, files in os.walk(LIB):
        for f in files:
            if f.lower().endswith((".mov", ".avi")) and "dxv" not in f.lower():
                p = os.path.join(dirpath, f)
                try:
                    if os.path.getsize(p) > 1 << 20:
                        out.append(p)
                except OSError:
                    pass
            if len(out) >= 40:
                break
        if len(out) >= 40:
            break
    return out


if dxvnative is not None and dxvnative.available():
    try:
        import av
        cands = find_dxv()
        print("  候选文件 %d 个，逐个探测 DXV 帧头…" % len(cands))
        picked = 0
        for p in cands:
            if picked >= 2:
                break
            try:
                c = av.open(p, metadata_errors="replace")
            except Exception:
                continue
            st = c.streams.video[0]
            if st.codec_context.name != "dxv":
                c.close()
                continue
            w, h = int(st.width), int(st.height)
            if w % 4 or h % 4:
                c.close()
                continue
            pkt = None
            for pk in c.demux(st):
                if pk.size:
                    pkt = bytes(pk)
                    break
            c.close()
            hd = dxvnative.parse_header(pkt) if pkt else None
            if hd is None or int(hd[0]) not in (1, 5):
                print("  跳过（帧头不支持）：%s %s" % (os.path.basename(p), hd))
                continue
            fmt = int(hd[0])
            print("  命中 %s  %dx%d  DXT%d  %s"
                  % (os.path.basename(p), w, h, 5 if fmt == 5 else 1, os.path.dirname(p)[-40:]))
            bench("dxvnative.frame_blocks(LZ 解包) %dx%d" % (w, h),
                  lambda p=p: dxvnative.frame_blocks(p, w, h), n=20, freq=60)
            r = dxvnative.frame_blocks(p, w, h)
            if r:
                nbytes = len(r[1])
                print("    BC 块字节数 = %.2f MB/帧，LZ 后 %.2f MB"
                      % (nbytes / 1e6, nbytes / 0.55 / 1e6))
            picked += 1
    except Exception as e:  # noqa: BLE001
        print("  DXV 取样失败：%s: %s" % (type(e).__name__, e))
else:
    print("  （跳过：dxvnative 不可用）")

print("\n--- D. GL 解码现状（回读口径，单次） ---")
try:
    import glctx
    ok = glctx.preinit()
    print("  glctx.preinit =", ok)
    if ok:
        w = glctx.GLWorker.get()
        ready = w.ensure()
        print("  GLWorker.ensure =", ready, "err =", (w.last_error or "")[:60])
        if ready:
            bw, bh = 256, 144
            blocks = bytes(os.urandom(bw // 4 * (bh // 4) * 8))
            dek = glctx.GLWorker.get().decode
            bench("GLWorker.decode DXT1 256x144 -> 1920x1080", lambda: dek(
                blocks, bw, bh, 1, 1920, 1080, bgra=True), n=10, warm=2, freq=60)
            print("  stats =", w.stats())
except Exception as e:  # noqa: BLE001
    print("  GL 取样失败：%s: %s" % (type(e).__name__, e))
