# -*- coding: utf-8 -*-
"""枚举所有 loopback 设备，逐个录 0.5s，报告峰值。
目的：确认浏览器此刻是否真的在放声、放到了哪个设备上。
这是"进程采集能不能绕过系统调音"结论的前置条件 —— 基线为 0 则任何结论都无意义。
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import numpy as np
import soundcard as sc

mics = sc.all_microphones(include_loopback=True)
print("全部设备 %d 个：" % len(mics))
lb = []
for m in mics:
    flag = getattr(m, "isloopback", False)
    print("  [loopback=%s] %s" % (flag, m.name))
    if flag:
        lb.append(m)

def default_out():
    try:
        return sc.default_speaker()
    except Exception as e:
        return "默认扬声器获取失败: %s" % e

print("\n默认输出设备:", default_out())

print("\n逐个 loopback 设备录 0.5s（48000Hz / 2ch）：")
best = None
for m in lb:
    try:
        with m.recorder(samplerate=48000, channels=2) as rec:
            d = rec.record(numframes=24000)   # 0.5 s
        peak = float(np.abs(d).max()) if d.size else -1.0
        rms = float(np.sqrt(np.mean(np.square(d)))) if d.size else -1.0
        print("  %-42s 峰值 %.6f  rms %.6f" % (m.name[:42], peak, rms))
        if best is None or peak > best[1]:
            best = (m.name, peak)
    except Exception as e:
        print("  %-42s 失败: %s" % (m.name[:42], e))

print("\n最大峰值设备:", best)
print("结论:", "有声音，音频链路正常" if (best and best[1] > 1e-4) else "★全静音 —— 浏览器没在放声，或放到了非 loopback 可见的设备")
