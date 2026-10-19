# -*- coding: utf-8 -*-
"""GLWorker 吞吐实测：单线程串行解码在「4 图层并发」下是否仍够 30fps。

思路：所有图层共用**唯一** GL 工作线程串行解码。测：
  - 单次 decode 的稳态耗时（预热后）；
  - 4 个线程同时压 worker 时，总吞吐与「每路出一帧」的间隔。

用法：venv\\Scripts\\python.exe tools\\_gl_worker_throughput.py [图层数=4] [每路帧数=60]
"""
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

LIB = r"Y:\EasyRealityAutoVJ\素材库"
OUT = []


def w(s=""):
    print(s, flush=True)
    OUT.append(str(s))


def dxv_candidates():
    import av
    out = []
    for f in sorted(os.listdir(LIB)):
        if not f.lower().endswith((".mov", ".mp4", ".mkv")):
            continue
        p = os.path.join(LIB, f)
        try:
            c = av.open(p, metadata_errors="replace")
            st = c.streams.video[0]
            ok = st.codec_context.name == "dxv" and st.width * st.height > 1.9e6
            c.close()
            if ok:
                out.append(p)
        except Exception:
            continue
    return out


def main():
    layers = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    per = int(sys.argv[2]) if len(sys.argv) > 2 else 60

    import av
    import dxvnative
    import glctx

    w("=" * 90)
    w("GLWorker 吞吐实测（唯一上下文串行解码）   %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    w("=" * 90)

    if not glctx.preinit():
        w("★ glfw 不可用，跳过")
        return
    worker = glctx.GLWorker.get()
    if not worker.ensure():
        w("★ GL 工作线程不可用：%s" % worker.last_error)
        return

    path = None
    blocks = []
    sw = sh = 0
    for cand in dxv_candidates():
        try:
            c = av.open(cand, metadata_errors="replace")
            st = c.streams.video[0]
            cw, ch = int(st.width), int(st.height)
            got = []
            for p in c.demux(st):
                if not p.size:
                    continue
                r = dxvnative.frame_blocks(bytes(p), cw, ch)
                if r is None:
                    continue
                got.append((int(r[0]), r[1]))
                if len(got) >= 8:
                    break
            c.close()
        except Exception:
            continue
        if got:
            path, blocks, sw, sh = cand, got, cw, ch
            break
    if not path:
        w("★ 没找到能解析出 DXV 块的素材")
        return
    w("素材：%s" % os.path.basename(path))
    dw, dh = 1280, 720
    w("源 %dx%d ，目标 %dx%d，样本块 %d 个" % (sw, sh, dw, dh, len(blocks)))

    # 预热
    for _ in range(15):
        worker.decode(blocks[0][1], sw, sh, blocks[0][0], dw, dh, bgra=True)

    # 单线程
    N = 150
    t0 = time.perf_counter()
    for i in range(N):
        fmt, blk = blocks[i % len(blocks)]
        if worker.decode(blk, sw, sh, fmt, dw, dh, bgra=True) is None:
            w("★ 解码失败（worker=%s）" % worker.stats())
            return
    single_ms = (time.perf_counter() - t0) / N * 1000

    # layers 路并发
    lat = []
    latch = threading.Lock()

    def one_layer(k):
        for i in range(per):
            fmt, blk = blocks[(i + k) % len(blocks)]
            t = time.perf_counter()
            worker.decode(blk, sw, sh, fmt, dw, dh, bgra=True)
            with latch:
                lat.append(time.perf_counter() - t)

    ths = [threading.Thread(target=one_layer, args=(k,)) for k in range(layers)]
    t0 = time.perf_counter()
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    wall = time.perf_counter() - t0
    total = layers * per

    w("")
    w("单线程稳态：        %.3f ms/次解码  → 串行上限 %.0f 帧/s" % (single_ms, 1000 / single_ms))
    w("%d 路并发合计：      %d 次解码 / %.3f s  → %.0f 帧/s（全局）"
      % (layers, total, wall, total / wall))
    w("  平均每次解码间隔： %.3f ms（排队等待 + 解码）" % (wall / total * 1000))
    w("  每路出一帧间隔：   %.3f ms  → 每路最高 %.1f fps"
      % (wall / per * 1000, per / wall))
    w("  单次解码最大耗时： %.3f ms" % (max(lat) * 1000))
    w("  30fps 预算：每路 %.1f fps %s"
      % (per / wall, "✓ 够用" if (per / wall) >= 30 else "★ 不足"))
    w("  worker=%s" % worker.stats())

    with open(os.path.join(ROOT, "_gl_worker_throughput.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    w("→ 已写入 _gl_worker_throughput.txt")


if __name__ == "__main__":
    main()
