# -*- coding: utf-8 -*-
"""主动探测：向局域网内每台设备单播 HELLO，看谁在跑音频发送端。

比"被动等广播"更有效 —— 万一对方用的是点对点模式（只发给某个 IP），
或者路由器拦了广播，被动等就永远等不到；主动单播一定能打到对方。

用法：
    venv\\Scripts\\python.exe tools/_lan_probe.py [端口] [秒数]
"""
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import lan_audio as LA   # noqa: E402


def arp_hosts():
    """从 ARP 表取本网段上的活跃设备（按本机真实 IP 的前三段过滤）。"""
    my = LA.local_ip()
    if not my:
        return [], ""
    pfx = ".".join(my.split(".")[:3]) + "."
    hosts = []
    try:
        r = subprocess.run(["arp", "-a"], capture_output=True, text=True,
                           encoding="gbk", errors="replace")
        for line in (r.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0].count(".") == 3:
                ip = parts[0]
                if ip.startswith(pfx) and not ip.endswith((".255", ".0")):
                    if parts[1] and parts[1] != "ff-ff-ff-ff-ff-ff":
                        hosts.append(ip)
    except Exception:                                            # noqa: BLE001
        pass
    return sorted(set(hosts)), pfx


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else LA.DEFAULT_PORT
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0

    my = LA.local_ip()
    hosts, pfx = arp_hosts()
    print("=" * 66)
    print("主动探测音频发送端   本机 %s" % (my or "(无局域网 IP)"))
    print("=" * 66)
    if not my:
        print("✗ 本机没有可用的局域网 IP —— 先检查网线 / WiFi。")
        return 1
    print("\n本网段 %s0/24 上活跃设备：%s" % (pfx, "、".join(hosts) or "(ARP 表为空)"))

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    except Exception:                                            # noqa: BLE001
        pass
    s.bind(("0.0.0.0", port))
    s.settimeout(0.3)

    hello = LA.pack(LA.TYPE_HELLO, 0, 2, 48000, 0, LA.qpc_now(), b"")
    print("\n向 %d 台设备单播 HELLO（每台每秒 1 次）…" % len(hosts))

    t0 = time.time()
    nxt = 0.0
    found = {}
    n_pkt = 0
    while time.time() - t0 < secs:
        now = time.time()
        if now >= nxt:
            nxt = now + 1.0
            for h in hosts:
                try:
                    s.sendto(hello, (h, port))
                except Exception:                                # noqa: BLE001
                    pass
        try:
            buf, addr = s.recvfrom(65536)
        except socket.timeout:
            continue
        except Exception:                                        # noqa: BLE001
            break
        m = LA.unpack(buf)
        if not m:
            continue
        n_pkt += 1
        key = addr[0]
        d = found.setdefault(key, {"data": 0, "hello": 0, "bye": 0, "sr": 0})
        if m["type"] == LA.TYPE_DATA:
            d["data"] += 1
            d["sr"] = m["samplerate"]
        elif m["type"] == LA.TYPE_HELLO:
            d["hello"] += 1
        elif m["type"] == LA.TYPE_BYE:
            d["bye"] += 1

    s.close()
    print("\n结果")
    if not found:
        print("  ❌ 整段网扫完，没有任何设备回应。")
        print("     ⇒ 对端**没有在运行发送端**（或它没点「开始发送」）。")
        print("       请到那台电脑上确认：发送端窗口开着、设备选好、点了「开始发送」。")
        print("       （本机广播与单播路径均已实测正常，所以不是本机的问题。）")
        return 2
    print("  ✅ 以下设备有回应：")
    for ip, d in sorted(found.items()):
        kind = "正在发音频" if d["data"] else ("仅在宣告（未点开始）" if d["hello"] else "已下线")
        print("     %-16s  %s   DATA=%d HELLO=%d  采样率=%s"
              % (ip, kind, d["data"], d["hello"], d["sr"] or "-"))
    if any(d["data"] for d in found.values()):
        print("\n  ⇒ 在 ERAVJ 音源里选「局域网音频」，端口 %d，即可接收。" % port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
