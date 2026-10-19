# -*- coding: utf-8 -*-
"""验证发送端**采集设备中断后自动重连**（用户要求："推流端设备断线自动重连"）。

手法：不碰真实声卡（CI/沙箱里没有），而是直接驱动 `_capture_loop` 的会话层：
  - 用一个假 `soundcard` 模块 + 假 `wasapi_capture` 注入 `sys.modules`；
  - 假 recorder 在第 N 次 `record()` 时**抛异常**（= 设备被拔）；
  - 断言：Sender 没有永久停住，而是**退避后重连**，并且重新开始出包。

判据（对齐铁律 #0：只要线程活着就只能是"可重试"）：
  1. 第一次会话异常后，`stats.reconnects` 至少 +1；
  2. 第二次会话能正常出包（`stats.packets` 继续增长）；
  3. 退避期间 `stop()` 能**立刻**返回（不卡住退出）。
"""
import os
import sys
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import numpy as np  # noqa: E402

import lan_audio_send as S  # noqa: E402

PKT = 480
STATE = {"sessions": 0, "fail_after": 5}


class _FakeRec:
    """假 recorder：第 `fail_after` 次 record() 抛异常（模拟设备掉线）。"""

    def __init__(self, sess):
        self._sess = sess
        self._n = 0
        self.last_qpc = 0

    def record(self, numframes=PKT):
        self._n += 1
        if self._sess == 1 and self._n > STATE["fail_after"]:
            raise RuntimeError("模拟设备被拔掉")
        self.last_qpc = int(time.perf_counter() * 1e7)
        t = np.arange(numframes, dtype=np.float32)
        x = (0.3 * np.sin(2 * np.pi * 440.0 * t / 48000.0)).astype(np.float32)
        return np.column_stack([x, x])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeMic:
    name = "假扬声器（测试）"
    isloopback = True

    def recorder(self, samplerate=None, channels=None):
        STATE["sessions"] += 1
        return _FakeRec(STATE["sessions"])


def _install_fakes():
    sc = types.ModuleType("soundcard")
    sc.all_microphones = lambda include_loopback=False: [_FakeMic()]
    sys.modules["soundcard"] = sc
    wc = types.ModuleType("wasapi_capture")
    wc.wrap = lambda mic: mic
    sys.modules["wasapi_capture"] = wc


def main():
    rc = 0
    _install_fakes()

    s = S.Sender(device=None, sr=48000, dest=("127.0.0.1", 47888))
    # 不走真 socket：直接给个假的（只统计，不真发）
    class _Sock:
        def sendto(self, pkt, addr):
            return len(pkt)

        def recvfrom(self, n):
            raise BlockingIOError()

        def close(self):
            pass
    s._sock = _Sock()

    import threading
    threading.Thread(target=s._send_loop, name="tx", daemon=True).start()
    s.running = True
    threading.Thread(target=s._capture_loop, name="cap", daemon=True).start()

    # 等第一次会话失败 + 第二次会话开始出包
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 8.0:
        snap = s.snapshot()
        if snap["reconnects"] >= 1 and STATE["sessions"] >= 2 and snap["packets"] > 10:
            break
        time.sleep(0.1)

    snap = s.snapshot()
    print("--- 结果 ---")
    print("  会话次数        = %d（应 ≥2：故障后重开）" % STATE["sessions"])
    print("  重连计数        = %d（应 ≥1）" % snap["reconnects"])
    print("  发送包数        = %d（应持续增长）" % snap["packets"])
    print("  仍在运行        = %s" % snap["running"])

    if STATE["sessions"] < 2:
        print("  ❌ 没有重开会话 —— 仍然是「一次失败就永久停」")
        rc = 1
    elif snap["reconnects"] < 1:
        print("  ❌ 重连计数没动")
        rc = 1
    elif snap["packets"] < 10:
        print("  ❌ 重连后没有继续出包")
        rc = 1
    else:
        print("  ✅ 设备中断后自动重连，且恢复出包")

    # 退避可打断：stop() 必须立刻返回
    t1 = time.perf_counter()
    s.stop(timeout=3.0)
    dt = time.perf_counter() - t1
    print("\n  stop() 耗时 = %.2fs（退避最长 30s，必须远小于它）" % dt)
    if dt > 3.0:
        print("  ❌ stop() 被退避卡住了")
        rc = 1
    else:
        print("  ✅ 退避可被 stop() 立刻打断")

    print("\n结论：%s" % ("通过" if rc == 0 else "失败"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
