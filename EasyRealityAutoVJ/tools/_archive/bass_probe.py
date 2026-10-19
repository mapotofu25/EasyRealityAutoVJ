# -*- coding: utf-8 -*-
r"""低频增强隔离实测（bass_probe）

做一件事：播放一段「已知电平的多频点测试音」，同时录下系统采集路径，逐频点算出增益 dB。
- 不经任何播放器/DAW（直接 WASAPI 播放），所以测到的抬升只可能来自系统链（音频引擎/APO/驱动）。
- 分别测两条采集路径：
    A) 系统回环 loopback（AutoVJ 的能量分析就用这条）
    B) 立体声混音 / 其它可用采集端点（如果 Windows 里启用了）
- 另外做一次「电平步进」：同一频率在 -30/-18/-6 dBFS 各放一次，
  若小信号被抬得更多 → 是「等响度/响度均衡/智能音量」这类动态增强。

用法：P:\AutoVJ\venv\Scripts\python.exe tools\bass_probe.py
"""
import sys
import time
import wave
import threading
import numpy as np

try:
    import soundcard as sc
except Exception as e:  # noqa
    print("soundcard 导入失败:", e)
    sys.exit(2)

SR = 48000
FREQS = [40, 60, 90, 125, 180, 250, 400, 700, 1000, 2000, 4000, 8000]
SEG = 0.6          # 每频点时长（含 0.15s 间隔）
TONE = 0.45
LEAD = 1.2         # 前置静音
AMP = 0.12         # ≈ -18 dBFS
SWEEP = [(60, 0.012), (60, 0.12), (60, 0.60), (1000, 0.012), (1000, 0.12), (1000, 0.60)]
SW_SEG = 0.7
OUT_TXT = r"P:\AutoVJ\_bass_probe.txt"
OUT_WAV = r"P:\AutoVJ\_bassprobe_loopback.wav"

buf = []


def w(s=""):
    buf.append(str(s))
    print(s, flush=True)


def build_signal():
    """返回 (音频, marks)；marks 里 start 是「相对第一个测试音起点」的秒数（可正可负判断用），
    这样录音只要对齐到第一个音的起点，就能直接按秒取窗。"""
    n_lead = int(LEAD * SR)
    n_seg = int(SEG * SR)
    n_tone = int(TONE * SR)
    parts = []
    marks = []
    off = 0
    t = np.arange(n_tone) / SR
    for f in FREQS:
        seg = np.zeros(n_seg)
        seg[:n_tone] = AMP * np.sin(2 * np.pi * f * t)
        marks.append((f, AMP, (off + int(0.08 * SR)) / SR, 0.30))   # 窗完全落在音里
        parts.append(seg)
        off += n_seg
    for f, a in SWEEP:
        nn = int(SW_SEG * SR) - int(0.2 * SR)
        seg = np.zeros(int(SW_SEG * SR))
        seg[:nn] = a * np.sin(2 * np.pi * f * np.arange(nn) / SR)
        marks.append((f, a, (off + int(0.06 * SR)) / SR, 0.40))
        parts.append(seg)
        off += int(SW_SEG * SR)
    parts.append(np.zeros(int(0.5 * SR)))
    x = np.concatenate(parts)
    return x.astype(np.float32), marks


def gain_db(rec, sr, marks):
    rows = []
    for f, amp, start, ln in marks:
        s = int(start * sr)
        e = min(len(rec), s + int(ln * sr))
        if e - s < 256:
            rows.append((f, amp, float("nan")))
            continue
        chunk = rec[s:e]
        if chunk.ndim > 1:
            chunk = chunk.mean(axis=1)
        x = chunk * np.hanning(len(chunk))
        sp = np.abs(np.fft.rfft(x)) / (len(x) / 4.0)
        fr = np.fft.rfftfreq(len(x), 1.0 / sr)
        i0 = int(np.searchsorted(fr, f * 0.93))
        i1 = max(i0 + 1, int(np.searchsorted(fr, f * 1.07)))
        peak = float(sp[i0:i1].max())
        rows.append((f, amp, 20 * np.log10(peak / max(amp, 1e-9))))
    return rows


def record_while_playing(spk, mic, data, ch):
    got = {}

    def rec():
        try:
            with mic.recorder(samplerate=SR, channels=ch) as r:
                got["x"] = r.record(int((len(data) / SR + 0.8) * SR))
        except Exception as e:  # noqa
            got["err"] = e

    th = threading.Thread(target=rec)
    th.start()
    time.sleep(0.35)
    try:
        with spk.player(samplerate=SR, channels=min(2, spk.channels)) as p:
            p.play(np.repeat(data.reshape(-1, 1), min(2, spk.channels), axis=1))
    except Exception as e:  # noqa
        w("  播放失败: %s" % e)
    th.join()
    if "err" in got:
        w("  录音失败: %s" % got["err"])
        return None
    return np.asarray(got["x"], dtype=np.float64)


def analyze(tag, rec, marks, data):
    if rec is None:
        return
    w("")
    w("── %s ──" % tag)
    # 对齐：找第一个超过阈值的位置
    mono = rec.mean(axis=1) if rec.ndim > 1 else rec
    idx = np.where(np.abs(mono) > max(0.02, AMP * 0.25))[0]
    t0 = int(idx[0]) if len(idx) else 0
    w("  噪声底 ≈ %.1f dBFS   对齐点 = %.3f s" % (
        20 * np.log10(np.sqrt(np.mean(mono[:max(1, t0)] ** 2)) + 1e-12), t0 / SR))
    aligned = rec[t0:]
    rows = gain_db(aligned, SR, marks)
    w("  %8s %10s %10s" % ("频率Hz", "原始幅度", "测得增益dB"))
    for f, amp, g in rows:
        flag = ""
        if not np.isnan(g):
            if f <= 125 and g > 2:
                flag = "  ← 低频被抬"
            if f >= 1000 and abs(g) < 2:
                flag += "  (基准)"
        w("  %8.0f %10.3f %10.2f%s" % (f, amp, g, flag))

    # 电平依赖：同频率不同电平的增益差
    by = {}
    for f, amp, g in rows:
        by.setdefault(f, []).append((amp, g))
    for f in (60, 1000):
        if len(by.get(f, [])) >= 3:
            v = sorted(by[f])
            g_small, g_big = v[0][1], v[-1][1]
            w("  → %dHz 电平依赖：小信号(%s) %+.2f dB vs 大信号(%s) %+.2f dB  ⇒ 差 %+.2f dB%s"
              % (f, "%.3f" % v[0][0], g_small, "%.3f" % v[-1][0], g_big, g_small - g_big,
                 "  ← 小信号被抬更多 = 动态增强(等响度类)" if (g_small - g_big) > 2 else ""))


def main():
    w("=" * 68)
    w("低频增强隔离实测  %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    w("=" * 68)
    spks = sc.all_speakers()
    w("所有播放设备：")
    for s in spks:
        w("   - %-46s ch=%s" % (s.name, s.channels))
    loop = sc.all_microphones(include_loopback=True)
    w("所有采集/回环设备：")
    for m in loop:
        w("   - %-46s ch=%s loopback=%s" % (m.name, m.channels, getattr(m, "isloopback", "?")))

    spk = None
    for s in spks:
        if "Realtek" in s.name:
            spk = s
            break
    if spk is None:
        spk = sc.default_speaker()
    w("")
    w("→ 用播放设备：%s" % spk.name)

    data, marks = build_signal()
    w("信号：%d 个频点各 %.2fs + %d 段电平步进，总长 %.1fs，基础电平 %.3f(%+.0f dBFS)"
      % (len(FREQS), TONE, len(SWEEP), len(data) / SR, AMP, 20 * np.log10(AMP)))

    # A) 系统回环
    mic = None
    for m in sc.all_microphones(include_loopback=True):
        if m.name == spk.name:
            mic = m
            break
    if mic is None:
        for m in sc.all_microphones(include_loopback=True):
            if "Realtek" in m.name:
                mic = m
                break
    if mic is None:
        w("找不到 %s 的回环设备" % spk.name)
    else:
        w("")
        w("A) 系统回环：%s  —— 播放中…" % mic.name)
        rec = record_while_playing(spk, mic, data, max(1, min(2, mic.channels)))
        if rec is not None:
            try:
                with wave.open(OUT_WAV, "wb") as f:
                    f.setnchannels(rec.shape[1] if rec.ndim > 1 else 1)
                    f.setsampwidth(2)
                    f.setframerate(SR)
                    f.writeframes((np.clip(rec, -1, 1) * 32767).astype("<i2").tobytes())
                w("  （已存原始录音 %s）" % OUT_WAV)
            except Exception as e:  # noqa
                w("  存 WAV 失败: %s" % e)
            analyze("A) 系统回环 loopback", rec, marks, data)

    # B) 立体声混音等普通采集端点
    w("")
    w("B) 普通采集端点：")
    try:
        caps = [m for m in sc.all_microphones(include_loopback=False)]
    except Exception as e:  # noqa
        caps = []
        w("  枚举失败 %s" % e)
    for m in caps:
        if any(k in m.name for k in ("立体声混音", "Stereo Mix", "线路输入", "Line In", "麦克风")):
            w("  测试：%s  —— 播放中…" % m.name)
            try:
                rec = record_while_playing(spk, m, data, max(1, min(2, m.channels)))
            except Exception as e:  # noqa
                w("   跳过（%s）" % e)
                continue
            analyze("B) %s" % m.name, rec, marks, data)

    w("")
    w("读数说明：0 dB = 与原始信号一致；低频档位为正数 = 系统在抬低频。")
    w("若 1kHz 档也明显非 0，说明整条链有增益/限幅，需按 1kHz 归一化后再看低频。")
    open(OUT_TXT, "w", encoding="utf-8").write("\n".join(buf))


if __name__ == "__main__":
    main()
