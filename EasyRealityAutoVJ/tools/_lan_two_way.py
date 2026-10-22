# -*- coding: utf-8 -*-
"""局域网音频「收不到」双向诊断 —— 一次跑完，直接给出哪一环断了。

## 为什么需要它

现场「A 机在发、B 机收不到」的原因有 6+ 种，肉眼分不出。本工具把
**每一条可能的断点分别验证一遍**，用实测数据定位，而不是猜。

## 做哪几件事

  [1] 本机网卡 / 网段（是否和对端同段）
  [2] 收：绑 UDP 47811 听 N 秒 —— 统计 DATA / HELLO 的来源 IP
      ★ SETUP 阶段跑 —— 此时 A 机若正常，应当能听到 HELLO（每秒 1 个）
  [3] 发：向广播 + 网段定向广播 各发 3 个 HELLO —— 看 sendto 是否报错
      （SO_BROADCAST 缺失会静默失败，正是"看起来在发、实际没出去"）
  [4] 回环自检：本机同时收自己发的包（验证 socket 配置本身没问题）
  [5] 结论：按证据给结论，并指出**下一步该在 A 机做什么**

用法：
    venv\\Scripts\\python.exe tools/_lan_two_way.py [端口] [监听秒数]
"""
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import lan_audio as LA   # noqa: E402

TYPE_NAME = {LA.TYPE_DATA: "DATA(音频)", LA.TYPE_HELLO: "HELLO(宣告)",
             LA.TYPE_BYE: "BYE(下线)"}


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else LA.DEFAULT_PORT
    listen_s = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0

    print("=" * 68)
    print("局域网音频「收不到」双向诊断   端口 UDP %d   监听 %.0f 秒" % (port, listen_s))
    print("=" * 68)

    # ---- [1] 网卡 ----
    print("\n[1] 本机网段")
    my = LA.local_ip()
    print("    推荐 IP（界面会给对端填的）: %s" % (my or "(无)"))
    for ip in LA.local_ipv4_all():
        tag = "  ← 虚拟/回环" if (ip.startswith(("198.18.", "198.19.", "127.", "169.254."))
                                 or ip == "10.6.22.1") else ""
        print("      · %s%s" % (ip, tag))
    print("    广播目标: %s" % (LA.broadcast_targets(port),))

    # ---- [2] 收 ----
    print("\n[2] 监听：看有没有包进来（能听到 HELLO 就说明链路通、只是没点开始）")
    try:
        rx = LA.make_receiver(port, "0.0.0.0")
    except Exception as e:
        print("    ✗ 绑定 UDP %d 失败：%s" % (port, e))
        print("      ⇒ 端口被占用（本机另开了接收端/AutoVJ？）或权限不足")
        return 1
    got = []
    stop = threading.Event()

    def rx_loop():
        while not stop.is_set():
            try:
                buf, addr = rx.recvfrom(65536)
            except BlockingIOError:
                time.sleep(0.002)
                continue
            except Exception:
                return
            m = LA.unpack(buf)
            if m:
                got.append((addr[0], TYPE_NAME.get(m["type"], "?"), m["samplerate"]))

    t = threading.Thread(target=rx_loop, daemon=True)
    t.start()

    # ---- [3] 发（广播）----
    print("\n[3] 尝试发送（广播 + 网段定向）—— 看 sendto 是否报错")
    try:
        tx = LA.make_sender(port, "0.0.0.0")
    except Exception as e:
        print("    ✗ 建发送 socket 失败：%s" % e)
        tx = None
    send_errs = []
    if tx is not None:
        pkt = LA.pack(LA.TYPE_HELLO, 0, 2, 48000, 0, LA.qpc_now(), b"")
        ok_targets = []
        for a in LA.broadcast_targets(port):
            try:
                tx.sendto(pkt, a)
                ok_targets.append(a)
            except Exception as e:                              # noqa: BLE001
                send_errs.append("%s: %s" % (a, e))
        # 定向单播到每个网段的 .255 之外也试一个 —— 顺便探本机其它设备
        try:
            tx.sendto(pkt, ("192.168.31.255", port))
        except Exception:
            pass
        print("    ✓ 成功发出到: %s" % (ok_targets,))
        if send_errs:
            print("    ✗ 发送失败的目标:")
            for e in send_errs:
                print("        %s" % e)
            print("      ⇒ ⚠⚠ `255.255.255.255` 失败通常意味着缺 SO_BROADCAST，")
            print("         会让「看起来在宣告、实际一个包都没出去」（本项目踩过）")

    # ---- [4] 回环自检 ----
    print("\n[4] 等 %.0f 秒收包…" % listen_s)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < listen_s:
        time.sleep(0.2)
        el = time.perf_counter() - t0
        if el > 0 and abs(el - round(el)) < 0.15 and int(round(el)) % 2 == 0:
            print("      …已等 %d 秒，收到 %d 个包" % (int(round(el)), len(got)))
    stop.set()
    time.sleep(0.1)

    # 本机回环：我们自己的广播**不会**回到本进程（同端口），
    # 但可以用另一个端口验证 socket 本身能发能收
    loop_ok = _loopback_selftest(port + 1)
    print("    回环自检（另一端口自发自收）: %s" % ("✓ 正常" if loop_ok else "✗ 失败"))

    try:
        rx.close()
    except Exception:
        pass
    if tx is not None:
        try:
            tx.close()
        except Exception:
            pass

    # ---- [5] 结果 ----
    print("\n[5] 收包结果")
    print("    共收到 %d 个包" % len(got))
    from collections import Counter
    srcs = Counter(g[0] for g in got)
    kinds = Counter(g[1] for g in got)
    for ip, c in srcs.most_common():
        print("      来源 %-16s %d 包" % (ip, c))
    for k, c in kinds.most_common():
        print("      类型 %-14s %d 包" % (k, c))

    print("\n[6] 结论")
    if not got:
        print("    ❌ 一个包都没收到 ⇒ **包根本没到达本机**（不是解析问题）。")
        print("       下一步请在 A 机（发送端）上确认：")
        print("         · 发送端界面的『目标地址』栏 —— 若填过具体 IP，**清空**改回广播；")
        print("           发送端一旦「学到过」某个地址就只发那一台，再也不广播。")
        print("         · 发送端显示的『模式』应是『广播』；若显示『点对点/指定地址』")
        print("           而那个地址不是 %s，就是发错地方了。" % (my or "本机IP"))
        print("         · A 机防火墙放行 UDP %d（**入站+出站**都要）。" % port)
        print("         · 两台机前三段 IP 相同（本机是 %s）。" % (my or "?"))
        print("         · 路由器是否开了 AP 隔离 / 客户端隔离（访客 WiFi）。")
    else:
        if kinds.get("DATA(音频)"):
            print("    ✅ 收到了音频 DATA —— 链路是通的！若界面仍报错，是软件状态问题，")
            print("       请把这段输出发我。")
        elif kinds.get("HELLO(宣告)"):
            print("    ⚠ 只收到 HELLO、没收到 DATA ⇒ A 机在宣告但**没有推音频**：")
            print("       在 A 机上点『开始发送』（或检查采集设备是否选到有声的）。")
        else:
            print("    ⚠ 收到了协议外的包（来源 %s）—— 可能是别的程序占用端口。" % list(srcs))
    return 0


def _loopback_selftest(port):
    """自发自收（另一端口），验证 socket 本身没问题。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.settimeout(1.0)
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        c.sendto(b"ping", ("127.0.0.1", port))
        d, _ = s.recvfrom(64)
        c.close()
        s.close()
        return d == b"ping"
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())
