# -*- coding: utf-8 -*-
"""决定性实验：UDP 47811 现在**到底有几个进程在绑**？AutoVJ 在绑吗？

## 为什么必须做这个

上一轮 `_lan_two_way.py` 说"✅ 收到了 814 个包"，但用户说界面收不到。
两者**都不假** —— Windows 的 `SO_REUSEADDR` 语义下，**多个进程可以同时 bind
同一 UDP 端口**，收到的包会被**分给其中一个**（或都收到，取决于是否独占）。
⇒ "工具能收到" 完全不能推出 "软件能收到"。必须先查清端口归属。

## 判据

  · 若 AutoVJ 进程存在 **且** 端口被绑 ⇒ 软件在收，问题在状态机/消费侧；
  · 若 AutoVJ 进程存在 **但端口没被绑** ⇒ `LanDevice.start()` 没成功，
    或 bind 失败后没重试（`self.error` 会写"无法绑定"）；
  · 若 AutoVJ 进程不存在 ⇒ 用户根本没开接收端界面；
  · 若端口被**别的程序**占（非 AutoVJ）⇒ 冲突，AutoVJ 静默收不到。

用法：
    venv\\Scripts\\python.exe tools/_lan_port_owner.py [端口]
"""
import os
import subprocess
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 47811


def sh(cmd):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=25, encoding="utf-8", errors="replace")
        return (p.stdout or "") + (p.stderr or "")
    except Exception as e:                                       # noqa: BLE001
        return "<<err %s>>" % e


print("=" * 70)
print("UDP %d 端口归属诊断" % PORT)
print("=" * 70)

# ---- 1. 进程 ----
print("\n[1] AutoVJ 进程")
out = sh('tasklist /FI "IMAGENAME eq EasyRealityAutoVJ.exe" /FO CSV')
lines = [l for l in out.splitlines() if "EasyRealityAutoVJ" in l or "PID" in l]
for l in lines:
    print("    " + l.strip())
if not lines or all("No tasks" in l for l in lines):
    print("    (未发现 EasyRealityAutoVJ.exe)")

print("\n[2] python 进程（有没有残留的诊断/测试接收端还在绑端口）")
out2 = sh('tasklist /FI "IMAGENAME eq python.exe" /FO CSV')
for l in out2.splitlines():
    if l.strip().startswith('"python.exe"'):
        print("    " + l.strip())

# ---- 3. 端口归属（netstat -ano，UDP 行 + PID）----
print("\n[3] UDP 端口占用（netstat -ano）")
ns = sh("netstat -ano -p UDP")
found = []
for l in ns.splitlines():
    s = l.strip()
    if not s.startswith("UDP"):
        continue
    parts = s.split()
    if len(parts) >= 4 and parts[1].endswith(":%d" % PORT):
        found.append(parts)
        print("    " + s)
if not found:
    print("    (没有任何进程绑定 UDP %d)" % PORT)

# ---- 4. PID → 进程名 ----
print("\n[4] 占用该端口的进程名")
pids = sorted({p[-1] for p in found})
if not pids:
    print("    (无)")
for pid in pids:
    info = sh('tasklist /FI "PID eq %s" /FO CSV /NH' % pid)
    print("    PID %-8s %s" % (pid, (info or "").strip() or "?"))
    # 命令行（判断是不是 AutoVJ / 诊断脚本）
    w = sh('wmic process where "ProcessId=%s" get CommandLine /value' % pid)
    cl = []
    for ln in w.splitlines():
        ln = ln.strip()
        if ln.startswith("CommandLine="):
            cl.append(ln[len("CommandLine="):])
    for c in cl:
        print("         cmd: %s" % c[:200])

# ---- 5. 结论 ----
print("\n[5] 结论")
has_av = any("EasyRealityAutoVJ" in l for l in lines)
has_bind = bool(found)
if has_av and has_bind:
    print("    ✔ 软件在运行，且 UDP %d 被绑定 ⇒ 接收端**在收包**。" % PORT)
    print("      若界面仍显示没收到，问题在【状态机/消费侧】，不在网络。")
elif has_av and not has_bind:
    print("    ⚠ 软件在运行，但 **UDP %d 没有任何进程绑定** ⇒ `LanDevice.start()`" % PORT)
    print("      没跑起来（或 bind 失败后没重试）。看界面/日志里的 `error`。")
elif not has_av and has_bind:
    print("    ⚠ 软件**没在运行**，但端口被绑着 ⇒ 有残留进程占着端口，")
    print("      会让软件启动时收不到（虽然 SO_REUSEADDR 允许共享，但分发不确定）。")
else:
    print("    ℹ 软件未运行、端口空闲 —— 现在是干净的初始状态。")
    print("      请在 B 机上启动 AutoVJ 并切到局域网音频，再跑本脚本对比。")
print("=" * 70)
