# -*- coding: utf-8 -*-
"""NDI 接收自检 —— 在**另一台电脑**上运行，确认本软件的 NDI 画面能不能收到。

【为什么需要它】
「NDI 能不能用」取决于四件事：两台机器在同一局域网、发送端防火墙放行、发送端真的在发帧、
接收端能显示。用这个工具跑一次就能定位是哪一环出问题，**不需要装 NDI Tools、也不需要装
OBS 插件**。

【它自带 NDI 运行库】
cyndilib 的 wheel 里带完整的 `Processing.NDI.Lib.x64.dll`，打包成 exe 后接收端那台电脑
**什么都不用装**，双击即用。

用法（双击运行 = 自动找源并收 15 秒）：
  NDI接收自检.exe                   自动找名字含 EasyRealityAutoVJ 的源
  NDI接收自检.exe --list            只列出局域网里发现的所有 NDI 源
  NDI接收自检.exe --secs 30         接收 30 秒
  NDI接收自检.exe --name 主机名     按关键字指定要收的源（不区分大小写）

收到画面会存成 `ndi_received.bmp`（双击用系统看图工具就能看）。
"""
import os
import socket
import struct
import sys
import time

DEFAULT_NAME = "EasyRealityAutoVJ"
OUT_BMP = "ndi_received.bmp"


def _p(s=""):
    """打印（GBK 控制台安全：不用 emoji / 特殊符号）"""
    try:
        print(s, flush=True)
    except Exception:                                            # noqa: BLE001
        print(str(s).encode("gbk", "replace").decode("gbk"), flush=True)


def local_ips():
    """本机的局域网 IPv4 地址（用来和发送端比对是否同网段）"""
    out = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in out and not ip.startswith("127."):
                out.append(ip)
    except Exception:                                            # noqa: BLE001
        pass
    if not out:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            out.append(s.getsockname()[0])
            s.close()
        except Exception:                                        # noqa: BLE001
            pass
    return out


def write_bmp32(path, w, h, bgra):
    """写 32 位 BMP。BMP 行序自下而上，而 NDI 给的是自上而下 → 倒着写。

    用 BMP 而不是 PNG，是为了**不引入 Pillow / Qt 依赖**（那会让工具体积翻好几倍）。
    """
    row = w * 4
    size = row * h
    with open(path, "wb") as f:
        f.write(b"BM" + struct.pack("<IHHI", 14 + 40 + size, 0, 0, 54))
        f.write(struct.pack("<IiiHHIIiiII", 40, w, h, 1, 32, 0, size, 2835, 2835, 0, 0))
        for y in range(h - 1, -1, -1):
            f.write(bgra[y * row:(y + 1) * row])


def find_sources(secs=15.0):
    """返回 [(显示名, Source对象)]"""
    from cyndilib.finder import Finder
    f = Finder()
    f.open()
    f.wait_for_sources(int(secs * 1000))
    out = []
    for s in f.iter_sources():
        try:
            s.update()
        except Exception:                                        # noqa: BLE001
            pass
        out.append((str(getattr(s, "name", "") or "?"), s))
    return f, out


def diag_no_source():
    _p("    " + "-" * 62)
    _p("    一个源都没发现。按可能性从高到低逐项排查：")
    _p("")
    _p("    1) 发送端（装了本软件那台）的软件里，打开「输出」面板，")
    _p("       勾上「NDI 输出（音画同步）」。")
    _p("       [注意] 软件版本必须是 2026.09.25.02 或更新 ——")
    _p("              更早的版本 NDI 输出是坏的（开关能开、但一帧都不发）。")
    _p("")
    _p("    2) 发送端防火墙放行（在发送端以管理员身份运行一次即可）：")
    _p('       netsh advfirewall firewall add rule name="NDI" dir=in action=allow protocol=UDP localport=5353')
    _p('       netsh advfirewall firewall add rule name="NDI TCP" dir=in action=allow protocol=TCP localport=5960-5990')
    _p('       netsh advfirewall firewall add rule name="NDI UDP" dir=in action=allow protocol=UDP localport=5960-5990')
    _p("")
    _p("    3) 两台电脑必须在同一局域网、同一网段（IP 前三段相同）。")
    _p("       发送端那台的 Windows 网络类型要设为「专用网络」")
    _p("       （「公用网络」防火墙更严，会挡掉 NDI 的发现多播）。")
    _p("")
    _p("    4) 路由器如果开了「AP 隔离 / 客户端隔离」，设备之间互相看不见，")
    _p("       多播也会被挡 → 在路由器设置里关掉它。")
    _p("")
    _p("    5) 有些交换机 / 无线中继会吃掉 mDNS 多播。若第 4 步都正常，")
    _p("       可改用有线连接再试一次。")


def main():
    secs = 15.0
    want = DEFAULT_NAME
    only_list = False
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--list":
            only_list = True
        elif a == "--secs" and i + 1 < len(argv):
            secs = float(argv[i + 1])
            i += 1
        elif a == "--name" and i + 1 < len(argv):
            want = argv[i + 1]
            i += 1
        i += 1

    _p("=" * 66)
    _p("NDI 接收自检        %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    _p("=" * 66)
    _p("本机名称 : %s" % socket.gethostname())
    _p("本机 IP  : %s" % (", ".join(local_ips()) or "(取不到)"))
    _p("→ 发送端那台电脑的 IP 应该和上面在同一网段（前三段相同）")
    _p("")

    try:
        import cyndilib
        _p("[0] NDI 运行库 : 自带（%s）" % cyndilib.get_ndi_version())
    except Exception as e:                                       # noqa: BLE001
        _p("[0] ★ NDI 运行库加载失败：%s" % str(e)[:120])
        _p("    这个工具自带运行库，正常不会失败 —— 若被杀毒软件拦了，请放行后再试。")
        return
    _p("")

    _p("[1] 搜索局域网里的 NDI 源（最多 15 秒）…")
    try:
        finder, sources = find_sources(15)
    except Exception as e:                                       # noqa: BLE001
        _p("    ★ 搜索失败：%s" % str(e)[:160])
        return
    if not sources:
        _p("    （没有发现任何源）")
        _p("")
        diag_no_source()
        return

    _p("    发现 %d 个源：" % len(sources))
    for k, (nm, _s) in enumerate(sources):
        _p("      %d) %s" % (k + 1, nm))
    if only_list:
        return

    target_name, target = None, None
    for nm, s in sources:
        if want.lower() in nm.lower():
            target_name, target = nm, s
            break
    _p("")
    if target is None:
        _p("    ★ 没找到名字含「%s」的源。" % want)
        _p("      说明发送端那台电脑的「NDI 输出」开关没打开，或者还没开始输出。")
        _p("      （想收上面列的其它源：NDI接收自检.exe --name 关键字）")
        return

    from cyndilib.receiver import Receiver, ReceiveFrameType
    from cyndilib.video_frame import VideoRecvFrame
    try:
        from cyndilib.wrapper import RecvBandwidth, RecvColorFormat
    except Exception:                                            # noqa: BLE001
        RecvBandwidth = RecvColorFormat = None

    _p("[2] 连接：%s" % target_name)
    kw = {}
    if RecvColorFormat is not None:
        kw["color_format"] = RecvColorFormat.BGRX_BGRA     # 发送端是 BGRA，最省转换
    if RecvBandwidth is not None:
        kw["bandwidth"] = RecvBandwidth.highest
    r = Receiver(**kw)
    rf = VideoRecvFrame()
    r.set_video_frame(rf)
    # ★★ 关键：cyndilib 的 Receiver(source_name=...) **只记名字、不发起连接**，
    #    必须显式 set_source() 才会真正调 NDIlib_recv_connect()。
    r.set_source(target)

    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 6:
        if r.is_connected():
            break
        time.sleep(0.1)
    if r.is_connected():
        _p("    已连接 [OK]")
    else:
        _p("    ★ 连不上（connected=False）—— 多半是发送端防火墙没放行。")
        _p("      按下面第 2 条做一次即可。")
        _p("")
        diag_no_source()
        return

    import numpy as np
    _p("")
    _p("[3] 接收 %.0f 秒…" % secs)
    buf = None
    snap = None
    frames = 0
    first_at = None
    sizes = set()
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < secs:
        try:
            got = r.receive(ReceiveFrameType.recv_video, 300)
        except Exception as e:                                   # noqa: BLE001
            _p("    ★ receive() 出错：%s" % str(e)[:120])
            break
        if got & ReceiveFrameType.recv_video:
            frames += 1
            if first_at is None:
                first_at = time.perf_counter() - t0
            sizes.add((int(rf.xres), int(rf.yres)))
            n = int(rf.get_buffer_size())
            if n > 0:
                if buf is None or len(buf) != n:
                    buf = np.empty(n, np.uint8)
                # ⚠ 收到帧必须把数据取走，否则内部环形缓冲填满会停止收帧
                rf.fill_p_data(buf)
                if frames % 10 == 1:
                    snap = buf.copy()
    wall = time.perf_counter() - t0

    _p("")
    _p("[4] 结果")
    _p("    收到帧数 : %d 帧 / %.1f 秒  =  %.1f fps" % (frames, wall, frames / max(wall, 0.01)))
    _p("    画面尺寸 : %s" % (sorted(sizes) if sizes else "（无）"))
    _p("    首帧延迟 : %s" % ("%.2f 秒" % first_at if first_at is not None else "—"))
    _p("")

    if frames > 0 and snap is not None:
        w, h = int(rf.xres), int(rf.yres)
        try:
            write_bmp32(os.path.join(os.getcwd(), OUT_BMP), w, h, bytes(snap))
            _p("    已保存截图：%s" % os.path.join(os.getcwd(), OUT_BMP))
            _p("    双击打开它 —— 看到画面就说明 NDI 完全通了。")
        except Exception as e:                                   # noqa: BLE001
            _p("    （截图保存失败：%s）" % str(e)[:100])
        _p("")
        _p("  结论：**通** —— 这台电脑能正常接收本软件的 NDI 画面。")
    elif frames > 0:
        _p("  结论：收到帧，但取不到像素数据（内部缓冲异常）。")
    else:
        _p("  结论：连上了但一帧都没收到。")
        _p("    · 发送端软件是否**正在播放**（没在播就没有画面可发）？")
        _p("    · 发送端版本是否 2026.09.25.02 或更新？")
        _p("      更早版本的 NDI 输出在非 720p 分辨率下一帧都不发（已修）。")

    try:
        r.disconnect()
    except Exception:                                            # noqa: BLE001
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:                                            # noqa: BLE001
        import traceback
        traceback.print_exc()
    # 双击运行时别让窗口一闪而过
    if getattr(sys, "frozen", False) and sys.stdin is not None:
        try:
            input("\n按回车键关闭窗口…")
        except Exception:                                        # noqa: BLE001
            pass
