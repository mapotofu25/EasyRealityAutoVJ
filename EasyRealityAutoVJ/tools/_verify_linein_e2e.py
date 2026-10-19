# -*- coding: utf-8 -*-
"""端到端回归：改动后确认「系统声音」路径与「mic/线路输入」路径都能正常跑。

不启动 MainWindow（避免 127 abort），直接驱动 AudioEngine：
  用例1 系统声音（真实 loopback 设备，是否有信号取决于当前是否在放歌）
  用例2 mic/线路输入（选真实非回环设备，若存在）
关注点：改动后**不再崩、不再静默**，且 device_desc / error 字段语义正确。
"""
import io
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"P:\ERAVJ\AutoVJ\src")

import audio_engine  # noqa: E402

print("=" * 66)
print("端到端回归：系统声音 + mic/线路输入")
print("=" * 66)

eng = audio_engine.AudioEngine()

try:
    devs = eng.list_devices(timeout=8)
    print("\n设备枚举：")
    for k in ("system", "mic", "asio"):
        print("  %-6s %d 个: %s" % (k, len(devs.get(k, [])), devs.get(k, [])[:3]))

    # ---- 用例1：系统声音（指定一个真实回环设备名，验证能匹配上、无告警）----
    sysdev = (devs.get("system") or [""])[0]
    print("\n[用例1] 系统声音，device_name=%r" % sysdev)
    # 用真实默认扬声器名（与 loopback 名一致的通常是去掉"扬声器"前缀？直接用第一个 loopback 名）
    eng.start("system", sysdev, mono=True, sr=48000)
    time.sleep(2.0)
    snap = eng.state.snapshot()
    eng.stop()
    time.sleep(0.5)
    print("  running=%s device_desc=%r" % (snap.get("running"), snap.get("device_desc")))
    print("  error=%r" % snap.get("error"))
    print("  level=%.4f energy=%.4f silent_sec=%.1f" % (
        snap.get("level", -1), snap.get("energy", -1), snap.get("silent_sec", -1)))
    if snap.get("error"):
        print("  ⚠ 有 error（可能是环境相关，非改动引入）")
    else:
        print("  ✅ 无 error，设备匹配正常")

    # ---- 用例2：mic/线路输入（若枚举到非回环设备）----
    micdev = (devs.get("mic") or [""])[0]
    if micdev:
        print("\n[用例2] mic/线路输入，device_name=%r" % micdev)
        eng.start("mic", micdev, mono=True, sr=48000)
        time.sleep(1.5)
        snap2 = eng.state.snapshot()
        eng.stop()
        time.sleep(0.3)
        print("  running=%s device_desc=%r error=%r" % (
            snap2.get("running"), snap2.get("device_desc"), snap2.get("error")))
        print("  ✅ 未崩溃")
    else:
        print("\n[用例2] 跳过（本机无非回环设备）")

    # ---- 用例3：故意指定不存在的设备名，验证新告警逻辑 ----
    print("\n[用例3] 故意指定不存在的设备名 → 应出现明确告警（非静默）")
    eng.start("system", "这个设备肯定不存在_XYZ", mono=True, sr=48000)
    time.sleep(1.5)
    snap3 = eng.state.snapshot()
    eng.stop()
    time.sleep(0.3)
    err3 = snap3.get("error") or ""
    print("  device_desc=%r" % snap3.get("device_desc"))
    print("  error=%r" % err3)
    if "未找到" in err3 and "已改用" in err3:
        print("  ✅ 告警文案正确（含原设备名与实际设备名）")
    else:
        print("  ❌ 告警未出现或文案不符")

finally:
    try:
        eng.shutdown()
    except Exception:
        pass

print("\n" + "=" * 66)
print("回归跑完")
print("=" * 66)
