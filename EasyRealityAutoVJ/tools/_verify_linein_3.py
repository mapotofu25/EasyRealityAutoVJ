# -*- coding: utf-8 -*-
"""深入：抖动/误切 精确计数 + st.error 生命周期 + system 分支一致性 +
   _run 现场路径可达性（空 cands 是否真死代码）+ mono=False 未动确认。
"""
import io, sys, threading
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import numpy as np
sys.path.insert(0, r"P:\ERAVJ\AutoVJ\src")
import audio_engine

SR = 48000
N = audio_engine.BLOCK


class _Ctx:
    def __init__(self, r): self.r = r
    def __enter__(self): return self.r
    def __exit__(self, *a): return False


class SeqRec:
    HARD_CAP = 500
    def __init__(self, fn): self.fn = fn; self.k = 0
    def record(self, numframes=N):
        if self.k >= self.HARD_CAP:
            raise RuntimeError("HARD_CAP")
        o = self.fn(self.k); self.k += 1; return o


class FakeMic:
    def __init__(self, rec): self.name = "桩"; self._rec = rec
    def recorder(self, samplerate=None, channels=None): return _Ctx(self._rec)


def run(fn, mono=True, blocks=200):
    eng = audio_engine.AudioEngine.__new__(audio_engine.AudioEngine)
    eng.state = audio_engine.AudioState()
    eng._genre_lock = threading.RLock(); eng._genre_sr = 0
    eng._genre_buf = audio_engine._AudioRing(int(SR * 12))
    eng._gen = 1; eng.energy_scale = 1.0
    eng.push_ndi_audio = lambda *a, **k: None
    eng.state.ndi_audio_on = False; eng.state.ndi_feed = None
    orig = audio_engine._Analyzer.feed
    trace = []
    def cf(self, d, st):
        try: trace.append(float(np.abs(np.asarray(d)).max()))
        except Exception: trace.append(float("nan"))
        if len(trace) >= blocks: eng._gen = 999
        return orig(self, d, st)
    audio_engine._Analyzer.feed = cf
    try:
        eng._capture_loop(FakeMic(SeqRec(fn)), mono, SR, 1)
    except Exception as e:
        print("   (loop异常 %r)" % e)
    finally:
        audio_engine._Analyzer.feed = orig
    return eng.state, trace


print("=" * 76)
print("2A 精确：切到右声道需要多少块？左声道每 N 块冒一次 >=1e-4 能否切断？")
print("=" * 76)


def mk(noise_period):
    """左声道：默认 0，每 noise_period 块出现一次 2e-4；右声道恒 0.9。"""
    def f(i):
        L = np.zeros(N, dtype=np.float32)
        if noise_period and i % noise_period == noise_period - 1:
            L[:] = 2.0e-4
        R = np.full(N, 0.9, dtype=np.float32)
        return np.column_stack([L, R])
    return f


for period in [0, 6, 7, 8, 9, 16, 100]:
    st, tr = run(mk(period), mono=True, blocks=200)
    right_n = sum(1 for v in tr if v > 0.5)
    print("  左噪底周期=%-4s → 取右块数=%3d/200  首个取右块=%s"
          % (period if period else "无",
             right_n,
             next((j for j, v in enumerate(tr) if v > 0.5), None)))

print("\n  含义：周期 <8（每 ≤7 块就冒一次）→ dead_left 永远到不了 8 → 永不切换")

print("\n" + "=" * 76)
print("2B 左声道恒 1.1e-4 (噪声底>阈值) + 右声道恒 0.9：是否永不切右")
print("=" * 76)


def tiny_left(i):
    L = np.full(N, 1.1e-4, dtype=np.float32)
    R = np.full(N, 0.9, dtype=np.float32)
    return np.column_stack([L, R])


st, tr = run(tiny_left, mono=True, blocks=40)
print("  feed 峰值集合 =", sorted(set(round(v, 6) for v in tr))[:6])
print("  level=%.6f  lv_db=%.2f  取右块数=%d/40"
      % (st.level, st.lv_db, sum(1 for v in tr if v > 0.5)))
print("  含义：左声道只要恒 >=1e-4（哪怕只是噪底），永远判为健康 → 用户听/看到的仍是噪底")

print("\n" + "=" * 76)
print("2C 切换时点精确：左死右活连续 → 第几块开始取右")
print("=" * 76)


def dead_right_alive(i):
    L = np.zeros(N, dtype=np.float32)
    R = np.full(N, 0.9, dtype=np.float32)
    return np.column_stack([L, R])


st, tr = run(dead_right_alive, mono=True, blocks=30)
first = next((j for j, v in enumerate(tr) if v > 0.5), None)
print("  首个取右块的序号 =", first, "(期望 7，因 dead_left 累到 8 才切)")
print("  前 10 块 feed 峰值 =", [round(v, 2) for v in tr[:10]])

print("\n" + "=" * 76)
print("3C/3D st.error 生命周期")
print("=" * 76)
with open(r"P:\ERAVJ\AutoVJ\src\audio_engine.py", encoding="utf-8") as f:
    src = f.read()
lines = src.splitlines()
print("  所有 st.error 赋值点：")
for i, ln in enumerate(lines):
    if "st.error" in ln and "=" in ln and "==" not in ln.split("=")[0]:
        print("    L%d: %s" % (i + 1, ln.strip()))
print("  → 采集正常运行时（_capture_loop 内）是否把 st.error 清空？")
print("     _capture_loop 内 st.error 赋值点：")
for i in range(1556, 1653):
    if "st.error" in lines[i]:
        print("    L%d: %s" % (i + 1, lines[i].strip()))

print("\n" + "=" * 76)
print("3B _run 现场路径：cands 为空时 _match_device 是否可达 None 分支")
print("=" * 76)
print("  源码 1529-1538 已确认：system/mic 两分支都在调用前 raise 空情况")
print("  → _match_device 收到空 cands 的唯一可能是别人直接调用它（非 _run 路径）")
print("  → 在 _run 里 None 分支不可达 = 死代码；但若未来有人复用给空列表会 AttributeError")

print("\n" + "=" * 76)
print("4 回归：mono=False 路径 + system 分支 diff")
print("=" * 76)
import subprocess
d = subprocess.run(["git", "show", "8879702", "--", "src/audio_engine.py"],
                   cwd=r"P:\ERAVJ\AutoVJ", capture_output=True, text=True,
                   encoding="utf-8", errors="replace").stdout
# 检查 else: analyzer.feed(data, st) 是否被改
print("  diff 中 'analyzer.feed(data, st)' 出现行：")
for ln in d.splitlines():
    if "analyzer.feed" in ln:
        print("    %s" % ln)
