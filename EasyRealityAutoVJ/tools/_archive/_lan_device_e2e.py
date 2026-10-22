# -*- coding: utf-8 -*-
"""端到端：真启一个 LanDevice（就是 AutoVJ 用的那个类），看它到底能不能收到。

## 为什么这个测试有决定性

前面用裸 socket 收 → 能收到 814 包。
但 AutoVJ 用的是 `LanDevice` 类，它比裸 socket 多了一层：
  · `SO_REUSEADDR` 共享绑定
  · `_my_ips` 自滤
  · **`peer_hint` 过滤**（用户 config 里存了 192.168.31.96:47811）
  · `snapshot()` 的 `connected` 判据

如果裸 socket 能收、`LanDevice` 收不到 ⇒ 一定是**类内部某个过滤/判据**的问题。

用法：
    venv\\Scripts\\python.exe tools/_lan_device_e2e.py [端口] [秒数] [peer_hint]
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import lan_audio_recv as LR   # noqa: E402
import lan_audio as LA        # noqa: E402

port = int(sys.argv[1]) if len(sys.argv) > 1 else 47811
secs = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0
hint = sys.argv[3] if len(sys.argv) > 3 else ""

print("=" * 74)
print("LanDevice 真身端到端   端口 %d   %.0f 秒  peer_hint=%r" % (port, secs, hint))
print("=" * 74)

dev = LR.LanDevice(port=port, peer_hint=hint)
print("解析后 peer_hint = %r   peer_hint_port = %d" % (dev.peer_hint, dev.peer_hint_port))
print("本机 IP 集合 = %s" % sorted(dev._my_ips))

ok = dev.start()
print("start() -> %s   error=%r" % (ok, dev.error))
if not ok:
    sys.exit(1)

# 等第一包（与 _run_lan 相同的 5 秒门限）
t0 = time.perf_counter()
first = None
while time.perf_counter() - t0 < 5.0:
    if dev.samplerate:
        first = time.perf_counter() - t0
        break
    time.sleep(0.02)

print("\n--- 5 秒握手窗口 ---")
print("  首包到达: %s" % ("%.2f s" % first if first is not None else "❌ 没见过"))
print("  samplerate = %s" % dev.samplerate)

# 再观察
while time.perf_counter() - t0 < secs:
    time.sleep(0.5)
    sn = dev.snapshot()
    print("  t=%.1fs  packets=%d  peer=%s  pps=%.1f  depth=%.1fms  lost=%d  "
          "hint_rejects=%d" %
          (time.perf_counter() - t0, sn["packets"], sn["peer"] or "-", sn["pps"],
           sn["depth_ms"], sn["lost"], sn["hint_rejects"]))

sn = dev.snapshot()
print("\n--- 最终 snapshot ---")
for k, v in sn.items():
    print("  %-14s %s" % (k, v))

print("\n--- 结论 ---")
if sn["packets"] == 0:
    print("❌ LanDevice **一个包都没收到**，但裸 socket 能收到 ⇒")
    print("   问题在 LanDevice 内部（过滤 / 绑定 / 判据），不在网络。")
else:
    print("✅ LanDevice 收到了 %d 包，connected=%s" % (sn["packets"], sn["connected"]))
    if sn["hint_rejects"] > 0:
        print("   ⚠ 有 %d 个包被 peer_hint=%r 挡掉了" % (sn["hint_rejects"], dev.peer_hint))
    if sn["connected"]:
        print("   ⇒ 类本身工作正常。用户现场收不到，只能是**运行环境差异**：")
        print("      · 当时 A 机没在发（实测已证明现在在发）")
        print("      · 或 AutoVJ 那次 bind 到的端口/参数不同")

# 消费测试：模拟 _capture_loop 拉数据
print("\n--- 消费测试（模拟 _capture_loop 拉 1 秒）---")
rec = dev.recorder(samplerate=dev.samplerate or 48000, channels=2)
import numpy as np
got = 0
t1 = time.perf_counter()
while time.perf_counter() - t1 < 2.0:
    d = rec.record(numframes=960)
    if d is None:
        time.sleep(0.005)
        continue
    got += d.shape[0]
    if got and got % (48000 // 2) < 960:
        amp = float(np.abs(d).max())
        print("  已消费 %d 帧  depth=%.1fms  峰值=%.4f" %
              (got, dev.snapshot()["depth_ms"], amp))
print("  2 秒共消费 %d 帧（= %.0fms 音频）" % (got, got / (dev.samplerate or 48000) * 1000))

dev.stop()
print("\n完成。")
