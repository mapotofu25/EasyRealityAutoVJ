# -*- coding: utf-8 -*-
"""验证 P3：st.error 粘滞修复 —— 首块采集成功后应清空告警。"""
import io
import sys
import threading

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import numpy as np

sys.path.insert(0, r"P:\ERAVJ\AutoVJ\src")
import audio_engine  # noqa: E402

SR = 48000
N = audio_engine.BLOCK


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
    # ★ 模拟"设备未找到"告警已写入
    eng.state.error = "配置的音频设备「X」未找到，已改用「Y」。"
    return eng


class _Ctx:
    def __init__(self, rec):
        self.rec = rec

    def __enter__(self):
        return self.rec

    def __exit__(self, *a):
        return False


class GenRec:
    def __init__(self):
        self.k = 0

    def record(self, numframes=N):
        t = (np.arange(numframes) + self.k * numframes) / SR
        s = (0.5 * np.sin(2 * np.pi * 1000.0 * t)).astype(np.float32)
        self.k += 1
        return np.column_stack([s, s])


class FakeMic:
    name = "假设备"

    def recorder(self, samplerate=None, channels=None):
        return _Ctx(GenRec())


def run(nblocks):
    eng = make_engine()
    orig = audio_engine._Analyzer.feed
    cnt = {"n": 0}
    seen_err = []

    def spy(self, data, st):
        cnt["n"] += 1
        seen_err.append(st.error)
        if cnt["n"] >= nblocks:
            eng._gen = 999
        return orig(self, data, st)

    audio_engine._Analyzer.feed = spy
    try:
        eng._capture_loop(FakeMic(), True, SR, 1)
    finally:
        audio_engine._Analyzer.feed = orig
    return eng.state, seen_err


print("=" * 66)
print("验证 P3：st.error 粘滞修复")
print("=" * 66)

st, seen = run(10)
print("\n采集前 error = '...未找到，已改用...'")
print("采集 10 块后 error = %r" % st.error)
print("首块 feed 时的 error = %r" % seen[0])
print("第二块 feed 时的 error = %r" % (seen[1] if len(seen) > 1 else "(无)"))
print()

# 清空动作发生在 feed 之后 ⇒ 第一块 feed 时还是旧值，第二块起应为空
ok = st.error == "" and (len(seen) < 2 or seen[1] == "")
print("判据：采集首块成功后 error 应被清空（第二块起为空）")
print("结果:", "✅ 通过（告警已被清空，不再永久挂界面）" if ok else "❌ 失败")

print("\n" + "=" * 66)
print("总判定:", "✅ 通过" if ok else "❌ 失败")
print("=" * 66)
sys.exit(0 if ok else 1)
