# -*- coding: utf-8 -*-
"""NDI 本机 loopback 验证：同一个进程里「自己发、自己收」。

【为什么之前一直失败】
2026-09-25 之前认定「NDI 的『被接收』这条路走不通」（Receiver 一直 connected=False、
0 帧），真实原因是 **cyndilib 的 `Receiver(source_name=...)` 只把名字记下来，从不发起连接**：
`receiver.pyx` 的 `build_create_p()` 里那段
    # if len(src_name_py): p.source_to_connect_to.p_ndi_name = src_name_c
**是被注释掉的**，而 `__init__` 里 `self.source = source` 也只是存引用。
真正建立连接的只有 `set_source(src)` / `connect_to(src)` —— 内部调 `NDIlib_recv_connect()`。
所以正确顺序是：Finder 发现 → 拿 Source 对象 → `src.update()` → `recv.set_source(src)`。

【这一步能验证什么】
· 本机能否自发自收（不需要第二台机器、不需要装 NDI Tools）
· 真被接收时发送端的开销（NDI 在没有接收端时直接跳过编码，所以以前量到的 0.00ms 只是下限）

用法：venv\\Scripts\\python.exe tools\\_ndi_loopback_check.py [秒数=10]
"""
import sys
import threading
import time

import numpy as np

NAME = "AutoVJ-LoopbackCheck"
W, H = 1280, 720
FOURCC_BGRA = 0x41524742          # 'BGRA' little-endian，与 ndi_out.py 一致


def main(secs=10.0):
    from cyndilib.finder import Finder
    from cyndilib.receiver import Receiver, ReceiveFrameType
    from cyndilib.sender import Sender
    from cyndilib.video_frame import VideoRecvFrame, VideoSendFrame
    try:
        from cyndilib.wrapper import RecvBandwidth
    except Exception:                                            # noqa: BLE001
        RecvBandwidth = None

    log = []

    def w(s):
        print(s, flush=True)
        log.append(s)

    w("=" * 78)
    w("NDI 本机 loopback 验证")
    w("=" * 78)

    # ---------------------------------------------------------- 1) 发送端
    snd = Sender(NAME)
    v = VideoSendFrame()
    v.set_resolution(W, H)
    v.set_fourcc(FOURCC_BGRA)
    v.set_frame_rate(60)
    v.set_progressive(True)
    snd.set_video_frame(v)
    snd.open()
    w("[1] 发送端已打开：%s" % NAME)

    img = np.zeros((H, W, 4), np.uint8)
    img[:, :, 0] = 40
    img[:, :, 1] = 40
    img[:, :, 2] = 200
    img[:, :, 3] = 255
    stop = threading.Event()
    sent = [0]
    errs = []

    # 先同步试发一帧，把异常直接暴露出来（异步线程里报错会被静默吞掉）
    time.sleep(0.6)
    try:
        v.write_data(img.reshape(-1))          # ★ write_data 只收一维 buffer
        r = snd.send_video_async()
        w("    试发一帧：write_data OK → send_video_async=%s" % r)
    except Exception as e:                                      # noqa: BLE001
        w("    ★ 试发失败：%s: %s" % (type(e).__name__, str(e)[:220]))

    def feed():
        i = 0
        while not stop.is_set():
            img[:, :8, 0] = (i * 4) & 0xFF          # 滚动色块 = 画面是"活"的
            try:
                v.write_data(img.reshape(-1))
                snd.send_video_async()
            except Exception as e:                              # noqa: BLE001
                errs.append("%s: %s" % (type(e).__name__, str(e)[:200]))
                break
            i += 1
            sent[0] = i
            time.sleep(1.0 / 60)

    threading.Thread(target=feed, daemon=True).start()
    time.sleep(1.2)
    w("    后台发帧中（已发 %d 帧）" % sent[0])

    # ---------------------------------------------------------- 2) 发现
    f = Finder()
    f.open()
    f.wait_for_sources(15000)
    names = f.get_source_names()
    w("[2] Finder 发现 %d 个源：%s" % (len(names), names))

    target = None
    for s in f.iter_sources():
        nm = str(getattr(s, "name", "") or "")
        if NAME in nm:
            target = s
            break
    if target is None:
        w("    ★ 没发现自己的源 —— 到此终止")
        stop.set()
        snd.close()
        return log

    ok = bool(target.update())
    w("    选中：%s   update=%s   valid=%s"
      % (str(getattr(target, "name", "?")), ok, getattr(target, "valid", "?")))

    # ---------------------------------------------------------- 3) 接收端
    recv = Receiver(bandwidth=RecvBandwidth.highest) if RecvBandwidth else Receiver()
    rf = VideoRecvFrame()
    recv.set_video_frame(rf)
    if hasattr(recv, "set_source"):
        recv.set_source(target)                 # ★ 唯一真正建立连接的调用
    else:                                       # 旧版没有 set_source 就退回 connect_to
        recv.connect_to(target)
    time.sleep(0.5)
    w("[3] 已 set_source，connected=%s" % recv.is_connected())

    # ---------------------------------------------------------- 4) 收帧
    t0 = time.perf_counter()
    frames = 0
    first_at = None
    sizes = set()
    while time.perf_counter() - t0 < secs:
        try:
            r = recv.receive(ReceiveFrameType.recv_video, 500)
        except Exception as e:                                  # noqa: BLE001
            w("    receive() 抛异常：%s" % str(e)[:100])
            break
        if r & ReceiveFrameType.recv_video:
            frames += 1
            if first_at is None:
                first_at = time.perf_counter() - t0
            sizes.add((int(rf.xres), int(rf.yres)))
        if frames == 0 and time.perf_counter() - t0 > 6 and not recv.is_connected():
            w("    6 秒仍未连上（connected=False）")

    wall = time.perf_counter() - t0
    w("")
    w("[4] 结果：收到 %d 帧 / %.1f 秒   首帧 %.2f 秒   画面尺寸 %s"
      % (frames, wall, first_at if first_at is not None else -1, sorted(sizes)))
    w("    最终 connected=%s   发送端累计发出 %d 帧" % (recv.is_connected(), sent[0]))
    if errs:
        w("    ★ 发帧线程异常：%s" % errs[0])
    w("")
    if frames > 0:
        w("  ✅ 本机 loopback **通了** —— NDI 的输出链路可以在本地验证，不需要第二台机器。")
    else:
        w("  ★ 仍未收到帧。排查方向：① NDI 防火墙/访问权限 ② 发送端是否真在发 "
          "(sent>0 说明在发) ③ 本机 loopback 是否被 NDI 运行时禁止。")

    stop.set()
    time.sleep(0.3)
    try:
        recv.disconnect()
    except Exception:                                           # noqa: BLE001
        pass
    try:
        snd.close()
    except Exception:                                           # noqa: BLE001
        pass
    try:
        f.close()
    except Exception:                                           # noqa: BLE001
        pass

    with open("_ndi_loopback_check.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(log))
    print("\n→ 已写入 _ndi_loopback_check.txt")
    return log


if __name__ == "__main__":
    n = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
    main(n)
