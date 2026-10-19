# -*- coding: utf-8 -*-
r"""BPM 检测离线体检：把音频文件按真实块大小喂给 _Analyzer，逐秒看 BPM 是否建立。

用户报「BPM 不显示了，一直是 0」。用这个脚本区分两种情况：
  ① 离线喂文件也出不来 BPM  → 算法/代码回归（要修代码）
  ② 离线正常、现场为 0      → 输入侧问题（音源设备没声音 / 播放器独占 / 回环静音）

用法：
  venv\Scripts\python.exe tools\bpm_check.py "文件路径" [起始秒] [时长秒]
输出：P:\AutoVJ\_bpm_check.txt
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import numpy as np  # noqa: E402
import av  # noqa: E402

from audio_engine import AudioState, _Analyzer, BLOCK  # noqa: E402

out = []


def w(s=""):
    out.append(str(s))
    print(s, flush=True)


def decode(path, sr_want=44100):
    """解码成 float32 单声道（+ 原始声道数）"""
    c = av.open(path)
    st = c.streams.audio[0]
    res = av.AudioResampler(format="fltp", layout="stereo", rate=sr_want)
    chunks = []
    for frame in c.decode(st):
        for f in res.resample(frame):
            arr = f.to_ndarray()                      # (ch, n)
            chunks.append(arr.T.astype(np.float32))
    for f in res.resample(None):
        arr = f.to_ndarray()
        chunks.append(arr.T.astype(np.float32))
    c.close()
    if not chunks:
        return None, sr_want
    x = np.concatenate(chunks, axis=0)
    return x, sr_want


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else r"W:\临时结界相关\2of\tofu\Rolling Contact - Euclid Number.flac"
    t0s = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
    dur = float(sys.argv[3]) if len(sys.argv) > 3 else 40.0
    if not os.path.exists(path):
        w("文件不存在: %s" % path)
        open(r"P:\AutoVJ\_bpm_check.txt", "w", encoding="utf-8").write("\n".join(out))
        return

    w("=" * 78)
    w("BPM 检测离线体检")
    w("=" * 78)
    w("文件: %s" % path)
    w("区间: %.1fs ~ %.1fs   块大小 BLOCK=%d" % (t0s, t0s + dur, BLOCK))
    w("")

    x, sr = decode(path)
    if x is None:
        w("解码失败")
        return
    w("解码: %.1fs / %d Hz / shape=%s" % (len(x) / sr, sr, x.shape))
    seg = x[int(t0s * sr):int((t0s + dur) * sr)]

    an = _Analyzer(sr)
    st = AudioState()
    n = len(seg)
    pos = 0
    last_report = -1
    t_wall = time.perf_counter()
    onset_marks = 0
    prev_vote_n = 0
    while pos + BLOCK <= n:
        blk = seg[pos:pos + BLOCK]
        an.feed(blk, st)
        pos += BLOCK
        # 每次新增投票时记一条（诊断用：看票值是否分散）
        if len(an._bpm_votes) > prev_vote_n:
            prev_vote_n = len(an._bpm_votes)
        sec = pos / sr
        if int(sec) != last_report:
            last_report = int(sec)
            bpm = an.bpm_smooth
            # 复刻引擎的建锁判定，看是卡在哪一条
            votes = list(an._bpm_votes)
            mode_s = conf_s = "-"
            if len(votes) >= 6:
                v = np.array(votes, dtype=np.float32)
                hist, edges = np.histogram(v, bins=np.arange(60.0, 201.0, 1.0))
                pk = int(np.argmax(hist))
                mode_s = "%.1f" % (edges[pk] + 0.5)
                conf_s = "%.3f" % (float(hist[pk]) / len(v))
            w("  t=%5.1fs  BPM=%6.2f  投票=%2d  锁定=%-5s  拍钟:interval=%.3f count=%d  "
              "beat_active=%-5s  flux_ref=%.4f  众数=%-6s conf=%-6s  票值范围=%s"
              % (sec, bpm, len(an._bpm_votes), an._bpm_lock, an.beat_interval,
                 an.beat_count, st.beat_active, an.flux_ref, mode_s, conf_s,
                 ("%.1f~%.1f" % (min(votes), max(votes))) if votes else "-"))
    w("")
    w("用时 %.1fs（%.2f× 实时）" % (time.perf_counter() - t_wall, dur / max(1e-6, time.perf_counter() - t_wall)))
    w("")
    if an.bpm_display > 0:
        w("✅ 离线能给出 BPM = %.2f（显示值；拍钟锁定值 %.2f，锁定 %s）"
          % (an.bpm_display, an.bpm_smooth, an._bpm_lock))
        w("   ⇒ 算法本身正常。现场显示 0 时按这个顺序查：")
        w("     · 音源设备选错 / 没有声音（回环拿到的是静音）→ 跑 tools/bpm_live.py 看电平")
        w("     · 播放器走了独占模式（ASIO/WASAPI Exclusive），回环听不到")
        w("     · 系统音量或播放器音量被拉到极低")
        w("     · 播放器自带的「音频增强/音量均衡」关掉后音量明显变小？看 RMS")
    else:
        w("❌ 离线也显示不出 BPM（投票 %d）→ 算法/代码回归，需要修" % len(an._bpm_votes))
    open(r"P:\AutoVJ\_bpm_check.txt", "w", encoding="utf-8").write("\n".join(out))


if __name__ == "__main__":
    main()
