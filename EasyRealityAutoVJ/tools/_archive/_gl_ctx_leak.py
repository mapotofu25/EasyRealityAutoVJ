# -*- coding: utf-8 -*-
"""GL 上下文泄漏复现（2026-09-26 卡死事件的根因验证）

背景：用户报告的「窗口卡死」现场实测到 **线程 2386 个 / 句柄 15442 / 提交内存 8 GB**，
远超正常稳态（310~330 线程）。嫌疑是 `glctx.GLDecoder.close()` 只删纹理和 FBO、
**不销毁 glfw 窗口与 GL 上下文**（见 glctx.py 的注释「窗口交给 glfw 统一回收（进程退出时）；
不单独 destroy」）—— 于是每切换一次 DXV 素材就永久漏掉一个 GL 上下文。

本脚本验证三件事：
  1) 一个 GL 上下文到底带来多少线程 / 多少提交内存
  2) 反复创建（模拟素材切换）时是否线性增长
  3) `destroy()` 之后能否回落（验证修复方案有效）

用法：venv\\Scripts\\python.exe tools\\_gl_ctx_leak.py [个数=10]
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

PID = os.getpid()
k32 = ctypes.windll.kernel32
psapi = ctypes.WinDLL("psapi")


class PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def snapshot():
    """(线程数, 句柄数, 工作集MB, 提交MB) —— 进程级读数，不受 Python 线程表影响"""
    class TE(ctypes.Structure):
        _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                    ("th32ThreadID", ctypes.c_ulong), ("th32OwnerProcessID", ctypes.c_ulong),
                    ("tpBasePri", ctypes.c_long), ("tpDeltaPri", ctypes.c_long), ("dwFlags", ctypes.c_ulong)]

    snap = k32.CreateToolhelp32Snapshot(0x00000004, 0)
    n = 0
    te = TE()
    te.dwSize = ctypes.sizeof(TE)
    if k32.Thread32First(snap, ctypes.byref(te)):
        while True:
            if te.th32OwnerProcessID == PID:
                n += 1
            if not k32.Thread32Next(snap, ctypes.byref(te)):
                break
    k32.CloseHandle(snap)

    h = k32.GetCurrentProcess()
    hc = ctypes.c_ulong(0)
    k32.GetProcessHandleCount(h, ctypes.byref(hc))
    p = PMC()
    p.cb = ctypes.sizeof(PMC)
    psapi.GetProcessMemoryInfo(h, ctypes.byref(p), p.cb)
    return n, hc.value, p.WorkingSetSize / 1048576.0, p.PagefileUsage / 1048576.0


OUT = []


def w(s=""):
    print(s, flush=True)
    OUT.append(str(s))


def main():
    n_make = int(sys.argv[1]) if len(sys.argv) > 1 else 10

    import glctx

    w("=" * 84)
    w("GL 上下文泄漏复现   %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    w("=" * 84)

    if not glctx.preinit():
        w("★ glfw 不可用，无法复现")
        return

    t0, h0, ws0, cm0 = snapshot()
    w("基线（还没建任何上下文）      线程 %4d   句柄 %5d   工作集 %6.1f MB   提交 %6.1f MB"
      % (t0, h0, ws0, cm0))

    w("")
    w("[1] 逐个创建 GL 上下文（模拟一次素材切换 = 建一个）")
    decs = []
    for i in range(n_make):
        d = glctx.GLDecoder()
        decs.append(d)
        t, h, ws, cm = snapshot()
        w("  第 %2d 个: ok=%-5s  线程 %4d (%+d)   句柄 %5d (%+d)   提交 %6.1f MB (%+.1f)"
          % (i + 1, d.ok, t, t - t0, h, h - h0, cm, cm - cm0))

    t1, h1, ws1, cm1 = snapshot()
    w("")
    w("  小结：%d 个上下文 → 线程 +%d（每个 +%.1f）  句柄 +%d  提交 +%.1f MB"
      % (n_make, t1 - t0, (t1 - t0) / n_make, h1 - h0, cm1 - cm0))

    w("")
    w("[2] 按**现状**销毁（GLDecoder.close()：只删纹理/FBO，不销毁窗口）")
    for d in decs:
        try:
            d.close()
        except Exception:
            pass
    time.sleep(1.5)
    t2, h2, ws2, cm2 = snapshot()
    w("  close() 之后                  线程 %4d (%+d)   句柄 %5d (%+d)   提交 %6.1f MB (%+.1f)"
      % (t2, t2 - t0, h2, h2 - h0, cm2, cm2 - cm0))
    leaked = t2 - t0
    if leaked > 5:
        w("  ★ **泄漏成立**：close() 之后仍有 %d 个线程没归还（%d 个上下文 × %.1f）"
          % (leaked, n_make, leaked / n_make))
        w("    ⇒ 每切换一次素材就永久漏一个 GL 上下文；跑一小时 = 几百个 ⇒ 线程爆到几千、")
        w("      提交内存到 GB 级，最终主线程抢不到 GIL → 窗口未响应。")
    else:
        w("  （线程已归还，无泄漏）")

    w("")
    w("[3] 验证修复方案：对上面那 %d 个调 destroy()，看线程能否收回来" % n_make)
    ok_destroy = hasattr(glctx.GLDecoder, "destroy")
    w("  GLDecoder.destroy() 是否存在：%s" % ok_destroy)
    if not ok_destroy:
        w("  ★ 还没有 destroy()，无法验证（这就是修复前的情况）")
    else:
        for d in decs:
            try:
                d.destroy()
            except Exception:
                pass
        time.sleep(2.0)
        t4, h4, ws4, cm4 = snapshot()
        w("  destroy() 之后：线程 %4d（相对基线 %+d）" % (t4, t4 - t0))
        if (t4 - t0) < n_make * 4:
            w("  ✓ **修复有效**：%d 个上下文的线程全部归还（每个上下文回收约 21 个线程）"
              % n_make)
        else:
            w("  ★ 仍有 %d 个线程残留，需继续查" % (t4 - t0))

        w("")
        w("[4] 再干净测一轮：连建 %d 个 + 全部 destroy，看净增量" % n_make)
        t5, _, _, _ = snapshot()
        tmp = [glctx.GLDecoder() for _ in range(n_make)]
        t6, _, _, _ = snapshot()
        for d in tmp:
            d.close()
            d.destroy()
        time.sleep(2.0)
        t7, h7, ws7, cm7 = snapshot()
        w("  建之前 %4d → 建之后 %4d（+%d）→ destroy 之后 %4d（净 +%d）"
          % (t5, t6, t6 - t5, t7, t7 - t5))
        w("  → %s" % ("✓ **不泄漏**：建了又销毁，线程数回到原点" if (t7 - t5) < n_make * 4
                      else "★ 还有泄漏"))

        w("")
        w("[5] 配额守卫：连建 %d 个（超过 MAX_CTX=%d 应开始拒绝）" % (glctx.MAX_CTX + 6, glctx.MAX_CTX))
        kept = []
        denied = 0
        for i in range(glctx.MAX_CTX + 6):
            d = glctx.GLDecoder()
            if d.ok:
                kept.append(d)
            else:
                denied += 1
                last_err = d.err
        w("  成功 %d 个，被拒 %d 个" % (len(kept), denied))
        if denied:
            w("  最后一条拒绝原因：%s" % last_err)
            w("  ✓ 守卫生效：超过上限会拒绝新建（调用方自动转软解），线程数不会再无声暴涨")
        else:
            w("  ★ 守卫没生效，需要检查")
        for d in kept:
            d.close()
            d.destroy()

    w("")
    w("=" * 84)
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                           "_gl_ctx_leak.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    w("→ 已写入 _gl_ctx_leak.txt")


if __name__ == "__main__":
    main()
