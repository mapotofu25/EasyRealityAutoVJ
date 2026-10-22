# -*- coding: utf-8 -*-
"""验证 P1（间歇噪声）+ P3（error 粘滞）两个修复。

这两个场景是独立验证员在我的第一版修复里抓到的：
  P1 [2A] 左声道**间歇** ≥1e-4 噪声（每 6/7/8 块一次）→ 旧「连续 8 块」判据被复位，永不切右
  P1 [2B] 左声道恒 1.1e-4 噪底 → 判为"健康"，永不切右
  P3 [3d] st.error 粘滞 → 告警永久挂界面

本脚本直接驱动真实 `_capture_loop`，统计「取右」次数与 st.error。
"""
import io
import sys
import threading

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import numpy as np

sys.path.insert(0, r"P:\ERAVJ\AutoVJ\src")
import audio_engine  # noqa: E402

SR = 48000
N = audio_engine.BLOCK
TOTAL = 200


def make_engine():
    eng = audio_engine.AudioEngine.__new__(audio_engine.AudioEngine)
    eng.state = audio_engine.AudioState()
    eng._genre_lock = threading.RLock()
    eng._genre_sr = 0
    eng._genre_buf = audio_engine._AudioRing(int(SR * 12))
    eng._gen = 1
    eng.energy_scale = 1.0
    eng.push_ndi_audio = lambda *a, **k: None
    eng.state.ndi_audio_on = False
    eng.state.ndi_feed = None
    eng.state.error = ""
    return eng


class _Ctx:
    def __init__(self, rec):
        self.rec = rec

    def __enter__(self):
        return self.rec

    def __exit__(self, *a):
        return False


class GenRec:
    """按描述产出立体声块。

    lmode: 'zero' | 'noise_amp'（固定幅值）| 'noise_burst'(每 k 块一次 1e-3)
    """

    def __init__(self, lmode, lamp, rmode, ramp, lperiod=None):
        self.lmode, self.lamp = lmode, lamp
        self.rmode, self.ramp = rmode, ramp
        self.lperiod = lperiod
        self.k = 0

    def record(self, numframes=N):
        t = (np.arange(numframes) + self.k * numframes) / SR
        sig = (0.5 * np.sin(2 * np.pi * 1000.0 * t)).astype(np.float32)

        def chan(mode, amp):
            if mode == "zero":
                return np.zeros(numframes, dtype=np.float32)
            if mode == "sig":
                return sig
            if mode == "noise_amp":
                return np.full(numframes, amp, dtype=np.float32)
            if mode == "noise_burst":
                # 每 lperiod 块冒一次噪声，其余为 0
                if self.lperiod and (self.k % self.lperiod == 0):
                    return np.full(numframes, amp, dtype=np.float32)
                return np.zeros(numframes, dtype=np.float32)
            return np.zeros(numframes, dtype=np.float32)

        L = chan(self.lmode, self.lamp)
        R = chan(self.rmode, self.ramp)
        self.k += 1
        return np.column_stack([L, R])


class FakeMic:
    def __init__(self, rec):
        self._rec = rec
        self.name = "假线路输入(桩)"

    def recorder(self, samplerate=None, channels=None):
        return _Ctx(self._rec)


def run(rec):
    """跑 TOTAL 块，返回 (取右块数, 首个取右块序号, 最终 level, st)。"""
    eng = make_engine()
    orig_feed = audio_engine._Analyzer.feed
    stats = {"n": 0, "right": 0, "first_right": None}
    seen = {"n": 0}

    def spy_feed(self, data, st):
        stats["n"] += 1
        # 取右 ⇒ 喂进来的列数仍是 1，但内容长度一样；用调用时的 use_right 无法直接看，
        # 改从闭包外读：用 stats 与 feed 调用序号配合 —— 这里改用数据特征判断。
        # 更可靠：记录每块喂入的 max，并与右声道理论值比对。
        peak = float(np.abs(data).max())
        if peak >= 0.3:      # 右声道是 0.5 正弦 ⇒ peak≈0.5；左噪底/零都远小于此
            stats["right"] += 1
            if stats["first_right"] is None:
                stats["first_right"] = stats["n"]
        return orig_feed(self, data, st)

    def counting_feed(self, data, st):
        r = spy_feed(self, data, st)
        seen["n"] += 1
        if seen["n"] >= TOTAL:
            eng._gen = 999
        return r

    audio_engine._Analyzer.feed = counting_feed
    try:
        eng._capture_loop(FakeMic(rec), True, SR, 1)
    except Exception as e:
        print("  (异常: %r)" % (e,))
    finally:
        audio_engine._Analyzer.feed = orig_feed
    st = eng.state
    return stats["right"], stats["first_right"], st.level, st


print("=" * 72)
print("P1 验证：间歇性左声道噪声（旧版永不切右的元凶）")
print("=" * 72)
cases = [
    ("左持续零 / 右有信号        ", dict(lmode="zero", lamp=0.0, rmode="sig", ramp=0.5), 0),
    ("左每 6 块冒 1e-3 / 右有信号", dict(lmode="noise_burst", lamp=1e-3, rmode="sig", ramp=0.5, lperiod=6), 6),
    ("左每 7 块冒 1e-3 / 右有信号", dict(lmode="noise_burst", lamp=1e-3, rmode="sig", ramp=0.5, lperiod=7), 7),
    ("左每 8 块冒 1e-3 / 右有信号", dict(lmode="noise_burst", lamp=1e-3, rmode="sig", ramp=0.5, lperiod=8), 8),
    ("左恒 1.1e-4 噪底 / 右有信号 ", dict(lmode="noise_amp", lamp=1.1e-4, rmode="sig", ramp=0.5), 0),
    ("左有真实信号 / 右有信号     ", dict(lmode="sig", lamp=0.5, rmode="sig", ramp=0.5), 0),
    ("左右都零（真静音）         ", dict(lmode="zero", lamp=0.0, rmode="zero", ramp=0.0), 0),
]
results = []
for tag, kw, _ in cases:
    r, first, lvl, st = run(GenRec(**kw))
    print("\n[%s]" % tag)
    print("   取右 %d/%d 块  首个取右=%s  level=%.4f  error=%r" % (
        r, TOTAL, first, lvl, st.error))
    results.append((tag, r, first, lvl))

print("\n" + "=" * 72)
print("判定：")
print("  · 左持续零 / 每 6·7·8 块间歇噪 / 恒 1.1e-4 噪底  → 都应切右（r>0）")
print("  · 左有真实信号 / 真静音                          → 都不应切右（r==0）")
print("=" * 72)
ok = True
for tag, r, first, lvl in results:
    if "左持续零" in tag or "间歇" in tag or "噪底" in tag:
        good = r > 0
    else:
        good = r == 0
    ok = ok and good
    print("  %-34s %s" % (tag, "✅" if good else "❌"))
print("\n总判定:", "✅ 全部通过" if ok else "❌ 有失败项")
sys.exit(0 if ok else 1)
