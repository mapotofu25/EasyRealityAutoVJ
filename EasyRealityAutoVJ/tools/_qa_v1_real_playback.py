# -*- coding: utf-8 -*-
"""V1（最重要）—— 正常播放的素材**绝不**被看门狗误判重建。

与 `_test_heal_rate.py` 的关键区别：本测试用 **真实解码器 + 真实素材 + 真实
`AutoVJEngine._get_player` / `_heal_stalled`**，跑真实的时间（30~60s），而不是
假播放器 + 假 `stalled()`。这样能覆盖真实 `_DecoderHealth` 的 `_frames` / `_last_t`
更新时序（上一轮翻车正是"看假逻辑没问题、真实时序出问题"）。

判据（全部为实测）：
  · 正常播放的素材，看门狗触发重建 **必须 = 0**；
  · 同时断言解码器**确实在产帧**（`_frames` 有增长），否则 0 触发没意义。

用法：QT_QPA_PLATFORM=offscreen venv/Scripts/python.exe -u tools/_qa_v1_real_playback.py
退出码 0=通过，1=失败。
"""
import json
import os
import sys
import time
import types

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


# ---------------------------------------------------------------- 真实素材
def real_videos(limit=None):
    with open(os.path.join(_ROOT, "config.json"), "r", encoding="utf-8") as f:
        cfg = json.load(f)
    out = []
    exts = (".mov", ".mp4", ".mkv", ".avi", ".webm")
    for lay in cfg.get("layers", []):
        for c in (lay.get("clips") or []):
            p = c if isinstance(c, str) else (c.get("path", "") if isinstance(c, dict) else "")
            if p and os.path.exists(p) and os.path.splitext(p)[1].lower() in exts:
                out.append(p)
    for p in cfg.get("library", []):
        if isinstance(p, str) and os.path.exists(p) and os.path.splitext(p)[1].lower() in exts:
            out.append(p)
    seen = []
    for v in out:
        if v not in seen:
            seen.append(v)
    return seen[:limit] if limit else seen


# ---------------------------------------------------------------- 真实引擎壳
class EngineShell:
    """只提供真实 `_heal_stalled` / `_get_player` 需要的属性（其余走真实方法）。"""

    def __init__(self):
        self.running = True
        self._heal_last_global_t = 0.0
        self.canvas = QImage(1280, 720, QImage.Format_RGB32)
        self.layers = []


EngineShell._heal_stalled = engine.AutoVJEngine._heal_stalled
EngineShell._get_player = engine.AutoVJEngine._get_player


def make_real_layer(paths, force_alpha=None):
    lay = engine.Layer("QA-V1")
    lay.cur = 0
    lay.visible = True
    lay.clips = []
    for p in paths:
        it = mm.MediaItem(p)
        if force_alpha is not None:
            it._has_alpha = force_alpha
        lay.clips.append(it)
    clip = lay.clips[0]
    p = mm.make_player(clip)
    p.set_max_size(1920, 1080)
    lay.player = p
    lay.cache[clip.path] = p
    lay._heal_path = clip.path
    return lay


def run_real_playback(label, paths, force_alpha, seconds, cycle_secs=None):
    """真实 tick 循环：每 tick 调真实 `_heal_stalled`，并像一个真 tick 那样置 active。"""
    print("\n[%s] 素材=%s alpha=%s 时长=%.0fs" %
          (label, [os.path.basename(x) for x in paths], force_alpha, seconds))
    shell = EngineShell()
    lay = make_real_layer(paths, force_alpha)
    shell.layers = [lay]
    ptype = type(lay.player).__name__
    print("      播放器类型=%s（真实解码）" % ptype)

    events = []
    tracked = set()                                     # 跑过的所有播放器（累计产帧）
    t0 = time.perf_counter()
    last_switch = t0
    cur_idx = 0
    ticks = 0
    while True:
        now = time.perf_counter()
        el = now - t0
        if el >= seconds:
            break
        # 模拟真实 tick 的 active 维护（等价 `_tick_locked` 里的 set_active）
        lay.player.set_active(True)
        lay.player.set_speed(1.0)
        # 可选：每隔 cycle_secs 换一次素材，覆盖"刚切进来"的时序
        if cycle_secs and el - (last_switch - t0) >= cycle_secs and len(paths) > 1:
            cur_idx = (cur_idx + 1) % len(paths)
            lay.cur = cur_idx
            lay.player = mm.make_player(lay.clips[cur_idx])
            lay.player.set_max_size(1920, 1080)
            lay.cache[lay.clips[cur_idx].path] = lay.player
            last_switch = now
        old = lay.player
        old_frames = getattr(old, "_frames", 0)
        old_last = getattr(old, "_last_t", 0.0)
        engine.AutoVJEngine._heal_stalled(shell, lay)
        tracked.add(lay.player)
        if lay.player is not old:                       # 触发了重建
            events.append({
                "clip": os.path.basename(getattr(lay.clips[lay.cur], "path", "?")),
                "player": type(old).__name__,
                "frames": old_frames,
                "age": (time.perf_counter() - old_last) if old_last else None,
                "at": el,
            })
        ticks += 1
        time.sleep(1.0 / 60.0)                          # ~60fps tick 节奏
    dur = time.perf_counter() - t0
    # ★ 正确度量：跨"所有跑过的播放器"累计产帧（切换素材会新建播放器，单看最后一个会误判）
    grown = sum(getattr(p, "_frames", 0) for p in tracked)
    print("      实测：%.1fs / %d ticks；触发重建 %d 次；累计产帧=%d（%.1f fps 均）"
          % (dur, ticks, len(events), grown, grown / max(dur, 0.001)))
    for e in events[:10]:
        print("        ✗ 触发：clip=%s player=%s frames=%d age=%s"
              % (e["clip"], e["player"], e["frames"],
                 ("%.2f" % e["age"]) if e["age"] is not None else "None(从未出帧)"))
    try:
        lay.close_all() if hasattr(lay, "close_all") else None
    except Exception:                                   # noqa: BLE001
        pass
    for v in lay.cache.values():
        try:
            v.close()
        except Exception:                               # noqa: BLE001
            pass
    check("%s：解码器确实在产帧（累计产帧 > 时长×10fps）" % label, grown > seconds * 10,
          "累计产帧=%d ≈%.0f fps" % (grown, grown / max(dur, 0.001)))
    check("%s：正常播放触发重建 == 0" % label, len(events) == 0,
          "triggers=%d" % len(events))
    return events


def main():
    vids = real_videos()
    print("真实视频库：%d 个" % len(vids))
    if len(vids) < 4:
        print("!! 真实视频不足，无法做 V1")
        sys.exit(2)
    # 场景 A：真实 OpenCV VideoPlayer 长跑 40s
    run_real_playback("V1-A VideoPlayer 长跑", vids[:1], False, 40.0)
    # 场景 B：真实 PyAV AvAlphaPlayer（强制 alpha 走 PyAV），30s
    run_real_playback("V1-B AvAlphaPlayer 长跑", vids[1:2], True, 30.0)
    # 场景 C：多个素材循环切换（每 6s 切一次），覆盖"刚切进来"的时序
    run_real_playback("V1-C 多素材循环切换", vids[2:6], False, 32.0, cycle_secs=6.0)

    print()
    print("结果：%s" % ("V1 全部通过 ✓" if not FAILS else "V1 失败 %d 项 ✗ %s" % (len(FAILS), FAILS)))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
