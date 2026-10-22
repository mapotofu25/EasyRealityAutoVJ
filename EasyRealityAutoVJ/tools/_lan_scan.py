# -*- coding: utf-8 -*-
"""局域网音频「有没有源在发」扫描器。

判据不靠猜：**真的绑上 UDP 端口去收包**，并解析自研协议头。

用法：
    venv\\Scripts\\python.exe tools/_lan_scan.py [端口] [秒数]

会做三件事：
  1. 列出本机所有网卡 IPv4 + 广播地址（弄清在哪个网段上听）
  2. 绑定 UDP 端口收 N 秒，打印**每一个**来包的来源 IP、类型、采样率
  3. 结论：有无 DATA 包（= 真有音频在发）、有无 HELLO（= 有发送端但没点开始）
"""
import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import lan_audio as LA   # noqa: E402

TYPE_NAME = {LA.TYPE_DATA: "DATA(音频)", LA.TYPE_HELLO: "HELLO(宣告)",
             LA.TYPE_BYE: "BYE(下线)"}


def list_ifaces():
    """列出本机 IPv4（含虚拟网卡，好让用户看清是不是连错网段了）。"""
    out = []
    try:
        import subprocess
        r = subprocess.run(["ipconfig"], capture_output=True, text=True,
                           encoding="gbk", errors="replace")
        cur = ""
        for line in (r.stdout or "").splitlines():
            s = line.strip()
            if s.endswith(":") and ("适配器" in s or "adapter" in s.lower()):
                cur = s.rstrip(":").strip()
            elif "IPv4" in s or "IP Address" in s:
                ip = s.split(":")[-1].strip()
                if ip:
                    out.append((cur, ip))
    except Exception as e:                                       # noqa: BLE001
        out.append(("(枚举失败)", str(e)))
    return out


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else LA.DEFAULT_PORT
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0

    print("=" * 66)
    print("局域网音频扫描  端口 UDP %d  监听 %.0f 秒" % (port, secs))
    print("=" * 66)

    print("\n[1] 本机网卡 IPv4：")
    for name, ip in list_ifaces():
        tag = ""
        if ip.startswith(("198.18.", "198.19.")):
            tag = "   ← 虚拟网卡（代理 TUN），不是真实局域网"
        elif ip.startswith("169.254."):
            tag = "   ← 未拿到 DHCP 地址"
        print("    %-46s %s%s" % (name[:46], ip, tag))

    # ---- 绑定收包 ----
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        except Exception:                                        # noqa: BLE001
            pass
        # ⚠ 必须绑 0.0.0.0 而不是某个网卡地址 —— 发送端可能用广播，
        #   绑具体地址会漏掉（广播包的本地投递路径与单播不同）。
        s.bind(("0.0.0.0", port))
        s.settimeout(0.5)
    except Exception as e:                                       # noqa: BLE001
        print("\n✗ 绑定 UDP %d 失败：%s" % (port, e))
        print("  ⇒ 端口可能被别的程序占着。换一个端口试试，或先关掉占用它的程序。")
        return 1

    print("\n[2] 开始监听（端口已绑好，等包…）")
    print("    ⚠ 注意：若**这台电脑上也跑着发送端**，同端口下广播不会回环给本进程；")
    print("      想同机自测请让发送端改发到本扫描器的端口（见文件末尾说明）。")
    t0 = time.time()
    seen_peer = {}
    n_total = 0
    n_data = 0
    n_hello = 0
    n_bye = 0
    n_bad = 0
    first_pkt = None

    while time.time() - t0 < secs:
        try:
            buf, addr = s.recvfrom(65536)
        except socket.timeout:
            continue
        except Exception:                                        # noqa: BLE001
            break
        n_total += 1
        m = LA.unpack(buf)
        if not m:
            n_bad += 1
            # 不是我们的包也要报一次来源（可能是别的程序用了这个端口）
            if n_bad <= 5:
                print("    [非本协议包] 来自 %s:%d  长度 %d 字节  头=%r"
                      % (addr[0], addr[1], len(buf), buf[:12]))
            continue
        key = "%s:%d" % (addr[0], addr[1])
        if key not in seen_peer:
            seen_peer[key] = {"data": 0, "hello": 0, "bye": 0, "sr": 0, "first": time.time()}
            print("    ★ 发现发送端  %s   类型 %s  源采样率 %s"
                  % (key, TYPE_NAME.get(m["type"], m["type"]),
                     m["samplerate"] or "-"))
        p = seen_peer[key]
        if m["type"] == LA.TYPE_DATA:
            n_data += 1
            p["data"] += 1
            p["sr"] = m["samplerate"]
            if first_pkt is None:
                first_pkt = m
        elif m["type"] == LA.TYPE_HELLO:
            n_hello += 1
            p["hello"] += 1
        elif m["type"] == LA.TYPE_BYE:
            n_bye += 1
            p["bye"] += 1

    s.close()
    dt = time.time() - t0

    print("\n[3] 结果")
    print("    收到包总数 %d（其中协议外 %d）" % (n_total, n_bad))
    print("    DATA(音频) %d   HELLO(宣告) %d   BYE(下线) %d" % (n_data, n_hello, n_bye))
    if first_pkt:
        print("    首个音频包：%d 声道 / %d Hz / %d 帧"
              % (first_pkt["channels"], first_pkt["samplerate"], first_pkt["nframes"]))
    if n_data:
        print("    实测音频包速率 ≈ %.1f pps（理想 ~100）" % (n_data / dt))

    print("\n[4] 结论")
    if n_data > 0:
        print("    ✅ 扫到了正在发送的音频源：")
        for k, v in seen_peer.items():
            print("       %s   音频包 %d   采样率 %s" % (k, v["data"], v["sr"] or "-"))
        print("\n    ⇒ 在 ERAVJ 音源里选「局域网音频」，端口填 %d 即可。" % port)
        print("      ⚠ 若 ERAVJ 里填了对端 IP，请把它清空（留空=广播，最省事）。")
    elif n_hello or n_bye:
        print("    ⚠ 扫到发送端在**宣告**，但没有音频数据：")
        for k, v in seen_peer.items():
            print("       %s   HELLO %d  BYE %d  DATA %d" % (k, v["hello"], v["bye"], v["data"]))
        print("    ⇒ 对端软件开了，但**没有点「开始发送」**（或采集设备没选对）。")
    else:
        print("    ❌ 完全没扫到任何源。可能原因（按概率排序）：")
        print("       ① 另一台电脑的发送端**没在运行**，或没点「开始发送」")
        print("       ② 两台电脑**不在同一网段**（对比上面 [1] 里本机 IP")
        print("          与对端界面上显示的 IP：前三段应当一样，如都是 192.168.31.x）")
        print("       ③ **防火墙拦了 UDP %d**（发送端所在机器 / 本机都要放行）" % port)
        print("       ④ 发送端用的是**点对点**模式发给了别的 IP ⇒ 让它留空用广播")
        print("       ⑤ 路由器开了 **AP 隔离 / 客户端隔离**（访客 WiFi 常有）")
        print("       ⑥ 端口不一致（对端改了端口）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
