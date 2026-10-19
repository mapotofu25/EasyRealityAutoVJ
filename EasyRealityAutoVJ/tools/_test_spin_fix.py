# -*- coding: utf-8 -*-
"""回归测试：解不出来的素材**不能再空转**（旧代码在这里是死循环）。

做法：造一个 `decode()` 必抛异常的假容器，跑 `_soft_frames()`：
  · 旧实现：while 里没有任何 sleep ⇒ 死循环、占满一个核、永不返回；
  · 新实现：退避 sleep，连续空转到上限后**放弃**（StopIteration）⇒ CPU 近乎不涨。
判据：① 能终止（不挂）；② 进程 CPU 时间 ≪ 墙钟时间（说明在 sleep 而不是空转）；
      ③ 放弃时写了日志。
用法：venv/Scripts/python.exe -u tools/_test_spin_fix.py
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

import media_manager as mm                                       # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


class _FakeContainer:
    """decode() 永远抛异常（模拟"解不出来的素材"）。"""

    def __init__(self):
        self.calls = 0

    def decode(self, video=0):
        self.calls += 1
        raise RuntimeError("模拟坏容器")
        yield  # noqa  (让它是个生成器)


def make_player(rewind_ok=True):
    p = mm.GpuDxvPlayer.__new__(mm.GpuDxvPlayer)      # 不走 __init__（那会起线程）
    p.path = "fake_broken.mov"
    p.c = _FakeContainer()
    p._rewind = lambda: rewind_ok
    p._max_w = 0
    p._max_h = 0
    p._qfmt = 4
    p._dead = False        # 真实播放器在 __init__ 里一定有的字段
    return p


logged = []
try:
    import stallwatch
    _orig = stallwatch.log_line
    stallwatch.log_line = lambda m: logged.append(m)
except Exception:                                                   # noqa: BLE001
    pass

print("① 解不出来的素材：应当退避并放弃，而不是死循环")
p = make_player()
gen = p._soft_frames()
wall0, cpu0 = time.perf_counter(), time.process_time()
res = "?"
try:
    next(gen)                    # 永远不 yield ⇒ 一路走到放弃
    res = "竟然 yield 了（不该）"
except StopIteration:
    res = "已放弃(StopIteration)"
except BaseException as e:                                          # noqa: BLE001
    res = "异常 %s: %s" % (type(e).__name__, e)
wall = time.perf_counter() - wall0
cpu = time.process_time() - cpu0
print("     结果=%s  墙钟 %.2fs  CPU %.2fs  decode 调用 %d 次"
      % (res, wall, cpu, p.c.calls))
check("能终止、不挂死", res.startswith("已放弃"), res)
check("CPU 时间远小于墙钟（在退避而不是空转）", cpu < wall * 0.5,
      "cpu=%.2fs wall=%.2fs" % (cpu, wall))
check("空转达到上限后放弃（decode 调用 ≈ 上限 30）", 25 <= p.c.calls <= 40,
      "%d 次" % p.c.calls)
check("放弃时有日志", any("放弃" in m for m in logged), str(logged[:1])[:80])

print("\n② 重绕都失败时：应当直接收工（不多绕）")
p2 = make_player(rewind_ok=False)
gen2 = p2._soft_frames()
try:
    next(gen2)
    r2 = "竟然 yield 了"
except StopIteration:
    r2 = "收工"
check("_rewind 返回 False 时立刻收工", r2 == "收工", r2)
check("只解码了 1 次（没反复重绕）", p2.c.calls == 1, "%d 次" % p2.c.calls)

print()
print("结果：%s" % ("全部通过 ✓" if not FAILS else "失败 %d 项 ✗ %s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
