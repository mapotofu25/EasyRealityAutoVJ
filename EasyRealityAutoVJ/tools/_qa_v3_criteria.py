# -*- coding: utf-8 -*-
"""V3 —— 判据修正正确性（两条根因分别验证）。

  (1) **从未出过帧**的播放器（指向损坏/不存在的文件）⇒ 看门狗重建 **必须 = 0**。
      用**真实解码器**（`make_player` 指向不存在的文件）验证：解码线程会自救收工
      （`_ended=True`），但看门狗绝不因 `_frames==0` 去重建它（这正是根因②）。
  (2) **出过帧后卡住**的 ⇒ 必须在合理时间（≤ 5s 量级）内被重建 1 次，且之后不再反复。
      · 替换后若健康 ⇒ 恰好重建 1 次；
      · 替换后仍卡 ⇒ 两次重建间隔 ≥ 冷却(5s)，且总数封顶(3)。

用法：QT_QPA_PLATFORM=offscreen venv/Scripts/python.exe -u tools/_qa_v3_criteria.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from PySide6.QtWidgets import QApplication                          # noqa: E402
from PySide6.QtGui import QImage                                    # noqa: E402
_app = QApplication.instance() or QApplication([])

import engine                                                       # noqa: E402
import media_manager as mm                                          # noqa: E402

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


# ================================================================ (1) 真实损坏文件
class EngineShell:
    def __init__(self):
        self.running = True
        self._heal_last_global_t = 0.0
        self.canvas = QImage(1280, 720, QImage.Format_RGB32)
        self.layers = []


EngineShell._heal_stalled = engine.AutoVJEngine._heal_stalled
EngineShell._get_player = engine.AutoVJEngine._get_player


class Clip:
    def __init__(self, path):
        self.path = path


def test_never_frames_real():
    print("(1) 从未出帧的真实播放器（文件不存在）⇒ 看门狗重建必须 = 0")
    shell = EngineShell()
    lay = engine.Layer("V3-broken")
    lay.cur = 0
    bad = os.path.join(os.environ.get("TEMP", "/tmp"), "autovj_qa_nonexistent_%d.mov" % os.getpid())
    assert not os.path.exists(bad)
    clip = mm.MediaItem(bad)
    clip._has_alpha = False
    lay.clips = [clip]
    p = mm.make_player(clip)                      # 真实 VideoPlayer，打开会一直失败
    p.set_max_size(1920, 1080)
    p.set_active(True)
    lay.player = p
    lay.cache[clip.path] = p
    lay._heal_path = clip.path

    rebuilds = []
    orig_close = p.close

    def spy_close():
        rebuilds.append(("close", time.perf_counter()))
        return orig_close()

    p.close = spy_close

    t0 = time.perf_counter()
    ticks = 0
    while time.perf_counter() - t0 < 10.0:         # 跑 10s（> 旧代码 6s 误判窗口）
        p.set_active(True)
        old = lay.player
        engine.AutoVJEngine._heal_stalled(shell, lay)
        if lay.player is not old:
            rebuilds.append(("replaced", time.perf_counter()))
        ticks += 1
        time.sleep(1.0 / 60.0)
    dur = time.perf_counter() - t0
    print("     实测 %.1fs / %d ticks；frames=%d  _ended=%s  last_t=%s  重建事件=%d"
          % (dur, ticks, getattr(p, "_frames", 0), getattr(p, "_ended", None),
             getattr(p, "_last_t", 0.0), len(rebuilds)))
    check("从未出帧 ⇒ _frames==0", getattr(p, "_frames", 0) == 0,
          "frames=%d" % getattr(p, "_frames", 0))
    check("从未出帧 ⇒ 看门狗重建 == 0（看门狗不碰它）", len(rebuilds) == 0,
          "events=%d" % len(rebuilds))
    check("解码线程已自救收工（_ended=True，交给 _get_player 健康校验）",
          getattr(p, "_ended", False) is True, "_ended=%s" % getattr(p, "_ended", None))
    try:
        p.close()
    except Exception:                              # noqa: BLE001
        pass


# ================================================================ (2) 出过帧后卡住
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
        return {"reason": "V3"}

    def close(self):
        self.closed += 1


class FakeLayer:
    def __init__(self, clip, player):
        self.visible = True
        self.cur = 0
        self.clips = [clip]
        self.cache = {clip.path: player}
        self.player = player


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def perf_counter(self):
        return self.t


_real_time = engine.time


def make_engine_healthy_replacement():
    class E:
        def __init__(self):
            self.running = True
            self._heal_last_global_t = 0.0
            self.times = []

        def _get_player(self, lay, clip):
            self.times.append(_C.t)
            p = FakePlayer(frames=5, stalled=False)   # 重建后健康 ⇒ 不该再重建
            lay.cache[clip.path] = p
            return p
    return E()


def make_engine_stuck_replacement():
    class E:
        def __init__(self):
            self.running = True
            self._heal_last_global_t = 0.0
            self.times = []

        def _get_player(self, lay, clip):
            self.times.append(_C.t)
            p = FakePlayer(frames=5, stalled=True)    # 重建后仍卡 ⇒ 受冷却/封顶约束
            lay.cache[clip.path] = p
            return p
    return E()


_C = Clock()
engine.time = _C

print("\n(2a) 出过帧后卡住、重建后健康 ⇒ 恰好重建 1 次（且不反复）")
eng = make_engine_healthy_replacement()
clip = FakeClip("stuck_then_ok.mov")
lay = FakeLayer(clip, FakePlayer(frames=5, stalled=True))
_C.t = 5000.0
rebuild_t = None
for _ in range(int(60 / 0.05)):                    # 模拟 60 秒
    engine.AutoVJEngine._heal_stalled(eng, lay)
    _C.t += 0.05
check("恰好重建 1 次", len(eng.times) == 1, "rebuilds=%d" % len(eng.times))
check("重建发生在首帧检测后 ≤ 5s",
      eng.times and (eng.times[0] - 5000.0) <= 5.0 + 1e-6,
      "delay=%.2fs" % ((eng.times[0] - 5000.0) if eng.times else -1))
check("之后不再反复（60s 内仍只有 1 次）", len(eng.times) == 1, "rebuilds=%d" % len(eng.times))

print("\n(2b) 出过帧后卡住、重建后仍卡 ⇒ 间隔 ≥ 冷却(5s) 且总数封顶(%d)"
      % engine._HEAL_MAX_PER_CLIP)
eng = make_engine_stuck_replacement()
clip = FakeClip("always_stuck.mov")
lay = FakeLayer(clip, FakePlayer(frames=5, stalled=True))
_C.t = 20000.0
for _ in range(int(120 / 0.05)):                   # 模拟 120 秒
    engine.AutoVJEngine._heal_stalled(eng, lay)
    _C.t += 0.05
gaps = [eng.times[i] - eng.times[i - 1] for i in range(1, len(eng.times))]
print("     实测：120s 重建 %d 次；间隔=%s 秒" % (len(eng.times), ["%.1f" % g for g in gaps]))
check("总重建 == 封顶(%d)" % engine._HEAL_MAX_PER_CLIP,
      len(eng.times) == engine._HEAL_MAX_PER_CLIP, "rebuilds=%d" % len(eng.times))
check("相邻重建间隔 ≥ 冷却(5s)", all(g >= engine._HEAL_CLIP_COOLDOWN - 1e-6 for g in gaps),
      "gaps=%s" % ["%.2f" % g for g in gaps])

engine.time = _real_time
print()
print("结果：%s" % ("V3 全部通过 ✓" if not FAILS else "V3 失败 %d 项 ✗ %s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
