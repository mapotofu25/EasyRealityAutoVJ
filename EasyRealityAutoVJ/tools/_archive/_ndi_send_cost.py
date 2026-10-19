# -*- coding: utf-8 -*-
"""NDI **真在输出**时的发送端开销（本机 loopback，双进程）。

【为什么以前量不到】
`tools/_output_bench.py` 量到 `NDI send_video = 0.00 ms/帧`，但那是因为**本机没有接收端时
NDI SDK 直接跳过编码**，0.00 只是下限。要拿到真值必须有接收端，而以前本机接收端连不上。

【现在为什么能了】两个坑（都是 2026-09-25 查明的）：
1. cyndilib 的 `Receiver(source_name=...)` **只记名字、不连接**（receiver.pyx 里那段
   `p.source_to_connect_to.p_ndi_name = ...` 被注释掉了）。必须显式 `recv.set_source(src)`
   —— 补上这一步后 `connected=True` 立刻成立。
2. `VideoSendFrame.write_data()` **只接受一维 buffer**（传三维 ndarray 报
   `ValueError: Buffer has wrong number of dimensions`）。项目里引擎传的是
   `np.frombuffer(canvas.bits(), ...)`，本来就是 1 维，所以没踩到。

结构：父进程 = 发送端 + 计时；子进程 = 接收端（避免抢 GIL）。
先量“无接收端”基线，再量“有接收端”，差值就是 NDI 编码+发包的真实成本。

用法：venv\\Scripts\\python.exe tools\\_ndi_send_cost.py [帧数=300]
"""
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes as wt

import numpy as np

W, H = (1920, 1080) if os.environ.get("BIG") else (1280, 720)
FOURCC_BGRA = 0x41524742
NAME = "AutoVJ-NDI-bench"
RECV_SECS = 30.0

k32 = ctypes.windll.kernel32


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wt.DWORD), ("dwHighDateTime", wt.DWORD)]


def _ft(f):
    return (f.dwHighDateTime << 32) | f.dwLowDateTime


k32.GetProcessTimes.argtypes = [wt.HANDLE, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
                               ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME)]
k32.GetCurrentProcess.restype = wt.HANDLE


def proc_cpu():
    c, e, k, u = FILETIME(), FILETIME(), FILETIME(), FILETIME()
    if not k32.GetProcessTimes(k32.GetCurrentProcess(), ctypes.byref(c), ctypes.byref(e),
                               ctypes.byref(k), ctypes.byref(u)):
        return 0.0
    try:
        # 取本进程全部线程的核心态+用户态（含 NDI 内部线程）
        return _ft(k) + _ft(u)
    except Exception:                                           # noqa: BLE001
        return 0.0


# ------------------------------------------------------------------ 子进程：接收端
def run_receiver(secs=RECV_SECS):
    from cyndilib.finder import Finder
    from cyndilib.receiver import Receiver, ReceiveFrameType
    from cyndilib.video_frame import VideoRecvFrame
    try:
        from cyndilib.wrapper import RecvBandwidth
        kw = {"bandwidth": RecvBandwidth.highest}
    except Exception:                                           # noqa: BLE001
        kw = {}

    f = Finder()
    f.open()
    f.wait_for_sources(15000)
    target = None
    for s in f.iter_sources():
        if NAME in str(getattr(s, "name", "") or ""):
            target = s
            break
    if target is None:
        print("RECV no-source", flush=True)
        return
    target.update()

    r = Receiver(**kw)
    rf = VideoRecvFrame()
    r.set_video_frame(rf)
    r.set_source(target)                    # ★ 必须显式调用，否则永远 connected=False
    print("RECV connected=%s" % r.is_connected(), flush=True)

    t0 = time.perf_counter()
    got = 0
    sizes = set()
    # ⚠ VideoRecvFrame 自带环形缓冲：**收到帧必须把数据取走**（fill_p_data / buffer 协议），
    # 否则 can_receive() 很快变 False，receive() 只返回 recv_buffers_full → 只收到几帧就"断流"。
    bufs = {}

    def dest_for(rf):
        n = int(rf.get_buffer_size())
        if n not in bufs:
            bufs[n] = np.empty(n, np.uint8)
        return bufs[n]

    while time.perf_counter() - t0 < secs:
        if r.receive(ReceiveFrameType.recv_video, 200) & ReceiveFrameType.recv_video:
            got += 1
            try:
                rf.fill_p_data(dest_for(rf))
            except Exception:                                   # noqa: BLE001
                try:
                    memoryview(rf)                              # 退路：buffer 协议也算消费
                except Exception:                               # noqa: BLE001
                    pass
            sizes.add((int(rf.xres), int(rf.yres)))
    print("RECV frames=%d/%d sizes=%s connected=%s"
          % (got, int(secs * 60), sorted(sizes), r.is_connected()), flush=True)
    try:
        r.disconnect()
    except Exception:                                           # noqa: BLE001
        pass


# ------------------------------------------------------------------ 父进程
def bench(send, frames, label, log):
    # 预热
    for _ in range(30):
        send()
    time.sleep(0.25)

    c0, t0 = proc_cpu(), time.perf_counter()
    for i in range(frames):
        send()
    wall = time.perf_counter() - t0
    cpu = proc_cpu() - c0
    line = ("  %-26s 墙钟 %6.3f ms/帧   CPU %6.3f ms/帧   60fps 核当量 %5.3f"
            % (label, wall / frames * 1000, cpu / 1e7 / frames * 1000,
               (cpu / 1e7 / frames) * 60))
    print(line, flush=True)
    log.append(line)


def main(frames=300):
    from cyndilib.sender import Sender
    from cyndilib.video_frame import VideoSendFrame

    log = []

    def w(s):
        print(s, flush=True)
        log.append(s)

    w("=" * 86)
    w("NDI 真在输出时的发送端开销（本机 loopback，双进程）   %s" % time.strftime("%H:%M:%S"))
    w("=" * 86)

    if os.environ.get("CANVAS"):
        # 用**真实的 QImage 画布对象**（引擎里就是 QImage.Format_RGB32 + bits() 一维视图），
        # 走一遍完整数据链，确认格式/长度真的能对接 NDI
        from PySide6.QtGui import QImage
        qi = QImage(W, H, QImage.Format_RGB32)
        qi.fill(0xFF204080)
        assert qi.bytesPerLine() == W * 4, "画布有行填充 → NDI 期望长度会对不上"
        flat = np.frombuffer(qi.bits(), dtype=np.uint8, count=qi.bytesPerLine() * H)
        w("画布：真实 QImage Format_RGB32  %dx%d  bpl=%d  bits=%d 字节"
          % (W, H, qi.bytesPerLine(), flat.nbytes))
    else:
        img = np.zeros((H, W, 4), np.uint8)
        img[:, :, 2] = 200
        img[:, :, 3] = 255
        flat = img.reshape(-1)

    if os.environ.get("PROJ"):
        # 用**项目自己的 ndi_out.py** —— 这才是用户实际会走的那条路
        # （引擎把 canvas.bits() 的 np.frombuffer 一维数组交给它）
        sys.path.insert(0, "src")
        from ndi_out import get_ndi
        d = get_ndi(NAME)
        if not d._ensure(W, H):                  # ★ 现在必须给真实分辨率（open 前定死）
            w("★ 项目 NDI 发送端起不来：%s" % d._last_err)
            return
        send = lambda: d.send_video(flat, W, H)
        conn_fn = lambda: (d._sender.get_num_connections(0.0) if d._sender is not None else 0)
        closer = d.close
        w("发送端：**项目 ndi_out.NDIOutput**  (%dx%d BGRA)" % (W, H))
    else:
        snd = Sender(NAME)
        v = VideoSendFrame()
        v.set_resolution(W, H)
        v.set_fourcc(FOURCC_BGRA)
        v.set_frame_rate(60)
        v.set_progressive(True)
        snd.set_video_frame(v)
        snd.open()
        flat = img.reshape(-1)

        def send():
            v.write_data(flat)
            snd.send_video_async()

        conn_fn = lambda: snd.get_num_connections(0.0)
        closer = snd.close
        w("发送端：裸 cyndilib Sender  (%dx%d BGRA)" % (W, H))
    w("")

    w("[基线] 没有接收端时（NDI 会跳过编码，这是下限）")
    bench(send, frames, "无接收端", log)

    w("")
    w("[测真值] 起一个本机接收端（子进程）")
    child = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--recv"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, encoding="utf-8", errors="replace")
    nconn = 0
    t = time.perf_counter()
    while time.perf_counter() - t < 25:
        try:
            nconn = conn_fn()
        except Exception:                                       # noqa: BLE001
            nconn = 0
        if nconn > 0:
            break
        # 光有连接还不够，NDI 需要看到帧才会开始编码
        send()
        time.sleep(0.05)
    print("  连接数 = %d（用时 %.1f 秒）" % (nconn, time.perf_counter() - t), flush=True)
    log.append("  连接数 = %d" % nconn)
    if nconn <= 0:
        w("  ★ 没建立连接 —— 无接收端时 NDI 不编码，量不到真值")
    else:
        time.sleep(1.0)
        bench(send, frames, "有接收端", log)

    closer()
    try:
        out, _ = child.communicate(timeout=RECV_SECS + 20)
        for ln in (out or "").splitlines():
            if ln.startswith("RECV"):
                print("  " + ln, flush=True)
                log.append("  " + ln)
    except Exception:                                           # noqa: BLE001
        child.kill()

    with open("_ndi_send_cost.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(log))
    print("\n→ 已写入 _ndi_send_cost.txt")


if __name__ == "__main__":
    if "--recv" in sys.argv:
        run_receiver()
    else:
        main(int(sys.argv[1]) if len(sys.argv) > 1 else 300)
