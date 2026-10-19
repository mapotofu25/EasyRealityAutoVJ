"""DXV 解码路径规范基准（CPU 精确到 0.1ms）

⚠ 关键测量坑：Windows 的 `time.process_time()` 底层是 `GetProcessTimes`，
   **值只在时钟中断（15.625ms）时更新** → 单帧调用会得到 0.00 / 15.62 这类假读数。
   所以这里一律「多轮 × 多帧累加后再除」，让分辨率降到 0.1ms 级。

分项对照（同一批帧）：
  A. 只跑 C 的 LZ 解包
  B. 只跑 GPU（BC 纹理 → 解压 → 缩放 → 回读），两种目标尺寸
  C. FFmpeg 软解，1 / 2 / 4 / 8 线程

用法：venv\\Scripts\\python.exe tools\\dxv_gpu_bench.py
"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

OUT = []
LIB = r"Y:\EasyRealityAutoVJ\素材库"
N = 60          # 每轮帧数
ROUNDS = 3      # 轮数


def w(s=""):
    print(s, flush=True)
    OUT.append(s)


def save():
    with open(os.path.join(ROOT, "_dxv_gpu_bench.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(OUT))


def pick():
    """各挑一个：DXT5-1080p / DXT1-1080p / DXT5-4K / DXT1-4K"""
    import av
    import dxvnative
    want = {}
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
            info = dxvnative.parse_header(bytes(pk))
        except Exception:
            continue
        if not info:
            continue
        fmt = info[0]
        big = ww * hh > 3000 * 1600
        if fmt == 5 and not big:
            key = "DXT5-1080p"
        elif fmt == 1 and not big:
            key = "DXT1-1080p"
        elif fmt == 5 and big:
            key = "DXT5-大尺寸"
        elif fmt == 1 and big:
            key = "DXT1-大尺寸"
        else:
            continue
        want.setdefault(key, (p, ww, hh, fmt))
    return want


def main():
    import av
    import dxvnative
    from glctx import GLDecoder

    w("=" * 90)
    w("DXV 解码路径规范基准   %s   每项 %d 帧 × %d 轮" % (time.strftime("%H:%M:%S"), N, ROUNDS))
    w("=" * 90)
    dec = GLDecoder()
    if not (dxvnative.available() and dec.ok):
        w("★ 前置条件不满足：dxvnative=%s GL=%s" % (dxvnative.available(), dec.ok))
        save()
        return
    clips = pick()
    if not clips:
        w("找不到可用素材")
        save()
        return
    w("素材：")
    for k, (p, ww, hh, fmt) in clips.items():
        w("   %-12s %-42s %dx%d  BC%d" % (k, os.path.basename(p)[:42], ww, hh,
                                          1 if fmt == 1 else 3))
    w("")
    w("%-12s %-16s %10s %10s" % ("素材", "项目", "CPU ms/帧", "墙钟 ms/帧"))
    w("-" * 90)

    res = {}
    for key, (path, vw, vh, fmt) in clips.items():
        # 取 N 帧负载
        c1 = av.open(path, metadata_errors="replace")
        s1 = c1.streams.video[0]
        pk = c1.demux(s1)
        payloads = []
        for _ in range(N):
            try:
                pkt = next(pk)
            except StopIteration:
                break
            if pkt.size == 0:
                continue
            payloads.append(bytes(pkt))
        c1.close()
        if not payloads:
            continue
        n = len(payloads)

        # ---- A. LZ ----
        best = None
        for _ in range(ROUNDS):
            p0 = time.process_time(); t0 = time.perf_counter()
            for pl in payloads:
                dxvnative.frame_blocks(pl, vw, vh)
            cpu = (time.process_time() - p0) / n * 1000
            wall = (time.perf_counter() - t0) / n * 1000
            best = (cpu, wall) if best is None or cpu < best[0] else best
        w("%-12s %-16s %10.2f %10.2f" % (key, "A LZ 解包(C)", best[0], best[1]))
        res[(key, "LZ")] = best[0]

        blocks = [dxvnative.frame_blocks(pl, vw, vh) for pl in payloads]
        blocks = [b for b in blocks if b]

        # ---- B. GPU ----
        for tw, th, tag in ((vw, vh, "B GPU→原尺寸"), (1920, 1080, "B GPU→1080p")):
            if (tw, th) == (vw, vh) and tw <= 1920:
                pass
            best = None
            for _ in range(ROUNDS):
                p0 = time.process_time(); t0 = time.perf_counter()
                for f_, blk in blocks:
                    dec.decode(blk, vw, vh, f_, tw, th)
                cpu = (time.process_time() - p0) / len(blocks) * 1000
                wall = (time.perf_counter() - t0) / len(blocks) * 1000
                best = (cpu, wall) if best is None or cpu < best[0] else best
            w("%-12s %-16s %10.2f %10.2f" % (key, tag, best[0], best[1]))
            res[(key, tag.split()[1])] = best[0]

        # ---- C. FFmpeg 软解 ----
        for th in (1, 2, 4, 8):
            best = None
            for _ in range(ROUNDS):
                c3 = av.open(path, metadata_errors="replace")
                s3 = c3.streams.video[0]
                try:
                    s3.codec_context.thread_count = th
                except Exception:
                    pass
                it3 = c3.decode(s3)
                next(it3)
                p0 = time.process_time(); t0 = time.perf_counter()
                k = 0
                for fr in it3:
                    fr.to_ndarray(format="rgba")
                    k += 1
                    if k >= n:
                        break
                cpu = (time.process_time() - p0) / max(1, k) * 1000
                wall = (time.perf_counter() - t0) / max(1, k) * 1000
                c3.close()
                best = (cpu, wall) if best is None or cpu < best[0] else best
            w("%-12s %-16s %10.2f %10.2f" % (key, "C 软解 %d 线程" % th, best[0], best[1]))
            res[(key, "soft%d" % th)] = best[0]
        w("")

    w("=" * 90)
    w("小结（CPU ms/帧）")
    w("=" * 90)
    for key in clips:
        g = res.get((key, "GPU→1080p"), res.get((key, "GPU→原尺寸")))
        if g is None:
            continue
        lz = res.get((key, "LZ"), 0)
        s1 = res.get((key, "soft1"))
        s4 = res.get((key, "soft4"))
        tot = lz + g
        w("  %-12s GPU 路径合计 %.2f（LZ %.2f + GPU %.2f）   软解1线程 %.2f  软解4线程 %.2f"
          % (key, tot, lz, g, s1 or -1, s4 or -1))
    save()
    print("\n→ 已写入 _dxv_gpu_bench.txt")


if __name__ == "__main__":
    main()
