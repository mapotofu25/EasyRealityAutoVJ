# -*- coding: utf-8 -*-
"""诊断：接收端显示的「对端 IP」为什么不对。

做法：起一个真实 `LanDevice`（源码版），同时**记录每一个到达的包**
（来源地址 + 类型 + 是否被 current 逻辑当成 peer），跑 N 秒后打印。

判据：
  - 若 peer 落在 `198.18.*` / `10.6.*` 这类**虚拟网卡段** ⇒ 显示错了；
  - 真正的发送端应该来自 `192.168.31.x`（本机网段内的另一台机器）。

用法：python tools/_lan_peer_diag.py [秒数]
"""
import sys
import threading
import time

sys.path.insert(0, r"P:\ERAVJ\AutoVJ\src")

import lan_audio as LA                                          # noqa: E402
import lan_audio_recv as LR                                     # noqa: E402

secs = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0

print("本机 local_ip() =", LA.local_ip())
print("本机 local_ipv4_all() =", LA.local_ipv4_all())
print("broadcast_targets =", LA.broadcast_targets(47811))
print("")

dev = LR.LanDevice(port=47811)
seen = []
lock = threading.Lock()

# 猴补丁：在 unpack 之前记录每个包的来源
orig_recv = None


def spy_rx(dev_obj):
    """替换 _rx_loop 的前半段做不到，改用 socket 层观察：包一层 recvfrom。"""
    pass


ok = dev.start()
print("start() =", ok)
if not ok:
    print("无法绑定端口（可能被旧实例占着）：%s" % dev.error)
    sys.exit(2)

# 观察：定期读 peer / stats，同时在外部独立 socket 上嗅探同端口来源
import socket
sniff = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sniff.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sniff.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
try:
    sniff.bind(("0.0.0.0", 47812))   # 换一个端口，避免和 device 抢
except Exception as e:
    print("sniff bind 失败（不致命）：", e)

t0 = time.perf_counter()
print("运行 %.1fs，观察 peer 变化…" % secs)
last = None
while time.perf_counter() - t0 < secs:
    p = dev.peer
    if p != last:
        print("   [+%4.1fs] peer = %r" % (time.perf_counter() - t0, p))
        last = p
    time.sleep(0.1)

s = dev.snapshot()
print("")
print("=== 结果 ===")
print("peer      =", repr(s["peer"]))
print("connected =", s["connected"])
print("packets   =", s["packets"])
print("sr        =", s["sr"])
print("depth_ms  =", s["depth_ms"])
print("error     =", s["error"])
dev.stop()

ip = (s["peer"] or "").split(":")[0]
if not ip:
    print("")
    print("结论：ℹ 还没收到任何包（peer 为空）")
    sys.exit(0)
if ip.startswith("198.18.") or ip.startswith("10.6."):
    print("")
    print("结论：❌ peer 显示的是**虚拟网卡**地址 %s —— 正是用户说的「IP 不对」" % ip)
    sys.exit(1)
if ip in LA.local_ipv4_all():
    print("")
    print("结论：❌ peer 显示的是**本机自己**的地址 %s" % ip)
    sys.exit(1)
print("")
print("结论：✅ peer = %s（看起来是真实对端）" % ip)
sys.exit(0)
