# -*- coding: utf-8 -*-
"""连续采样 60 秒：A 机的包到底是不是**连续**的？

用户报告：「软件没打开时显示已握手，打开后又显示没收到包」。

若 A 机的发送**时断时续**（例如采集线程在重连、或音频静音时停发），
就会出现：
  · 刚打开时赶上 A 机正在发 ⇒ 秒握手，显示正常；
  · 过一会儿 A 机停了 ⇒ 显示"没收到包"。

本脚本每秒统计一次收包数，给出连续性画像。

用法：
    venv\\Scripts\\python.exe tools/_lan_continuity.py [端口] [秒数]
"""
import os
import socket
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import lan_audio as LA   # noqa: E402

port = int(sys.argv[1]) if len(sys.argv) > 1 else 47811
secs = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0

print("=" * 74)
print("A 机发包连续性采样   端口 %d   %.0f 秒" % (port, secs))
print("=" * 74)

s = LA.make_receiver(port, "0.0.0.0")
print("bind OK\n")

from collections import Counter
buckets = []
cur = Counter()
t_start = time.perf_counter()
last = t_start

while time.perf_counter() - t_start < secs:
    try:
        buf, addr = s.recvfrom(65536)
    except BlockingIOError:
        time.sleep(0.001)
        continue
    except Exception as e:                                       # noqa: BLE001
        print("recv 错:", e)
        break
    m = LA.unpack(buf)
    if m:
        nm = {LA.TYPE_DATA: "DATA", LA.TYPE_HELLO: "HELLO",
              LA.TYPE_BYE: "BYE"}.get(m["type"], "?")
        cur[(addr[0], nm)] += 1
    now = time.perf_counter()
    if now - last >= 1.0:
        el = now - t_start
        d = cur.get(("192.168.31.96", "DATA"), 0)
        h = cur.get(("192.168.31.96", "HELLO"), 0)
        tot = sum(cur.values())
        other = {k: v for k, v in cur.items() if k[0] != "192.168.31.96"}
        bar = "#" * min(60, d // 2)
        print("  t=%5.1fs  DATA=%4d  HELLO=%2d  总=%4d  %s%s" %
              (el, d, h, tot, bar.split("#")[0] and "" or "", bar))
        if other:
            print("           其它来源: %s" % other)
        buckets.append((el, d, tot))
        cur = Counter()
        last = now

s.close()

print("\n--- 画像 ---")
if buckets:
    datas = [b[1] for b in buckets]
    zero = sum(1 for d in datas if d == 0)
    avg = sum(datas) / len(datas)
    print("  每秒 DATA 包数: 均值 %.1f  最小 %d  最大 %d" % (avg, min(datas), max(datas)))
    print("  零包秒数: %d / %d (%.0f%%)" % (zero, len(datas), zero / len(datas) * 100))
    if zero > len(datas) * 0.2:
        print("  ⚠⚠ A 机发送**时断时续** ⇒ 这正是「一会儿正常一会儿收不到」的原因")
    elif avg > 80:
        print("  ✅ A 机发送**连续稳定**（~100 包/秒 = 10ms 包长）")
    else:
        print("  ⚠ 发包率偏低（%.1f/s），可能 A 机没在放音乐 / 采集有问题" % avg)
