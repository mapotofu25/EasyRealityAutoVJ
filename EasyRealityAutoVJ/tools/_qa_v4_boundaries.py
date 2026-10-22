# -*- coding: utf-8 -*-
"""V4 —— 边界与不变式。

  (a) `running=False`（预览/停止态）⇒ 触发 0 次；
  (b) 同一素材超过封顶后不再重建，且"放弃重建"日志**只写一次**；
  (c) 换素材后 冷却 / 计数 / 放弃位 **确实重置**（否则用户换个片段就永远不被救）；
  (d) 热路径廉价：不需要重建的那一趟，`stalled()` / `health()` 调用次数 = 0。

用法：QT_QPA_PLATFORM=offscreen venv/Scripts/python.exe -u tools/_qa_v4_boundaries.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

import engine                                                       # noqa: E402

FAILS = []
LOGGED = []
try:
    import stallwatch
    stallwatch.log_line = lambda m: LOGGED.append(m)
except Exception:                                                   # noqa: BLE001
    pass


def check(name, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


class FakeClip:
    def __init__(self, path):
        self.path = path


class FakePlayer:
    def __init__(self, frames, stalled):
        self._frames = frames
        self._active = True
        self._stalled = stalled
        self.closed = 0
        self.stalled_calls = 0
        self.health_calls = 0

    def stalled(self, secs=None):
        self.stalled_calls += 1
        return self._stalled

    def health(self):
        self.health_calls += 1
        return {"reason": "V4"}

    def close(self):
        self.closed += 1


class FakeLayer:
    """多素材图层：支持切换 `cur`（模拟换素材）。"""

    def __init__(self, paths, cur=0):
        self.visible = True
        self.cur = cur
        self.clips = [FakeClip(p) for p in paths]
        self.cache = {}
        self.player = FakePlayer(frames=5, stalled=True)
        self.cache[self.clips[cur].path] = self.player


class FakeEngine:
    def __init__(self, running=True):
        self.running = running
        self._heal_last_global_t = 0.0
        self.rebuilt_paths = []

    def _get_player(self, lay, clip):
        self.rebuilt_paths.append(clip.path)
        p = FakePlayer(frames=5, stalled=True)      # 重建后仍卡（最坏情况）
        lay.cache[clip.path] = p
        return p


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def perf_counter(self):
        return self.t


_real_time = engine.time
CLOCK = Clock()
engine.time = CLOCK
DT = 0.05


def drive(eng, lay, secs):
    for _ in range(int(secs / DT)):
        engine.AutoVJEngine._heal_stalled(eng, lay)
        CLOCK.t += DT


# ---------------- (a) running=False ----------------
print("(a) running=False（预览/停止态）⇒ 触发 0 次")
eng = FakeEngine(running=False)
lay = FakeLayer(["stopped.mov"])
CLOCK.t = 1000.0
drive(eng, lay, 30.0)
check("running=False ⇒ 重建 0 次", len(eng.rebuilt_paths) == 0,
      "rebuilds=%d" % len(eng.rebuilt_paths))
check("running=False ⇒ 播放器未被关闭", lay.player.closed == 0,
      "closed=%d" % lay.player.closed)

# ---------------- (b) 封顶 + 放弃日志只写一次 ----------------
print("\n(b) 同素材到顶后不再重建，'放弃'日志只写一次")
LOGGED.clear()
eng = FakeEngine(running=True)
lay = FakeLayer(["cap.mov"])
CLOCK.t = 5000.0
drive(eng, lay, 60.0)
n_after_storm = len(eng.rebuilt_paths)
player_at_cap = lay.player
drive(eng, lay, 300.0)                              # 再跑 5 分钟
giveup = [m for m in LOGGED if "放弃重建" in m]
print("     60s 重建 %d 次；再来 300s 后仍 %d 次；放弃日志 %d 条"
      % (n_after_storm, len(eng.rebuilt_paths), len(giveup)))
check("达到封顶(%d)后重建次数不再增长" % engine._HEAL_MAX_PER_CLIP,
      len(eng.rebuilt_paths) == engine._HEAL_MAX_PER_CLIP,
      "rebuilds=%d" % len(eng.rebuilt_paths))
check("封顶后图层播放器不再被替换", lay.player is player_at_cap)
check("'放弃重建'日志只写一次", len(giveup) == 1,
      "count=%d %s" % (len(giveup), giveup[:1]))
check("放弃位已置位", getattr(lay, "_heal_gave_up", False) is True)

# ---------------- (c) 换素材重置冷却/计数/放弃 ----------------
print("\n(c) 换素材后 冷却/计数/放弃位 确实重置（否则换片段永远不被救）")
eng = FakeEngine(running=True)
lay = FakeLayer(["clipA.mov", "clipB.mov"], cur=0)
CLOCK.t = 10000.0
drive(eng, lay, 60.0)                               # 把 clipA 打到放弃
check("clipA 已被放弃", getattr(lay, "_heal_gave_up", False) is True)
a_rebuilds = len(eng.rebuilt_paths)
# 换到 clipB
lay.cur = 1
lay.player = FakePlayer(frames=5, stalled=True)
lay.cache[lay.clips[1].path] = lay.player
drive(eng, lay, 0.1)                                # 只跑几帧，让重置发生并允许重建
print("     换素材前 clipA 重建 %d 次；换到 clipB 后 _heal_count=%d gave_up=%s"
      % (a_rebuilds, getattr(lay, "_heal_count", -1), getattr(lay, "_heal_gave_up", None)))
check("换素材后 gave_up 复位为 False", getattr(lay, "_heal_gave_up", True) is False)
check("换素材后 _heal_path 指向新素材",
      getattr(lay, "_heal_path", None) == lay.clips[1].path)
check("换素材后立即又能重建（clipB 被救）", len(eng.rebuilt_paths) > a_rebuilds,
      "before=%d after=%d" % (a_rebuilds, len(eng.rebuilt_paths)))

# ---------------- (d) 热路径廉价 ----------------
print("\n(d) 健康播放器 ⇒ 不重建，且热路径不调用 stalled()/health()")
eng = FakeEngine(running=True)
lay = FakeLayer(["healthy.mov"])
lay.player = FakePlayer(frames=100, stalled=False)   # 健康：出过帧、未卡
lay.cache[lay.clips[0].path] = lay.player
CLOCK.t = 60000.0
drive(eng, lay, 30.0)
check("健康播放器 ⇒ 重建 0 次", len(eng.rebuilt_paths) == 0,
      "rebuilds=%d" % len(eng.rebuilt_paths))
check("热路径未调用 stalled()", lay.player.stalled_calls == 0,
      "stalled_calls=%d" % lay.player.stalled_calls)
check("热路径未调用 health()", lay.player.health_calls == 0,
      "health_calls=%d" % lay.player.health_calls)

engine.time = _real_time
print()
print("结果：%s" % ("V4 全部通过 ✓" if not FAILS else "V4 失败 %d 项 ✗ %s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
