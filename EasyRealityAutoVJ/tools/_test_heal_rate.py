# -*- coding: utf-8 -*-
"""回归测试：卡帧看门狗 `engine._heal_stalled` **不得失控**（2026-10-01 现场卡死根因）。

现场事故：一个"从未解出首帧"的素材被看门狗**每个 tick 同步重建**（开容器 = I/O + 起线程），
日志 90 秒里重建 70+ 次 ⇒ tick 速率崩塌 ⇒ 自动切换停摆、音频/界面饿死 ⇒ 卡死。

本测试**不启动 GUI、不解码真视频**：用自己造的假图层 / 假播放器 + 可控时钟，
直接调用 `AutoVJEngine._heal_stalled`，断言：

  (a) 从未出帧的播放器 ⇒ **绝不**重建（甚至连 `stalled()` 都不该被调用）；
  (b) 出过帧后卡住的 ⇒ **会**重建（确认真卡帧仍能自愈）；
  (c) 连续模拟 N 秒 tick ⇒ 重建次数 ≤ 设计上限（远小于每 tick 一次）；
  (d) `running=False`（预览/停止态）⇒ 不重建；
  (e) 同一素材超过封顶后 ⇒ 不再重建（且放弃日志只写一次）；
  (f) 健康播放器（出过帧、未卡）⇒ 不重建，热路径**不调用** `health()`，
      且 `stalled()` 受检查节流（≤ tick 数/10）；
  (g) 换素材 ⇒ 计数复位，新素材允许重新重建；
  (h) 真实健康态（`_last_t` 每帧更新）⇒ `stalled()` 调用 **== 0**（廉价健康快路）；
  (i) 卡死态（`_last_t` 停在很久以前）⇒ 检查 ≤ tick 数/10，且仍能自愈重建。

用法（离屏）：QT_QPA_PLATFORM=offscreen venv/Scripts/python.exe -u tools/_test_heal_rate.py
退出码 0 = 全部通过；1 = 有断言失败。
"""
import os
import sys

# 必须离屏：本测试不建 QApplication，但导入 engine 会拉进 PySide6。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

import engine                                                    # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


# ---------------------------------------------------------------- 夹具
class FakeClip:
    def __init__(self, path):
        self.path = path


class FakePlayer:
    """最小假播放器：只实现 `_heal_stalled` 会碰到的接口。"""

    def __init__(self, frames=0, stalled=True, last_t=0.0):
        self._frames = frames        # 产帧计数（0 = 从未出帧）
        self._active = True          # 活跃（不处于静音暂停 / 预热挂起）
        self._stalled = stalled      # `stalled()` 的返回（模拟"冻在某一帧"）
        # 最近一次产帧时刻（真实播放器每产一帧由 `_mark_frame()` 更新）。默认 0.0 =
        # 无 `_last_t`，等价于缺失 ⇒ `getattr` 兜底为 0 ⇒ 走不到健康快路（保持旧断言语义）。
        self._last_t = float(last_t)
        self.closed = 0
        self.stalled_calls = 0
        self.health_calls = 0

    def stalled(self, secs=None):
        self.stalled_calls += 1
        return self._stalled

    def health(self):
        self.health_calls += 1
        return {"reason": "测试注入"}

    def close(self):
        self.closed += 1


class FakeLayer:
    def __init__(self, clip, player):
        self.visible = True
        self.cur = 0
        self.clips = [clip]
        self.cache = {clip.path: player}
        self.player = player
        # 注意：**故意不**预置 _heal_* 字段，验证看门狗用 getattr 兜底也能正常工作。


class FakeEngine:
    """假引擎：只提供 `_heal_stalled` 需要的 self.running / 全局时间戳 / _get_player。"""

    def __init__(self, running=True):
        self.running = running
        self._heal_last_global_t = 0.0
        self.rebuilds = 0
        self.created = []

    def _get_player(self, lay, clip):
        """重建：返回一个"出过帧但再次卡住"的新播放器，让看门狗会持续想重建它。"""
        self.rebuilds += 1
        p = FakePlayer(frames=5, stalled=True)
        self.created.append(p)
        lay.cache[clip.path] = p
        return p


# ---------------------------------------------------------------- 可控时钟
class _Clock:
    def __init__(self, t0=1000.0):
        self.t = t0

    def perf_counter(self):
        return self.t


_real_engine_time = engine.time
CLOCK = _Clock()


def tick(eng, lay):
    """模拟一帧：调用看门狗（等价于 `_tick_locked` 里那次 `self._heal_stalled(lay)`）。"""
    engine.AutoVJEngine._heal_stalled(eng, lay)


# 捕获看门狗日志
LOGGED = []
try:
    import stallwatch
    stallwatch.log_line = lambda m: LOGGED.append(m)
except Exception:                                                # noqa: BLE001
    pass

engine.time = CLOCK      # 用可控时钟替换引擎模块的时间源（只影响本测试进程的 engine）

print("看门狗参数：冷却=%.1fs 同素材封顶=%d 全局间隔=%.1fs"
      % (engine._HEAL_CLIP_COOLDOWN, engine._HEAL_MAX_PER_CLIP, engine._HEAL_GLOBAL_INTERVAL))

# ================================================================
print("\n(a) 从未出帧的播放器 ⇒ 绝不重建（连 stalled() 都不该调）")
eng = FakeEngine(running=True)
clip = FakeClip("never_frame.mov")
p0 = FakePlayer(frames=0, stalled=True)       # frames=0：从未出帧，但 stalled() 会返回 True
lay = FakeLayer(clip, p0)
CLOCK.t = 1000.0
for _ in range(600):                          # 模拟 600 帧（远超旧代码 6s 误判窗口）
    tick(eng, lay)
    CLOCK.t += 0.05
check("从未出帧 ⇒ 重建 0 次", eng.rebuilds == 0, "rebuilds=%d" % eng.rebuilds)
check("从未出帧 ⇒ 没有关闭播放器", p0.closed == 0, "closed=%d" % p0.closed)
check("从未出帧 ⇒ 连 stalled() 都没被调用（热路径廉价）", p0.stalled_calls == 0,
      "stalled_calls=%d" % p0.stalled_calls)

# ================================================================
print("\n(b) 出过帧后卡住 ⇒ 会重建（真卡帧仍能自愈）")
eng = FakeEngine(running=True)
clip = FakeClip("real_stall.mov")
p1 = FakePlayer(frames=5, stalled=True)       # frames>0：出过帧，现在卡住
lay = FakeLayer(clip, p1)
CLOCK.t = 2000.0
tick(eng, lay)
check("出过帧的卡帧 ⇒ 重建了 1 次", eng.rebuilds == 1, "rebuilds=%d" % eng.rebuilds)
check("旧播放器被关闭", p1.closed == 1, "closed=%d" % p1.closed)
check("图层换成了新播放器", lay.player is eng.created[-1])
check("缓存里也是新播放器（没留僵尸）", lay.cache.get(clip.path) is eng.created[-1])

# ================================================================
print("\n(c) 连续模拟 N 秒 tick ⇒ 重建次数 ≤ 设计上限（远小于每 tick 一次）")
eng = FakeEngine(running=True)
clip = FakeClip("storm.mov")
p2 = FakePlayer(frames=5, stalled=True)
lay = FakeLayer(clip, p2)
CLOCK.t = 3000.0
TICKS, DT = 300, 0.05                          # 300 帧 × 0.05s = 15 秒
for _ in range(TICKS):
    tick(eng, lay)
    CLOCK.t += DT
elapsed = TICKS * DT
print("     模拟 %.0f 秒 / %d 帧 ⇒ 重建 %d 次（旧代码会是每帧一次 = %d 次）"
      % (elapsed, TICKS, eng.rebuilds, TICKS))
check("重建次数 == 同素材封顶（冷却生效 + 封顶生效）",
      eng.rebuilds == engine._HEAL_MAX_PER_CLIP,
      "rebuilds=%d 上限=%d" % (eng.rebuilds, engine._HEAL_MAX_PER_CLIP))
check("重建次数远小于 tick 数（没有每帧重建风暴）",
      eng.rebuilds <= TICKS // 20, "rebuilds=%d ticks=%d" % (eng.rebuilds, TICKS))

# ================================================================
print("\n(e) 到顶后不再重建，且放弃日志只写一次")
before = eng.rebuilds
player_at_cap = lay.player
for _ in range(200):                          # 再跑 200 帧（时钟早已越过冷却）
    tick(eng, lay)
    CLOCK.t += 0.05
check("封顶后重建次数不再增加", eng.rebuilds == before, "before=%d after=%d" % (before, eng.rebuilds))
check("封顶后图层播放器不再被替换", lay.player is player_at_cap)
giveup = [m for m in LOGGED if "放弃重建" in m]
check("放弃日志只写了一次", len(giveup) == 1, "count=%d %s" % (len(giveup), giveup[:1]))

# ================================================================
print("\n(d) running=False（预览/停止态）⇒ 不重建")
eng = FakeEngine(running=False)
clip = FakeClip("stopped.mov")
p3 = FakePlayer(frames=5, stalled=True)
lay = FakeLayer(clip, p3)
CLOCK.t = 5000.0
for _ in range(100):
    tick(eng, lay)
    CLOCK.t += 0.05
check("running=False ⇒ 重建 0 次", eng.rebuilds == 0, "rebuilds=%d" % eng.rebuilds)
check("running=False ⇒ 不关闭播放器", p3.closed == 0, "closed=%d" % p3.closed)

# ================================================================
print("\n(f) 健康播放器（出过帧、未卡）⇒ 不重建，且热路径节流（不每 tick 都调 stalled()）")
eng = FakeEngine(running=True)
clip = FakeClip("healthy.mov")
p4 = FakePlayer(frames=5, stalled=False)      # 正常出帧，未卡
lay = FakeLayer(clip, p4)
CLOCK.t = 6000.0
for _ in range(300):
    tick(eng, lay)
    CLOCK.t += 0.05
print("     健康场景：%d 帧 ⇒ stalled() 调用 %d 次（节流前会是 %d 次）"
      % (300, p4.stalled_calls, 300))
check("健康播放器 ⇒ 不重建", eng.rebuilds == 0, "rebuilds=%d" % eng.rebuilds)
check("热路径没调用 health()（只在真要重建时才调）", p4.health_calls == 0,
      "health_calls=%d" % p4.health_calls)
check("健康素材检查节流：stalled() 调用 ≤ tick 数/10（0.5s 间隔 @20Hz）",
      p4.stalled_calls <= 300 // 10, "stalled_calls=%d" % p4.stalled_calls)

# ================================================================
print("\n(g) 换素材 ⇒ 计数复位，新素材允许重新重建")
eng = FakeEngine(running=True)
clip_a = FakeClip("cap_a.mov")
lay = FakeLayer(clip_a, FakePlayer(frames=5, stalled=True))
CLOCK.t = 7000.0
for _ in range(400):                          # 先把 A 素材撞到封顶
    tick(eng, lay)
    CLOCK.t += 0.05
capped_a = eng.rebuilds
check("A 素材已封顶（重建 == 上限）", capped_a == engine._HEAL_MAX_PER_CLIP,
      "rebuilds=%d" % capped_a)
# 切换到新素材 B（不同 path）⇒ 计数应复位
clip_b = FakeClip("cap_b.mov")
lay.clips = [clip_b]
lay.cur = 0
lay.player = FakePlayer(frames=5, stalled=True)
lay.cache = {clip_b.path: lay.player}
CLOCK.t += 10.0                               # 越过冷却/全局限流
tick(eng, lay)
check("换素材后计数复位 ⇒ 新素材允许重建 1 次",
      eng.rebuilds == capped_a + 1,
      "before=%d after=%d" % (capped_a, eng.rebuilds))

# ================================================================
print("\n(h) 真实健康态（_last_t 每帧都在更新）⇒ stalled() 调用 == 0（廉价健康快路）")
eng = FakeEngine(running=True)
clip = FakeClip("live_healthy.mov")
p5 = FakePlayer(frames=100, stalled=False)    # 出过帧、未卡、且持续产帧
lay = FakeLayer(clip, p5)
CLOCK.t = 8000.0
for _ in range(600):                          # 30s @20Hz
    p5._last_t = CLOCK.t - 0.033              # 模拟 30fps 每帧更新 `_last_t`
    tick(eng, lay)
    CLOCK.t += 0.05
print("     真实健康态：%d 帧 / %d 次 tick ⇒ stalled() 调用 %d 次（节流前 600，节流后 60，快路后 0）"
      % (600, 600, p5.stalled_calls))
check("真实健康态 ⇒ stalled() 调用 == 0（快路直接放过，连判据都不调）",
      p5.stalled_calls == 0, "stalled_calls=%d" % p5.stalled_calls)
check("真实健康态 ⇒ 不重建", eng.rebuilds == 0, "rebuilds=%d" % eng.rebuilds)

# ================================================================
print("\n(i) 卡死态（_last_t 停在很久以前）⇒ 检查 ≤ tick 数/10，且仍能自愈重建")
eng = FakeEngine(running=True)
clip = FakeClip("dead_stale.mov")
p6 = FakePlayer(frames=500, stalled=True)
lay = FakeLayer(clip, p6)
CLOCK.t = 9000.0
p6._last_t = CLOCK.t - 100.0                  # 很久没产帧（远 > _STALL_SECS）
for _ in range(600):                          # 30s @20Hz
    tick(eng, lay)
    CLOCK.t += 0.05
total_stalled = p6.stalled_calls + sum(q.stalled_calls for q in eng.created)
print("     卡死态：%d 次 tick ⇒ 重建 %d 次 / stalled() 合计 %d 次（上限 %d）"
      % (600, eng.rebuilds, total_stalled, 600 // 10))
check("卡死态 ⇒ 仍能自愈重建（重建 == 同素材封顶）",
      eng.rebuilds == engine._HEAL_MAX_PER_CLIP, "rebuilds=%d" % eng.rebuilds)
check("卡死态 ⇒ 检查次数 ≤ tick 数/10",
      total_stalled <= 600 // 10, "stalled_calls=%d" % total_stalled)

# ================================================================
engine.time = _real_engine_time
print()
print("结果：%s" % ("全部通过 ✓" if not FAILS else "失败 %d 项 ✗ %s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
