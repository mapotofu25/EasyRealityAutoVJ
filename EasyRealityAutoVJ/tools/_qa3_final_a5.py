# -*- coding: utf-8 -*-
"""QA(严过关) 出包前独立复验 —— A5：真实用户配置 120s 离屏压测。

把用户真实配置只读复制到 tempfile.mkdtemp() 后用副本驱动**真实解码器 + 真实
_heal_stalled**，模拟"缓存里闲置的播放器刚被激活"的真实节奏（切换间隔 6s、
提前 6s 预热爱下一个素材 ⇒ 该播放器闲置 ~6s 才被激活，正是现场误杀签名）。

判据（实测）：重建次数 == 0，且累计产帧在增长（证明健康且真在跑，不是没跑）。
绝不写入 P:\\AutoVJ；绝不覆盖用户 config。
"""
import json
import os
import random
import shutil
import sys
import tempfile
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

USER_CFG = r"C:\Users\Administrator\AppData\Local\AutoVJ\config.json"
SECONDS = float(os.environ.get("QA3_A5_SECS", "120"))
SWITCH = 6.0                        # 切换间隔（≈现场 4~8s / 16 拍）
EXTS = (".mov", ".mp4", ".mkv", ".avi", ".webm")
FAILS = []
LOGGED = []
try:
    import stallwatch
    stallwatch.log_line = lambda m: LOGGED.append(m)
except Exception:                                                   # noqa: BLE001
    pass


def check(name, ok, extra=""):
    print(("  PASS " if ok else "  FAIL ") + name + (("   | " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


# ---- 只读复制用户配置到临时目录 ----
tmp = tempfile.mkdtemp(prefix="qa3_a5_")
cfg_copy = os.path.join(tmp, "config.json")
shutil.copy2(USER_CFG, cfg_copy)
cfg = json.load(open(cfg_copy, encoding="utf-8"))

pool = []
for lay in cfg.get("layers", []):
    for c in (lay.get("clips") or []):
        p = c if isinstance(c, str) else (c.get("path", "") if isinstance(c, dict) else "")
        if p and os.path.splitext(p)[1].lower() in EXTS and os.path.exists(p):
            pool.append(p)
# 去重
seen = set()
pool = [p for p in pool if not (p in seen or seen.add(p))]
print("配置副本：%s" % cfg_copy)
print("可用真实素材：%d 个" % len(pool))
if len(pool) < 4:
    print("!! 素材不足，无法压测")
    sys.exit(2)


class EngineShell:
    def __init__(self):
        self.running = True
        self._heal_last_global_t = 0.0
        self.canvas = QImage(1280, 720, QImage.Format_RGB32)
        self.layers = []


EngineShell._heal_stalled = engine.AutoVJEngine._heal_stalled
EngineShell._get_player = engine.AutoVJEngine._get_player


def build_layer(clips, first):
    lay = engine.Layer("QA-A5")
    lay.cur = first
    lay.visible = True
    lay.clips = [mm.MediaItem(p) for p in clips]
    lay.cache = {}
    lay.player = None
    lay._heal_path = lay.clips[first].path
    return lay


def player_for(lay, idx, all_players):
    clip = lay.clips[idx]
    p = lay.cache.get(clip.path)
    if p is None:
        p = mm.make_player(clip)
        p.set_max_size(1920, 1080)
        lay.cache[clip.path] = p
        all_players.append(p)
    return p


# 2 个图层，各自池子
half = len(pool) // 2
layers = [build_layer(pool[:half], 0), build_layer(pool[half:], 0)]
shell = EngineShell()
shell.layers = layers
all_players = []
for lay in layers:
    lay.player = player_for(lay, 0, all_players)
    lay._heal_path = lay.clips[0].path

rng = random.Random(42)
# 预热爱"下一个 6s 后要用"的素材（让它闲置 ~6s 再被激活 = 现场误杀签名）
nxt = {}
for li, lay in enumerate(layers):
    nxt[li] = (lay.cur + 1) % len(lay.clips)
    q = player_for(lay, nxt[li], all_players)
    q.warm_up()                     # 预热爱首帧；此刻仍不活跃
    q.set_active(False)

print("开始 %.0fs 离屏压测（切换间隔 %.0fs，预热提前 1 个周期）..." % (SECONDS, SWITCH))
events = []
t0 = time.perf_counter()
last_switch = t0
ticks = 0
while True:
    now = time.perf_counter()
    el = now - t0
    if el >= SECONDS:
        break
    for li, lay in enumerate(layers):
        # 真实 tick 的 active 维护：当前播放器活跃、其余缓存播放器不活跃
        lay.player.set_active(True)
        for pth, pp in lay.cache.items():
            if pp is not lay.player:
                pp.set_active(False)
        old = lay.player
        engine.AutoVJEngine._heal_stalled(shell, lay)
        if lay.player is not old:                    # 被看门狗重建
            events.append({"layer": li, "at": el,
                           "clip": os.path.basename(getattr(lay.clips[lay.cur], "path", "?")),
                           "player": type(old).__name__,
                           "frames": getattr(old, "_frames", 0),
                           "age": (time.perf_counter() - getattr(old, "_last_t", 0.0))
                                  if getattr(old, "_last_t", 0.0) else None})
    # 到点切换：激活已预热(闲置 ~6s)的播放器
    if now - last_switch >= SWITCH:
        for li, lay in enumerate(layers):
            lay.cur = nxt[li]
            lay.player = player_for(lay, lay.cur, all_players)   # 缓存命中=闲置6s的播放器
            lay._heal_path = lay.clips[lay.cur].path
            # 再预热下一个
            nxt[li] = (lay.cur + 1) % len(lay.clips)
            q = player_for(lay, nxt[li], all_players)
            q.warm_up()
            q.set_active(False)
        last_switch = now
    ticks += 1
    time.sleep(1.0 / 60.0)

dur = time.perf_counter() - t0
grown = sum(getattr(p, "_frames", 0) for p in all_players)
print("实测：%.1fs / %d ticks；播放器总数=%d；触发重建=%d 次；累计产帧=%d（均 %.1f fps）"
      % (dur, ticks, len(all_players), len(events), grown, grown / max(dur, 0.001)))
for e in events[:15]:
    print("   触发：layer=%s clip=%s player=%s frames=%d age=%s"
          % (e["layer"], e["clip"], e["player"], e["frames"],
             ("%.2f" % e["age"]) if e["age"] is not None else "None"))
heal_logs = [m for m in LOGGED if "看门狗" in m]
print("stallwatch 看门狗日志条数=%d %s" % (len(heal_logs), heal_logs[:3]))

check("A5 真实配置 %ds：看门狗重建次数 == 0" % SECONDS, len(events) == 0,
      "rebuilds=%d" % len(events))
check("A5 累计产帧在增长（引擎确实在跑）", grown > SECONDS * 10,
      "frames=%d ≈%.0f fps" % (grown, grown / max(dur, 0.001)))
check("A5 stallwatch 无看门狗重建日志", len(heal_logs) == 0, "logs=%d" % len(heal_logs))

# 清理
for p in all_players:
    try:
        p.close()
    except Exception:                                               # noqa: BLE001
        pass
shutil.rmtree(tmp, ignore_errors=True)

print()
print("结果：%s" % ("A5 通过" if not FAILS else "A5 失败 %s" % FAILS))
sys.exit(1 if FAILS else 0)
