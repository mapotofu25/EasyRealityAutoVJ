# -*- coding: utf-8 -*-
"""QA(严过关) 出包前独立复验 —— A 线（卡帧看门狗误杀修复）。

不引用任何他人结论；每条断言都来自本脚本实测。
A1 代码核对 / A2 对抗(健康冻结仍重建) / A3 戳只打一次 / A4 误杀不再发生。

用法：
  QT_QPA_PLATFORM=offscreen P:/AutoVJ/venv/Scripts/python.exe -u tools/_qa3_final_a.py
退出码 0 = 全部通过；1 = 有失败。
"""
import os
import re
import sys
import time as _time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

import media_manager as mm          # noqa: E402
import engine                       # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print(("  PASS " if ok else "  FAIL ") + name + (("   | " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


# =====================================================================
print("=" * 72)
print("A1 代码核对：5 条改动真实存在")
print("=" * 72)
mm_src = open(os.path.join(_ROOT, "src", "media_manager.py"), encoding="utf-8").read()
en_src = open(os.path.join(_ROOT, "src", "engine.py"), encoding="utf-8").read()

check("_set_active 定义存在且只在 False->True 跳变时打戳",
      "def _set_active(self, a):" in mm_src and
      "if a and not getattr(self, \"_active\", False):" in mm_src and
      "self._active_since = time.perf_counter()" in mm_src)
check("_init_health 里初始化了 self._active_since = 0.0",
      re.search(r"_init_health[\s\S]{0,600}?self\._active_since = 0\.0", mm_src) is not None)

# 5 处 set_active 定义
defs = re.findall(r"def set_active\(self[^)]*\):\n(.*?)\n(?=\s*(?:def |class )|\Z)", mm_src)
print("     set_active 定义处数 = %d" % len(defs))
check("media_manager 里 set_active 定义恰好 5 处", len(defs) == 5, "count=%d" % len(defs))
check("其中至少 3 处调用 self._set_active(a)（Video/AvAlpha/GpuDxv）",
      sum(1 for d in defs if "_set_active(a)" in d) == 3,
      "n=%d" % sum(1 for d in defs if "_set_active(a)" in d))
pass_defs = [i for i, d in enumerate(defs) if d.strip() == "pass" or d.strip().startswith("pass")]
check("恰好 2 处仍是 `pass` 空实现（Gif/Image 未被误改）",
      len(pass_defs) == 2, "indices=%s bodies=%s" % (pass_defs, [d.strip() for d in defs]))
check("stalled() 基准 = max(_last_t, _active_since)",
      "base = last if last > active_since else active_since" in mm_src or
      "base = max(_last_t, _active_since)" in mm_src)
check("重建日志含 active=%.1fs", "\"active=%.1fs\"" in en_src or "active=%.1fs" in en_src)

# =====================================================================
print()
print("=" * 72)
print("A2 对抗测试：看门狗没有被'修哑' —— 连续活跃且冻结仍须被判卡帧/重建")
print("=" * 72)


class RealishPlayer(mm._DecoderHealth):
    """用真实 _DecoderHealth.stalled() 逻辑的播放器（只剥掉解码线程）。"""

    def __init__(self):
        self._active = False
        self.closed = 0
        self._init_health()

    def set_active(self, a):
        self._set_active(a)

    def stalled(self, secs=mm._STALL_SECS):
        return mm._DecoderHealth.stalled(self, secs)

    def health(self):
        return mm._DecoderHealth.health(self)

    def close(self):
        self.closed += 1


# ---- A2a：mixin 级，真实时钟：出过帧 -> 激活 -> 再不产帧 => stalled 在 ~1.5s 变真 ----
p = RealishPlayer()
p._mark_frame()                     # 出过首帧（_frames=1, _last_t=now）
p.set_active(True)                  # 被激活且此后永不产帧
t0 = _time.perf_counter()
check("A2a 刚激活瞬间 stalled(1.5)=False（不误判）", p.stalled(1.5) is False,
      "frames=%d" % p._frames)
elapsed = None
while _time.perf_counter() - t0 < 5.0:
    p.set_active(True)              # 模拟引擎每 tick 都调（应无副作用）
    if p.stalled(1.5):
        elapsed = _time.perf_counter() - t0
        break
    _time.sleep(0.02)
check("A2a 连续活跃且冻结 => stalled(1.5) 最终变真（未修哑）", elapsed is not None)
check("A2a 判定耗时落在 [1.4, 2.5]s（完整 1.5s 窗口）",
      elapsed is not None and 1.4 <= elapsed <= 2.5,
      "elapsed=%.3fs" % (elapsed if elapsed else -1))

# ---- A2b：引擎级，真实时钟：真实 _heal_stalled 在 ~1.5s 触发重建 ----
class FakeClip:
    def __init__(self, path):
        self.path = path


class FakeLayer:
    def __init__(self, clip, player):
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
        self.created = []
        self.first_rebuild_elapsed = None
        self._start = _time.perf_counter()

    def _get_player(self, lay, clip):
        self.rebuilds += 1
        if self.first_rebuild_elapsed is None:
            self.first_rebuild_elapsed = _time.perf_counter() - self._start
        np = RealishPlayer()
        np._mark_frame()
        np.set_active(True)
        self.created.append(np)
        lay.cache[clip.path] = np
        return np


eng = FakeEngine()
clip = FakeClip("frozen_active.mov")
pl = RealishPlayer()
pl._mark_frame()                    # 出过帧
pl.set_active(True)                 # 活跃
lay = FakeLayer(clip, pl)
eng._start = _time.perf_counter()
t0 = _time.perf_counter()
early_rebuild = False
while _time.perf_counter() - t0 < 5.0 and eng.rebuilds == 0:
    el = _time.perf_counter() - t0
    if el < 1.0 and eng.rebuilds:
        early_rebuild = True
    engine.AutoVJEngine._heal_stalled(eng, lay)
    if eng.rebuilds and el < 1.0:
        early_rebuild = True
    _time.sleep(0.05)
check("A2b 真实 _heal_stalled 重建了一个'连续活跃且冻结'的播放器", eng.rebuilds >= 1,
      "rebuilds=%d" % eng.rebuilds)
check("A2b 首次重建耗时落在 [1.4, 5.0]s（既没被修哑，也没乱杀）",
      eng.first_rebuild_elapsed is not None and 1.4 <= eng.first_rebuild_elapsed <= 5.0,
      "first_rebuild=%.3fs" % (eng.first_rebuild_elapsed if eng.first_rebuild_elapsed else -1))
check("A2b 前 1.0s 内不得重建（不是见谁杀谁）", early_rebuild is False)
check("A2b 旧播放器已被关闭", pl.closed >= 1, "closed=%d" % pl.closed)

# =====================================================================
print()
print("=" * 72)
print("A3 可控时钟：_active_since 只在 False->True 跳变时打戳，绝不每 tick 刷新")
print("=" * 72)


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def perf_counter(self):
        return self.t


CLK = Clock()
_mm_time, _en_time = mm.time, engine.time
mm.time = CLK
try:
    q = RealishPlayer()             # _init_health 会用 CLK
    q.set_active(True)
    stamp0 = q._active_since
    check("A3 首次 set_active(True) 打戳 == 当前时钟", abs(stamp0 - CLK.t) < 1e-9,
          "stamp=%.3f clock=%.3f" % (stamp0, CLK.t))

    CLK.t = 101.0
    for _ in range(20):
        q.set_active(True)          # 模拟引擎每 tick 的重复置真
    check("A3 连续 20 次 set_active(True) 后戳不变", abs(q._active_since - stamp0) < 1e-9,
          "stamp=%.3f (期望 %.3f)" % (q._active_since, stamp0))

    CLK.t = 110.0
    q.set_active(True)
    check("A3 时钟走到 110 再置真，戳仍不变", abs(q._active_since - stamp0) < 1e-9,
          "stamp=%.3f" % q._active_since)
    check("A3 停滞窗口随真实时间成长：110s 时 stalled(1.5)=True", q.stalled(1.5) is True)

    # 反面：若每 tick 刷新，则窗口永远长不熟。此处验证 False->True 才重打戳。
    q.set_active(False)
    CLK.t = 111.0
    q.set_active(True)
    check("A3 set_active(False)->(True) 重新打戳 == 111", abs(q._active_since - 111.0) < 1e-9,
          "stamp=%.3f" % q._active_since)
    check("A3 重打戳后窗口归零：111s 时 stalled(1.5)=False", q.stalled(1.5) is False)
finally:
    mm.time = _mm_time

# =====================================================================
print()
print("=" * 72)
print("A4 误杀场景不再发生：出过帧 -> 不活跃 -> 假装过 30s -> 设回活跃 => 不重建")
print("=" * 72)

mm.time = CLK
engine.time = CLK
try:
    CLK.t = 200.0
    eng4 = FakeEngine()
    clip4 = FakeClip("idle_then_active.mov")
    p4 = RealishPlayer()
    p4._mark_frame()                # 200.0 出过帧
    p4.set_active(False)            # 被切走 / 挂在缓存里（合法不活跃）
    lay4 = FakeLayer(clip4, p4)

    CLK.t = 230.0                   # 假装闲置 30s
    p4.set_active(True)             # 刚被激活
    check("A4 激活瞬间 _active_since==230", abs(p4._active_since - 230.0) < 1e-9,
          "active_since=%.3f" % p4._active_since)

    # 接下来的 0.5s 内应继续正常产帧；同时引擎每 tick 检查，不得重建
    rebuilt = 0
    t = 230.0
    while t <= 230.5:
        engine.AutoVJEngine._heal_stalled(eng4, lay4)
        p4._mark_frame()            # 模拟"它接下来 0.5s 正常产帧"
        t += 0.05
        CLK.t = t
    rebuilt = eng4.rebuilds
    check("A4 闲置 30s 后重新激活 => 0 次重建（误杀已消除）", rebuilt == 0,
          "rebuilds=%d" % rebuilt)
    check("A4 已出过帧且持续产帧（帧计数在涨）", p4._frames >= 10, "frames=%d" % p4._frames)

    # 再压：激活后先卡 1.4s（不足 1.5s）不该重建；越过 1.5s 才该重建（证明窗口没被拉长失效）
    CLK.t = 300.0
    eng5 = FakeEngine()
    clip5 = FakeClip("reactivation_frozen.mov")
    p5 = RealishPlayer()
    p5._mark_frame()
    p5.set_active(False)
    CLK.t = 330.0
    p5.set_active(True)             # 激活后**不再产帧**
    engine.AutoVJEngine._heal_stalled(eng5, lay5 := FakeLayer(clip5, p5))
    CLK.t = 331.45                   # 激活后仅 1.45s
    engine.AutoVJEngine._heal_stalled(eng5, lay5)
    check("A4 激活后 1.45s 无帧 => 尚不重建（窗口未被缩短）", eng5.rebuilds == 0,
          "rebuilds=%d" % eng5.rebuilds)
    # 越过激活后 1.5s；同时步进 >0.5s 以越过已知的检查节流 _HEAL_CHECK_INTERVAL
    CLK.t = 332.00                   # 激活后 2.00s
    engine.AutoVJEngine._heal_stalled(eng5, lay5)
    check("A4 激活后 2.00s 仍无帧 => 重建（窗口未被拉长失效）", eng5.rebuilds == 1,
          "rebuilds=%d" % eng5.rebuilds)
finally:
    mm.time = _mm_time
    engine.time = _en_time

# =====================================================================
print()
print("结果：%s" % ("全部通过" if not FAILS else "失败 %d 项 -> %s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
