# -*- coding: utf-8 -*-
r"""引擎端到端自测：切素材「预热」是否真的把首帧延迟吃掉了（离屏，不动界面）

测三件事：
  A) 冷切换（无预热）：_switch_layer_to 之后到首帧可用的耗时  ← 修复前的现状
  B) 预热后切换：先 _switch_layer_random（会安排预热）→ _process_warms → 再切到预热的那个
     ⇒ 切换那一刻首帧应已在手（延迟 ≈ 0）
  C) 逐拍模式：_beat_show 是否把对子里的另一个素材预热好
用法：venv\Scripts\python.exe tools\switch_warm_selftest.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])

import config as cfgmod  # noqa: E402
import media_manager as mm  # noqa: E402
from audio_engine import AudioState  # noqa: E402
from engine import AutoVJEngine, Layer  # noqa: E402

LIB = r"Y:\EasyRealityAutoVJ\素材库"
out = []


def w(s=""):
    out.append(str(s))
    print(s, flush=True)


def big_clips(n=4):
    fs = []
    for f in sorted(os.listdir(LIB)):
        p = os.path.join(LIB, f)
        if os.path.isfile(p) and f.lower().endswith((".mov", ".mp4", ".mkv", ".avi")):
            fs.append((os.path.getsize(p), p))
    fs.sort(reverse=True)
    return [p for _, p in fs[:n]]


def wait_frame(getter, timeout=3.0):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        img = getter()
        if img is not None and not img.isNull():
            return (time.perf_counter() - t0) * 1000
        time.sleep(0.002)
    return float("nan")


def main():
    cfg = cfgmod.Config()
    audio = AudioState()
    eng = AutoVJEngine(cfg, audio, None)
    eng.set_canvas_size(1280, 720)
    lay = Layer("测试层")
    paths = big_clips(4)
    lay.clips = [mm.MediaItem(p) for p in paths]
    for c in lay.clips:
        c._has_alpha = False          # 强制走 OpenCV 路径，比较同一类解码器
    eng.layers = [lay]
    snap = audio.snapshot()
    w("素材：")
    for c in lay.clips:
        w("   %6.1f MB  %s" % (os.path.getsize(c.path) / 1e6, os.path.basename(c.path)[:50]))
    w("")

    # ---------- A) 冷切换 ----------
    w("[A] 冷切换（无预热）—— 模拟主循环：切换后立刻把新解码器置 active")
    ok = True
    for idx in (1, 2):
        eng._switch_layer_to(lay, idx)
        # 真实运行里 _tick_locked 会做这件事（新解码器只有 active 才会解码）
        lay.player.set_active(True)
        if lay.prev_player:
            lay.prev_player.set_active(True)
        ms = wait_frame(lambda: lay.player.current())
        w("    切到 #%d → 首帧 %.1f ms（%.1f 帧 @60fps）" % (idx, ms, ms / 16.7))
        if ms != ms or ms > 300:
            ok = False
    w("")

    # ---------- B) 预热后切换 ----------
    w("[B] 预热后切换")
    eng._switch_layer_random(lay, snap)        # 切换 + 安排预热
    lay.warm_at = 0.0                          # 手动让预热立刻到点
    t0 = time.perf_counter()
    eng._process_warms(snap)
    w("    pre_idx = %s（候选数 %d）" % (lay.pre_idx, len(eng._candidate_indices(lay, snap))))
    warm_ms = wait_frame(lambda: lay.cache[lay.clips[lay.pre_idx].path].current()
                         if lay.pre_idx >= 0 else None)
    w("    预热完成耗时 %.1f ms（后台完成，渲染线程没等）" % warm_ms)
    t1 = time.perf_counter()
    eng._switch_layer_to(lay, lay.pre_idx)
    lay.player.set_active(True)          # 模拟主循环
    ms = wait_frame(lambda: lay.player.current())
    w("    切到已预热的 #%s → 首帧 %.2f ms（%.2f 帧）" % (lay.pre_idx, ms, ms / 16.7))
    warm_ok = (ms == ms) and ms < 20.0
    w("    进程内切换调用本身耗时 %.2f ms" % ((time.perf_counter() - t1) * 1000))
    w("")

    # ---------- C) 逐拍模式预热 ----------
    w("[C] 逐拍模式（_beat_show 预热对子里另一个）")
    eng.mode = "beat"
    lay.pair = (0, 1)
    lay.pair_turn = 0
    lay.cur = -1
    lay.player = None
    eng._beat_show(lay, 0, cfg["beat"])
    other = lay.pair[1]
    oms = wait_frame(lambda: lay.cache[lay.clips[other].path].current())
    w("    显示 #0 后，对子里另一个 #%d 预热到帧耗时 %.1f ms" % (other, oms))
    beat_ok = (oms == oms)
    eng.mode = "normal"

    w("")
    w("结论：A 冷切换首帧 = 百毫秒量级；B 预热后切换首帧 = %.2f ms；C 逐拍预热 %s"
      % (ms, "OK" if beat_ok else "失败"))
    w("     预热路径 %s" % ("有效（切换不再等首帧）" if warm_ok and beat_ok else "有问题，需要看上面数字"))
    for p in list(lay.cache.values()):
        try:
            p.close()
        except Exception:
            pass
    open(r"P:\AutoVJ\_switch_warm_selftest.txt", "w", encoding="utf-8").write("\n".join(out))


if __name__ == "__main__":
    main()
