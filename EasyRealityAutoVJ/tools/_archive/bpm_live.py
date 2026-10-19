# -*- coding: utf-8 -*-
r"""BPM 现场体检：实时播放音乐 → 采集系统回环 → 喂真实 _Analyzer，逐秒打印 BPM 建立过程。

用途：判定「BPM 一直是 0」到底卡在哪一侧（三段门控全打印出来，不用猜）
  ① 电平极低（lv_db < -50）= 回环拿到静音 → 音源设备选错 / 播放器独占输出
  ② 有声音、但投票不涨       → 周期估计阶段没触发（kick 与全频都没找到周期）
  ③ 投票在涨、众数在 115~185 外或 conf<0.45 → 建锁门控拒绝（BPM 不显示）

用法：
  venv\Scripts\python.exe tools\bpm_live.py [文件] [起始秒] [时长秒]
输出：P:\AutoVJ\_bpm_live.txt
"""
import os
import sys
import time
import threading
import collections

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import numpy as np  # noqa: E402

try:
    import soundcard as sc
except Exception as e:  # noqa
    print("soundcard 导入失败:", e)
    sys.exit(2)

import av  # noqa: E402
from audio_engine import AudioState, _Analyzer, BLOCK  # noqa: E402

SR = 48000
GAIN = 0.5           # 播放增益（防太响；BPM 检测靠自相关，与绝对电平无关）
OUT = r"P:\AutoVJ\_bpm_live.txt"
out = []


def w(s=""):
    out.append(str(s))
    print(s, flush=True)


def decode(path, t0, dur):
    c = av.open(path)
    st = c.streams.audio[0]
    res = av.AudioResampler(format="fltp", layout="stereo", rate=SR)
    frames = []
    for frame in c.decode(st):
        for f in res.resample(frame):
            frames.append(f.to_ndarray().T.astype(np.float32))
    for f in res.resample(None):
        frames.append(f.to_ndarray().T.astype(np.float32))
    c.close()
    x = np.concatenate(frames, axis=0) if frames else np.zeros((0, 2), np.float32)
    a, b = int(t0 * SR), int(min(len(x), (t0 + dur) * SR))
    return x[a:b] * GAIN


def pick_devices():
    spks = sc.all_speakers()
    mics = sc.all_microphones(include_loopback=True)
    spk = None
    for s in spks:
        if "Realtek" in s.name:
            spk = s
            break
    if spk is None:
        spk = sc.default_speaker()
    mic = None
    for m in mics:
        if m.name == spk.name:
            mic = m
            break
    if mic is None:
        for m in mics:
            if "Realtek" in m.name:
                mic = m
                break
    return spk, mic, spks, mics


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else r"W:\临时结界相关\2of\tofu\Rolling Contact - Euclid Number.flac"
    t0 = float(sys.argv[2]) if len(sys.argv) > 2 else 80.0
    dur = float(sys.argv[3]) if len(sys.argv) > 3 else 30.0

    w("=" * 96)
    w("BPM 现场体检（实时播放 + 回环采集 + 真实分析器）  %s" % time.strftime("%H:%M:%S"))
    w("=" * 96)
    if not os.path.exists(path):
        w("文件不存在: %s" % path)
        open(OUT, "w", encoding="utf-8").write("\n".join(out))
        return
    seg = decode(path, t0, dur)
    w("文件 %s  区间 %.0fs~%.0fs  时长 %.1fs  BLOCK=%d" % (os.path.basename(path), t0, t0 + dur, len(seg) / SR, BLOCK))
    spk, mic, spks, mics = pick_devices()
    w("播放: %s" % (spk.name if spk else "(无)"))
    w("采集: %s" % (mic.name if mic else "(找不到回环)"))
    if spk is None or mic is None:
        w("设备缺失，无法测")
        open(OUT, "w", encoding="utf-8").write("\n".join(out))
        return
    w("")

    stop = threading.Event()

    def player():
        try:
            with spk.player(samplerate=SR, channels=2, blocksize=BLOCK * 4) as p:
                i = 0
                while i < len(seg) and not stop.is_set():
                    p.play(seg[i:i + BLOCK * 4])
                    i += BLOCK * 4
        except Exception as e:  # noqa
            w("播放线程异常: %s" % e)

    th = threading.Thread(target=player, daemon=True)
    th.start()

    an = _Analyzer(SR)
    st = AudioState()
    lv_hist = collections.deque(maxlen=60)
    t_start = time.perf_counter()
    last_report = -1
    with mic.recorder(samplerate=SR, channels=max(1, min(2, mic.channels)), blocksize=BLOCK) as rec:
        while time.perf_counter() - t_start < dur + 1.0:
            data = rec.record(numframes=BLOCK)
            d = np.asarray(data, dtype=np.float32)
            if d.ndim == 1:
                d = d.reshape(-1, 1)
            try:
                an.feed(d, st)
            except Exception as e:  # noqa
                w("feed 异常: %s" % e)
                break
            lv_hist.append(st.lv_db)
            el = time.perf_counter() - t_start
            if int(el) != last_report:
                last_report = int(el)
                votes = list(an._bpm_votes)
                mode_s = conf_s = "-"
                if len(votes) >= 6:
                    v = np.array(votes, dtype=np.float32)
                    hist, edges = np.histogram(v, bins=np.arange(60.0, 201.0, 1.0))
                    pk = int(np.argmax(hist))
                    mode_s = "%.1f" % (edges[pk] + 0.5)
                    conf_s = "%.3f" % (float(hist[pk]) / len(v))
                w("  t=%4.1fs  电平=%6.1fdB  能量=%.2f  BPM=%6.2f  投票=%2d  锁定=%-5s  "
                  "众数=%-6s conf=%-6s  beat_active=%-5s  interval=%.3f"
                  % (el, st.lv_db, st.energy, an.bpm_smooth, len(votes), an._bpm_lock,
                     mode_s, conf_s, st.beat_active, an.beat_interval))
            if el > dur + 0.5 and not stop.is_set():
                stop.set()

    stop.set()
    med = float(np.median(lv_hist)) if lv_hist else -99.0
    w("")
    w("=" * 96)
    w("结论")
    w("=" * 96)
    w("采集电平中位 %.1f dBFS   显示 BPM %.2f（锁定值 %.2f / 投票 %d / 锁定 %s）"
      % (med, an.bpm_display, an.bpm_smooth, len(an._bpm_votes), an._bpm_lock))
    if med < -50:
        w("❌ 回环基本是静音 → **音频输入侧**问题：音源设备选错、播放器走了独占输出（ASIO/WASAPI Exclusive）、")
        w("   或者播放设备不是当前默认输出。这条链路拿不到声音，BPM/能量自然全是 0。")
    elif an.bpm_display > 0:
        w("✅ 实时链路能显示 BPM → 算法、采集、显示都正常。若软件界面仍是 0，")
        w("   重新确认软件里的「音源」与刚才播放的设备是同一台（播放中对照 HUD 的能量有没有动）。")
    elif len(an._bpm_votes) < 6:
        w("⚠️ 有声音但几乎没投票 → 曲子缺规律鼓点（ambient/人声/长音），或 kick 被削（低音增强/压缩）")
    else:
        w("⚠️ 有声音、投票也在涨，但众数与集中度都没过显示门槛（需票数≥10 且 conf≥0.40）→ 门槛偏严，需要放宽")
    open(OUT, "w", encoding="utf-8").write("\n".join(out))


if __name__ == "__main__":
    main()
