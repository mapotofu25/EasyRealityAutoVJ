"""GPU 解码并发/稳定性联调（模拟演出：多图层同时播放）

集成后最大的未知不是单帧正确性（那个 `dxv_gpu_verify.py` 已经逐像素验过），
而是**多个图层各有一个 GL 上下文时稳不稳**，以及长时间连播会不会掉帧/泄漏。

所以这里开 4 个播放器并行跑，比较：
  · GPU 解码 vs 软解的总 CPU（同一份代码路径，只差解码方式，可比性最好）
  · 4 个 GL 上下文并存是否正常出帧
  · 连播期间的工作集内存有没有持续增长

用法：venv\\Scripts\\python.exe tools\\gpu_soak.py [每组秒数，默认 20]
"""

import ctypes
import ctypes.wintypes
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np  # noqa: E402

import media_manager as mm  # noqa: E402

LIB = r"Y:\EasyRealityAutoVJ\素材库"
OUT = []


def w(s=""):
    print(s, flush=True)
    OUT.append(str(s))


class PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.wintypes.DWORD), ("PageFaultCount", ctypes.wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def mem_mb():
    p = PMC()
    p.cb = ctypes.sizeof(PMC)
    try:
        k32 = ctypes.windll.kernel32
        fn = k32.K32GetProcessMemoryInfo
        fn.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(PMC), ctypes.wintypes.DWORD]
        fn.restype = ctypes.wintypes.BOOL
        if not fn(k32.GetCurrentProcess(), ctypes.byref(p), p.cb):
            return -1.0
        return p.WorkingSetSize / 1048576.0
    except Exception:
        try:
            ctypes.windll.psapi.GetProcessMemoryInfo(
                ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(p), p.cb)
            return p.WorkingSetSize / 1048576.0
        except Exception:
            return -1.0


def pick(n):
    """挑 n 个能走 GPU 的 DXV 素材（DXT5 优先，尺寸尽量大）。"""
    import av
    import dxvnative
    out = []
    for f in sorted(os.listdir(LIB)):
        if not f.lower().endswith(".mov"):
            continue
        p = os.path.join(LIB, f)
        try:
            c = av.open(p, metadata_errors="replace")
            try:
                st = c.streams.video[0]
                if st.codec_context.name != "dxv":
                    continue
                ww, hh = int(st.width), int(st.height)
                pk = next(c.demux(st))
                hd = dxvnative.parse_header(bytes(pk))
            finally:
                c.close()
        except Exception:
            continue
        if hd is None:
            continue
        fmt = hd[0]
        if fmt == 1 and ww * hh < mm._gpu_dxt1_min_px():
            continue                                # 会被门槛挡掉，别拿来测
        out.append((p, ww, hh, fmt))
        if len(out) >= n * 3:
            break
    out.sort(key=lambda r: -(r[1] * r[2]))          # 大的优先（差异更明显）
    return out[:n]


def run_group(paths, gpu, secs):
    """一组播放器并行跑。gpu=True 时用**真实分流**（走 GpuDxvPlayer 的 GPU 路径），
    否则关掉开关走软件里实际会用的路径（cv2 或 PyAV，取决于该素材有没有 alpha）——
    这样量出来的差值就是「这个开关到底省多少 CPU」。"""
    mm.set_gpu_decode(gpu)
    players, kinds = [], []
    try:
        for p, _, _, _ in paths:
            it = mm.MediaItem(p)
            it._is_dxv = True                      # 模拟编码普查已完成
            pl = mm.create_video_player(it)
            pl.set_active(True)
            players.append(pl)
            kinds.append(type(pl).__name__)
        time.sleep(2.0)                              # 让它们都开起来
        paths_gpu = [getattr(pl, "gpu", None) for pl in players]
        t0, c0, m0 = time.perf_counter(), time.process_time(), mem_mb()
        samples, seen = [], [None] * len(players)
        frames = [0] * len(players)
        while time.perf_counter() - t0 < secs:
            for i, pl in enumerate(players):
                img = pl.current()
                if img is not None and img is not seen[i]:
                    seen[i] = img
                    frames[i] += 1
            if int(time.perf_counter() - t0) % 5 == 0:
                el = time.perf_counter() - t0
                if not samples or el - samples[-1][0] > 4.5:
                    samples.append((el, (time.process_time() - c0) / el, mem_mb()))
            time.sleep(0.004)
        wall = time.perf_counter() - t0
        cpu = (time.process_time() - c0) / wall
        m1 = mem_mb()
        return {"kinds": kinds, "gpu": paths_gpu, "frames": frames, "cpu": cpu, "wall": wall,
                "mem0": m0, "mem1": m1, "samples": samples}
    finally:
        for pl in players:
            pl.close()
        time.sleep(0.6)
        mm.set_gpu_decode(False)


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    paths = pick(4)
    w("=" * 92)
    w("GPU 解码并发/稳定性联调   %s   每组 %.0f 秒" % (time.strftime("%Y-%m-%d %H:%M:%S"), secs))
    w("=" * 92)
    ok, msg = mm.gpu_status()
    w("gpu_status = %s / %s" % (ok, msg))
    for p, ww, hh, fmt in paths:
        w("  素材 %-42s %dx%d  DXT%d" % (os.path.basename(p)[:42], ww, hh, 5 if fmt == 5 else 1))
    if not paths:
        w("★ 没有可用素材")
        return
    w("")

    res = {}
    for tag, gpu in (("GPU", True), ("软解", False)):
        r = run_group(paths, gpu, secs)
        res[tag] = r
        w("--- %s 组 ---" % tag)
        kinds = set(r["kinds"])
        w("  播放器 %s" % (r["kinds"][0] if len(kinds) == 1 else r["kinds"]))
        w("  实际路径 %s   出帧 %s（%.0f 秒内）" % (r["gpu"], r["frames"], r["wall"]))
        w("  总 CPU %.3f 核当量   工作集 %.0f → %.0f MB（%+.0f）"
          % (r["cpu"], r["mem0"], r["mem1"], r["mem1"] - r["mem0"]))
        for el, cpu, mem in r["samples"]:
            w("     t=%4.1fs  CPU %.3f 核  内存 %.0f MB" % (el, cpu, mem))
        w("")

    g, s = res["GPU"], res["软解"]
    if s["cpu"] > 0.01:
        w("★ 4 图层并行：GPU %.3f 核 vs 软解 %.3f 核 → %s %.1f%%   （差 %.3f 核）"
          % (g["cpu"], s["cpu"], "省" if g["cpu"] < s["cpu"] else "多花",
             abs(1 - g["cpu"] / s["cpu"]) * 100, abs(s["cpu"] - g["cpu"])))
    w("★ 稳定性：GPU 组内存变化 %+.0f MB；帧数 %s（四个都应 > 0 且相近）"
      % (g["mem1"] - g["mem0"], g["frames"]))
    with open(os.path.join(ROOT, "_gpu_soak.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    print("\n→ 已写入 _gpu_soak.txt")


if __name__ == "__main__":
    main()
