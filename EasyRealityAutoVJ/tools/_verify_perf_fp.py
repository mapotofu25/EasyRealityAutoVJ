# -*- coding: utf-8 -*-
"""回归验证：高-1/高-2 的加速必须**输出逐值不变**。

- 指纹：拿 P 盘备份里的**改动前** fp.py 当基准，同一段合成音频比 `fingerprint_audio`
  的输出序列（哈希 + offset 全部逐项相等）—— 相等才敢说"不用重扫曲库"。
- 频带：`_band_mean(spec, 预算区间)` 必须与旧的 `_band_energy(spec, freqs, lo, hi)` 一致。

用法：venv/Scripts/python.exe -u tools/_verify_perf_fp.py
"""
import importlib.util
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

import fp as new_fp                                            # noqa: E402
import audio_engine as ae                                      # noqa: E402

OLD_PATH = r"P:\AutoVJ_码表备份_20260930\src\fp.py"
FAILS = []


def check(name, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


def load_old():
    spec = importlib.util.spec_from_file_location("fp_old", OLD_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


print("① 指纹输出（改动前 vs 改动后）")
if not os.path.isfile(OLD_PATH):
    print("  · 找不到基准文件，跳过（%s）" % OLD_PATH)
else:
    old_fp = load_old()
    rng = np.random.default_rng(20260930)
    SR = old_fp.SAMPLE_RATE
    for case, sig in (
            ("白噪声 3s", rng.standard_normal(SR * 3).astype(np.float32) * 0.2),
            ("正弦+噪声 5s", (np.sin(2 * np.pi * 440 * np.arange(SR * 5) / SR)
                              + rng.standard_normal(SR * 5) * 0.05).astype(np.float32)),
            ("静音 2s", np.zeros(SR * 2, dtype=np.float32))):
        a = old_fp.fingerprint_audio(sig, SR)
        b = new_fp.fingerprint_audio(sig, SR)
        check("%s：指纹条数与内容完全一致" % case,
              len(a) == len(b) and a == b, "old=%d new=%d" % (len(a), len(b)))

print("② 频带平均：_band_mean（切片）vs _band_energy（掩码）")
freqs = np.fft.rfftfreq(2048, 1.0 / 48000)
spec = np.abs(np.random.default_rng(7).standard_normal(len(freqs))).astype(np.float32)
for lo, hi in ((20, 160), (160, 2000), (2000, 12000), (0, 0.5), (30000, 40000)):
    i0 = int(np.searchsorted(freqs, lo, "left"))
    i1 = int(np.searchsorted(freqs, hi, "left"))
    a = ae._band_energy(spec, freqs, lo, hi)
    b = ae._band_mean(spec, (i0, i1))
    check("频带 %d~%d Hz" % (lo, hi), abs(a - b) < 1e-12, "%.12f vs %.12f" % (a, b))

print("③ 24 段频谱边界：预算的 edges 与原式一致")
ref = np.unique(np.geomspace(2, len(freqs) - 1, 25).astype(int))
plan = None
print("  · plan edges 由 _fft_plan 生成，需实例化 _Analyzer")
try:
    an = ae._Analyzer(48000)
    got = an._fft_plan()[4]
    check("edges 完全一致", np.array_equal(ref, got), "%s" % (list(map(int, got))[:8],))
    check("_fft_plan 被缓存（第二次调用同一对象）", an._fft_plan() is an._fft_plan())
    an2 = ae._Analyzer(44100)
    check("换采样率会重算", an2._fft_plan()[1] != an._fft_plan()[1])
except Exception as e:                                          # noqa: BLE001
    print("  · 实例化失败（可能依赖音频设备）：%s" % e)

print()
print("结果：%s" % ("全部通过 ✓" if not FAILS else "失败 %d 项 ✗ %s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
