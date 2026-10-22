"""Spout / NDI 发送的**单帧成本**微基准（精确、不受引擎漂移影响）

为什么不用整机对比：引擎本身的 CPU 在 3.3~3.8 核之间天然漂移（解码缓存、素材切换、
后台识别），±0.4 核的噪声直接把「+0.2 核的输出开销」淹掉 —— 实测过，末段"都关"甚至
比"都开"还高。所以改成直接量**发送动作本身**：发 N 帧取平均，再 × 帧率换算核当量。

测三样：
  · Spout sendImage（CPU 共享内存路径，每帧一次全画布拷贝）
  · NDI send_video（发送端要编码 + 组包）
  · 对照：一帧的纯内存拷贝（memcpy 8.3MB），用来判断"贵不贵"

⚠ NDI 的开销与「有没有接收端」有关：本机没有接收端时发送端可能跳过实际编码，
   所以这里的数字是**下限** —— 真接上另一台机器收流时会更贵。

用法：venv\\Scripts\\python.exe tools\\_output_bench.py [帧数=300]
"""

import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np  # noqa: E402

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.c_void_p] * 4
k32.GetCurrentProcess.restype = wt.HANDLE
NCORE = os.cpu_count() or 4
OUT = []


class FILETIME(ctypes.Structure):
    _fields_ = [("lo", wt.DWORD), ("hi", wt.DWORD)]


def w(s=""):
    print(s, flush=True)
    OUT.append(str(s))


def proc_cpu():
    c, e, k, u = FILETIME(), FILETIME(), FILETIME(), FILETIME()
    k32.GetProcessTimes(k32.GetCurrentProcess(), ctypes.byref(c), ctypes.byref(e),
                        ctypes.byref(k), ctypes.byref(u))
    return ((k.hi << 32 | k.lo) + (u.hi << 32 | u.lo)) / 1e7


def bench(label, fn, n):
    fn()                                        # 预热一次（首次可能建资源）
    time.sleep(0.3)
    c0, t0 = proc_cpu(), time.perf_counter()
    for _ in range(n):
        fn()
    wall = time.perf_counter() - t0
    cpu = proc_cpu() - c0
    ms_wall = wall / n * 1000
    ms_cpu = cpu / n * 1000
    w("  %-34s 墙钟 %6.2f ms/帧   CPU %6.2f ms/帧   → 60fps 时 %.3f 核   (%.0f fps 上限)"
      % (label, ms_wall, ms_cpu, ms_cpu / 1000 * 60, n / wall))
    return ms_cpu


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    W, H = 1920, 1080
    src = np.zeros((H, W, 4), dtype=np.uint8)
    src[:, :, 3] = 255
    src[::7, :, 0] = 200                        # 给点内容，避免压缩器走极端
    w("=" * 104)
    w("Spout / NDI 发送开销微基准   %s   %dx%d  每项 %d 帧   本机 %d 核"
      % (time.strftime("%H:%M:%S"), W, H, n, NCORE))
    w("=" * 104)
    w("")
    w("[对照] 纯内存拷贝")
    dst = np.empty_like(src)

    def _memcpy():
        dst[:] = src

    bench("整幅 memcpy 8.3MB", _memcpy, n)
    w("")

    w("[Spout] sendImage（CPU 共享内存路径）")
    try:
        from SpoutGL import SpoutSender
        from SpoutGL.enums import GL_BGRA_EXT
        s = SpoutSender()
        s.setSenderName("AutoVJ-bench")
        s.createOpenGL()
        mv = memoryview(src).cast("B")
        bench("Spout sendImage 1080p", lambda: s.sendImage(mv, H, W, GL_BGRA_EXT, True, 0), n)
        try:
            s.releaseSender()
        except Exception:
            pass
    except Exception as e:                                            # noqa: BLE001
        w("  ★ Spout 不可用/失败：%s" % str(e)[:90])
    w("")

    w("[NDI] send_video（发送端编码 + 组包）")
    try:
        from ndi_out import get_ndi
        d = get_ndi("AutoVJ-bench")
        if not d._ensure(W, H):        # ⚠ 必须给真实分辨率（发送端尺寸只能在 open 前定死）
            w("  ★ NDI 发送端起不来（缺 Runtime？）")
        else:
            arr = src.reshape(-1)
            bench("NDI send_video 1080p", lambda: d.send_video(arr, W, H), n)
            try:
                d.close()
            except Exception:
                pass
    except Exception as e:                                            # noqa: BLE001
        w("  ★ NDI 不可用/失败：%s" % str(e)[:90])

    w("")
    w("=" * 104)
    w("读法：核当量 = CPU ms/帧 ÷ 1000 × 60（60fps 口径）。这些开销是**每帧固定**的，")
    w("      所以渲染帧率选 30 时它们也减半。")
    w("⚠ NDI 是下限：本机无接收端时可能跳过实际编码。接了接收端会更贵。")
    with open(os.path.join(ROOT, "_output_bench.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    print("\n→ 已写入 _output_bench.txt")


if __name__ == "__main__":
    main()
