# -*- coding: utf-8 -*-
"""独立验证员：证伪 commit 8879702 的改动 2（mono 死零兜底）与改动 1（_match_device）。
覆盖 team-lead 提出的边界问题。逐条打印实测输出。
"""
import io
import sys
import threading
import tempfile
import os
import shutil

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


class SeqRec:
    """按块序号产出：callable(i)->np.ndarray"""
    HARD_CAP = 500   # 硬上限：防桩异常导致死循环被沙箱 SIGTERM

    def __init__(self, fn):
        self.fn = fn
        self.k = 0

    def record(self, numframes=N):
        if self.k >= self.HARD_CAP:
            raise RuntimeError("HARD_CAP reached (test harness guard)")
        out = self.fn(self.k)
        self.k += 1
        return out


class FakeMic:
    def __init__(self, rec):
        self.name = "假设备(桩)"
        self._rec = rec

    def recorder(self, samplerate=None, channels=None):
        return _Ctx(self._rec)


def run_loop(gen_fn, mono=True, blocks=60, feed_hook=None):
    """跑 _capture_loop，返回 (state, feed_trace[(idx, first_col_value)])。"""
    eng = make_engine()
    orig_feed = audio_engine._Analyzer.feed
    trace = []

    def counting_feed(self, data, st):
        # data 是 feed 时传入的实际数组（列已选好）
        try:
            trace.append(float(np.abs(np.asarray(data)).max()))
        except Exception:
            trace.append(float("nan"))
        if len(trace) >= blocks:
            eng._gen = 999
        return orig_feed(self, data, st)

    audio_engine._Analyzer.feed = counting_feed
    exc = None
    try:
        eng._capture_loop(FakeMic(SeqRec(gen_fn)), mono, SR, 1)
    except Exception as e:
        exc = e
    finally:
        audio_engine._Analyzer.feed = orig_feed
    return eng.state, trace, exc


def col2(L, R):
    return np.column_stack([L, R])


def ones(n=N):
    return np.ones(n, dtype=np.float32)


def zeros(n=N):
    return np.zeros(n, dtype=np.float32)


print("=" * 78)
print("(1) 边界：形状 / NaN / inf / dtype / 空块")
print("=" * 78)

# --- 1a: 1 维输入（单声道设备返回 1 维）---
def gen_1d(i):
    return ones(N)  # 1 维

st, tr, exc = run_loop(gen_1d, mono=True, blocks=12)
print("\n[1a] data 为 1 维 (单声道设备):")
print("     异常 =", repr(exc))
print("     feed 调用次数 =", len(tr), " level = %.4f" % st.level)
print("     → 1 维时 data.ndim==2 为假，走旧路径 data[:, :1]... 见下")

# --- 1b: shape[1] == 1（单列 2 维）---
def gen_1col(i):
    return ones(N).reshape(-1, 1)

st, tr, exc = run_loop(gen_1col, mono=True, blocks=12)
print("\n[1b] data 为 (N,1) 2 维单列:")
print("     异常 =", repr(exc))
print("     feed 调用次数 =", len(tr), " level = %.4f" % st.level)

# --- 1c: 空块 (0,2) ---
def gen_empty(i):
    return np.zeros((0, 2), dtype=np.float32)

st, tr, exc = run_loop(gen_empty, mono=True, blocks=12)
print("\n[1c] data 为 (0,2) 空块:")
print("     异常 =", repr(exc))
print("     feed 调用次数 =", len(tr), "（守卫 len()==0 应挡住）")

# --- 1d: 0 行但 shape=(0,2) 绕过守卫？len 判断 ---
def gen_empty2(i):
    return np.zeros((0, 2), dtype=np.float32)

print("\n[1d] 空块 max() 直接调用会怎样：")
try:
    np.abs(np.zeros((0, 2), dtype=np.float32)[:, 0]).max()
    print("     未抛异常")
except Exception as e:
    print("     np.zeros((0,2))[:,0].max() →", repr(e))
print("     注意：len(np.zeros((0,2))) ==", len(np.zeros((0, 2), dtype=np.float32)),
      "⇒ `len(data)==0` 守卫为真吗?", len(np.zeros((0, 2), dtype=np.float32)) == 0)

# --- 1e: NaN 输入 ---
def gen_nan(i):
    L = ones(N).copy(); L[0] = np.nan
    R = ones(N).copy()
    return col2(L, R)

st, tr, exc = run_loop(gen_nan, mono=True, blocks=12)
print("\n[1e] 左声道含 NaN:")
print("     异常 =", repr(exc))
print("     level = %.4f" % st.level, " nan列?", np.isnan(st.level))
lmax_nan = float(np.abs(np.array([np.nan, 1.0], dtype=np.float32)).max())
print("     np.abs([nan,1]).max() =", lmax_nan, "⇒ NaN 参与 max 会传染")

# --- 1f: inf 输入 ---
def gen_inf(i):
    L = ones(N).copy(); L[0] = np.inf
    R = ones(N).copy()
    return col2(L, R)

st, tr, exc = run_loop(gen_inf, mono=True, blocks=12)
print("\n[1f] 左声道含 inf:")
print("     异常 =", repr(exc), " level = %.4f" % st.level)

# --- 1g: dtype int16 溢出测试 ---
l_i16 = np.zeros(N, dtype=np.int16); l_i16[0] = -32768
try:
    v = np.abs(l_i16).max()
    print("\n[1g] int16 的 np.abs(-32768).max() =", v, "dtype=", np.abs(l_i16).dtype)
    print("     −32768 abs 后仍是 −32768（补码溢出）⇒ 值为负，>= 1e-4 为假")
    print("     (lpk =", float(v), ", >=1e-4 →", float(v) >= 1e-4, ")")
except Exception as e:
    print("\n[1g] int16 abs 抛异常:", repr(e))

print("\n" + "=" * 78)
print("(2) 行为：抖动 / 误切 / 混合切换")
print("=" * 78)

# --- 2a: 左声道每 8 块冒一次微弱信号（1.1e-4）---
def gen_intermittent(i):
    L = zeros().copy()
    if i % 8 == 7:
        L[:] = 2.0e-4   # >= 1e-4 ⇒ 复位 dead_left
    R = ones()          # 右声道恒有强信号
    return col2(L, R)

st, tr, exc = run_loop(gen_intermittent, mono=True, blocks=200)
# 统计 feed 时取的是右还是左：右声道恒 1.0，左声道几乎 0
right_blocks = sum(1 for v in tr if v > 0.5)
print("\n[2a] 左声道每 8 块冒一次 2e-4（>=1e-4）噪声，右声道恒 1.0:")
print("     异常 =", repr(exc))
print("     取到右声道的块数 = %d / %d" % (right_blocks, len(tr)))
print("     → 是否永远切不过去（横跳/永不切）:", "是，切不过去" if right_blocks == 0 else "部分切换")

# --- 2b: 左声道恒为 1.1e-4（>阈值，含噪底）---
def gen_tiny_left(i):
    L = np.full(N, 1.1e-4, dtype=np.float32)
    R = ones()
    return col2(L, R)

st, tr, exc = run_loop(gen_tiny_left, mono=True, blocks=40)
right_blocks = sum(1 for v in tr if v > 0.5)
print("\n[2b] 左声道恒 1.1e-4（恰 > 阈值，噪声底），右声道恒 1.0:")
print("     异常 =", repr(exc), " level = %.4f" % st.level)
print("     取到右声道的块数 = %d / %d" % (right_blocks, len(tr)))
print("     → 是否被判为健康永不切右:", "是（永不切）" if right_blocks == 0 else "否")

# --- 2c: 混合：前 20 块左死右活，21 起左恢复 ---
def gen_switch_back(i):
    if i < 20:
        L, R = zeros().copy(), ones()
    else:
        L, R = ones(), zeros().copy()   # 左恢复、右停
    return col2(L, R)

st, tr, exc = run_loop(gen_switch_back, mono=True, blocks=60)
# 前 20 块应取右(=1.0)，之后应切回左(=1.0)。两种都一样非零，需判别取哪列
# 用特值区分：右=0.9，左=0.3
def gen_switch_back2(i):
    if i < 20:
        L = np.zeros(N, dtype=np.float32); R = np.full(N, 0.9, dtype=np.float32)
    else:
        L = np.full(N, 0.3, dtype=np.float32); R = np.zeros(N, dtype=np.float32)
    return col2(L, R)

st, tr2, exc = run_loop(gen_switch_back2, mono=True, blocks=60)
first20 = tr2[:20]
after = tr2[20:]
print("\n[2c] 前20块左死(0)/右0.9；第21块起左0.3/右死(0):")
print("     异常 =", repr(exc))
print("     前20块 feed 峰值集合 =", sorted(set(round(v, 2) for v in first20)))
print("     第21块起 feed 峰值集合 =", sorted(set(round(v, 2) for v in after)))
# 判别：块 20..27 期间 dead_left 累积，第 20+8=28 块才切回左
sw_back_idx = None
for j, v in enumerate(tr2):
    if j >= 20 and abs(v - 0.3) < 0.05:
        sw_back_idx = j
        break
print("     切回左声道(0.3)首次出现的块序号 =", sw_back_idx, "(应为 28 左右，滞后 8 块)")
print("     滞后块数 =", (sw_back_idx - 20) if sw_back_idx else None)

print("\n" + "=" * 78)
print("(3) 改动 1：_match_device 边界")
print("=" * 78)


class M:
    def __init__(self, name):
        self.name = name


# --- 3a: cands 为空 ---
print("\n[3a] _match_device([], 'X'):")
try:
    r = audio_engine._match_device([], "X")
    print("     返回 =", r)
    if r[0] is None:
        print("     → 返 None。_run 里 mic.name 是否会 AttributeError? 见下")
        try:
            _ = r[0].name
        except Exception as e:
            print("     访问 None.name →", repr(e))
except Exception as e:
    print("     抛异常:", repr(e))

# --- 3b: 空 cands 在 _run 中可达吗 ---
print("\n[3b] _run 调用点（看源码 1531-1538）：")
print("     system:  if not loop: raise ...  → 空则先 raise，_match_device 不会收到空")
print("     mic   :  if not cands: raise ... → 同上")
print("     ⇒ cands 为空的 None 分支是**死代码**（但防御性保留，无副作用）")

# --- 3c: 自相矛盾文案 ---
print("\n[3c] 自相矛盾文案测试：")
cases = [
    # (device_name 配置名, 枚举设备名列表)
    ("USB Audio Device", ["USB Audio"]),      # 配置更长，枚举更短
    ("USB Audio", ["USB Audio Device"]),      # 配置更短，枚举更长（子串命中）
    ("线路输入", ["线路输入 (Realtek)"]),
    ("Line In", ["麦克风 (USB)"]),           # 完全不命中
    ("", ["A", "B"]),                        # 未指定
]
for cfg, names in cases:
    cands = [M(n) for n in names]
    dev, warn = audio_engine._match_device(cands, cfg)
    contradiction = ("未找到" in warn and dev is not None and ("「%s」" % dev.name) in warn
                     and ("「%s」" % cfg) in warn and cfg == dev.name)
    print("     配置=%-18r 设备列表=%-28r" % (cfg, names))
    print("        → 选中=%r  告警=%r" % (dev.name if dev else None, warn))
    if warn and cfg and cfg in (dev.name if dev else ""):
        print("        ⚠ 可能出现“X 未找到，已改用 X”矛盾(需 cfg==dev.name)")

# --- 3d: 名字包含关系导致的两难 ---
print("\n[3d] 子串命中方向测试：配置名是枚举名的真子串时命中；反之时不命中")
dev, warn = audio_engine._match_device([M("USB Audio")], "USB Audio Device")
print("     配置='USB Audio Device'，枚举='USB Audio' → 命中? %s, warn=%r"
      % (dev is not None, warn))

print("\n" + "=" * 78)
print("(4) 回归：st.error 生命周期 / mono=False 路径")
print("=" * 78)

# st.error 赋值点
import subprocess
print("\n[4a] 源码里 st.error = \"\" 的赋值点 (src/audio_engine.py):")
print("     line 1475: st.error = \"\"  ← 见下，确认它是否在采集成功后清空告警")

print("\n[4b] 检查 1475 行上下文：")
with open(r"P:\ERAVJ\AutoVJ\src\audio_engine.py", encoding="utf-8") as f:
    lines = f.readlines()
for i in range(1468, 1482):
    print("     %d: %s" % (i + 1, lines[i].rstrip()))
print("     ---- 1541-1549 ----")
for i in range(1540, 1550):
    print("     %d: %s" % (i + 1, lines[i].rstrip()))
