# -*- coding: utf-8 -*-
"""组 C（C1）修复验证：`ercue.audio.player` 的"打开设备失败永不复位"。

跑法（Windows）：
    P:\\ERAVJ\\EasyRealityCue\\venv\\Scripts\\python.exe  P:\\ERAVJ\\EasyRealityCue\\tools\\_fix_c_verify.py

★ 本脚本**不打开真实声卡**：`sink_factory` / `device_probe` 全部 monkeypatch，
  `AudioTrack` 也用一个内存假体（不需要任何音频文件、不需要 PyAV/sounddevice）。

判据（对应 team-lead 的 C1 验收）：
  1. mock 第一次 open 抛异常 ⇒ `play()` 返回 **False** 且**有错误提示**（error 非空）；
  2. 第二次 `play()`（mock 成功）⇒ **能起播**（返回 True 且真的重新尝试了打开设备）；
  3. 连续失败 ⇒ **有退避**（不长枪连发地重试；退避阶梯 1→2→4s，成功即清零）；
  4. **反向验证**：把"复位/退避恢复"去掉（等价修复前）⇒ 第二次 `play()` 仍失败。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

# 这些用例**故意**走"打开失败"路径，会被 log.exception 记录；挂 NullHandler 吸收掉。
_lg = logging.getLogger("ercue.audio.player")
_lg.propagate = False
if not _lg.handlers:
    _lg.addHandler(logging.NullHandler())

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import numpy as np                                             # noqa: E402
from ercue.audio import player as ap                           # noqa: E402
from ercue.audio.player import AudioPlayer                     # noqa: E402


# ==========================================================================
# 假体：内存 track + 假 sink（不碰真实文件 / 声卡）
# ==========================================================================
class FakeTrack:
    """`play()` / `_run()` 需要的最小 track 接口（ok / frames / samples）。"""

    def __init__(self, frames=48000 * 5):
        self._frames = int(frames)
        self._buf = np.zeros((2, self._frames), dtype=np.float32)
        self.error = ""

    @property
    def ok(self):
        return True

    @property
    def frames(self):
        return self._frames

    @property
    def duration_sec(self):
        return self._frames / 48000.0

    @property
    def samplerate(self):
        return 48000

    @property
    def channels(self):
        return 2

    def samples(self):
        return self._buf


class NullSink:
    """立即吞掉写入（不阻塞）—— 用于"打开成功"路径。"""

    def __init__(self):
        self.frames = 0

    def write(self, block):
        self.frames += int(block.shape[0])

    def close(self):
        pass


class GateSink:
    """写入阻塞直到放行 —— 让播放线程保持存活，便于验证"确实起播了"。"""

    def __init__(self):
        self.entered = threading.Event()

    def write(self, block):
        self.entered.set()
        time.sleep(0.01)          # 轻微限速，别把 CPU 打满

    def close(self):
        pass


def _mk_player(factory):
    p = AudioPlayer(sink_factory=factory, device_probe=lambda sr: True)
    p._track = FakeTrack()        # 绕过解码，直接给一个"已装载"的假 track
    return p


# ==========================================================================
# 判据 1：第一次 open 抛异常 ⇒ play() 返回 False 且有错误提示
# ==========================================================================
def check_1_first_fail_returns_false():
    def boom(sr, ch):
        raise RuntimeError("模拟打开声卡失败")

    p = _mk_player(boom)
    assert p.available is True, "打开前探测说有设备 ⇒ available 应为 True"
    r = p.play(0.0)
    assert r is False, "打开失败时 play() 必须返回 False（C1），实际 %r" % (r,)
    assert p.error and "打开" in p.error, "必须给出错误提示（error），实际 %r" % (p.error,)
    assert p.available is False, "失败后进入冷却 ⇒ available 应为 False"
    assert p.is_playing() is False
    print("  ✓ 判据1：首次打开失败 → play()=False、error=%r、available=False" % p.error)
    return True


# ==========================================================================
# 判据 2：第二次 play()（mock 成功）⇒ 能起播（证明已复位/重试）
# ==========================================================================
def check_2_second_play_recovers():
    calls = {"n": 0}

    def flaky(sr, ch):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("模拟第一次打开失败")
        return GateSink()

    p = _mk_player(flaky)
    assert p.play(0.0) is False, "第一次应失败"
    assert calls["n"] == 1
    # 立刻再 play（模拟用户再按一次 ▶）—— ★ 关键：不能被冷却期永久挡住
    r2 = p.play(0.0)
    assert r2 is True, "★ 第二次 play()（mock 成功）应能起播，实际 %r" % (r2,)
    assert calls["n"] == 2, "必须真的**重新尝试**打开设备（而不是永久拒绝），实际尝试 %d 次" % calls["n"]
    assert p.available is True, "恢复后 available 应回到 True"
    assert p.is_playing() is True, "应处于播放中"
    p.stop()
    print("  ✓ 判据2：首败后第二次 play() 成功起播（重试次数=%d，available 恢复 True）" % calls["n"])
    return True


# ==========================================================================
# 判据 3：连续失败 ⇒ 有退避（不立即、无节制地重试）
# ==========================================================================
def check_3_backoff_on_repeated_fail():
    attempts = []

    def always_fail(sr, ch):
        attempts.append(time.monotonic())
        raise RuntimeError("总是失败")

    p = _mk_player(always_fail)
    results = [p.play(0.0) for _ in range(5)]
    assert all(r is False for r in results), results
    # 5 次调用里只应真正尝试打开 2 次（首败后放行 1 次重试；此后进入退避，不再尝试）
    assert len(attempts) == 2, "退避应抑制高频重试：5 次调用只应尝开 2 次，实际 %d" % len(attempts)
    # 退避窗口应起作用（下次允许尝试还须等待）
    gap = p._next_open_ts - time.monotonic()
    assert gap > 0.0, "退避窗口应仍有效，实际剩余 %.3fs" % gap
    # 退避阶梯 1 → 2 → 4（封顶 4）
    assert ap._open_backoff_sec(1) == 1.0, ap._open_backoff_sec(1)
    assert ap._open_backoff_sec(2) == 2.0, ap._open_backoff_sec(2)
    assert ap._open_backoff_sec(3) == 4.0, ap._open_backoff_sec(3)
    assert ap._open_backoff_sec(9) == ap.OPEN_RETRY_MAX_SEC == 4.0
    assert ap._open_backoff_sec(0) == 0.0
    # 冷却期内再 play 仍=False 且**不再尝试**
    n_before = len(attempts)
    assert p.play(0.0) is False
    assert len(attempts) == n_before, "冷却期内不得再尝试打开"
    print("  ✓ 判据3：连续失败进入退避（5 次调用仅尝开 2 次；剩余冷却 %.2fs；阶梯 1→2→4s）" % gap)
    return True


# ==========================================================================
# 判据 4（反向验证）：把"复位/退避恢复"去掉 ⇒ 第二次 play() 仍失败
# ==========================================================================
def check_4_reverse_without_reset():
    calls = {"n": 0}

    def flaky(sr, ch):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("模拟第一次打开失败")
        return NullSink()

    p = _mk_player(flaky)
    assert p.play(0.0) is False                 # 第一次失败
    assert calls["n"] == 1
    # —— 去掉复位：让 `_open_failed` 永不复位（等价修复前：available 恒 False 把它永久挡住）——
    p._open_fail_count = 99
    p._next_open_ts = time.monotonic() + 1e9    # 退避永不结束 ⇒ 等同于"永不复位"
    assert p.available is False, "去掉恢复后 available 应恒为 False"
    r = p.play(0.0)
    assert r is False, "★ 去掉复位后，第二次 play() 仍失败（重现 C1 永久无声）"
    assert calls["n"] == 1, "去掉复位后根本不会再尝试打开设备"
    # 对照：把退避窗打开（= 允许恢复）⇒ 立刻能恢复
    p._open_fail_count = 1
    p._next_open_ts = 0.0
    assert p.play(0.0) is True, "恢复退避窗口后第二次 play() 成功（证明复位/退避恢复才是关键）"
    assert calls["n"] == 2, "此时应真的重新尝试打开"
    print("  ✓ 判据4（反向）：去掉复位 → 第二次 play() 仍失败（%r）；恢复后 → 成功" % (r,))
    return True


# ==========================================================================
def main() -> int:
    print("=" * 72)
    print("组 C（C1）修复验证  (python %s)" % sys.version.split()[0])
    print("退避参数：base=%.1fs  max=%.1fs  阶梯=%s  等待打开=%.2fs"
          % (ap.OPEN_RETRY_BASE_SEC, ap.OPEN_RETRY_MAX_SEC,
             [ap._open_backoff_sec(i) for i in range(1, 5)], ap.OPEN_WAIT_SEC))
    print("=" * 72)
    tests = [
        ("判据1 首次失败→play()=False", check_1_first_fail_returns_false),
        ("判据2 二次 play 恢复", check_2_second_play_recovers),
        ("判据3 连续失败退避", check_3_backoff_on_repeated_fail),
        ("判据4 反向：去掉复位仍失败", check_4_reverse_without_reset),
    ]
    ok_all = True
    for name, fn in tests:
        print("\n--- %s ---" % name)
        try:
            fn()
        except Exception as e:                                  # noqa: BLE001
            ok_all = False
            import traceback
            print("  ✗ %s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()))
    print("\n" + "=" * 72)
    print("汇总：%s" % ("全部通过 ✓" if ok_all else "有失败 ✗"))
    print("=" * 72)
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
