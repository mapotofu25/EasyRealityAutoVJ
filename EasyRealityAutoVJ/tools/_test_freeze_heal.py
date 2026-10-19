# -*- coding: utf-8 -*-
"""回归测试：解码线程必须能**自愈重开**（旧代码一次失败就永久死掉 ⇒ 画面卡住）。

场景：
  ① `_open()` 前两次失败、第三次成功 ⇒ 必须照样出帧（旧代码第 1 次就 return 了）；
  ② `_open()` 一直失败 ⇒ 退避重试 `_REOPEN_MAX` 轮后收工并记日志（不能忙等）。
用法：venv/Scripts/python.exe -u tools/_test_freeze_heal.py
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

import media_manager as mm                                       # noqa: E402

FAILS = []
logged = []


def check(name, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


try:
    import stallwatch
    stallwatch.log_line = lambda m: logged.append(m)
except Exception:                                                   # noqa: BLE001
    pass


class _FakeFrame:
    def __init__(self, v=1):
        self.v = v

    def to_ndarray(self, format="bgra"):
        import numpy as np
        return np.full((4, 4, 4), self.v, dtype=np.uint8)


def make_player(open_fail_times, frame_factory):
    p = mm.GpuDxvPlayer.__new__(mm.GpuDxvPlayer)
    p.path = "fake.mov"
    p._dead = False
    p._active = True
    p._warm = 0
    p.fps = 30.0
    p.speed = 1.0
    p._img = None
    p._max_w = 0
    p._max_h = 0
    p._qfmt = 4
    p.gpu = False
    p.gpu_want = False
    p.c = None
    p.st = None
    p._released = 0
    p._opens = 0

    def _open():
        p._opens += 1
        if p._opens <= open_fail_times:
            raise RuntimeError("模拟打开失败 #%d" % p._opens)
        return object(), object()

    def _release():
        p._released += 1
        p.c = None

    p._open = _open
    p._release = _release
    p._soft_frames = lambda: frame_factory(p)
    return p


def one_frame_then_end(p):
    yield _FakeFrame(7)
    return


print("① _open 前两次失败、第三次成功 ⇒ 必须自愈出帧")
p = make_player(2, one_frame_then_end)
th = __import__("threading").Thread(target=p._loop, daemon=True)
th.start()
t0 = time.perf_counter()
while time.perf_counter() - t0 < 4.0 and p._img is None:
    time.sleep(0.02)
check("打开失败两次后仍然出帧（旧代码这里永久死掉）", p._img is not None,
      "打开尝试 %d 次，_img=%s" % (p._opens, "有" if p._img is not None else "无"))
p._dead = True
th.join(timeout=2)

print("\n② _open 一直失败 ⇒ 退避重试 N 轮后收工（不能忙等）")
mm._REOPEN_MAX = 3
logged.clear()
p2 = make_player(999, one_frame_then_end)
th2 = __import__("threading").Thread(target=p2._loop, daemon=True)
wall0, cpu0 = time.perf_counter(), time.process_time()
th2.start()
th2.join(timeout=10)
wall, cpu = time.perf_counter() - wall0, time.process_time() - cpu0
check("线程正常结束（不挂死）", not th2.is_alive(), "alive=%s" % th2.is_alive())
check("按 _REOPEN_MAX 轮收工", p2._opens == 3, "打开尝试 %d 次" % p2._opens)
check("CPU 时间远小于墙钟（在退避而不是忙等）", cpu < wall * 0.5,
      "cpu=%.2fs wall=%.2fs" % (cpu, wall))
check("收工时有日志", any("打开失败" in m for m in logged), str(logged[:1])[:90])

print()
print("结果：%s" % ("全部通过 ✓" if not FAILS else "失败 %d 项 ✗ %s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
