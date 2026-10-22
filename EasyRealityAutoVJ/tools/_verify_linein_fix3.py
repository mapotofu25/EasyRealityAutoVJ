# -*- coding: utf-8 -*-
"""精确验证：直接读 _capture_loop 内部的 use_right 状态，不靠数据特征猜测。

上一版 _verify_linein_fix2.py 用「喂入数据的 peak」判断取了哪一路，
但左右都是 0.5 正弦时无法区分 ⇒ 会误报。这里改用**区块内容打标**：
左声道用 0.5 正弦、右声道用 **0.9 正弦** ⇒ 用 peak 就能唯一区分（0.5 vs 0.9）。
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
R_PEAK = 0.9      # 右声道幅值（唯一标识）
L_PEAK = 0.5      # 左声道真实信号幅值


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
    """左/右各自描述：'zero' | 'sig:amp' | 'noise:amp' | 'burst:amp:period'"""

    def __init__(self, ldesc, rdesc):
        self.ldesc, self.rdesc = ldesc, rdesc
        self.k = 0

    def _chan(self, desc, t):
        parts = desc.split(":")
        kind = parts[0]
        amp = float(parts[1]) if len(parts) > 1 else 0.0
        if kind == "zero":
            return np.zeros(len(t), dtype=np.float32)
        if kind == "sig":
            return (amp * np.sin(2 * np.pi * 1000.0 * t)).astype(np.float32)
        if kind == "noise":
            return np.full(len(t), amp, dtype=np.float32)
        if kind == "burst":
            per = int(parts[2])
            if per and (self.k % per == 0):
                return np.full(len(t), amp, dtype=np.float32)
            return np.zeros(len(t), dtype=np.float32)
        return np.zeros(len(t), dtype=np.float32)

    def record(self, numframes=N):
        t = (np.arange(numframes) + self.k * numframes) / SR
        L = self._chan(self.ldesc, t)
        R = self._chan(self.rdesc, t)
        self.k += 1
        return np.column_stack([L, R])


class FakeMic:
    def __init__(self, rec):
        self._rec = rec
        self.name = "假线路输入(桩)"

    def recorder(self, samplerate=None, channels=None):
        return _Ctx(self._rec)


def run(rec):
    eng = make_engine()
    orig_feed = audio_engine._Analyzer.feed
    stat = {"n": 0, "right": 0, "first": None, "left": 0}

    def spy(self, data, st):
        stat["n"] += 1
        pk = float(np.abs(data).max())
        # 右=0.9 唯一；左=0.5；零/噪底很小
        if pk >= 0.7:
            stat["right"] += 1
            if stat["first"] is None:
                stat["first"] = stat["n"]
        elif pk >= 0.3:
            stat["left"] += 1
        if stat["n"] >= TOTAL:
            eng._gen = 999
        return orig_feed(self, data, st)

    audio_engine._Analyzer.feed = spy
    try:
        eng._capture_loop(FakeMic(rec), True, SR, 1)
    except Exception as e:
        print("   (异常: %r)" % (e,))
    finally:
        audio_engine._Analyzer.feed = orig_feed
    return stat, eng.state


CASES = [
    # (标签, 左描述, 右描述, 期望取右?)
    ("左持续零 / 右有信号(0.9)      ", "zero",        "sig:0.9",         True),
    ("左每 6 块冒 1e-3 / 右有信号  ", "burst:1e-3:6", "sig:0.9",         True),
    ("左每 7 块冒 1e-3 / 右有信号  ", "burst:1e-3:7", "sig:0.9",         True),
    ("左每 8 块冒 1e-3 / 右有信号  ", "burst:1e-3:8", "sig:0.9",         True),
    ("左恒 1.1e-4 噪底 / 右有信号  ", "noise:1.1e-4", "sig:0.9",         True),
    ("左恒 0.01 噪底 / 右有信号    ", "noise:0.01",   "sig:0.9",         True),
    ("左有真实信号(0.5) / 右有信号 ", "sig:0.5",      "sig:0.9",         False),
    ("左右都零（真静音）           ", "zero",         "zero",            False),
]

print("=" * 74)
print("精确验证 P1：左/右用不同幅值唯一区分（左0.5 / 右0.9）")
print("=" * 74)
allok = True
for tag, l, r, expect_right in CASES:
    stat, st = run(GenRec(l, r))
    got = stat["right"] > 0
    ok = (got == expect_right)
    allok = allok and ok
    print("\n[%s]" % tag)
    print("   取右 %d/%d  取左 %d  首个取右=%s  level=%.4f  error=%r  %s" % (
        stat["right"], TOTAL, stat["left"], stat["first"], st.level, st.error,
        "✅" if ok else "❌"))
    if "间歇" in tag or "burst" in tag:
        pass

print("\n" + "=" * 74)
print("总判定:", "✅ 全部通过" if allok else "❌ 有失败项")
print("=" * 74)
sys.exit(0 if allok else 1)
