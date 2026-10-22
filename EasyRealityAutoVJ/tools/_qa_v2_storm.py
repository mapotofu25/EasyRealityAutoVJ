# -*- coding: utf-8 -*-
"""V2 —— 长跑（模拟 10 分钟 tick）不得复发"重建风暴"，重建次数**有界**。

用可控时钟把 10 分钟压缩成瞬时（不必真等）。对比：
  · 旧行为：每个 tick 都重建 ⇒ 60fps × 600s ≈ **36000 次**（这是现场卡死的根因）；
  · 新行为：受「同素材 5s 冷却 + 每素材 3 次封顶 + 全局 2s」三重限流 ⇒ 有界。

场景：
  (a) 健康场景（多图层、都正常出帧）⇒ 10 分钟总重建 **必须 = 0**；
  (b) 单个"出过帧后卡死"的素材 ⇒ 总重建 == 每素材封顶(3)；
  (c) 20 个图层同时卡死 ⇒ 总重建 ≤ 图层数 × 每素材封顶（证明有界，不随 tick 线性增长）。

用法：QT_QPA_PLATFORM=offscreen venv/Scripts/python.exe -u tools/_qa_v2_storm.py
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

    def stalled(self, secs=None):
        return self._stalled

    def health(self):
        return {"reason": "V2"}

    def close(self):
        self.closed += 1


class FakeLayer:
    def __init__(self, name, player, clip):
        self.name = name
        self.visible = True
        self.cur = 0
        self.clips = [clip]
        self.cache = {clip.path: player}
        self.player = player


class FakeEngine:
    def __init__(self):
        self.running = True
        self._heal_last_global_t = 0.0
        self.rebuilds = 0

    def _get_player(self, lay, clip):
        self.rebuilds += 1
        # 重建后给一个"仍然卡死"的新播放器（最坏情况：素材一直坏）
        p = FakePlayer(frames=5, stalled=True)
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

TICKS = 36000          # 10 分钟 @ 60fps
DT = 0.05              # 60fps（20ms/帧；用 50ms 更保守，总时长 30 分钟）
DT = 1.0 / 60.0
DUR = TICKS * DT

print("模拟 %d ticks = %.0f 秒 = %.1f 分钟；旧行为重建数 ≈ %d（每 tick 一次）"
      % (TICKS, DUR, DUR / 60.0, TICKS))

# ---------------- (a) 健康场景 ----------------
print("\n(a) 健康场景：20 个图层都正常出帧、不卡 ⇒ 10 分钟总重建必须 = 0")
eng = FakeEngine()
layers = []
for i in range(20):
    clip = FakeClip("healthy_%d.mov" % i)
    p = FakePlayer(frames=100, stalled=False)      # 出过帧、未卡
    layers.append(FakeLayer("L%d" % i, p, clip))
CLOCK.t = 100000.0
for _ in range(TICKS):
    for lay in layers:
        engine.AutoVJEngine._heal_stalled(eng, lay)
    CLOCK.t += DT
check("健康场景总重建 == 0", eng.rebuilds == 0, "rebuilds=%d" % eng.rebuilds)

# ---------------- (b) 单个卡死素材 ----------------
print("\n(b) 单个'出过帧后卡死'的素材 ⇒ 总重建 == 每素材封顶(%d)"
      % engine._HEAL_MAX_PER_CLIP)
eng = FakeEngine()
clip = FakeClip("stuck.mov")
lay = FakeLayer("L", FakePlayer(frames=5, stalled=True), clip)
CLOCK.t = 200000.0
for _ in range(TICKS):
    engine.AutoVJEngine._heal_stalled(eng, lay)
    CLOCK.t += DT
print("     实测：%.0f 分钟总重建 %d 次（旧行为 ≈ %d 次）"
      % (DUR / 60.0, eng.rebuilds, TICKS))
check("总重建 == 每素材封顶（有界，不随 tick 增长）",
      eng.rebuilds == engine._HEAL_MAX_PER_CLIP,
      "rebuilds=%d" % eng.rebuilds)
check("远小于旧行为（每 tick 一次）", eng.rebuilds <= TICKS // 1000,
      "rebuilds=%d vs 旧 %d" % (eng.rebuilds, TICKS))

# ---------------- (c) 20 图层同时卡死 ----------------
print("\n(c) 20 个图层同时卡死 ⇒ 总重建 ≤ 图层数 × 每素材封顶（证明全局有界）")
eng = FakeEngine()
layers = []
for i in range(20):
    clip = FakeClip("bad_%d.mov" % i)
    layers.append(FakeLayer("L%d" % i, FakePlayer(frames=5, stalled=True), clip))
CLOCK.t = 300000.0
for _ in range(TICKS):
    for lay in layers:
        engine.AutoVJEngine._heal_stalled(eng, lay)
    CLOCK.t += DT
bound = 20 * engine._HEAL_MAX_PER_CLIP
print("     实测：20 图层 %.0f 分钟总重建 %d 次；上界=%d；旧行为 ≈ %d×20=%d"
      % (DUR / 60.0, eng.rebuilds, bound, TICKS, TICKS * 20))
check("20 图层总重建 ≤ 图层数×封顶", eng.rebuilds <= bound, "rebuilds=%d bound=%d" % (eng.rebuilds, bound))
check("全局限流也生效（远小于每 tick×图层）", eng.rebuilds < TICKS, "rebuilds=%d" % eng.rebuilds)

engine.time = _real_time
print()
print("结果：%s" % ("V2 全部通过 ✓" if not FAILS else "V2 失败 %d 项 ✗ %s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
