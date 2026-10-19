# -*- coding: utf-8 -*-
"""QA5 Round2 窄范围独立复验（不跑工程师的测试）：锁不变式 + 配额不泄漏 + 不空转。

A. 不变式：线程存活 ⇒ `_dead` 必须为 False（列出全部置死点 + 3 类实测）
B. 重试循环不泄漏 GL 上下文配额（实测 50+ 次抢锁失败后 _CTX_N 仍为 0）
C. 重试间隔 _READY_RETRY_SECS=0.5 生效（不忙等）
D. A3 复跑（50 次 ensure 毫秒级 / 主线程钳 3s）

用法：QT_QPA_PLATFORM=offscreen venv/Scripts/python.exe -u tools/_qa5_round2.py
退出码 0=全通过；1=有失败。
"""
import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

import glctx                                                    # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print(("  PASS " if ok else "  FAIL ") + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


_SAVED_PREINIT = glctx.preinit
_SAVED_DEC = glctx.GLDecoder


class _RealFailDec:
    """真·创建失败（非抢锁）——构造函数一抛，`_run` 的 except 兜住 ⇒ 置死。"""

    def __init__(self):
        raise RuntimeError("模拟 glfwCreateWindow/着色器 真失败")

    def decode(self, *a, **k):
        return None


print("== A. 不变式：线程存活 ⇒ _dead=False ==")
print("   （代码里 _dead=True 的赋值点仅两处：glctx.py:821 与 :833，均紧跟 _ready_evt.set()+return）")

# ---------------------------------------------------------------- A1: 持锁 → 未就绪但不置死；释放 → 就绪
print("A1) 外部真持 `_CREATE_LOCK` ⇒ ensure=False 且 _dead=False 且线程存活；释放 ⇒ 就绪（真 GL）")
glctx.preinit = lambda *a, **k: True
_REC = []


class _RecordingDec(glctx.GLDecoder):
    """真 GLDecoder + 记录每次尝试的 (ok, lock_timeout, err)。"""

    def __init__(self):
        super().__init__()
        _REC.append((self.ok, self.lock_timeout, self.err))


glctx.GLDecoder = _RecordingDec
try:
    holder_got = threading.Event()
    holder_done = threading.Event()

    def holder():
        glctx._CREATE_LOCK.acquire()
        holder_got.set()
        try:
            time.sleep(3.0)                    # 持锁 3s（< GL_LOCK_POISON_S=5s，不污染）
        finally:
            glctx._CREATE_LOCK.release()
            holder_done.set()

    threading.Thread(target=holder, daemon=True).start()
    holder_got.wait(1.0)

    w = glctx.GLWorker()
    t0 = time.perf_counter()
    ok1 = w.ensure(ready_timeout=1.0)          # 主线程启动者：min(1.0,3)=1.0 → 超时
    dt1 = time.perf_counter() - t0
    check("A1.1 持锁期间 ensure()=False", ok1 is False, "dt=%.3f" % dt1)
    check("A1.2 持锁期间 _dead is False（不变式）", w._dead is False, "_dead=%s" % w._dead)
    check("A1.3 持锁期间线程仍存活", bool(w._thread and w._thread.is_alive()))
    check("A1.4 持锁期间 available()=False", w.available() is False)
    holder_done.wait(4.0)                       # 等到锁释放
    ok_ready = w._ready_evt.wait(15.0)          # 等 worker 抢到锁建成
    check("A1.5 释放锁后就绪（ready=True）", bool(ok_ready))
    check("A1.6 就绪后 available()=True", w.available() is True)
    check("A1.7 就绪后 _dead 仍 False + 线程存活",
          w._dead is False and w._thread.is_alive())
    lt = [r for r in _REC if r[1]]              # lock_timeout=True 的记录
    ok_rec = [r for r in _REC if r[0]]          # ok=True 的记录
    check("A1.8 确有 GLLockUnavailable 尝试记录（走的是重试分支）",
          len(lt) >= 1, "lock_timeout 次数=%d 总尝试=%d" % (len(lt), len(_REC)))
    check("A1.9 最终成功建成（ok=True 记录 ≥1）", len(ok_rec) >= 1)
finally:
    glctx.GLDecoder = _SAVED_DEC
    glctx.preinit = _SAVED_PREINIT

# ---------------------------------------------------------------- A2: 真失败 ⇒ 置死且线程退出
print("A2) 真·创建失败（非抢锁）⇒ 置死且线程退出（验证「置死 ⇔ 线程退出」）")
glctx.preinit = lambda *a, **k: True
glctx.GLDecoder = _RealFailDec
try:
    w2 = glctx.GLWorker()
    ok2 = w2.ensure(ready_timeout=2.0)
    w2._thread.join(timeout=3.0)
    check("A2.1 真失败 ⇒ ensure=False + _dead=True", ok2 is False and w2._dead is True,
          "ok=%s dead=%s" % (ok2, w2._dead))
    check("A2.2 置死的同时线程确已退出", not w2._thread.is_alive())
    check("A2.3 last_error 可读", bool(w2.last_error), repr(w2.last_error))
finally:
    glctx.GLDecoder = _SAVED_DEC
    glctx.preinit = _SAVED_PREINIT

# ---------------------------------------------------------------- A3: 驱动判定挂死 ⇒ 置死（设计内）
print("A3) gl_poisoned()=True（驱动挂死）⇒ 置死（设计内，真 GLDecoder 走 RuntimeError 分支）")
glctx.preinit = lambda *a, **k: True
_saved_poison = glctx._LOCK_POISON[0]
try:
    glctx._LOCK_POISON[0] = True
    w3 = glctx.GLWorker()
    ok3 = w3.ensure(ready_timeout=2.0)
    w3._thread.join(timeout=3.0)
    check("A3.1 污染态 ⇒ ensure=False + _dead=True", ok3 is False and w3._dead is True,
          "ok=%s dead=%s" % (ok3, w3._dead))
    check("A3.2 污染态判定后线程退出", not w3._thread.is_alive())
finally:
    glctx._LOCK_POISON[0] = _saved_poison
    glctx.preinit = _SAVED_PREINIT
    glctx.GLDecoder = _SAVED_DEC

# ---------------------------------------------------------------- B: 配额不泄漏（实测 55+ 次）
print("== B. 重试循环不泄漏 _ctx_reserve 配额（实测 55 次抢锁失败）==")
glctx.preinit = lambda *a, **k: True
_saved_enter = glctx._gl_enter
_saved_reserve = glctx._ctx_reserve
_saved_release = glctx._ctx_release
_saved_retry = glctx._READY_RETRY_SECS
_enter_calls = [0]
_reserve_calls = [0]
_release_calls = [0]
_fail_on = [True]


def _fake_enter(*a, **k):
    if _fail_on[0]:
        _enter_calls[0] += 1
        return False                       # 模拟"锁被占、抢不到"
    return _saved_enter(*a, **k)           # 放开后走真实现


def _counting_reserve():
    _reserve_calls[0] += 1
    return _saved_reserve()


def _counting_release():
    _release_calls[0] += 1
    return _saved_release()


try:
    glctx._READY_RETRY_SECS = 0.02         # 加速，只为快凑满 55 次（B 不测间隔）
    glctx._gl_enter = _fake_enter
    glctx._ctx_reserve = _counting_reserve
    glctx._ctx_release = _counting_release
    glctx.GLDecoder = _SAVED_DEC            # 用真 GLDecoder 走真 reserve/release
    # ⚠ 基线：A1 里成功建成的那个 worker 仍持有 1 个**存活**上下文，故这里非 0。
    #    B 只看**增量**（失败重试不得改变 _CTX_N）。
    ctx_before_B = glctx.ctx_count()
    w4 = glctx.GLWorker()
    w4.ensure(ready_timeout=0.2)            # 启动者超时（不影响）
    t0 = time.perf_counter()
    while _enter_calls[0] < 55 and (time.perf_counter() - t0) < 20.0:
        time.sleep(0.02)
    n_attempts = _enter_calls[0]
    ctx_after = glctx.ctx_count()
    check("B.1 抢锁失败尝试 ≥55 次（跨过 MAX_CTX=40）", n_attempts >= 55,
          "attempts=%d（说明未在 40 次处被配额卡死）" % n_attempts)
    check("B.2 55+ 次失败后配额**增量**为 0（基线 %d → 现 %d）"
          % (ctx_before_B, ctx_after), ctx_after == ctx_before_B,
          "delta=%d（reserve 调用 %d 次，每次都应成对 release）"
          % (ctx_after - ctx_before_B, _reserve_calls[0]))
    check("B.3 reserve/release 成对（reserve %d / release %d ≥ 55）"
          % (_reserve_calls[0], _release_calls[0]),
          _reserve_calls[0] >= 55 and _release_calls[0] >= 55, "attempts=%d" % n_attempts)
    check("B.4 期间 _dead 保持 False + 线程存活",
          w4._dead is False and w4._thread.is_alive(), "_dead=%s" % w4._dead)
    # 放开锁 ⇒ 必须仍能建成（配额没被耗尽）
    _fail_on[0] = False
    ok_ready = w4._ready_evt.wait(15.0)
    check("B.5 放开锁后仍能就绪（配额未被耗尽）", bool(ok_ready) and w4.available() is True,
          "ready=%s ctx=%d" % (bool(ok_ready), glctx.ctx_count()))
    check("B.6 就绪后增量恰为 +1（基线 %d → 现 %d）" % (ctx_before_B, glctx.ctx_count()),
          glctx.ctx_count() == ctx_before_B + 1, "_CTX_N=%d" % glctx.ctx_count())
finally:
    glctx._gl_enter = _saved_enter
    glctx._ctx_reserve = _saved_reserve
    glctx._ctx_release = _saved_release
    glctx._READY_RETRY_SECS = _saved_retry
    glctx.preinit = _SAVED_PREINIT
    glctx.GLDecoder = _SAVED_DEC

# ---------------------------------------------------------------- C: 不空转（0.5s 生效）
print("== C. 重试间隔 _READY_RETRY_SECS=0.5 生效（不忙等）==")
glctx.preinit = lambda *a, **k: True
_saved_enter = glctx._gl_enter
_enter_calls2 = [0]
_fail_on2 = [True]


def _fake_enter2(*a, **k):
    if _fail_on2[0]:
        _enter_calls2[0] += 1
        return False
    return _saved_enter(*a, **k)


try:
    glctx._gl_enter = _fake_enter2
    glctx.GLDecoder = _SAVED_DEC
    w5 = glctx.GLWorker()
    w5.ensure(ready_timeout=0.2)
    time.sleep(0.3)
    _enter_calls2[0] = 0                    # 归零后测 3.0s 窗口
    cpu0 = time.process_time()
    t0 = time.perf_counter()
    time.sleep(3.0)
    wall = time.perf_counter() - t0
    cpu = time.process_time() - cpu0
    n = _enter_calls2[0]
    check("C.1 3s 内抢锁尝试 ≈ wall/0.5（6 次左右，不是忙等上千次）",
          4 <= n <= 9, "attempts=%d in %.2fs wall" % (n, wall))
    check("C.2 进程 CPU 时间远小于墙钟（在 sleep，不烧核）", cpu < 0.6,
          "cpu=%.3fs wall=%.2fs（占比 %.0f%%）" % (cpu, wall, 100.0 * cpu / wall))
    check("C.3 持续未就绪但 _dead 保持 False + 线程存活",
          w5._dead is False and w5._thread.is_alive(), "_dead=%s" % w5._dead)
finally:
    glctx._gl_enter = _saved_enter
    glctx.preinit = _SAVED_PREINIT
    glctx.GLDecoder = _SAVED_DEC

# ---------------------------------------------------------------- D: A3 复跑
print("== D. A3 复跑（没被改坏）==")


class _NeverReadyDec:
    def __init__(self):
        time.sleep(30.0)
        self.ok = True
        self.err = ""
        self.lock_timeout = False

    def decode(self, *a, **k):
        return None


glctx.preinit = lambda *a, **k: True
glctx.GLDecoder = _NeverReadyDec
try:
    w6 = glctx.GLWorker()
    t0 = time.perf_counter()
    w6.ensure()                             # 主线程，默认 12s → 应钳到 3s
    dt_main = time.perf_counter() - t0
    check("D.1 主线程首等 ≈3s（不是 12s）", 2.5 < dt_main < 4.2, "dt=%.3fs" % dt_main)
    w7 = glctx.GLWorker()
    per = []
    t0 = time.perf_counter()
    for _ in range(50):
        tt = time.perf_counter()
        w7.ensure()
        per.append(time.perf_counter() - tt)
    dt50 = time.perf_counter() - t0
    check("D.2 50 次 ensure() 总耗时毫秒级级（<<600s）", dt50 < 6.0,
          "total=%.3fs（首调 %.3fs，其余 max %.4fs）" % (dt50, per[0], max(per[1:])))
    check("D.3 第 2..50 次每次 < 50ms", max(per[1:]) < 0.05, "max=%.4fs" % max(per[1:]))
finally:
    glctx.preinit = _SAVED_PREINIT
    glctx.GLDecoder = _SAVED_DEC

print()
print("结果：%s" % ("全部通过 ✓" if not FAILS else "失败 %d 项 ✗ %s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
