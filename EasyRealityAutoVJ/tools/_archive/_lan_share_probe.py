# -*- coding: utf-8 -*-
"""决定性实验：两个进程同时 bind UDP 47811，包**怎么分**？

## 背景（必须理解，否则会误判）

Windows 的 UDP + SO_REUSEADDR 有三种截然不同的行为，取决于**是否设了 SO_REUSEPORT**：

  A. 两个 socket 都设 SO_REUSEADDR，**都不设** SO_REUSEPORT
     ⇒ **只有最后 bind 的那个收到包**（先 bind 的被完全饿死）；
  B. 两个都设 SO_REUSEPORT（Windows 10 1803+ 支持）
     ⇒ 内核**按流/包做哈希分发**，两个都能收到一部分；
  C. 一个设、一个没设 ⇒ bind 直接失败。

本项目的 `make_receiver()` **只设了 SO_REUSEADDR**（见 lan_audio.py:359），
没设 SO_REUSEPORT ⇒ 落在 **A 情形**：谁最后 bind，谁独占收包。

## 这解释了什么

用户现场："A 机在发、B 机（本机）收不到"。
而我在本机跑诊断工具时**能收到 814 个包** —— 因为**当时 AutoVJ 没在运行**，
端口是空的，我后 bind 就我独占。

但如果 AutoVJ 正在运行、且**在它之后**又有什么东西 bind 了同一端口
（比如用户又点了一次「应用」导致重新 `LanDevice`、或另一个残留进程），
那么 **AutoVJ 的 socket 就被彻底饿死** —— 表现为「状态一直显示没收到包」，
且**重置音源也没用**（新 socket 又会被后来的抢）。

## 本脚本做什么

  1. 进程 P1：bind 47811，收 3 秒，报告收包数；
  2. 进程 P2：3 秒后启动，**也** bind 47811 并收 3 秒；
  3. 观察：P1 在 P2 启动后**是否还能收到包**。
     · P1 归零 ⇒ 情形 A（后 bind 独占）⇒ **这就是本 bug 的根因机制**；
     · P1/P2 都有 ⇒ 情形 B（哈希分发）⇒ 机制不同，另找原因。

用法：
    venv\\Scripts\\python.exe tools/_lan_share_probe.py [端口]
"""
import os
import subprocess
import sys
import tempfile

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 47811
HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

WORKER = r'''
import socket, sys, time, threading
sys.path.insert(0, sys.argv[1])
import lan_audio as LA
port = int(sys.argv[2]); tag = sys.argv[3]; dur = float(sys.argv[4])
delay = float(sys.argv[5])
time.sleep(delay)
s = LA.make_receiver(port, "0.0.0.0")
n = 0
t0 = time.perf_counter()
last_report = 0.0
while time.perf_counter() - t0 < dur:
    try:
        buf, addr = s.recvfrom(65536)
        m = LA.unpack(buf)
        if m:
            n += 1
    except BlockingIOError:
        time.sleep(0.002)
        continue
    except Exception:
        break
    el = time.perf_counter() - t0
    if el - last_report >= 0.5:
        last_report = el
        print("[%s] t=%.1fs  收到 %d 包" % (tag, el, n), flush=True)
print("[%s] 总计 %d 包" % (tag, n), flush=True)
'''

wf = os.path.join(tempfile.mkdtemp(prefix="lanshare_"), "w.py")
with open(wf, "w", encoding="utf-8") as f:
    f.write(WORKER)
SRC = os.path.join(HERE, "..", "src")

print("=" * 70)
print("UDP 端口共享行为探针   端口 %d" % PORT)
print("=" * 70)
print("P1 现在开绑，收 6 秒；P2 在 t=3s 才开绑，收 3 秒。")
print("看 P1 在 P2 起来之后是否被饿死。\n")

p1 = subprocess.Popen([PY, wf, SRC, str(PORT), "P1(先绑)", "6", "0"],
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      text=True, encoding="utf-8", errors="replace",
                      bufsize=1)
p2 = subprocess.Popen([PY, wf, SRC, str(PORT), "P2(后绑)", "3", "3"],
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      text=True, encoding="utf-8", errors="replace",
                      bufsize=1)

p1_out, p2_out = [], []
import threading


def rd(p, sink):
    for ln in p.stdout:
        ln = ln.rstrip()
        sink.append(ln)
        print(ln, flush=True)


t1 = threading.Thread(target=rd, args=(p1, p1_out), daemon=True)
t2 = threading.Thread(target=rd, args=(p2, p2_out), daemon=True)
t1.start()
t2.start()
t1.join()
t2.join()
p1.wait()
p2.wait()


def total(lines):
    for ln in reversed(lines):
        if "总计" in ln:
            try:
                return int(ln.split("总计")[1].split("包")[0].strip())
            except Exception:                                    # noqa: BLE001
                pass
    return 0


n1, n2 = total(p1_out), total(p2_out)
print("\n" + "=" * 70)
print("结果：P1(先绑) = %d 包    P2(后绑) = %d 包" % (n1, n2))
if n1 == 0 and n2 > 0:
    print("⇒ ⚠⚠ **情形 A：后 bind 者独占**。先绑的 socket 被完全饿死。")
    print("   这就是「AutoVJ 状态显示没收到包」的根因机制 —— 只要有任何进程在")
    print("   AutoVJ 之后 bind 了 47811，AutoVJ 就永久收不到，且重置音源也难自愈。")
    print("   修法：接收 socket 必须**独占**（不再设 SO_REUSEADDR），")
    print("         bind 失败时明确报错，而不是静默共享。")
elif n1 > 0 and n2 > 0:
    print("⇒ 情形 B：内核哈希分发，两个 socket 各分一部分。")
    print("   视频/音频包会被撕裂到两个 socket ⇒ AutoVJ 只能收到一部分，")
    print("   症状是断续/丢包而非完全没有。修法同样是**独占绑定**。")
elif n1 > 0 and n2 == 0:
    print("⇒ 情形 C：先 bind 者独占，后 bind 者收不到（或 P2 bind 失败）。")
    print("   若是这样，AutoVJ 作为「先绑」应当能收到 ⇒ 问题不在端口共享。")
else:
    print("⇒ 两个都没收到 —— 端口可能被页面上别的进程占着（先关掉再跑）。")
print("=" * 70)
