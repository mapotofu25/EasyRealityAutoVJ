# -*- coding: utf-8 -*-
"""确认 system 分支选择语义与旧版一致；确认 mono=False 与 1维/单列路径未变。"""
import io, sys, threading, subprocess
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import numpy as np
# 动态载入旧版 audio_engine（8879702^ 版本）里的选择逻辑做等价对照
sys.path.insert(0, r"P:\ERAVJ\AutoVJ\src")
import audio_engine

ROOT = r"P:\ERAVJ\AutoVJ"


def old_select(cands, device_name):
    """旧实现逐字复制（来自 diff 的删除行）。"""
    mic = cands[0]
    if device_name:
        for m in cands:
            if device_name in m.name:
                mic = m
                break
    return mic


class M:
    def __init__(self, n): self.name = n


names_pool = [
    ["麦克风 (Realtek)", "线路输入 (Realtek)", "USB Audio Device"],
    ["线路输入", "线路输入 (USB)"],
    ["A", "B", "C"],
    [],
]

cfgs = ["", "线路输入", "USB Audio", "USB Audio Device", "不存在", "B"]

diff = 0
checks = 0
for names in names_pool:
    cands = [M(n) for n in names]
    for cfg in cfgs:
        if not cands:
            # 旧版：cands[0] 会 IndexError；新版 _match_device 返 (None,"")
            try:
                old_select(cands, cfg)
                o = "IndexError?no"
            except Exception as e:
                o = "raise:%s" % type(e).__name__
            n = audio_engine._match_device(cands, cfg)
            print("  cands=[] cfg=%-16r 旧=%s 新=%r" % (cfg, o, n))
            continue
        osel = old_select(cands, cfg)
        nsel, warn = audio_engine._match_device(cands, cfg)
        checks += 1
        same = (osel.name == (nsel.name if nsel else None))
        if not same:
            diff += 1
            print("  ❌ 选择不一致 cfg=%r 旧=%r 新=%r" % (cfg, osel.name, nsel.name))
print("\n[system/mic 选择语义] 比对 %d 组，选择结果不一致 %d 组" % (checks, diff))
print("  → 0 组不一致 = 除新增告警外，选中的设备与旧版完全一致")

# mono=False / 单列 / 1维 是否与旧版逐块一致：旧版 mono 分支恒取 data[:, :1]
print("\n[mono=False 路径] diff 中是否有改动？")
d = subprocess.run(["git", "show", "8879702", "--", "src/audio_engine.py"],
                   cwd=ROOT, capture_output=True, text=True,
                   encoding="utf-8", errors="replace").stdout
for ln in d.splitlines():
    if ln.startswith("-") and ("analyzer.feed" in ln or "else:" in ln):
        print("   删除行: %s" % ln)
    if ln.startswith("+") and "analyzer.feed(data, st)" in ln:
        print("   新增行: %s" % ln)
print("  → 若无 '删除 analyzer.feed(data, st)' 或 '新增', 说明 mono=False 未动")

# 实测 mono=False：单列数据 feed 是否与旧版同（旧版也是 data 原样）
print("\n[mono=False 实测] 立体声输入 mono=False ⇒ 直接 feed 整块")


class _Ctx:
    def __init__(s, r): s.r = r
    def __enter__(s): return s.r
    def __exit__(s, *a): return False


class Rec:
    HARD_CAP = 50
    def __init__(s, data): s.data = data; s.k = 0
    def record(s, numframes=None):
        s.k += 1
        if s.k > s.HARD_CAP: raise RuntimeError("cap")
        return s.data


class Mic:
    name = "桩"
    def __init__(s, r): s.r = r
    def recorder(s, **k): return _Ctx(s.r)


def run(mono, data):
    eng = audio_engine.AudioEngine.__new__(audio_engine.AudioEngine)
    eng.state = audio_engine.AudioState()
    eng._genre_lock = threading.RLock(); eng._genre_sr = 0
    eng._genre_buf = audio_engine._AudioRing(int(48000 * 12))
    eng._gen = 1; eng.energy_scale = 1.0
    eng.push_ndi_audio = lambda *a, **k: None
    eng.state.ndi_audio_on = False; eng.state.ndi_feed = None
    orig = audio_engine._Analyzer.feed
    shapes = []
    def cf(self, d, st):
        shapes.append(np.asarray(d).shape)
        if len(shapes) >= 5: eng._gen = 999
        return orig(self, d, st)
    audio_engine._Analyzer.feed = cf
    try:
        eng._capture_loop(Mic(Rec(data)), mono, 48000, 1)
    finally:
        audio_engine._Analyzer.feed = orig
    return shapes


sig = (0.5 * np.sin(2 * np.pi * 1000 * np.arange(1024) / 48000)).astype(np.float32)
stereo = np.column_stack([sig, sig])
print("  mono=False, 立体声 → feed 形状序列:", run(False, stereo))
print("  mono=True , 立体声 → feed 形状序列:", run(True, stereo))
print("  → mono=False 应恒为 (1024,2) 原样；mono=True 恒为 (1024,1)")
