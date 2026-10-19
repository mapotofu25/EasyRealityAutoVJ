# -*- coding: utf-8 -*-
"""用真实音乐走一遍系统播放→回环，逐频段比较「播出去的」和「录回来的」。

- 播放由本脚本用 WASAPI 直接完成（PyAV 解码 + soundcard），**不经过任何播放器/DAW**，
  所以任何频段差异都只能来自系统链路（音频引擎 / APO / 驱动）。
- 分两个电平各放一遍，用来识别「只在小信号时抬低频」的动态增强（等响度类）。
用法：venv\\Scripts\\python.exe tools\\music_path_probe.py "<音频文件>" [起始秒] [时长秒]
"""
import os
import sys
import time
import threading
import wave
import numpy as np

try:
    import soundcard as sc
    import av
except Exception as e:  # noqa
    print("依赖导入失败:", e)
    sys.exit(2)

SR = 48000
BANDS = [(20, 40), (40, 60), (60, 90), (90, 125), (125, 180), (180, 250),
         (250, 500), (500, 1000), (1000, 2000), (2000, 4000), (4000, 8000), (8000, 16000)]
MID = (500, 4000)      # 归一化基准
OUT_TXT = r"P:\AutoVJ\_music_path.txt"

buf = []


def w(s=""):
    buf.append(str(s))
    print(s, flush=True)


def decode(path, start_s, dur_s):
    """PyAV 解码指定区段 -> float32 [n, 2] @48k"""
    want = int((start_s + dur_s) * SR)
    out = []
    with av.open(path) as c:
        st = c.streams.audio[0]
        res = av.AudioResampler(format="fltp", layout="stereo", rate=SR)
        got = 0
        for frame in c.decode(st):
            if frame.pts is None:
                continue
            t = float(frame.pts * frame.time_base)
            for f in (res.resample(frame) or []):
                arr = f.to_ndarray()          # [ch, n]
                arr = np.asarray(arr, dtype=np.float32).T   # [n, ch]
                if got + len(arr) <= int(start_s * SR):
                    got += len(arr)
                    continue
                skip = max(0, int(start_s * SR) - got)
                arr = arr[skip:]
                got += skip + len(arr)
                out.append(arr)
                if got >= want:
                    break
            if got >= want:
                break
    x = np.concatenate(out) if out else np.zeros((0, 2), np.float32)
    return x[:want]


def band_db(x):
    """各频段能量 dB。多声道时按「每声道能量相加」而不是把声道平均成单声道
    —— 声道反相时平均会抵消低频，会让结论完全反过来。"""
    x = np.asarray(x, dtype=np.float64)
    if x.ndim == 1:
        x = x.reshape(-1, 1)
    n = len(x)
    if n < SR // 4:
        return np.full(len(BANDS), np.nan)
    win = np.hanning(n)[:, None]
    wsum = float(np.sum(win[:, 0] ** 2))
    sp = np.abs(np.fft.rfft(x * win, axis=0)) ** 2
    fr = np.fft.rfftfreq(n, 1.0 / SR)
    res = []
    for lo, hi in BANDS:
        m = (fr >= lo) & (fr < hi)
        e = float(sp[m].sum())
        res.append(10 * np.log10(max(e / wsum, 1e-20)))
    return np.array(res)


def align(played, rec):
    """把录音对齐到播放信号：包络互相关求时延，返回 (录音切片, 时延秒)。
    不对齐的话「原文件 vs 录回」的频段差会被前导静音/截断污染成十几 dB 的假差异。"""
    env = lambda a: np.convolve(np.abs(a.mean(axis=1) if a.ndim > 1 else a),
                                np.ones(240) / 240, mode="same")[::8]
    a, b = env(np.asarray(played, dtype=np.float64)), env(np.asarray(rec, dtype=np.float64))
    if len(a) < 16 or len(b) < 16:
        return rec, 0.0
    n = 1
    while n < len(a) + len(b):
        n *= 2
    A = np.fft.rfft(a - a.mean(), n)
    B = np.fft.rfft(b - b.mean(), n)
    cc = np.fft.irfft(B * np.conj(A), n)
    lag = int(np.argmax(cc))
    if lag > n // 2:
        lag -= n
    off = lag * 8
    return rec[off:off + len(played)], off / SR


def play_record(spk, mic, data, ch):
    got = {}

    def rec():
        try:
            with mic.recorder(samplerate=SR, channels=ch) as r:
                got["x"] = r.record(int((len(data) / SR + 0.6) * SR))
        except Exception as e:  # noqa
            got["err"] = e

    th = threading.Thread(target=rec)
    th.start()
    time.sleep(0.3)
    try:
        with spk.player(samplerate=SR, channels=2) as p:
            p.play(data)
    except Exception as e:  # noqa
        w("  播放失败: %s" % e)
    th.join()
    if "err" in got:
        w("  录音失败: %s" % got["err"])
        return None
    return np.asarray(got["x"], dtype=np.float64)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else r"W:\临时结界相关\2of\tofu\Rolling Contact - Euclid Number.flac"
    start = float(sys.argv[2]) if len(sys.argv) > 2 else 130.0
    dur = float(sys.argv[3]) if len(sys.argv) > 3 else 12.0
    w("=" * 70)
    w("真实音乐链路对照  %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    w("文件：%s" % path)
    w("区段：%.0f s 起，%.0f s 长" % (start, dur))
    w("=" * 70)

    spk = None
    for s in sc.all_speakers():
        if "Realtek" in s.name:
            spk = s
            break
    if spk is None:
        spk = sc.default_speaker()
    mic = None
    for m in sc.all_microphones(include_loopback=True):
        if m.name == spk.name:
            mic = m
            break
    if mic is None:
        w("找不到 %s 的回环" % spk.name)
        return
    w("播放=%s   采集=%s" % (spk.name, mic.name))

    raw = decode(path, start, dur)
    if len(raw) < SR:
        w("解码失败或太短")
        return
    base = band_db(raw)
    w("")
    w("○ 原文件频段能量（播放前，作为基准）")

    for tag, peak in (("低电平", 0.12), ("高电平", 0.45)):
        mx = float(np.max(np.abs(raw))) + 1e-9
        sig = (raw / mx * peak).astype(np.float32)
        w("")
        w("── %s（峰值 %.2f ≈ %.0f dBFS）──" % (tag, peak, 20 * np.log10(peak)))
        rec = play_record(spk, mic, sig, max(1, min(2, mic.channels)))
        if rec is None:
            continue
        aligned, off = align(sig, rec)
        if len(aligned) < SR // 2:
            w("  对齐失败，跳过")
            continue
        if len(aligned) > len(raw):
            aligned = aligned[:len(raw)]
        recb = band_db(aligned)
        w("  对齐偏移 %.3f s，参与比较 %.1f s" % (off, len(aligned) / SR))
        mid_i = [i for i, (lo, hi) in enumerate(BANDS) if lo >= MID[0] and hi <= MID[1]]
        mid = float(np.mean([base[i] - recb[i] for i in mid_i]))
        w("  %9s %10s %10s %10s" % ("频段Hz", "原文件dB", "录回dB", "相对差dB"))
        for i, (lo, hi) in enumerate(BANDS):
            d = (base[i] - recb[i]) - mid      # 相对 0.5~4kHz 归一化后的偏差
            flag = ""
            if lo <= 125 and abs(d) > 2.0:
                flag = "  ← 低频被抬 %.1f dB" % d
            elif lo <= 125 and abs(d) <= 1.0:
                flag = "  （平）"
            w("  %4d-%-5d %10.1f %10.1f %10.2f%s" % (lo, hi, base[i], recb[i], d, flag))
        w("  ≈ 整体电平差 %+.1f dB（只反映播放音量，与音色无关）；"
          "「相对差」全为 0 = 这条链路完全透明" % mid)

    open(OUT_TXT, "w", encoding="utf-8").write("\n".join(buf))
    w("")
    w("已写 %s" % OUT_TXT)


if __name__ == "__main__":
    main()
