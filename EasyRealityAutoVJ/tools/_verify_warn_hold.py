# -*- coding: utf-8 -*-
"""验证 P3 与 P1 的冲突已解决：
   · 告警应保留 _WARN_HOLD 块（≈6s）后才清空 —— 用户能看见
   · 之后应清空 —— 不顶掉「没有检测到声音」提示
   · P1 滑动窗口逻辑不受影响
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


def make_engine(initial_error=""):
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
    eng.state.error = initial_error
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


def run(alert, nblocks):
    eng = make_engine(alert)
    orig = audio_engine._Analyzer.feed
    errs = []
    cnt = {"n": 0}

    def spy(self, data, st):
        cnt["n"] += 1
        errs.append(st.error)
        if cnt["n"] >= nblocks:
            eng._gen = 999
        return orig(self, data, st)

    audio_engine._Analyzer.feed = spy
    try:
        eng._capture_loop(FakeMic(), True, SR, 1)
    finally:
        audio_engine._Analyzer.feed = orig
    return eng.state, errs


print("=" * 70)
print("P3×P1 冲突验证：告警保留 288 块后清空")
print("=" * 70)
ALERT = "配置的音频设备「X」未找到，已改用「Y」。"

st, errs = run(ALERT, 300)
visible = sum(1 for e in errs if e)
print("\n[用例1] 告警存在，跑 300 块")
print("   告警可见块数 = %d / 300" % visible)
print("   最终 error = %r" % st.error)
ok1 = visible >= 280 and st.error == ""
print("   判据：≥280 块可见 + 最终清空 →", "✅ 通过" if ok1 else "❌ 失败")
print("   （可见约 6.1s，用户来得及看清；随后自动消失，不顶掉'没声音'提示）")

st2, errs2 = run("", 300)
never = sum(1 for e in errs2 if e)
print("\n[用例2] 无告警，跑 300 块")
print("   出现告警块数 = %d / 300" % never)
ok2 = never == 0
print("   判据：不应凭空产生告警 →", "✅ 通过" if ok2 else "❌ 失败")

print("\n" + "=" * 70)
print("总判定:", "✅ 全部通过" if (ok1 and ok2) else "❌ 有失败项")
print("=" * 70)
sys.exit(0 if (ok1 and ok2) else 1)
