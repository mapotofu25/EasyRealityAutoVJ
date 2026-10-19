# -*- coding: utf-8 -*-
"""QA5 独立复验（不采信工程师测试）：GLWorker 超时语义 + _heal_silent 无帧兜底 + 健康日志 gpu。

- A2 超时不置死 / 能恢复 / 只有崩溃置死
- A3 主线程首次等待被钳到 ~3s；50 次高频调用总耗时 << 50*12s
- A4 上层自动切回 GPU（available() 翻转 + _soft_until_recover 重探返回 True）+ 生成器热插拔（读码）
- A5 锁竞争吃掉就绪预算（+ 诚实记录首次建上下文的残留行为）
- B  _heal_silent 无帧兜底：重建/限流封顶/换素材复位/图片排除/健康排除/10min 高频上界
- C  健康快照 gpu= / 状态变化才额外记一行 / 无绝对路径

用法：QT_QPA_PLATFORM=offscreen venv/Scripts/python.exe -u tools/_qa5_glworker_heal.py
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

import numpy as np                                              # noqa: E402
from PySide6.QtGui import QImage                               # noqa: E402
from PySide6.QtWidgets import QApplication                     # noqa: E402

app = QApplication.instance() or QApplication([])

import glctx                                                    # noqa: E402
import engine                                                   # noqa: E402
import media_manager as mm                                      # noqa: E402
import stallwatch                                               # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print(("  PASS " if ok else "  FAIL ") + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


LOGGED = []
stallwatch.log_line = lambda m: LOGGED.append(m)

# ================================================================ A. GLWorker
print("== A2: 启动超时**不置死** / 稍后就绪能恢复 / 只有崩溃才置死 ==")

_SAVED_PREINIT = glctx.preinit
_SAVED_DEC = glctx.GLDecoder


class _SlowOkDec:
    """模拟"冷启动慢、但最终成功"的唯一上下文。"""

    def __init__(self):
        time.sleep(0.6)
        self.ok = True
        self.err = ""

    def decode(self, *a, **k):
        return None


class _FailDec:
    ok = False
    err = "模拟上下文创建失败"

    def decode(self, *a, **k):
        return None


def _boom():
    raise RuntimeError("模拟 GLDecoder 构造异常")


try:
    glctx.preinit = lambda *a, **k: True
    glctx.GLDecoder = _SlowOkDec
    w = glctx.GLWorker()
    t0 = time.perf_counter()
    ok1 = w.ensure(ready_timeout=0.2)                 # 主线程启动者：min(0.2,3)=0.2 → 超时
    dt1 = time.perf_counter() - t0
    check("A2.1 超时 ensure() == False", ok1 is False, "ok=%s dt=%.3f" % (ok1, dt1))
    check("A2.2 超时**未**置 `_dead`（关键修复）", w._dead is False, "_dead=%s" % w._dead)
    check("A2.3 超时后 available() == False（尚未就绪）", w.available() is False)
    check("A2.4 last_message 给出可读原因", "冷启动" in (w.last_message() or ""),
          repr(w.last_message()))
    t2 = time.perf_counter()
    r = w.ensure(ready_timeout=999.0)                 # 非启动者：必须非阻塞
    dt2 = time.perf_counter() - t2
    check("A2.5 超时后的重试**非阻塞**（<50ms）", dt2 < 0.05, "dt=%.4fs r=%s" % (dt2, r))
    time.sleep(0.7)
    check("A2.6 线程就绪后非阻塞 ensure() == True（能恢复）", w.ensure() is True)
    check("A2.7 available() 变 True（上层可自动切回 GPU）", w.available() is True)

    # 只有崩溃才置死：ok=False
    glctx.GLDecoder = _FailDec
    w2 = glctx.GLWorker()
    okc = w2.ensure(ready_timeout=2.0)
    check("A2.8 上下文 ok=False ⇒ _dead=True（判崩溃）", okc is False and w2._dead is True,
          "ok=%s dead=%s" % (okc, w2._dead))

    # 只有崩溃才置死：_run 抛异常
    glctx.GLDecoder = _boom
    w3 = glctx.GLWorker()
    okc3 = w3.ensure(ready_timeout=2.0)
    check("A2.9 `_run` 抛异常 ⇒ _dead=True + last_error 有值",
          okc3 is False and w3._dead is True and bool(w3.last_error),
          "ok=%s dead=%s err=%r" % (okc3, w3._dead, w3.last_error))
finally:
    glctx.preinit = _SAVED_PREINIT
    glctx.GLDecoder = _SAVED_DEC


print("== A3: 主线程首次等待被钳到 ~3s；50 次高频调用总耗时 << 50*12s ==")


class _NeverReadyDec:
    def __init__(self):
        time.sleep(30.0)                              # 预算内绝不就绪
        self.ok = True
        self.err = ""

    def decode(self, *a, **k):
        return None


try:
    glctx.preinit = lambda *a, **k: True
    glctx.GLDecoder = _NeverReadyDec

    w4 = glctx.GLWorker()
    t0 = time.perf_counter()
    w4.ensure()                                       # 主线程启动者，默认 12s → 应被钳到 3s
    dt_main = time.perf_counter() - t0
    check("A3.1 主线程首次等待 ≈3s（不是 12s）", 2.5 < dt_main < 4.2, "dt=%.3fs" % dt_main)

    # 新 worker，连续 50 次调用测总耗时
    w5 = glctx.GLWorker()
    per = []
    t0 = time.perf_counter()
    for _ in range(50):
        tt = time.perf_counter()
        w5.ensure()
        per.append(time.perf_counter() - tt)
    dt50 = time.perf_counter() - t0
    check("A3.2 50 次 ensure() 总耗时 << 50*12s=600s", dt50 < 6.0,
          "total=%.3fs（首调 %.3fs + 其余 49 次最大 %.4fs）"
          % (dt50, per[0], max(per[1:]) if len(per) > 1 else 0))
    check("A3.3 第 2..50 次每次 < 50ms（绝不重复同步长等）",
          max(per[1:]) < 0.05, "max=%.4fs" % max(per[1:]))

    # 非主线程启动者用调用者给的 to（证明 12 是真实预算，3 只是主线程钳制）
    box = {}

    def run_off():
        ww = glctx.GLWorker()
        t = time.perf_counter()
        ww.ensure(ready_timeout=1.5)
        box["dt"] = time.perf_counter() - t

    th = threading.Thread(target=run_off)
    th.start()
    th.join(8.0)
    check("A3.4 非主线程启动者按 `to` 等待（≈1.5s，未被钳到 3s）",
          1.2 < box.get("dt", -1) < 1.9, "dt=%.3fs" % box.get("dt", -1))

    check("A3.5 常量：GL_READY_TIMEOUT=12.0 / UI=3.0",
          glctx.GLWorker.GL_READY_TIMEOUT == 12.0 and glctx.GLWorker.GL_READY_TIMEOUT_UI == 3.0,
          "%s / %s" % (glctx.GLWorker.GL_READY_TIMEOUT, glctx.GLWorker.GL_READY_TIMEOUT_UI))
finally:
    glctx.preinit = _SAVED_PREINIT
    glctx.GLDecoder = _SAVED_DEC


print("== A4: 上层自动切回 GPU ==")
# A4.a  available() 在就绪后由 False→True（见 A2.6/A2.7）。
check("A4.1 available() 未就绪 False → 就绪 True（已在 A2 观测）", True)


# A4.b  _soft_until_recover：软解中每 3s 重探，worker.available() 变 True 即 return True
class _Frame:
    def to_ndarray(self, format=None):
        return np.zeros((4, 4, 4), dtype=np.uint8)


class _FakeC:
    def decode(self, video=0):
        while True:
            yield _Frame()


class _AvailWorker:
    def __init__(self, avail):
        self._avail = avail

    def available(self):
        return self._avail


class _FakePlayer:
    _GPU_REPROBE_SECS = 0.0
    _SOFT_EMPTY_GIVEUP = mm._SOFT_EMPTY_GIVEUP
    _SOFT_EMPTY_BACKOFF_AFTER = mm._SOFT_EMPTY_BACKOFF_AFTER

    def __init__(self):
        self.c = _FakeC()
        self.st = None
        self._dead = False
        self._qfmt = QImage.Format_ARGB32
        self._max_w = 0
        self._max_h = 0

    def _rewind(self):
        return True


_FakePlayer._soft_until_recover = mm.GpuDxvPlayer._soft_until_recover
fp = _FakePlayer()
g = fp._soft_until_recover(_AvailWorker(True))
recovered = None
try:
    next(g)                                            # 取一帧（软解出帧）
    next(g)                                            # 继续：重探 → available True → return True
except StopIteration as si:
    recovered = si.value
check("A4.2 `_soft_until_recover` 重探到 worker.available() ⇒ return True（切回 GPU）",
      recovered is True, "recovered=%r" % (recovered,))

print("== A5: 锁竞争吃掉就绪预算 ==")
try:
    glctx.preinit = lambda *a, **k: True

    class _LockNeedingDec:
        """复刻真实 `_init`：先抢 `_CREATE_LOCK`（GL_CREATE_TIMEOUT），拿不到就建不起来。"""

        def __init__(self):
            self.ok = False
            self.err = "锁不可用"
            if glctx._gl_enter(glctx.GL_CREATE_TIMEOUT):
                try:
                    self.ok = True
                    self.err = ""
                finally:
                    glctx._gl_exit()

        def decode(self, *a, **k):
            return None

    glctx.GLDecoder = _LockNeedingDec
    got = threading.Event()
    done = threading.Event()

    def holder():
        glctx._CREATE_LOCK.acquire()
        got.set()
        try:
            time.sleep(2.5)                            # > GL_CREATE_TIMEOUT(2.0)
        finally:
            glctx._CREATE_LOCK.release()
            done.set()

    threading.Thread(target=holder, daemon=True).start()
    got.wait(1.0)
    w6 = glctx.GLWorker()
    t0 = time.perf_counter()
    okA5 = w6.ensure(ready_timeout=1.0)
    dtA5 = time.perf_counter() - t0
    check("A5.1 外部持锁 2.5s ⇒ 就绪预算(1.0s)被吃光，ensure 未就绪",
          okA5 is False and dtA5 < 1.6, "ok=%s dt=%.3f" % (okA5, dtA5))
    done.wait(3.0)
    time.sleep(0.4)                                    # 让工作线程把 _gl_enter 超时(2.0s)走完
    check("A5.2（残留行为，诚实记录）首次建上下文因锁竞争失败 ⇒ 被当崩溃置 `_dead`",
          w6._dead is True, "_dead=%s（低可达性：GL 锁仅被唯一工作线程使用）" % w6._dead)
finally:
    glctx.preinit = _SAVED_PREINIT
    glctx.GLDecoder = _SAVED_DEC

# ================================================================ B/C 引擎
CLOCK_T = [1000.0]


class _Clock:
    @staticmethod
    def perf_counter():
        return CLOCK_T[0]

    @staticmethod
    def strftime(*a):
        return "00:00:00"


_SAVED_ENGINE_TIME = engine.time
engine.time = _Clock


class FakeClip:
    def __init__(self, path):
        self.path = path


class FakeVideoPlayer:
    def __init__(self, frames=0, active=True, ended=False):
        self._frames = int(frames)
        self._active = bool(active)
        self._last_t = 0.0
        self._ended = bool(ended)
        self.closed = 0

    def stalled(self, secs=None):
        return False

    def health(self):
        return {"frames": self._frames, "age": None}

    def set_active(self, a):
        self._active = bool(a)

    def set_speed(self, s):
        pass

    def close(self):
        self.closed += 1


class FakeImagePlayer:
    def set_active(self, a):
        pass

    def close(self):
        pass


class FakeLayer:
    def __init__(self, clips, player, cache=None, visible=True):
        self.name = "L"
        self.visible = visible
        self.cur = 0
        self.clips = list(clips)
        self.player = player
        self.prev_player = None
        self.cache = dict(cache) if cache else {}
        self.speed = 1.0
        self.is_kv = False
        self.pause_silent = False


class FakeEngine:
    def __init__(self, get_player):
        self.running = True
        self._silent_log_t = {}
        self._get_player = get_player


FakeEngine._heal_silent = engine.AutoVJEngine._heal_silent
FakeEngine._silent_log = engine.AutoVJEngine._silent_log


def heal(fe, lay):
    engine.AutoVJEngine._heal_silent(fe, lay)


print("== B1: 超过 2.2s ⇒ 重建 1 次，且从缓存摘除重建 ==")
new_p = FakeVideoPlayer(frames=0, active=True)
calls = {"n": 0}


def gp_one(lay, clip):
    calls["n"] += 1
    return new_p


p0 = FakeVideoPlayer(frames=0, active=True)
lay = FakeLayer([FakeClip("noframe.mov")], p0, cache={"noframe.mov": p0})
fe = FakeEngine(gp_one)
CLOCK_T[0] = 5000.0
LOGGED.clear()
heal(fe, lay)                                          # 记起点
check("B1.1 首拍只记起点不动作", calls["n"] == 0, "n=%d" % calls["n"])
CLOCK_T[0] += 1.0
heal(fe, lay)
check("B1.2 未过宽限(1.0s<2.2s)不动作", calls["n"] == 0, "n=%d" % calls["n"])
CLOCK_T[0] += 1.5                                      # 累计 2.5s
LOGGED.clear()
heal(fe, lay)
check("B1.3 越过 _NOFRAME_GRACE ⇒ 重建 1 次", calls["n"] == 1, "n=%d" % calls["n"])
check("B1.4 图层拿到新播放器", lay.player is new_p)
check("B1.5 旧播放器被 close + 从缓存摘除", p0.closed == 1 and lay.cache.get("noframe.mov") is not p0,
      "closed=%d cache=%r" % (p0.closed, lay.cache.get("noframe.mov")))
check("B1.6 写入「无帧重建」日志", any("无帧重建" in m for m in LOGGED), str(LOGGED[-1:])[:90])

print("== B2: 每层 ≥2s、同素材 ≤3 次封顶后只记日志 ==")
made = []


def gp_factory(lay, clip):
    p = FakeVideoPlayer(frames=0, active=True)
    made.append(p)
    return p


p_cap = FakeVideoPlayer(frames=0, active=True)
lay2 = FakeLayer([FakeClip("cap.mov")], p_cap, cache={"cap.mov": p_cap})
fe2 = FakeEngine(gp_factory)
CLOCK_T[0] = 100.0
LOGGED.clear()
heal(fe2, lay2)
# 逐 1.0s 步进，验证 2s 冷却 + 3 次封顶
rebuild_times = []
t = 100.0
for _ in range(40):
    t += 1.0
    CLOCK_T[0] = t
    before = len(made)
    heal(fe2, lay2)
    if len(made) > before:
        rebuild_times.append(t)
check("B2.1 同素材累计恰好重建 3 次", len(made) == 3, "rebuilds=%d" % len(made))
gaps = [round(rebuild_times[i + 1] - rebuild_times[i], 1) for i in range(len(rebuild_times) - 1)]
check("B2.2 相邻两次重建间隔 ≥2s（图层级冷却）", all(g >= 2.0 for g in gaps), "gaps=%s" % gaps)
check("B2.3 到顶后写「无帧封顶」日志", any("无帧封顶" in m for m in LOGGED),
      str([m for m in LOGGED if "无帧封顶" in m][-1:])[:100])

print("== B3: 换素材后计数复位 ==")
made2 = []


def gp_factory2(lay, clip):
    p = FakeVideoPlayer(frames=0, active=True)
    made2.append(p)
    return p


p_b = FakeVideoPlayer(frames=0, active=True)
lay_b = FakeLayer([FakeClip("cap.mov")], p_b, cache={"cap.mov": p_b})
lay_b._silent_noframe_path = "cap.mov"
lay_b._silent_noframe_count = 3                         # 模拟已封顶
lay_b._silent_noframe_last_t = 0.0
fe_b = FakeEngine(gp_factory2)
CLOCK_T[0] = 500.0
LOGGED.clear()
heal(fe_b, lay_b)                                       # 记起点（换素材 → 计数复位 0）
lay_b.clips = [FakeClip("other.mov")]                   # 换素材
lay_b.player = FakeVideoPlayer(frames=0, active=True)
lay_b.cache = {}
lay_b._silent_noframe_since = 0.0
for _ in range(12):                                     # 12*2.5s
    CLOCK_T[0] += 2.5
    heal(fe_b, lay_b)
check("B3.1 换素材后重新计数，能再次重建（≤3）", 0 < len(made2) <= 3, "rebuilds=%d" % len(made2))

print("== B4: 图片/GIF ⇒ 0 次动作 ==")
img_calls = {"n": 0}


def gp_img(lay, clip):
    img_calls["n"] += 1
    return FakeImagePlayer()


img = FakeImagePlayer()
lay4 = FakeLayer([FakeClip("pic.png")], img, cache={"pic.png": img})
fe4 = FakeEngine(gp_img)
CLOCK_T[0] = 700.0
LOGGED.clear()
for _ in range(10):
    CLOCK_T[0] += 2.5
    heal(fe4, lay4)
check("B4.1 图片图层 0 重建 / 0 无帧日志",
      img_calls["n"] == 0 and not any("无帧" in m for m in LOGGED),
      "n=%d logs=%s" % (img_calls["n"], LOGGED[:2]))

print("== B5: 健康播放器（frames 在涨）⇒ 0 次动作 ==")
ok_calls = {"n": 0}


def gp_ok(lay, clip):
    ok_calls["n"] += 1
    return FakeVideoPlayer(frames=100)


hp = FakeVideoPlayer(frames=1, active=True)
lay5 = FakeLayer([FakeClip("ok.mov")], hp, cache={"ok.mov": hp})
fe5 = FakeEngine(gp_ok)
CLOCK_T[0] = 900.0
LOGGED.clear()
for i in range(20):
    hp._frames += 5                                     # 帧数持续增长
    CLOCK_T[0] += 2.5
    heal(fe5, lay5)
check("B5.1 健康播放器 0 重建 / 0 无帧日志",
      ok_calls["n"] == 0 and not any("无帧" in m for m in LOGGED),
      "n=%d logs=%s" % (ok_calls["n"], LOGGED[:2]))

print("== B6: 模拟 10 分钟高频 tick（20Hz）⇒ 重建次数有上界 ==")
made3 = []


def gp_s3(lay, clip):
    p = FakeVideoPlayer(frames=0, active=True)
    made3.append(p)
    return p


p_s3 = FakeVideoPlayer(frames=0, active=True)
lay6 = FakeLayer([FakeClip("never.mov")], p_s3, cache={"never.mov": p_s3})
fe6 = FakeEngine(gp_s3)
CLOCK_T[0] = 10000.0
LOGGED.clear()
N_TICKS = 600 * 20                                       # 10 分钟 @20Hz = 12000 tick
for _ in range(N_TICKS):
    CLOCK_T[0] += 0.05
    heal(fe6, lay6)
check("B6.1 10min 高频 tick 重建次数有上界（≤3，未回到重建风暴）",
      0 < len(made3) <= 3, "rebuilds=%d / %d ticks" % (len(made3), N_TICKS))
check("B6.2 封顶后只记日志（有「无帧封顶」）", any("无帧封顶" in m for m in LOGGED))

print("== C: 健康日志 gpu= / 状态变化才额外记 / 无绝对路径 ==")


class _FakeWk:
    def __init__(self):
        self._a = True
        self._msg = ""

    def available(self):
        return self._a

    def last_message(self):
        return self._msg


_WK = _FakeWk()
_SAVED_WORKER = glctx.GLWorker


class _FakeWkHolder:
    @classmethod
    def get(cls):
        return _WK


glctx.GLWorker = _FakeWkHolder
FakeEngine._gpu_token = engine.AutoVJEngine._gpu_token
FakeEngine._health_snapshot = engine.AutoVJEngine._health_snapshot
try:
    fec = FakeEngine(lambda lay, clip: None)
    fec.layers = []
    fec._gpu_state = -1
    LOGGED.clear()
    t1 = fec._gpu_token()                               # -1 → '1'：变化，记一行
    n_after_first = len(LOGGED)
    t2 = fec._gpu_token()                               # '1' → '1'：不变，不记
    n_after_same = len(LOGGED)
    _WK._a = False                                      # '1' → '0'：变化，记一行
    t3 = fec._gpu_token()
    n_after_change = len(LOGGED)
    check("C1 首次取号写一行（状态变化 -1→1）", n_after_first == 1,
          "tok=%s n=%d %s" % (t1, n_after_first, LOGGED[:1]))
    check("C2 状态不变不写（不刷屏）", n_after_same == n_after_first,
          "tok=%s n=%d" % (t2, n_after_same))
    check("C3 状态变化(1→0)再写一行", n_after_change == n_after_first + 1,
          "tok=%s n=%d %s" % (t3, n_after_change, LOGGED[-1:]))

    # 快照含 gpu= 且无绝对路径
    hp2 = FakeVideoPlayer(frames=123)
    hp2._last_t = CLOCK_T[0] - 0.03
    lay_c = FakeLayer([FakeClip("C:\\secret\\path\\x.mov")], hp2, cache={"C:\\secret\\path\\x.mov": hp2})
    fec.layers = [lay_c]
    snap = engine.AutoVJEngine._health_snapshot(fec, CLOCK_T[0])
    check("C4 快照行含 gpu= 记号", "gpu=" in snap, snap[:70])
    check("C5 gpu= 取值属于 {1,0,?}", snap.startswith(("gpu=1", "gpu=0", "gpu=?")), snap[:6])
    check("C6 快照不含绝对路径（无盘符/反斜杠/文件名）",
          ":" not in snap and "\\" not in snap and "secret" not in snap, snap)
finally:
    glctx.GLWorker = _SAVED_WORKER

engine.time = _SAVED_ENGINE_TIME
print()
print("结果：%s" % ("全部通过 ✓" if not FAILS else "失败 %d 项 ✗ %s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
