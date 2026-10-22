# -*- coding: utf-8 -*-
"""回归断言：卡帧看门狗热路径 `_heal_stalled` 的 V4 不变式（真正会报警的版本）。

背景：工程师给 `_heal_stalled` 加了「廉价健康快路」——当 `p._frames > 0`
且 `now - p._last_t <= _STALL_SECS` 时**直接 return**（连 `stalled()` 都不调）。
因此本脚本必须用「每帧更新 `_last_t`」的真实健康假体来验证，而不是一个
从不设置 `_last_t` 的合成假体（那种会落到 0.5s 检查节流分支 ⇒ 30s 约 60 次，
旧脚本把「期望 0 / 未满足」写死在字符串里，无论修没修都显示同一句话并 `exit(0)`，
属**误导性取证**，现改为真实断言）：

  场景 A｜真实健康态：`_last_t` 每帧更新（模拟真实播放器 `_mark_frame()`）
      ⇒ 断言 `stalled()` 调用次数 == 0。
  场景 B｜卡死态：`_last_t` 停在很久以前
      ⇒ 断言「检查次数」（stalled()）<= tick 数 / 10，且确实触发重建（>= 1 次）。

任一断言失败即**非零退出**，并打印真实期望值与实测值（不写死文案）。

用法：
  QT_QPA_PLATFORM=offscreen P:/AutoVJ/venv/Scripts/python.exe -u \\
      tools/_qa_repro_stalled_calls.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

import engine                                                       # noqa: E402

TICKS = 600          # 30s @ 20Hz


class Clip:
    def __init__(self, p):
        self.path = p


class Clock:
    """替换 engine.time，让 perf_counter 完全可控。"""

    def __init__(self, t0=90000.0):
        self.t = t0

    def perf_counter(self):
        return self.t


class HealthyPlayer:
    """真实健康播放器：每产一帧 `_mark_frame()` 会更新 `_last_t`。"""

    def __init__(self, clock):
        self._clock = clock
        self._frames = 100
        self._active = True
        self._last_t = clock.t            # 已出帧
        self.stalled_calls = 0
        self.health_calls = 0

    def mark_frame(self):
        self._frames += 1
        self._last_t = self._clock.t

    def stalled(self, secs=None):
        self.stalled_calls += 1
        return False

    def health(self):
        self.health_calls += 1
        return {}

    def close(self):
        pass


class StalledPlayer:
    """卡死播放器：`_last_t` 停在很久以前，`stalled()` 恒 True。"""

    def __init__(self, clock, counter):
        self._clock = clock
        self._counter = counter
        self._frames = 100
        self._active = True
        self._last_t = clock.t - 1000.0   # 很久没产帧
        self.health_calls = 0

    def stalled(self, secs=None):
        self._counter["stalled"] += 1
        return True

    def health(self):
        self.health_calls += 1
        return {"frames": self._frames, "age": 1000.0, "reason": "fake-stalled"}

    def close(self):
        pass


class EngHealthy:
    """健康态引擎桩：不该发生任何重建。"""

    running = True
    _heal_last_global_t = 0.0

    def __init__(self):
        self.rebuilds = 0

    def _get_player(self, lay, clip):
        self.rebuilds += 1
        raise AssertionError("健康素材不该触发重建")


class EngStalled:
    """卡死态引擎桩：重建返回一个新的卡死播放器，并统计重建次数。"""

    running = True
    _heal_last_global_t = 0.0

    def __init__(self, clock, counter):
        self._clock = clock
        self._counter = counter

    def _get_player(self, lay, clip):
        self._counter["rebuilds"] += 1
        return StalledPlayer(self._clock, self._counter)


def make_layer(player):
    lay = type("Layer", (), {})()
    lay.visible = True
    lay.cur = 0
    lay.clips = [Clip("healthy.mov")]
    lay.player = player
    lay.cache = {lay.clips[0].path: player}
    return lay


def run_scenario_a():
    """真实健康态：每帧产帧 ⇒ 断言 stalled() == 0。"""
    clock = Clock()
    real = engine.time
    engine.time = clock
    try:
        player = HealthyPlayer(clock)
        lay = make_layer(player)
        eng = EngHealthy()
        for _ in range(TICKS):
            player.mark_frame()          # ← 关键：模拟真实播放器每帧产帧
            engine.AutoVJEngine._heal_stalled(eng, lay)
            clock.t += 0.05
        return {
            "stalled": player.stalled_calls,
            "health": player.health_calls,
            "rebuilds": eng.rebuilds,
        }
    finally:
        engine.time = real


def run_scenario_b():
    """卡死态：`_last_t` 很久以前 ⇒ 断言检查次数 <= tick/10 且确有重建。"""
    clock = Clock()
    real = engine.time
    engine.time = clock
    try:
        counter = {"stalled": 0, "rebuilds": 0}
        player = StalledPlayer(clock, counter)
        lay = make_layer(player)
        eng = EngStalled(clock, counter)
        for _ in range(TICKS):
            engine.AutoVJEngine._heal_stalled(eng, lay)
            clock.t += 0.05
        return counter
    finally:
        engine.time = real


def main():
    failures = []

    a = run_scenario_a()
    print("[场景 A｜真实健康态] tick 数 = %d" % TICKS)
    print("    stalled() 调用次数 = %d  （期望 == 0）" % a["stalled"])
    print("    health()  调用次数 = %d  （期望 == 0）" % a["health"])
    print("    重建次数           = %d  （期望 == 0）" % a["rebuilds"])
    if a["stalled"] != 0:
        failures.append("A: stalled()=%d，期望 0（健康素材热路径零触碰）"
                        % a["stalled"])
    if a["health"] != 0:
        failures.append("A: health()=%d，期望 0" % a["health"])
    if a["rebuilds"] != 0:
        failures.append("A: rebuilds=%d，期望 0（健康素材不该重建）"
                        % a["rebuilds"])

    b = run_scenario_b()
    limit = TICKS // 10
    print("[场景 B｜卡死态] tick 数 = %d，检查次数上限 = tick/10 = %d" % (TICKS, limit))
    print("    stalled() 调用次数 = %d  （期望 <= %d）" % (b["stalled"], limit))
    print("    重建次数           = %d  （期望 >= 1）" % b["rebuilds"])
    if b["stalled"] > limit:
        failures.append("B: stalled()=%d，超过上限 tick/10=%d（检查节流失效）"
                        % (b["stalled"], limit))
    if b["rebuilds"] < 1:
        failures.append("B: rebuilds=%d，期望 >= 1（卡死素材应被重建）"
                        % b["rebuilds"])

    if failures:
        print("结论：FAIL")
        for f in failures:
            print("  - " + f)
        return 1
    print("结论：PASS（A 健康零触碰；B 有节流且确有重建）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
