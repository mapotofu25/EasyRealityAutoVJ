# -*- coding: utf-8 -*-
"""DXT1-1080p 门槛重估（2026-09-26）

背景：`AUTO_VJ_GPU_DXT1_MIN_PX = 3.0 Mpx` 这道门槛当初是这样定的：
「DXT1-1080p（2.07 Mpx）实测**多花 46%**」——但那个 46% 是拿 **cv2 软解（0.50 核）** 当对照，
而且**把冷启动算进去了**。

现场实测（用户机器，4 图层运行中，`tools/_thread_cpu.py`）：
  · DXT1-1080p 走**降级软解**（`GpuDxvPlayer._soft_frames` = PyAV + 缩放到 1080p）
    → **每个线程 0.945~0.967 核**
  · DXT5-1080p 走 GPU → 1.00 核（含回读）

也就是说 DXV 素材一旦不走 GPU，走的是 **PyAV**（不是 cv2）→ 0.95 核，
而不是基准里那个 0.50 核。若 GPU 只要 0.54 核，**放行 DXT1 反而能省四成**。

本脚本按**现场同一条路径**重测，预热 1.5 秒后只计稳态，交替 3 轮取中位数。

用法：venv\\Scripts\\python.exe tools\\_dxt1_recheck.py
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

LIB = r"Y:\EasyRealityAutoVJ\素材库"
OUT = []


def w(s=""):
    print(s, flush=True)
    OUT.append(str(s))


def proc_cpu():
    import ctypes
    import ctypes.wintypes as wt
    class FT(ctypes.Structure):
        _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]
    k32 = ctypes.windll.kernel32
    k32.GetCurrentProcess.restype = wt.HANDLE        # ★ 不设会被当 32 位 int 截断
    # ★ GetProcessTimes 也必须显式给 argtypes：否则伪句柄 -1 会触发
    #   `OverflowError: int too long to convert`（HANDLE 被当 C int）
    k32.GetProcessTimes.argtypes = [wt.HANDLE, ctypes.POINTER(FT), ctypes.POINTER(FT),
                                    ctypes.POINTER(FT), ctypes.POINTER(FT)]
    h = k32.GetCurrentProcess()
    c, e, k, u = FT(), FT(), FT(), FT()
    ok = k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u))
    if not ok:
        raise OSError("GetProcessTimes failed")
    return ((k.hi << 32) | k.lo) / 1e7 + ((u.hi << 32) | u.lo) / 1e7


def run(path, gpu, secs=6.0, warm=1.5):
    """gpu=True 强制走 GPU（把 DXT1 门槛设为 0）；False 走现场降级路径（软解）"""
    import av
    import media_manager as mm

    old_min = mm._gpu_dxt1_min_px
    old_dis = mm._GPU_DISABLED
    if gpu:
        mm._gpu_dxt1_min_px = lambda: 0
        mm._GPU_DISABLED = ""
    else:
        mm._GPU_DISABLED = "recheck: forced software"
    p = None
    try:
        p = mm.GpuDxvPlayer(path, av, out_alpha=False)
        p.set_max_size(1920, 1080)     # 与现场一致（画布 ×1.5 上限，1080p 素材不缩）
        p.set_active(True)
        # 预热：开容器 + 建 GL 上下文 + 编译着色器 + 填满前几帧
        t = time.perf_counter()
        while time.perf_counter() - t < warm:
            p.current()
            time.sleep(0.005)
        c0, t0, n = proc_cpu(), time.perf_counter(), 0
        seen = None
        while time.perf_counter() - t0 < secs:
            img = p.current()
            if img is not None and img is not seen:
                seen = img
                n += 1
            time.sleep(0.003)
        wall = time.perf_counter() - t0
        return (proc_cpu() - c0) / wall, n / wall, p.gpu, p.reason
    finally:
        if p is not None:
            p.close()
        mm._gpu_dxt1_min_px = old_min
        mm._GPU_DISABLED = old_dis
        time.sleep(0.8)


def main():
    import av
    import media_manager as mm

    w("=" * 92)
    w("DXT1-1080p 门槛重估（GPU vs 现场降级软解）   %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    w("=" * 92)

    if not mm.gpu_status()[0]:
        w("★ GPU 不可用：%s" % (mm.gpu_status()[1],))
        return

    targets = []
    for f in sorted(os.listdir(LIB)):
        if not f.lower().endswith(".mov"):
            continue
        p = os.path.join(LIB, f)
        try:
            import dxvnative
            c = av.open(p, metadata_errors="replace")
            st = c.streams.video[0]
            if st.codec_context.name != "dxv" or int(st.width) * int(st.height) < 1.9e6 \
                    or int(st.width) * int(st.height) > 2.3e6:
                c.close()
                continue
            pk = None
            for pkt in c.demux(st):
                if pkt.size:
                    pk = bytes(pkt)
                    break
            c.close()
            hd = dxvnative.parse_header(pk) if pk else None
            if hd and int(hd[0]) == 1:
                targets.append(p)
        except Exception:
            continue
        if len(targets) >= 3:
            break

    if not targets:
        w("★ 没找到 DXT1-1080p 素材")
        return
    w("样本 %d 个 DXT1-1080p 素材" % len(targets))
    w("")

    rounds = 3
    for p in targets:
        w("── %s ──" % os.path.basename(p)[:60])
        g, s = [], []
        for r in range(rounds):
            order = ("gpu", "soft") if r % 2 == 0 else ("soft", "gpu")
            for tag in order:
                cpu, fps, went_gpu, reason = run(p, tag == "gpu")
                if tag == "gpu":
                    g.append(cpu)
                else:
                    s.append(cpu)
            w("    第 %d 轮: GPU %.3f 核   软解 %.3f 核" % (r + 1, g[-1], s[-1]))
        g.sort()
        s.sort()
        gm, sm = g[len(g) // 2], s[len(s) // 2]
        w("    中位数: GPU %.3f 核    软解 %.3f 核" % (gm, sm))
        if sm <= 0.001:
            w("    ★ 软解读数无效（%.3f），需重测" % sm)
        elif gm < sm * 0.95:
            w("    → ✓ GPU 更省 %.0f%%（应放行 DXT1-1080p）" % ((1 - gm / sm) * 100))
        else:
            w("    → GPU 略贵 %.0f%%（维持门槛）" % ((gm / max(sm, 1e-9) - 1) * 100))
        w("    实际路径: gpu=%s  reason=%s" % (went_gpu, reason[:60]))
        w("")

    w("=" * 92)
    with open(os.path.join(ROOT, "_dxt1_recheck.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    w("→ 已写入 _dxt1_recheck.txt")


if __name__ == "__main__":
    main()
