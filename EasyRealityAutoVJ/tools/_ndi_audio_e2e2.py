# -*- coding: utf-8 -*-
"""真机端到端（带声源）：播放一段测试音，验证能量与 NDI 投递都增长。

做法：后台用 soundcard 播放一段正弦，同时启动 AudioEngine 采集 loopback，
      观察 energy / ndi_q 投递计数。这样才能证明"采集→分析→NDI"整条链在动。
"""
import os
import sys
import threading
import time

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))


def play_tone(secs, sr=48000, freq=220.0):
    import soundcard as sc
    spk = sc.default_speaker()
    t = np.arange(int(sr * secs), dtype=np.float32) / sr
    # 加一点节奏（每 0.5s 一个包络）便于能量有起伏
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 2.0 * t)
    x = (0.3 * env * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    stereo = np.column_stack([x, x])
    with spk.player(samplerate=sr, channels=2) as p:
        step = 4800
        for i in range(0, len(stereo), step):
            p.play(stereo[i:i + step])


def main():
    import audio_engine as ae

    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
    th = threading.Thread(target=play_tone, args=(secs,), daemon=True)
    th.start()

    eng = ae.AudioEngine()
    eng.energy_scale = 1.0
    eng.start(source_type="system", device_name="", mono=False, sr=0)
    time.sleep(1.5)
    st = eng.state
    peak_e = 0.0
    pushes = []
    for i in range(int(secs)):
        time.sleep(1.0)
        e = float(getattr(st, "energy", 0.0) or 0.0)
        peak_e = max(peak_e, e)
        try:
            qlen = len(eng._ndi_q)
        except Exception:                                          # noqa: BLE001
            qlen = -1
        pushes.append(qlen)
        with st.lock:
            err = st.error
        print("  t=%2ds energy=%.3f qlen=%d err=%s" % (i + 1, e, qlen, err or "-"))
    eng.stop()
    time.sleep(0.3)
    print("=" * 50)
    print("峰值能量 %.3f" % peak_e)
    if peak_e > 0.05:
        print("✓ 能量链路正常（采集→分析 有数据流动）")
    else:
        print("✗ 能量始终接近 0 —— 采集可能没拿到数据")
    try:
        seen = sum(1 for q in pushes if q > 0)
        print("有投递的秒数 %d/%d" % (seen, len(pushes)))
    except Exception:                                              # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
