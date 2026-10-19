# -*- coding: utf-8 -*-
"""复现音频分析块的报错（现场状态栏那条 "分析块错误（已跳过）: ..."）。

做法：直接喂 1024 帧给 `_Analyzer.feed()`（引擎采集线程就是这么调的），
把异常完整打出来 —— 比在整机里看"被跳过"的提示有用得多。
"""
import os
import sys
import traceback

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

import audio_engine as ae                                       # noqa: E402

print("BLOCK =", ae.BLOCK)
for sr in (48000, 44100):
    print("\n===== 采样率 %d =====" % sr)
    an = ae._Analyzer(sr)
    st = ae.AudioState()
    rng = np.random.default_rng(20260930)
    for i in range(6):
        data = (rng.standard_normal((ae.BLOCK, 1)) * 0.1).astype(np.float32)
        try:
            an.feed(data, st)
        except Exception:                                       # noqa: BLE001
            print("★ 第 %d 块抛异常：" % (i + 1))
            traceback.print_exc()
            break
    else:
        print("6 块都正常；st.error =", repr(st.error),
              "| 能量 =", round(float(st.snapshot().get("energy", -1)), 3),
              "| BPM =", st.snapshot().get("bpm"))
    if st.error:
        print("st.error =", repr(st.error))
