"""DXV → GPU 解码 端到端验证

链路：DXV 帧负载 --(dxvnative: C 解 LZ)--> BC 压缩块 --(glctx: GPU 硬件解压+缩放)-->
      RGBA --(与 FFmpeg 软解的 RGBA 逐像素对比)-->

⚠ 测量注意：Windows 的 `time.process_time()` 只有 **15.6ms 时钟粒度**，
   单帧调用算出来会是 0.00 或 15.62 这种假读数。**必须整批累加再除以帧数**
   （第一版就踩了这个坑，把 GPU 报成 15.62ms/帧）。

用法：venv\\Scripts\\python.exe tools\\dxv_gpu_verify.py
"""

import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

OUT = []
LIB = r"Y:\EasyRealityAutoVJ\素材库"


def w(s=""):
    print(s, flush=True)
    OUT.append(s)


def save():
    with open(os.path.join(ROOT, "_dxv_gpu_verify.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(OUT))


def pick():
    import av
    import dxvnative
    small, big = [], []
    for f in sorted(os.listdir(LIB)):
        if not f.lower().endswith(".mov"):
            continue
        p = os.path.join(LIB, f)
        try:
            with open(p, "rb") as fh:
                c = av.open(fh, metadata_errors="replace")
                s = c.streams.video[0]
                if s.codec_context.name != "dxv":
                    c.close()
                    continue
                ww, hh = s.width, s.height
                pk = next(c.demux(s))
                c.close()
        except Exception:
            continue
        if dxvnative.parse_header(bytes(pk)) is None:
            continue                      # DXV2/LZF 等暂不支持的变体
        if 1900 * 1000 < ww * hh < 2100 * 1000 and not small:
            small.append((p, ww, hh))
        elif ww * hh > 3000 * 1600 and len(big) < 2:
            big.append((p, ww, hh))
    return small + big


def main():
    import numpy as np
    import av
    import dxvnative
    from glctx import GLDecoder

    w("=" * 86)
    w("DXV → GPU 解码 端到端验证   %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    w("=" * 86)
    dec = GLDecoder()
    w("dxvnative 可用 %s    GLDecoder 可用 %s" % (dxvnative.available(), dec.ok))
    if not (dxvnative.available() and dec.ok):
        w("★ 前置条件不满足，终止")
        save()
        return

    N = 30
    for path, vw, vh in pick():
        name = os.path.basename(path)
        c1 = av.open(path, metadata_errors="replace")
        s1 = c1.streams.video[0]
        c2 = av.open(path, metadata_errors="replace")
        s2 = c2.streams.video[0]
        pk = c1.demux(s1)
        it = c2.decode(s2)
        payloads, refs = [], []
        for _ in range(N + 2):
            try:
                pkt = next(pk)
            except StopIteration:
                break
            if pkt.size == 0:
                continue
            try:
                fr = next(it)
            except StopIteration:
                break
            payloads.append(bytes(pkt))
            refs.append(fr.to_ndarray(format="rgba"))
            if len(payloads) >= N:
                break
        c1.close()
        c2.close()
        if not payloads:
            continue

        w("")
        w("-" * 86)
        w("素材 %s   %dx%d   %d 帧" % (name, vw, vh, len(payloads)))

        # ---------- LZ 解包（C）----------
        t0 = time.perf_counter()
        p0 = time.process_time()
        blocks = []
        for pl in payloads:
            r = dxvnative.frame_blocks(pl, vw, vh)
            if r:
                blocks.append(r)
        lz_wall = (time.perf_counter() - t0) / max(1, len(payloads)) * 1000
        lz_cpu = (time.process_time() - p0) / max(1, len(payloads)) * 1000
        w("  LZ 解包       成功 %d/%d   格式 BC%d   墙钟 %.2f ms/帧   CPU %.2f ms/帧"
          % (len(blocks), len(payloads), 1 if blocks and blocks[0][0] == 1 else 3, lz_wall, lz_cpu))
        if not blocks:
            continue

        # ---------- GPU 解码：两种目标尺寸 ----------
        targets = [(vw, vh, "原尺寸"), (1920, 1080, "缩到 1080p")]
        if vw <= 1920:
            targets = [(1920, 1080, "原尺寸 1080p")]
        for tw, th, tag in targets:
            t0 = time.perf_counter()
            p0 = time.process_time()
            img = None
            for fmt, blk in blocks:
                img = dec.decode(blk, vw, vh, fmt, tw, th)
                if img is None:
                    break
            wall = (time.perf_counter() - t0) / len(blocks) * 1000
            cpu = (time.process_time() - p0) / len(blocks) * 1000
            if img is None:
                w("  GPU 解码      ★ 失败")
                continue
            # 像素比对（用原尺寸那一次比）
            if (tw, th) == (vw, vh):
                diffs = []
                for i, (fmt, blk) in enumerate(blocks):
                    g = dec.decode(blk, vw, vh, fmt, vw, vh)
                    r = refs[i]
                    d = np.abs(r[:g.shape[0], :g.shape[1]].astype(np.int16) - g.astype(np.int16))
                    diffs.append((d[..., 0].mean(), d[..., 1].mean(), d[..., 2].mean(),
                                  d[..., 3].mean(), int(d.max())))
                a = np.array(diffs)
                w("  与 FFmpeg 逐像素差：R %.2f  G %.2f  B %.2f  A %.2f  最大 %d   → %s"
                  % (a[:, 0].mean(), a[:, 1].mean(), a[:, 2].mean(), a[:, 3].mean(),
                     int(a[:, 4].max()),
                     "✅ 一致（肉眼无法分辨）" if a[:, :3].mean() < 1.0 else "★ 不一致"))
            w("  GPU 解码（%s %dx%d）  墙钟 %.2f ms/帧   CPU %.2f ms/帧   回读 %.1f MB"
              % (tag, tw, th, wall, cpu, tw * th * 4 / 1e6))

        # ---------- 对照：FFmpeg 软解 ----------
        c3 = av.open(path, metadata_errors="replace")
        s3 = c3.streams.video[0]
        s3.codec_context.thread_count = 4
        it3 = c3.decode(s3)
        next(it3)
        p0 = time.process_time()
        t0 = time.perf_counter()
        k = 0
        for fr in it3:
            fr.to_ndarray(format="rgba")
            k += 1
            if k >= N:
                break
        sw = (time.perf_counter() - t0) / max(1, k) * 1000
        sc = (time.process_time() - p0) / max(1, k) * 1000
        c3.close()
        w("  对照 FFmpeg 软解（4 线程）  墙钟 %.2f ms/帧   CPU %.2f ms/帧" % (sw, sc))

    dec.close()
    save()
    print("\n→ 已写入 _dxv_gpu_verify.txt")


if __name__ == "__main__":
    main()
