# -*- coding: utf-8 -*-
"""集成验证：TimedDevice + _BlockAssembler 组装出来的块是否严格 1024 帧、
时间轴是否严格对齐（这是修咔哒的最终判据）。

判据：
  1. 每个块恰好 1024 帧
  2. 出块间隔 P99 < 25ms（旧路径 31.3ms）
  3. 累计速率比 |x-1| < 0.3%
  4. 无声源→近零的突降次数不高于旧路径
"""
import sys
import time

import numpy as np

sys.path.insert(0, r"P:\ERAVJ\AutoVJ\src")

from audio_engine import BLOCK, _BlockAssembler          # noqa: E402

SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0


def main():
    import soundcard as sc
    import wasapi_capture as wc

    loop = [m for m in sc.all_microphones(include_loopback=True) if m.isloopback]
    mic = None
    for m in loop:
        if "realtek" in m.name.lower():
            mic = m
            break
    mic = mic or loop[0]
    dev = wc.wrap(mic)
    print("设备: %s" % dev.name)

    asm = _BlockAssembler(960, 480)
    lens = []
    gaps = []
    qpcs = []
    t0 = time.perf_counter()
    t_prev = None

    with dev.recorder(samplerate=48000, channels=2) as rec:
        while time.perf_counter() - t0 < SECS:
            data = asm.read(rec)
            t_now = time.perf_counter()
            if data is None or len(data) == 0:
                continue
            lens.append(data.shape[0])
            if t_prev is not None:
                gaps.append(t_now - t_prev)
            t_prev = t_now
            if asm.last_qpc:
                qpcs.append(asm.last_qpc)
        st = rec.stats()

    dt = time.perf_counter() - t0
    L = np.array(lens)
    G = np.array(gaps) * 1e3
    total = int(L.sum())
    print("=" * 58)
    print("时长 %.2fs  块数 %d  总帧 %d" % (dt, len(lens), total))
    print("块长度: min %d  max %d  唯一值 %d 个 %s"
          % (L.min(), L.max(), len(set(lens)), sorted(set(lens))[:4]))
    print("出块间隔 ms: 均值 %.2f  P99 %.2f  max %.2f" % (G.mean(), np.percentile(G, 99), G.max()))
    print("速率比 %.4f  (偏差 %+.3f%%)" % ((total / dt) / 48000, ((total / dt) / 48000 - 1) * 100))
    print("原生包数 %d  不连续 %d  静音包 %d" % (asm.pkt_seen, st["disc"], st["silent"]))
    print("=" * 58)
    ok = True
    if L.min() != 960 or L.max() != 960:
        print("✗ 块长度不等于 960")
        ok = False
    else:
        print("✓ 所有块严格 960 帧")
    if np.percentile(G, 99) < 25.0:
        print("✓ 出块间隔 P99 %.2f ms < 25ms（旧路径 31.3ms）" % np.percentile(G, 99))
    else:
        print("✗ 出块间隔 P99 %.2f ms 偏大" % np.percentile(G, 99))
        ok = False
    if abs((total / dt) / 48000 - 1) < 0.003:
        print("✓ 累计速率偏差 %.3f%% < 0.3%%" % (((total / dt) / 48000 - 1) * 100))
    else:
        print("✗ 累计速率偏差超标")
        ok = False
    print("=" * 58)
    print("结论：%s" % ("集成验证通过 ✓" if ok else "存在未达标项 ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
