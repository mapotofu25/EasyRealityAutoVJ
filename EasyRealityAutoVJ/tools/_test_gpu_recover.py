# -*- coding: utf-8 -*-
"""回归测试：GPU 解码被永久关闭 + 从未出帧播放器不恢复（2026-10-02 现场，进程 25488）。

两个叠加的真问题：
  ① `glctx.GLWorker.ensure()` 启动超时**永久置 `_dead=True`** ⇒ 整场退回软件解码
     （用户当天命中 3 次：ui_stall.log 04:03 / 05:02 / 07:59）⇒ 软解慢 ⇒ 首帧出不来 ⇒ 冻帧。
  ② 可见且当前素材的真解码器**长时间 `_frames==0`**（layer_health.log 连续 8s `frames=0`）
     无人恢复：`_heal_stalled` 因"从未出帧"提前返回、`_heal_silent` 又没覆盖这一类。

本测试**离屏、不建真实 GL 上下文**（把 `GLWorker._run` / `glctx.preinit` 换成受控替身，
只验证**状态机**），断言：
  (a) 启动超时**不置死**；工作线程稍后就绪后，下一次 `ensure()` 非阻塞返回 True；
  (b) 超时后的重试**非阻塞**（大 `ready_timeout` 下也 < 50ms，绝不重复同步长等）；
  (c) 只有工作线程**崩溃**才置 `_dead`（不可恢复）；
  (d) 锁竞争证据：外部持有 `_CREATE_LOCK` 会吃掉"就绪预算"（首建要抢 `GL_CREATE_TIMEOUT`），
      说明"3s 太紧"既有冷启动本身、也有锁获取的叠加 —— 故修复给非阻塞重试而非只调大超时；
  (e) `_heal_silent` 兜底重建"可见 + 当前素材 + 长时间 `_frames==0`"的真解码器；
  (f) 该兜底**限流**：每图层 ≥2s 一次、同素材累计 ≤3 次；到顶只记一行日志、不再重建；
  (g) 图片 / GIF（无 `stalled`）不被误重建；
  (h) 健康播放器（`frames>0`）不被"无帧"分支牵连；
  (i) `_health_snapshot` 行首含 `gpu=`（可用性取证）。

用法（离屏）：QT_QPA_PLATFORM=offscreen venv/Scripts/python.exe -u tools/_test_gpu_recover.py
退出码 0 = 全部通过；1 = 有断言失败。
"""
import os
import sys
import threading
import time

# 必须离屏：引擎是 QObject（__init__ 里建 QTimer）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from PySide6.QtWidgets import QApplication                    # noqa: E402

app = QApplication.instance() or QApplication([])

import glctx                                                    # noqa: E402
import engine                                                  # noqa: E402

# 等"抢锁超时"的工作线程重试建成所需的时间（留足几次 _READY_RETRY_SECS）
_GL_RETRY_WAIT = glctx._READY_RETRY_SECS * 3 + 0.4

FAILS = []


def check(name, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


# ---------------------------------------------------------------- 捕获日志
LOGGED = []
try:
    import stallwatch
    stallwatch.log_line = lambda m: LOGGED.append(m)
except Exception:                                              # noqa: BLE001
    pass


# ---------------------------------------------------------------- 可控时钟（引擎部分）
class _Clock:
    def __init__(self, t0=5000.0):
        self.t = float(t0)

    def perf_counter(self):
        return self.t


CLOCK = _Clock()
_real_engine_time = engine.time
engine.time = CLOCK


# ================================================================
# Part 1：GLWorker 状态机（不建真实 GL 上下文）
# ================================================================
class _FakeDec:
    ok = True
    err = ""

    def decode(self, *a, **k):
        return None


def _install_fake_preinit():
    _saved = glctx.preinit
    glctx.preinit = lambda *a, **k: True
    return _saved


print("(a) 启动超时**不置死**；稍后就绪后 `ensure()` 非阻塞返回 True")
_saved_preinit = _install_fake_preinit()
try:
    w = glctx.GLWorker()

    def slow_run():
        time.sleep(0.6)                       # 模拟冷启动 > ready_timeout
        w._dec = _FakeDec()
        w._ready_evt.set()

    w._run = slow_run
    t0 = time.perf_counter()
    ok1 = w.ensure(ready_timeout=0.2)         # 启动者：同步等 0.2s → 超时
    dt1 = time.perf_counter() - t0
    check("超时返回 False", ok1 is False, "ok=%s dt=%.3f" % (ok1, dt1))
    check("超时**未**置 `_dead`（关键修复）", w._dead is False, "_dead=%s" % w._dead)
    check("超时后 `available()` 仍为 False（还没就绪）", w.available() is False)
    check("超时给了可读原因", "冷启动" in (w.last_message() or ""),
          "msg=%r" % (w.last_message(),))
    time.sleep(0.7)                           # 等工作线程就绪

    t1 = time.perf_counter()
    ok2 = w.ensure()                          # 非阻塞探测
    dt2 = time.perf_counter() - t1
    check("就绪后非阻塞 `ensure()` 返回 True", ok2 is True, "ok=%s dt=%.4f" % (ok2, dt2))
    check("`available()` 变 True（上层可自动切回 GPU）", w.available() is True)
    check("这一次调用**非阻塞**（< 50ms）", dt2 < 0.05, "dt=%.4fs" % dt2)

    # ---- (b) 非阻塞重试：未就绪时，大 ready_timeout 也必须秒回 ----
    print("(b) 超时后的重试非阻塞：大 ready_timeout 下仍 < 50ms")
    w2 = glctx.GLWorker()
    w2._run = lambda: time.sleep(30)          # 永不就绪（模拟慢/挂），但**不崩溃**
    w2.ensure(ready_timeout=0.1)              # 启动者：超时
    t2 = time.perf_counter()
    r = w2.ensure(ready_timeout=999.0)        # 非启动者：必须立刻返回
    dt3 = time.perf_counter() - t2
    check("重试立刻返回 False", r is False, "r=%s dt=%.4f" % (r, dt3))
    check("重试耗时 < 50ms（绝不重复同步长等）", dt3 < 0.05, "dt=%.4fs" % dt3)
    check("重试后仍未置死（慢 ≠ 死）", w2._dead is False, "_dead=%s" % w2._dead)
finally:
    glctx.preinit = _saved_preinit

print("(c) 只有工作线程**崩溃**才置 `_dead`（不可恢复）")
_saved_preinit = _install_fake_preinit()
try:
    w3 = glctx.GLWorker()

    def crash_run():
        w3.last_error = "模拟上下文创建异常"
        w3._dead = True
        w3._ready_evt.set()

    w3._run = crash_run
    okc = w3.ensure(ready_timeout=2.0)
    check("崩溃 ⇒ `ensure()` 返回 False", okc is False, "ok=%s" % okc)
    check("崩溃 ⇒ `_dead` 为 True", w3._dead is True, "_dead=%s" % w3._dead)
    check("崩溃 ⇒ `available()` 为 False", w3.available() is False)
finally:
    glctx.preinit = _saved_preinit

print("(d) 锁竞争证据：外部持有 `_CREATE_LOCK` 会吃掉就绪预算（锁获取叠加在超时之上）")
_saved_preinit = _install_fake_preinit()
try:
    # 外部线程持有 GL 全局锁 2.5s（> GL_CREATE_TIMEOUT=2.0）
    holder_got = threading.Event()
    holder_done = threading.Event()

    def holder():
        glctx._CREATE_LOCK.acquire()
        holder_got.set()
        try:
            time.sleep(2.5)
        finally:
            glctx._CREATE_LOCK.release()
            holder_done.set()

    th = threading.Thread(target=holder, daemon=True)
    th.start()
    holder_got.wait(1.0)

    w4 = glctx.GLWorker()

    def run_needs_lock():
        # 模拟真实 `_init`：先抢锁（GL_CREATE_TIMEOUT），拿不到就"启动慢"
        if glctx._gl_enter(glctx.GL_CREATE_TIMEOUT):
            try:
                w4._dec = _FakeDec()
                w4._ready_evt.set()
            finally:
                glctx._gl_exit()

    w4._run = run_needs_lock
    t3 = time.perf_counter()
    okd = w4.ensure(ready_timeout=1.0)        # 1.0s 预算 < 锁被占 2.5s ⇒ 必超时
    dtd = time.perf_counter() - t3
    check("锁被外部占用 ⇒ 就绪预算被吃光、`ensure` 超时", okd is False, "ok=%s dt=%.3f" % (okd, dtd))
    check("超时**仍未**置死（锁竞争 ≠ 崩溃）", w4._dead is False, "_dead=%s" % w4._dead)
    check("（未触发 5s 污染，`_LOCK_POISON` 保持 False）", glctx._LOCK_POISON[0] is False,
          "poison=%s" % glctx._LOCK_POISON[0])
    holder_done.wait(3.0)
finally:
    glctx.preinit = _saved_preinit


# ---- (d2) 真实 `_run`：抢锁超时 ⇒ 不置死，且释放锁后能恢复 ----
print("(d2) 真实 `_run`：抢锁超时 ⇒ 不置死；释放锁后重试 ⇒ 能恢复")
_saved_preinit2 = _install_fake_preinit()
_saved_decoder = glctx.GLDecoder


class _LockHonestDecoder:
    """忠实模拟 `_init`：真去抢 `_CREATE_LOCK`（短超时）。抢到=ok；抢不到=lock_timeout。"""

    def __init__(self):
        got = glctx._gl_enter(0.05)
        try:
            if not got:
                self.ok = False
                self.lock_timeout = True
                self.err = "GL 锁在 2.0s 内未取得（另一线程正持锁）；本次未就绪，稍后重试"
            else:
                self.ok = True
                self.lock_timeout = False
                self.err = ""
                self.decode = lambda *a, **k: None
        finally:
            if got:
                glctx._gl_exit()


try:
    glctx.GLDecoder = _LockHonestDecoder
    w5 = glctx.GLWorker()
    glctx._CREATE_LOCK.acquire()              # 主线程持锁 = "另一线程正持锁做 GL"
    okd = w5.ensure(ready_timeout=0.6)        # 启动者：锁被占 ⇒ 超时
    check("抢锁超时 ⇒ `ensure()` 返回 False", okd is False, "ok=%s" % okd)
    check("抢锁超时 ⇒ **不置死**（关键不变式）", w5._dead is False, "_dead=%s" % w5._dead)
    alive = w5._thread is not None and w5._thread.is_alive()
    check("抢锁超时 ⇒ 工作线程仍在（重试中）", alive, "alive=%s" % alive)
    check("不变式：线程存活 ⇒ `_dead` 必须为 False",
          (not alive) or (w5._dead is False), "alive=%s _dead=%s" % (alive, w5._dead))
    check("`available()` 仍未就绪（未撒谎）", w5.available() is False)
    glctx._CREATE_LOCK.release()              # 放开锁 ⇒ 下一次重试即建成
    time.sleep(_GL_RETRY_WAIT)                # 等重试循环建成
    check("释放锁后重试 ⇒ 就绪（**能恢复**）",
          w5.ensure() is True and w5._ready_evt.is_set(),
          "ready=%s dead=%s" % (w5._ready_evt.is_set(), w5._dead))
    check("恢复后仍未置死", w5._dead is False, "_dead=%s" % w5._dead)
    check("恢复后 `available()` 为 True", w5.available() is True)
finally:
    glctx.GLDecoder = _saved_decoder
    glctx.preinit = _saved_preinit2

# ---- (c2) 不变式：真正的创建失败 ⇒ 置死；且 `_dead` 只在 `_run` 退出路径置真 ----
print("(c2) 不变式：真正的创建失败 ⇒ 置死；`_dead` 只在 `_run` 退出路径置真")
_saved_preinit3 = _install_fake_preinit()
_saved_decoder2 = glctx.GLDecoder


class _CrashDecoder:
    def __init__(self):
        raise RuntimeError("模拟 glfwCreateWindow 失败")


class _FailDecoder:
    def __init__(self):
        self.ok = False
        self.lock_timeout = False             # 不是抢锁，是真正的创建失败
        self.err = "创建离屏 GL 上下文失败"


try:
    glctx.GLDecoder = _CrashDecoder           # 抛异常 → `_run` 走 except → 置死
    w6 = glctx.GLWorker()
    w6.ensure(ready_timeout=1.0)
    check("创建抛异常 ⇒ `_dead` 置真", w6._dead is True, "_dead=%s" % w6._dead)
    dead_alive = w6._thread is not None and w6._thread.is_alive()
    check("创建抛异常 ⇒ 工作线程已退出（不再存活）", not dead_alive, "alive=%s" % dead_alive)
    check("不变式一致：置死 ⇔ 线程退出", w6._dead is True and not dead_alive)

    glctx.GLDecoder = _FailDecoder            # ok=False 且非抢锁 → 也置死
    w7 = glctx.GLWorker()
    w7.ensure(ready_timeout=1.0)
    check("真失败（`lock_timeout=False`）⇒ 置死", w7._dead is True, "_dead=%s" % w7._dead)
finally:
    glctx.GLDecoder = _saved_decoder2
    glctx.preinit = _saved_preinit3

# ---- (j) 非阻塞重试成本 + 主线程钳制（保持通过的既有断言） ----
print("(j) 就绪后 50 次 `ensure()` 毫秒级；主线程首次等待被钳到 3s")
_saved_preinit4 = _install_fake_preinit()
_saved_ui = glctx.GLWorker.GL_READY_TIMEOUT_UI
try:
    w8 = glctx.GLWorker()

    def ready_run():
        time.sleep(0.1)
        w8._dec = _FakeDec()
        w8._ready_evt.set()

    w8._run = ready_run
    w8.ensure(ready_timeout=1.0)              # 启动者：等一次（~0.1s）
    tj = time.perf_counter()
    ok_all = True
    for _ in range(50):
        ok_all = ok_all and (w8.ensure() is True)
    dtj = time.perf_counter() - tj
    check("50 次非阻塞 `ensure()` 全部 True", ok_all)
    check("50 次总耗时 < 50ms（毫秒级）", dtj < 0.05, "dt=%.4fs" % dtj)

    glctx.GLWorker.GL_READY_TIMEOUT_UI = 0.3  # 临时收紧，避免测试等满 3s
    w9 = glctx.GLWorker()
    w9._run = lambda: time.sleep(30)          # 永不就绪（但**不崩溃**）
    tj2 = time.perf_counter()
    r9 = w9.ensure(ready_timeout=999.0)       # 主线程 ⇒ 钳到 GL_READY_TIMEOUT_UI
    dtj2 = time.perf_counter() - tj2
    check("主线程首次等待被钳（不按 999s）",
          r9 is False and dtj2 < 1.0, "r=%s dt=%.3f" % (r9, dtj2))
    check("主线程钳后仍未置死", w9._dead is False, "_dead=%s" % w9._dead)
finally:
    glctx.GLWorker.GL_READY_TIMEOUT_UI = _saved_ui
    glctx.preinit = _saved_preinit4


# ================================================================
# Part 2：引擎 `_heal_silent` 的"从未出帧"兜底 + `gpu=` 取证
# ================================================================
class FakeClip:
    def __init__(self, path):
        self.path = path


class FakeVideoPlayer:
    """真解码器替身：有 `stalled`/`health`，可设 `_frames`/`_active`/`_ended`。"""

    def __init__(self, frames=0, active=True, ended=False):
        self._frames = int(frames)
        self._active = bool(active)
        self._last_t = 0.0
        self._ended = bool(ended)
        self.speed = 1.0
        self.closed = 0

    def stalled(self, secs=None):
        return False

    def health(self):
        return {"frames": self._frames, "age": None, "reason": "测试"}

    def set_active(self, a):
        self._active = bool(a)

    def set_speed(self, s):
        self.speed = s

    def close(self):
        self.closed += 1


class FakeImagePlayer:
    """图片/GIF 替身：**没有** `stalled`/`_frames`/`_active`。"""

    def set_active(self, a):
        pass

    def set_speed(self, s):
        pass

    def close(self):
        pass


class FakeLayer:
    def __init__(self, clip, player, cache=None, visible=True):
        self.name = "L"
        self.visible = visible
        self.cur = 0
        self.clips = [clip]
        self.player = player
        self.prev_player = None
        self.cache = dict(cache) if cache is not None else {}
        self.speed = 1.0
        self.is_kv = False
        self.pause_silent = False


def make_engine(layers, get_player):
    eng = engine.AutoVJEngine({"auto": {}}, _FakeAudio(), {})
    eng.freeze = False
    eng.running = True
    eng.pause_auto = False
    eng.locked = True
    eng.mode = "normal"
    eng.cfg = {"auto": {}}
    eng.layers = layers
    eng.set_canvas_size(160, 90)
    eng._health_tick = lambda: None
    eng._get_player = get_player
    return eng


class _FakeAudio:
    def snapshot(self):
        return {"level": 0.05, "bass": 0.05, "mid": 0.05, "high": 0.05, "energy": 0.5,
                "bpm": 128.0, "beat_phase": 0.0, "beat_count": 0, "bar_count": 0,
                "downbeat_off": 0.0, "drop": False, "is_beat": False, "is_bar": False,
                "beat_active": True, "onset_density": 0.0, "bpm_stab": 1.0,
                "beat_clarity": 1.0, "spec_complex": 0.0, "energy_trend": 0.0,
                "vocalness": 0.0, "segment": "main", "spectrum": [0.0] * 16,
                "silent_sec": 0.0, "running": True, "lv_db": -20.0,
                "recognized_song_id": -1, "recognized_offset": -1.0,
                "recognized_rate": 1.0, "recognized_t": 0.0, "recognized_votes": None,
                "genre_tags": [], "dbg": {}}


def heal_silent(eng, lay):
    engine.AutoVJEngine._heal_silent(eng, lay)


print("(e) `_heal_silent` 兜底重建「可见 + 当前素材 + 长时间 `_frames==0`」的真解码器")
new_p = FakeVideoPlayer(frames=0, active=True)
_gp_n = {"n": 0}


def gp_one(lay, clip_):
    _gp_n["n"] += 1
    return new_p


p0 = FakeVideoPlayer(frames=0, active=True)
lay = FakeLayer(FakeClip("noframe.mov"), p0, cache={"noframe.mov": p0})
eng = make_engine([lay], gp_one)
CLOCK.t = 30000.0
LOGGED.clear()
heal_silent(eng, lay)                                  # 首拍：仅记起始
check("首拍不动作（只记起始时刻）", _gp_n["n"] == 0, "n=%d" % _gp_n["n"])
CLOCK.t += 1.0
heal_silent(eng, lay)
check("未过宽限（1.0s < %.1fs）不重建" % engine._NOFRAME_GRACE, _gp_n["n"] == 0,
      "n=%d" % _gp_n["n"])
CLOCK.t += 1.5                                         # 累计 2.5s > 2.2s
LOGGED.clear()
heal_silent(eng, lay)
check("越过 `_NOFRAME_GRACE` ⇒ 重建（调用 `_get_player` 1 次）", _gp_n["n"] == 1,
      "n=%d" % _gp_n["n"])
check("图层拿到新播放器", lay.player is new_p, "player=%s" % (lay.player,))
check("旧播放器被 close", p0.closed == 1, "closed=%d" % p0.closed)
check("写了「无帧重建」兜底日志", any("无帧重建" in m for m in LOGGED),
      str(LOGGED[-1:])[:100])

print("(f) 限流：每图层 ≥2s 一次、同素材累计 ≤3 次；到顶只记日志不再重建")
created = []


def gp_factory(lay, clip_):
    p = FakeVideoPlayer(frames=0, active=True)
    created.append(p)
    return p


p_cap0 = FakeVideoPlayer(frames=0, active=True)
lay2 = FakeLayer(FakeClip("cap.mov"), p_cap0, cache={"cap.mov": p_cap0})
eng2 = make_engine([lay2], gp_factory)
CLOCK.t = 100.0
LOGGED.clear()
heal_silent(eng2, lay2)                                # 记起始
for _ in range(14):                                    # 每步 2.5s，跨越多轮宽限 + 冷却
    CLOCK.t += 2.5
    heal_silent(eng2, lay2)
check("同素材累计重建**恰好 3 次**（≤ `_SILENT_NOFRAME_MAX`）", len(created) == 3,
      "rebuilds=%d" % len(created))
check("到顶后记「无帧封顶」日志", any("无帧封顶" in m for m in LOGGED),
      str([m for m in LOGGED if "无帧封顶" in m][-1:])[:110])

print("(g) 图片 / GIF（无 `stalled`）不被「无帧」分支误重建")
_gp_img = {"n": 0}


def gp_img(lay, clip_):
    _gp_img["n"] += 1
    return FakeImagePlayer()


img = FakeImagePlayer()
lay3 = FakeLayer(FakeClip("pic.png"), img, cache={"pic.png": img})
eng3 = make_engine([lay3], gp_img)
CLOCK.t = 200.0
LOGGED.clear()
for _ in range(10):
    CLOCK.t += 2.5
    heal_silent(eng3, lay3)
check("图片图层 ⇒ 无重建", _gp_img["n"] == 0, "n=%d" % _gp_img["n"])
check("图片图层 ⇒ 无「无帧」兜底日志", not any("无帧" in m for m in LOGGED),
      str(LOGGED[:2])[:100])

print("(h) 健康播放器（`frames>0`）不被「无帧」分支牵连")
_gp_ok = {"n": 0}


def gp_ok(lay, clip_):
    _gp_ok["n"] += 1
    return FakeVideoPlayer(frames=999)


hh = FakeVideoPlayer(frames=999, active=True)
lay4 = FakeLayer(FakeClip("ok.mov"), hh, cache={"ok.mov": hh})
eng4 = make_engine([lay4], gp_ok)
CLOCK.t = 300.0
LOGGED.clear()
for _ in range(5):
    CLOCK.t += 2.5
    heal_silent(eng4, lay4)
check("健康播放器 ⇒ 无重建、无「无帧」日志",
      _gp_ok["n"] == 0 and not any("无帧" in m for m in LOGGED),
      "n=%d logs=%s" % (_gp_ok["n"], LOGGED[:2]))

print("(i) `_health_snapshot` 行首含 `gpu=`（GPU 可用性取证）")
snap = engine.AutoVJEngine._health_snapshot(eng4, CLOCK.t)
check("快照含 `gpu=` 记号", "gpu=" in snap, snap[:80])
check("`gpu=` 值属于 {1,0,?}", snap.startswith(("gpu=1", "gpu=0", "gpu=?")), snap[:8])

# ================================================================
engine.time = _real_engine_time
print()
print("结果：%s" % ("全部通过 ✓" if not FAILS else "失败 %d 项 ✗ %s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
