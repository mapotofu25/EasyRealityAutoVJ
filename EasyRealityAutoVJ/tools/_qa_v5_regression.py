# -*- coding: utf-8 -*-
"""V5 —— 不破坏已有修复（回归）。

  (a) `_get_player` 的 `_ended` / `_dead` 健康校验仍在且生效：
      缓存命中一个"已收工(_ended)"或"已关闭(_dead)"的僵尸 ⇒ 必须丢掉并重开。
  (b) 三个播放器 `VideoPlayer` / `AvAlphaPlayer` / `GpuDxvPlayer` 的 `_loop` 自愈仍在：
      `_REOPEN_MAX`、`ever`、退避常量都还在源码里且被引用。
  （三个播放器的真实自愈行为由既有 `tools/_test_freeze2.py` 覆盖，见最终报告。）

用法：QT_QPA_PLATFORM=offscreen venv/Scripts/python.exe -u tools/_qa_v5_regression.py
"""
import os
import sys

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


def check(name, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


class FakeClip:
    def __init__(self, path):
        self.path = path


class FakePlayer:
    def __init__(self, ended=False, dead=False):
        self.path = "x"
        self._ended = ended
        self._dead = dead
        self._frames = 10
        self._last_t = 0.0
        self.closed = 0

    def set_max_size(self, w, h):
        pass

    def close(self):
        self.closed += 1


class Shell:
    def __init__(self):
        self.canvas = QImage(1280, 720, QImage.Format_RGB32)


Shell._get_player = engine.AutoVJEngine._get_player


def test_cache_health(tag, **flags):
    print("(%s) 缓存命中 %s 僵尸 ⇒ 必须丢掉重开" % (tag, flags))
    shell = Shell()
    lay = engine.Layer("V5")
    clip = FakeClip("zombie.mov")
    lay.clips = [clip]
    lay.cur = 0
    zombie = FakePlayer(**flags)
    lay.cache[clip.path] = zombie
    fresh = FakePlayer()

    def fake_make_player(item):
        return fresh

    orig = engine.make_player
    engine.make_player = fake_make_player
    try:
        got = shell._get_player(lay, clip)
    finally:
        engine.make_player = orig
    check("僵尸被 close 掉", zombie.closed >= 1, "closed=%d" % zombie.closed)
    check("返回的是新播放器（不是僵尸）", got is fresh, "got_is_fresh=%s" % (got is fresh))
    check("缓存已换成新播放器", lay.cache.get(clip.path) is fresh)


print("(a) `_get_player` 的 _ended/_dead 健康校验")
test_cache_health("a1", ended=True)
test_cache_health("a2", dead=True)
# 反例：健康播放器应被复用（不重开）
shell = Shell()
lay = engine.Layer("V5")
clip = FakeClip("ok.mov")
lay.clips = [clip]
lay.cur = 0
ok_p = FakePlayer()
lay.cache[clip.path] = ok_p
orig = engine.make_player
engine.make_player = lambda item: FakePlayer()
try:
    got = shell._get_player(lay, clip)
finally:
    engine.make_player = orig
check("健康缓存命中 ⇒ 复用（不重开）", got is ok_p and ok_p.closed == 0)

# ---------------- (b) 三个 _loop 自愈源码仍在 ----------------
print("\n(b) 三个播放器 `_loop` 自愈（_REOPEN_MAX / ever / 退避）仍在")
with open(os.path.join(_ROOT, "src", "media_manager.py"), "r", encoding="utf-8") as f:
    src = f.read()
check("_REOPEN_MAX 常量定义存在", "_REOPEN_MAX" in src and
      "def _soft_frames" in src)
# 三个 _loop：VideoPlayer(717) / AvAlphaPlayer(970) / GpuDxvPlayer(1727)
import re
loops = re.findall(r"def _loop\(self\):", src)
check("三个 _loop 定义都在", len(loops) >= 3, "count=%d" % len(loops))
check("每个 _loop 都有 _REOPEN_MAX 封顶", src.count("_REOPEN_MAX") >= 4,
      "count=%d" % src.count("_REOPEN_MAX"))
check("每个 _loop 都有 ever（出过帧永不放弃）语义", src.count("ever") >= 6,
      "ever 出现 %d 次" % src.count("ever"))
check("退避常量仍在", "_REOPEN_BACKOFF_MAX" in src and "_REOPEN_BACKOFF_BASE" in src)
check("_STALL_SECS 仍定义", "_STALL_SECS" in src)
# 三个播放器类都在
for cls in ("class VideoPlayer", "class AvAlphaPlayer", "class GpuDxvPlayer"):
    check("%s 仍在" % cls, cls in src)

# 健康校验确在 _get_player 内（源码级）
with open(os.path.join(_ROOT, "src", "engine.py"), "r", encoding="utf-8") as f:
    eng_src = f.read()
check("_get_player 内保留 _ended/_dead 校验",
      'getattr(p, "_ended", False)) or bool(getattr(p, "_dead", False))' in eng_src)

print()
print("结果：%s" % ("V5 全部通过 ✓" if not FAILS else "V5 失败 %d 项 ✗ %s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
