# -*- coding: utf-8 -*-
"""验证「mono 左声道死零兜底」是否生效（2026-10-07 修复）。

复现脚本 _repro_linein_a_e.py 的场景 E 只测了 `_Analyzer.feed` 本身，
**没有走 `_capture_loop` 的新兜底分支** ⇒ 测不到修复。这里直接驱动采集循环。

做法：桩 mic 产出指定声道的块，喂给真实的 AudioEngine._capture_loop。

判据：
  用例1 左全0/右有信号 + mono=True → 修复后应自动改取右声道，level>0
  用例2 左有信号/右全0 + mono=True → 仍取左声道，level>0（未被破坏）
  用例3 左右都有信号 + mono=True → 健康路径，level>0（行为与旧版一致）
  用例4 左右都全零 + mono=True → 静音仍是静音，不应误判
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
    return eng


class _Ctx:
    def __init__(self, rec):
        self.rec = rec

    def __enter__(self):
        return self.rec

    def __exit__(self, *a):
        return False


class GenRec:
    """按模式产出立体声块。"""

    def __init__(self, mode):
        self.mode = mode
        self.k = 0

    def record(self, numframes=N):
        t = (np.arange(numframes) + self.k * numframes) / SR
        sig = (0.5 * np.sin(2 * np.pi * 1000.0 * t)).astype(np.float32)
        z = np.zeros(numframes, dtype=np.float32)
        if self.mode == "right_only":
            L, R = z, sig
        elif self.mode == "left_only":
            L, R = sig, z
        elif self.mode == "both":
            L, R = sig, sig
        else:  # silent
            L, R = z, z
        self.k += 1
        return np.column_stack([L, R])


class FakeMic:
    def __init__(self, mode):
        self.mode = mode
        self.name = "假线路输入(桩)"

    def recorder(self, samplerate=None, channels=None):
        return _Ctx(GenRec(self.mode))


def run_case(mode, mono, blocks=40):
    eng = make_engine()
    orig_feed = audio_engine._Analyzer.feed
    cnt = {"n": 0}

    def counting_feed(self, data, st):
        cnt["n"] += 1
        if cnt["n"] >= blocks:
            eng._gen = 999
        return orig_feed(self, data, st)

    audio_engine._Analyzer.feed = counting_feed
    try:
        eng._capture_loop(FakeMic(mode), mono, SR, 1)
    except Exception as e:
        print("  (异常: %r)" % (e,))
    finally:
        audio_engine._Analyzer.feed = orig_feed
    return eng.state


print("=" * 70)
print("验证 mono 左声道死零兜底（直接驱动 _capture_loop）")
print("=" * 70)
results = []

for tag, mode, mono, expect_positive in [
    ("用例1 左全0/右有信号 mono=True  ", "right_only", True, True),
    ("用例2 左有信号/右全0 mono=True  ", "left_only", True, True),
    ("用例3 左右都有信号 mono=True   ", "both", True, True),
    ("用例4 左右都全零 mono=True     ", "silent", True, False),
]:
    st = run_case(mode, mono)
    got_pos = st.level > 1e-3
    ok = (got_pos == expect_positive)
    results.append(ok)
    print("\n[%s] lv_db=%.2f level=%.4f energy=%.4f" % (tag, st.lv_db, st.level, st.energy))
    print("     期望%s → %s" % ("有信号" if expect_positive else "静音",
                                "✅ 通过" if ok else "❌ 失败"))

print("\n" + "=" * 70)
print("总判定:", "✅ 全部通过" if all(results) else "❌ 有失败项")
print("=" * 70)
sys.exit(0 if all(results) else 1)
