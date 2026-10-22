"""NDI 发送开销的**近似测量**：用 FFmpeg 的帧内编码器代替 NDI 的私有实现

⚠⚠ **本脚本 2026-09-25 实测「此路不通」，保留只为记录这条死路，别再去试**：
  · `speedhq` 编码器在本机 PyAV 18 里编不出帧 —— `avcodec_send_frame()` 恒返回 22 (EINVAL)，
    裸 CodecContext 和 mxf 容器两条路都试过；
  · 退一步用同为帧内 DCT 的 `mjpeg` 做量级参考，**同样 EINVAL** ⇒ 不是编码器约束，
    是 PyAV 这套用法本身不对（未继续深挖）。
  ⇒ **NDI 真实输出时的开销，目前没有实测数据**。要拿真值只能在另一台机器上
    用 NDI Studio Monitor / OBS(NDI 插件) 真接一次流，再看发送端 CPU 差。

为什么当初想走这条路：NDI 的发送端开销 = SpeedHQ 编码 + 组包，而**本机自发自收在这台机器上
建不起连接**（`_ndi_live_bench.py`：Finder 能发现源名 `主机名 (AutoVJ-xxx)`，
但 Receiver 70 秒一直 `connected=False`，用名字串和 Source 对象两条路都试过），
所以量不到真值。

用法：venv\\Scripts\\python.exe tools\\_speedhq_cost.py [帧数=300]
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


def run(n, threads, size, label, codec="speedhq", pix="yuv422p"):
    import av
    W, H = size
    tmp = os.path.join(ROOT, "_speedhq_tmp.mxf")
    src = np.zeros((H, W, 4), dtype=np.uint8)
    src[:, :, 3] = 255
    src[::7, :, 0] = 200
    src[::5, :, 1] = 120
    frames_np = []
    for i in range(6):                      # 预造几帧轮换，避免完全静止
        f = src.copy()
        f[i::11, :, 2] = 180
        frames_np.append(f)

    def mk(i):
        """每帧新建 VideoFrame 并设单调 pts —— ⚠ 复用同一对象/相同 pts 会让
        `avcodec_send_frame` 返回 22(EINVAL)。下面测的是「格式转换 + 编码」的总和，
        NDI 发送端内部同样要做 BGRA→UYVY 转换，所以这样比只测编码更贴近真实。"""
        fr = av.VideoFrame.from_ndarray(frames_np[i % 6], format="bgra")
        fr = fr.reformat(format=pix)
        fr.pts = i
        return fr

    try:
        # ⚠ 用裸 CodecContext，不经容器：mxf 会额外要求 time_base 等元数据，
        # 而 `avcodec_send_frame` 返回 22 基本都是「编码器参数与帧尺寸不符」
        # （容器路径下 stream 的属性设置时机不好控）。
        from fractions import Fraction
        ctx = av.CodecContext.create(codec, "w")
        ctx.width, ctx.height = W, H
        ctx.pix_fmt = pix
        ctx.time_base = Fraction(1, 60)
        try:
            ctx.thread_count = threads
        except Exception:
            pass
        # 预热
        for k in range(6):
            for _p in ctx.encode(mk(k)):
                pass
        time.sleep(0.2)
        c0, t0 = proc_cpu(), time.perf_counter()
        for i in range(n):
            for _p in ctx.encode(mk(i)):
                pass
        wall = time.perf_counter() - t0
        cpu = proc_cpu() - c0
        for _p in ctx.encode(None):
            pass
        try:
            ctx.close()
        except Exception:
            pass
        w("  %-30s 墙钟 %6.2f ms/帧   CPU %6.2f ms/帧   → 60fps 时 %.3f 核"
          % (label, wall / n * 1000, cpu / n * 1000, cpu / n * 1000 / 1000 * 60))
        return cpu / n * 1000
    except Exception as e:                                            # noqa: BLE001
        w("  %-30s ★ 失败：%s" % (label, str(e)[:80]))
        return None
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    w("=" * 96)
    w("NDI 发送开销**近似**测量（FFmpeg speedhq ≈ NDI 的 SpeedHQ 编码）   %s"
      % time.strftime("%H:%M:%S"))
    w("=" * 96)
    w("")
    # SpeedHQ 编码器在 PyAV 里编不了（参数约束），改用同为「帧内 DCT」的 MJpeg
    # 拿到量级参考：两者都是逐帧独立压缩，开销同阶；SpeedHQ 的实现更简单（更快些）。
    run(n, 1, (1920, 1080), "MJpeg 1080p 单线程（帧内参考）", "mjpeg", "yuvj420p")
    run(n, 0, (1920, 1080), "MJpeg 1080p 多线程", "mjpeg", "yuvj420p")
    run(n, 1, (1280, 720), "MJpeg 720p 单线程", "mjpeg", "yuvj420p")
    w("")
    w("=" * 96)
    w("⚠ 这是近似值：NDI 用的是自家优化的 SpeedHQ，且还要加上发包成本（本机环回几乎为 0，")
    w("   走网线到另一台机器则是真实网络开销）。仅用于判断「大概几个核」这个量级。")
    with open(os.path.join(ROOT, "_speedhq_cost.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    print("\n→ 已写入 _speedhq_cost.txt")


if __name__ == "__main__":
    main()
