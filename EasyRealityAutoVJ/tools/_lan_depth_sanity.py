# -*- coding: utf-8 -*-
"""焦点测试：LanDevice 的缓冲深度为什么会**恒定顶在 210ms**（MAX_SEC 之上）？

## 现象（上一次 e2e 实测）

    depth_ms = 210.0  ← **恒定不变**，从头到尾一格没动
    而 MIN=20ms / TARGET=60ms / MAX=200ms

210ms 比 MAX_SEC(200ms) 还大 ⇒ `_update_depth` 的兜底阈值是 200ms，
但它砍完立刻又涨回去了，所以**观测值永远停在砍之前的深度**。

## 关键疑问

`_update_depth()` 里的砍法是：
    cap = int(MAX_SEC * sr)          # 200ms * 48000 = 9600 帧
    if depth > cap:
        self._rec._buf = self._rec._buf[depth - cap:]   # 只留最后 cap 帧
        self.stats.trimmed += 1

如果 `trimmed` 一直在涨 ⇒ 说明砍完马上又涨回来 ⇒ **入不敷出**。
根本原因：**`record()` 从没被调用**（e2e 的观察阶段就没调），
所以唯一在动的是 feed() 往缓冲里灌。

⇒ 这个"210ms 恒定"其实在**观察阶段是正常的**（没人消费，当然一直涨到上限）。

## 真正要测的是：**消费起来之后，深度能不能稳在 60ms**。

本次做两件事：
  A. 复现"不消费 ⇒ 顶满 210ms"（有界，不会无限涨）——验证兜底有效；
  B. 起一个**真实消费线程**（模拟 _capture_loop 的节奏）跑 10 秒，
     看深度是否收敛到 TARGET_SEC=60ms 附近、trimmed 是否疯涨。

用法：
    venv\\Scripts\\python.exe tools/_lan_depth_sanity.py [端口] [秒数]
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import lan_audio_recv as LR   # noqa: E402

port = int(sys.argv[1]) if len(sys.argv) > 1 else 47811
secs = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0

print("=" * 74)
print("LanDevice 缓冲深度收敛测试   端口 %d   %.0f 秒" % (port, secs))
print("=" * 74)
print("TARGET_SEC = %.3f s   MIN = %.3f   MAX = %.3f" %
      (LR.TARGET_SEC, LR.MIN_SEC, LR.MAX_SEC))

dev = LR.LanDevice(port=port)
if not dev.start():
    print("start 失败:", dev.error)
    sys.exit(1)

# 等首包
t0 = time.perf_counter()
while time.perf_counter() - t0 < 5.0 and not dev.samplerate:
    time.sleep(0.02)
if not dev.samplerate:
    print("❌ 5 秒内没收到首包")
    dev.stop()
    sys.exit(1)
print("首包 OK  sr=%d" % dev.samplerate)

rec = dev.recorder(samplerate=dev.samplerate, channels=2)

# ---- A：不消费 2 秒，验证兜底有界 ----
print("\n[A] 不消费 2 秒（验证兜底有界，不会无限涨）")
time.sleep(2.0)
d_a = dev.snapshot()
print("    不消费 end: depth=%.1fms  trimmed=%d  packets=%d" %
      (d_a["depth_ms"], dev.stats.trimmed, d_a["packets"]))

# ---- B：起真实消费线程 ----
print("\n[B] 起消费线程，模拟 _capture_loop（每块 960 帧 / 20ms，缺数据就 sleep 5ms）")
running = threading.Event()
running.set()
stats = {"frames": 0, "none": 0, "max_sleep": 0.0}


def consumer():
    while running.is_set():
        t = time.perf_counter()
        d = rec.record(numframes=960)
        dt = time.perf_counter() - t
        if dt > stats["max_sleep"]:
            stats["max_sleep"] = dt
        if d is None:
            stats["none"] += 1
            time.sleep(0.005)
            continue
        stats["frames"] += d.shape[0]


th = threading.Thread(target=consumer, daemon=True)
th.start()

t1 = time.perf_counter()
last = 0.0
rows = []
while time.perf_counter() - t1 < secs:
    time.sleep(0.5)
    el = time.perf_counter() - t1
    sn = dev.snapshot()
    rows.append((el, sn["depth_ms"], sn["trimmed"] if "trimmed" in sn else dev.stats.trimmed,
                 sn["packets"], sn["jitter_ms"]))
    if el - last >= 1.0:
        last = el
        print("    t=%4.1fs depth=%6.1fms  trimmed=%4d  packets=%5d  jitter=%5.1fms  "
              "消费帧=%d" % (el, sn["depth_ms"], dev.stats.trimmed, sn["packets"],
                            sn["jitter_ms"], stats["frames"]))

running.clear()
th.join(1.0)
dev.stop()

# ---- 结论 ----
print("\n[C] 结论")
depths = [r[1] for r in rows if r[0] > 2.0]
if depths:
    avg = sum(depths) / len(depths)
    print("    消费期深度：均值 %.1f ms   最小 %.1f   最大 %.1f（采样 %d 次）" %
          (avg, min(depths), max(depths), len(depths)))
    print("    目标 %.0f ms；可接受区间 %d~%d ms" %
          (LR.TARGET_SEC * 1000, LR.MIN_SEC * 1000, LR.MAX_SEC * 1000))
    if min(depths) >= LR.MAX_SEC * 1000 - 1:
        print("    ⚠⚠ 深度**始终顶在上限** ⇒ 消费速度 < 到达速度（或裁剪逻辑失效）")
    elif LR.MIN_SEC * 1000 <= avg <= LR.TARGET_SEC * 1000 * 2:
        print("    ✅ 深度收敛在合理区间")
    else:
        print("    ⚠ 深度偏离目标，需进一步查")
print("    消费总帧=%d （≈%.2f 秒音频）  空转(None)次数=%d  单次最长耗时=%.1fms" %
      (stats["frames"], stats["frames"] / dev.samplerate, stats["none"],
       stats["max_sleep"] * 1000))
print("    trimmed(裁剪次数)=%d" % dev.stats.trimmed)
