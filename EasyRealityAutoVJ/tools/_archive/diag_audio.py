# -*- coding: utf-8 -*-
"""实时音频诊断：采集系统声音，存 wav + 能量轨迹，用于对比离线分析。
用法：venv/Scripts/python tools/diag_audio.py [输出目录] [采集秒数]
      采集秒数默认 180（覆盖到 3 分钟，能测到 1:50 的人声段）
产物：diag_capture.wav（原始采集音频）+ diag_energy.txt（每秒能量轨迹）
"""
import sys
import os
import json
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import numpy as np
import soundfile as sf


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    duration = float(sys.argv[2]) if len(sys.argv) > 2 else 180.0
    wav_path = os.path.join(out_dir, "diag_capture.wav")
    energy_path = os.path.join(out_dir, "diag_energy.txt")

    import soundcard as sc
    from audio_engine import _Analyzer, AudioState

    # 读配置（若存在）；其他电脑上没有 config 就用默认值，不依赖 Config 类
    a = {}
    cfg_path = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "config.json")
    if os.path.exists(cfg_path):
        try:
            a = json.load(open(cfg_path, encoding="utf-8")).get("audio", {}) or {}
        except Exception:
            a = {}
    device_name = a.get("device_name", "") or ""
    mics = sc.all_microphones(include_loopback=True)
    loop = [m for m in mics if getattr(m, "isloopback", False)]
    if not loop:
        print("找不到可采集系统声音的 Loopback 设备。")
        return
    mic = loop[0]
    if device_name:
        for m in loop:
            if device_name in m.name:
                mic = m
                break
    mono = bool(a.get("mono", True))
    sr = int(a.get("sample_rate", 0) or 0)
    if not sr:
        from audio_engine import _native_samplerate
        sr = _native_samplerate(mic) or 48000
    gain = float(a.get("gain", 1.0))
    print("采集设备:", mic.name, "| sr:", sr, "| 设备声道:", mic.channels, "| mono:", mono, "| gain:", gain)
    if mic.channels > 2:
        print("⚠ 设备当前是 %d 声道（环绕/空间音效）——loopback 采 2 声道会下混丢低频，能量会偏低！" % mic.channels)

    BLOCK = 1024
    analyzer = _Analyzer(sr)
    st = AudioState()
    buf = []
    energy_log = []
    gaps = []                     # 相邻块的墙钟间隔（检测丢块/卡顿）
    last_t = None
    t0 = time.perf_counter()
    last_sec = -1
    print(f"开始采集 {duration:.0f} 秒系统声音。")
    print("请按你平时的习惯：先保持安静，然后从头播放那首歌（覆盖到 1:50 之后的人声段）。")
    with mic.recorder(samplerate=sr, channels=2) as rec:
        while time.perf_counter() - t0 < duration:
            data = rec.record(numframes=BLOCK)
            now = time.perf_counter()
            if last_t is not None:
                gaps.append(now - last_t)
            last_t = now
            if data is None or len(data) == 0:
                continue
            buf.append(data.copy())
            # 与软件 _capture_loop 完全一致：按 mono 配置决定喂单声道还是立体声
            if mono:
                analyzer.feed(data[:, :1], st)
            else:
                analyzer.feed(data, st)
            sec = int(time.perf_counter() - t0)
            if sec != last_sec:
                last_sec = sec
                energy_log.append((sec, round(st.energy, 3), round(st.bass, 3), round(st.vocalness, 3)))
                print(f"  {sec:3d}s  能量={st.energy:.2f}  bass={st.bass:.3f}  人声={st.vocalness:.2f}")

    # 丢块/卡顿统计：正常块间隔 ≈ BLOCK/sr（如 1024/48000≈21ms）。出现 >3 倍的间隔
    # 说明采集线程被抢占或缓冲溢出（录屏/高负载场景）——这会让 kick 通量等瞬态特征失真。
    import statistics
    if gaps:
        expect = BLOCK / sr
        med = statistics.median(gaps)
        big = [g for g in gaps if g > expect * 3]
        print(f"\n采集健康度: 块间隔中位={med*1000:.1f}ms (期望 {expect*1000:.1f}ms)")
        print(f"  超过 3 倍期望间隔的次数: {len(big)}/{len(gaps)}" +
              (f"  最大 {max(gaps)*1000:.0f}ms  ⚠ 有卡顿/丢块" if big else "  ✓ 无卡顿"))

    audio = np.concatenate(buf, axis=0)
    sf.write(wav_path, audio, sr)
    with open(energy_path, "w", encoding="utf-8") as f:
        f.write("秒\t能量\tbass\t人声\n")
        for sec, e, b, v in energy_log:
            f.write(f"{sec}\t{e:.3f}\t{b:.3f}\t{v:.3f}\n")
    print(f"\n完成。音频已存: {wav_path}")
    print(f"能量轨迹已存: {energy_path}")


if __name__ == "__main__":
    main()
