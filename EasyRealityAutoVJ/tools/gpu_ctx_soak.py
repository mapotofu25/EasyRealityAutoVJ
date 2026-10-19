# -*- coding: utf-8 -*-
"""真实播放器生命周期下的 GL 上下文回收测试（2026-09-26 卡死事件回归）

`tools/_gl_ctx_leak.py` 验证的是裸 `GLDecoder`；这里验证**真实路径**：
反复「建 GpuDxvPlayer → 播几帧 → close()」，模拟演出中不断切素材。
修复前每次 close 都会永久留下一个 GL 上下文（约 21 个线程），
所以线程数会线性上涨；修复后应当收敛。

用法：venv\\Scripts\\python.exe tools\\gpu_ctx_soak.py [轮数=24]
"""
import ctypes
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

LIB = r"Y:\EasyRealityAutoVJ\素材库"
PID = os.getpid()
k32 = ctypes.windll.kernel32
psapi = ctypes.WinDLL("psapi")


class TE(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                ("th32ThreadID", ctypes.c_ulong), ("th32OwnerProcessID", ctypes.c_ulong),
                ("tpBasePri", ctypes.c_long), ("tpDeltaPri", ctypes.c_long), ("dwFlags", ctypes.c_ulong)]


class PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def snap():
    s = k32.CreateToolhelp32Snapshot(0x00000004, 0)
    n = 0
    te = TE()
    te.dwSize = ctypes.sizeof(TE)
    if k32.Thread32First(s, ctypes.byref(te)):
        while True:
            if te.th32OwnerProcessID == PID:
                n += 1
            if not k32.Thread32Next(s, ctypes.byref(te)):
                break
    k32.CloseHandle(s)
    h = k32.GetCurrentProcess()
    p = PMC()
    p.cb = ctypes.sizeof(PMC)
    psapi.GetProcessMemoryInfo(h, ctypes.byref(p), p.cb)
    return n, p.WorkingSetSize / 1048576.0


OUT = []


def w(s=""):
    print(s, flush=True)
    OUT.append(str(s))


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 24

    import av
    import glctx
    import media_manager as mm

    w("=" * 86)
    w("GPU 播放器生命周期回收测试（模拟切素材）   %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    w("=" * 86)

    if not glctx.preinit():
        w("★ glfw 不可用")
        return

    # 找 DXT5 1080p 素材（走 GPU 路径）
    paths = []
    for f in sorted(os.listdir(LIB)):
        if not f.lower().endswith((".mov", ".mp4", ".mkv")):
            continue
        p = os.path.join(LIB, f)
        try:
            c = av.open(p, metadata_errors="replace")
            st = c.streams.video[0]
            if st.codec_context.name == "dxv" and st.width * st.height > 1.9e6:
                paths.append(p)
            c.close()
        except Exception:
            continue
        if len(paths) >= max(rounds, 8):
            break
    if not paths:
        w("★ 素材库里没找到 DXT5-1080p 素材")
        return
    w("可用素材 %d 个，将循环 %d 轮" % (len(paths), rounds))

    t0, m0 = snap()
    w("基线（建任何播放器之前）        线程 %4d   工作集 %6.1f MB" % (t0, m0))
    w("")

    marks = []
    for i in range(rounds):
        p = paths[i % len(paths)]
        pl = mm.GpuDxvPlayer(p, av, out_alpha=True)
        pl.set_active(True)
        time.sleep(0.45)                       # 让它真开起来、出几帧
        img = pl.current()
        got = img is not None
        pl.close()
        time.sleep(0.35)                       # 给解码线程退出的时间
        t, m = snap()
        marks.append((i + 1, t, t - t0, m, m - m0, pl.gpu, got))
        if (i + 1) % 4 == 0 or i == 0:
            w("  第 %2d 轮: 线程 %4d (%+d)   工作集 %6.1f MB (%+.1f)   本播放器 gpu=%s 出帧=%s"
              % (i + 1, t, t - t0, m, m - m0, pl.gpu, got))

    time.sleep(2.0)
    t1, m1 = snap()
    w("")
    w("跑完 %d 轮之后                  线程 %4d (%+d)   工作集 %6.1f MB (%+.1f)"
      % (rounds, t1, t1 - t0, m1, m1 - m0))
    w("")
    growth = t1 - t0
    per = growth / rounds
    if growth < 60:
        w("  ✓ **不泄漏**：%d 轮切换后线程只增 %d 个（平均 %.2f 个/轮）" % (rounds, growth, per))
        w("    修复前每轮会让线程永久 +20.7 个（%d 轮 ≈ +%d），差距一目了然。"
          % (rounds, int(rounds * 20.7)))
    else:
        w("  ★ 仍有增长（%d 个 / %.1f 每轮），需继续排查" % (growth, per))

    w("")
    w("  详细曲线（每轮线程数）：")
    w("    " + "  ".join("%d:%d" % (r, t) for r, t, _, _, _, _, _ in marks[:rounds]))

    w("")
    w("=" * 86)
    with open(os.path.join(ROOT, "_gpu_ctx_soak.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    w("→ 已写入 _gpu_ctx_soak.txt")


if __name__ == "__main__":
    main()
