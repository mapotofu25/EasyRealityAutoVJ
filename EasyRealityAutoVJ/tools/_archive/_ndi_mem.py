# -*- coding: utf-8 -*-
"""NDI **真在发送**时的内存占用（2026-09-26 系统卡死事件）

现场差异：用户那头有接收端（另一台电脑真在收），我这台没有 ——
NDI SDK 在没有接收端时会**跳过编码与发送**，所以之前的测量和复现都碰不到这条路径。
本脚本本机自收自发（子进程接收端），让发送端真的编码发流，观察发送进程内存。

用法：venv\\Scripts\\python.exe tools\\_ndi_mem.py [秒数=180]
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

k32 = ctypes.windll.kernel32
psapi = ctypes.WinDLL("psapi")


class PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


_H = k32.OpenProcess(0x1000, False, os.getpid())


def mem():
    p = PMC()
    p.cb = ctypes.sizeof(PMC)
    psapi.GetProcessMemoryInfo(_H, ctypes.byref(p), p.cb)
    return p.WorkingSetSize / 1048576.0, p.PagefileUsage / 1048576.0


OUT = []


def w(s=""):
    print(s, flush=True)
    OUT.append(str(s))


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 180.0
    W, H = 1280, 720
    NAME = "AutoVJ-memtest"

    import numpy as np
    import ndi_out

    w("=" * 88)
    w("NDI 真发送时的内存   %s   %dx%d   %.0f 秒" % (time.strftime("%H:%M:%S"), W, H, secs))
    w("=" * 88)

    ws0, cm0 = mem()
    w("基线  工作集 %.0f MB  提交 %.0f MB" % (ws0, cm0))

    # 先起接收端（子进程），让 NDI 认为"有人在收" → 真的编码发流
    child = subprocess.Popen([sys.executable, os.path.join(ROOT, "tools", "_ndi_send_cost.py"), "--recv"],
                             cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    d = ndi_out.get_ndi(NAME)
    if not d._ensure(W, H):
        w("★ 发送端起不来：%s" % d._last_err)
        child.kill()
        return
    w("发送端已建立 %dx%d" % (W, H))

    img = np.zeros((H, W, 4), np.uint8)
    img[:, :, 2] = 180
    img[:, :, 3] = 255
    flat = img.reshape(-1)

    # 等接收端接上
    n = 0
    t = time.time()
    while time.time() - t < 25:
        d.send_video(flat, W, H)
        try:
            n = d._sender.get_num_connections(0.0)
        except Exception:
            n = 0
        if n > 0:
            break
        time.sleep(0.03)
    w("接收端连接数 = %d（用了 %.1f 秒）" % (n, time.time() - t))
    if n <= 0:
        w("★ 没连上 → NDI 不会编码，测不到真开销（这条路走不通）")
    w("")
    w("=== 持续发送，每 15 秒采样 ===")
    t0 = time.perf_counter()
    nxt, sent = 15.0, 0
    while time.perf_counter() - t0 < secs:
        d.send_video(flat, W, H)
        sent += 1
        time.sleep(1.0 / 60)
        el = time.perf_counter() - t0
        if el >= nxt:
            nxt += 15
            ws, cm = mem()
            w("  t=%4.0fs  工作集 %6.0f MB (%+6.0f)  提交 %6.0f MB (%+6.0f)  发出 %d 帧  %s"
              % (el, ws, ws - ws0, cm, cm - cm0, sent, time.strftime("%H:%M:%S")))

    ws1, cm1 = mem()
    rate = (cm1 - cm0) / (secs / 60.0)
    w("")
    w("→ 净增：工作集 %+.0f MB  提交 %+.0f MB  = **%.1f MB/分钟**" % (ws1 - ws0, cm1 - cm0, rate))
    w("  判据：< 30 MB/分钟 正常；> 100 MB/分钟 就是泄漏")
    if rate > 100:
        w("  ★ **NDI 发送端在泄漏内存** —— 这台机器上的现场就是这个量级（~400 MB/分钟）")
        w("    修法方向：发送前查 `get_write_available()`，不可写就丢这一帧（不排队）")
    else:
        w("  （NDI 发送端内存平稳）")
    try:
        d.close()
    except Exception:
        pass
    child.kill()

    w("")
    with open(os.path.join(ROOT, "_ndi_mem.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    w("→ 已写入 _ndi_mem.txt")


if __name__ == "__main__":
    main()
