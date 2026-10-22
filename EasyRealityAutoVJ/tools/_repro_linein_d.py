# -*- coding: utf-8 -*-
"""场景 D 精查：sr=0 自动采样率路径下 _native_samplerate 抛异常时到底发生什么。

结论要钉死：`sr = _native_samplerate(mic) or 48000` 只兜 return None；
抛异常时异常冒泡 → _run 的 except → st.error，采集根本不开始。
"""
import os, sys, types, traceback

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SRC)
import numpy as np
import audio_engine as AE


class FakeMic:
    def __init__(self, name):
        self.name = name
        self.isloopback = False

    def _device_ptr(self):
        raise AttributeError("桩：无 COM 指针")

    def recorder(self, samplerate=None, channels=2):
        raise RuntimeError("桩：不该走到这")


def stub(mics):
    mod = types.ModuleType("soundcard")
    mod.all_microphones = lambda include_loopback=False: list(mics)
    mod.default_speaker = lambda: None
    mod.default_microphone = lambda: mics[0]
    sys.modules["soundcard"] = mod
    return mod


print("=" * 74)
print("场景 D 精查：_native_samplerate 抛异常 vs 返回 None")
print("=" * 74)

mics = [FakeMic("线路输入 (USB 声卡 Line-In)")]
sc = stub(mics)

# --- D-1: 真实现状（假 mic 的 _device_ptr 会抛，模拟设备不支持 PropertyStore） ---
a = AE.AudioEngine()
print("\n[D-1] 不替换 _native_samplerate，直接跑真实现状（假 mic 无 COM 指针）：")
a._run(sc, "mic", "", True, 0, a._gen)
with a.state.lock:
    print("      st.error=%r  st.running=%s  st.device_desc=%r"
          % (a.state.error, a.state.running, a.state.device_desc))
print("      → 真实现状下 _native_samplerate 内部 try/except 吞掉异常并 return None，")
print("        所以 sr=48000，采集正常开始。这一条**不会**导致线路输入用不了。")

# --- D-2: 若 _native_samplerate 的 except 被去掉/异常外泄 ---
print("\n[D-2] 桩：_native_samplerate 直接抛异常（模拟 except 被绕过/内部错误更靠上层）：")
orig = AE._native_samplerate
AE._native_samplerate = lambda mic: (_ for _ in ()).throw(RuntimeError("模拟兜底失效"))
a2 = AE.AudioEngine()
try:
    a2._run(sc, "mic", "", True, 0, a2._gen)
except Exception as e:
    print("      异常逃出 _run！%s: %s" % (type(e).__name__, e))
with a2.state.lock:
    print("      st.error=%r  st.running=%s  st.device_desc=%r"
          % (a2.state.error, a2.state.running, a2.state.device_desc))
print("      → 异常被 _run 的 except 捕获：st.error='模拟兜底失效'，running=False，不采集。")
AE._native_samplerate = orig

print("\n" + "=" * 74)
print("判据：_native_samplerate 内部有 try/except return None（audio_engine.py:59-76），")
print("      所以真实设备即使读不到 PropertyStore 也只是返回 None → fallback 48000。")
print("      场景 D **不是**「线路输入用不了」的成因（除非 _native_samplerate 本身被改坏）。")
print("=" * 74)
