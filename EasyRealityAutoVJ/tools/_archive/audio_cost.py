# -*- coding: utf-8 -*-
r"""音频采集线程的 CPU 花在哪（微基准）。

现象：真实运行时 `autovj-audio` 线程占 0.78 个核。这个线程每秒做 47 次：
    rec.record(1024) → analyzer.feed() → _genre_buf.extend(mono.tolist())
其中 `tolist()` 会把 1024 个 numpy 标量变成 **Python 对象**（每秒 4.8 万个），
怀疑就是这里。本脚本分三段测，把「采集本身 / 列表转换 / 完整分析」拆开。

用法：venv\Scripts\python.exe tools\audio_cost.py [每段秒数]
输出：P:\AutoVJ\_audio_cost.txt
"""
import ctypes
import os
import sys
import threading
import time
from collections import deque
from ctypes import wintypes

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np  # noqa: E402

k32 = ctypes.windll.kernel32
k32.GetCurrentProcess.restype = wintypes.HANDLE
k32.OpenThread.restype = wintypes.HANDLE
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.GetThreadTimes.argtypes = [wintypes.HANDLE] + [ctypes.c_void_p] * 4
k32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
THREAD_QUERY_INFORMATION = 0x0040


class FILETIME(ctypes.Structure):
    _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]


def _ft(v):
    return (v.hi << 32) | v.lo


def thread_cpu_sec(tid):
    h = k32.OpenThread(THREAD_QUERY_INFORMATION, False, tid)
    if not h:
        return None
    try:
        c, e, k, u = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        if not k32.GetThreadTimes(h, ctypes.byref(c), ctypes.byref(e),
                                  ctypes.byref(k), ctypes.byref(u)):
            return None
        return (_ft(k) + _ft(u)) / 1e7
    finally:
        k32.CloseHandle(h)


out = []


def w(s=""):
    out.append(str(s))
    print(s, flush=True)


def main():
    per = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
    w("=" * 74)
    w("音频分析 CPU 微基准（喂真实音乐，不用设备采集）   %s" % time.strftime("%H:%M:%S"))
    w("=" * 74)
    from audio_engine import AudioState, _Analyzer, BLOCK

    # ⚠ 关键：必须喂**真实音乐**。静音输入会让 feed() 里大量分支走「无事发生」路径，
    # CPU 只有有信号时的几分之一（第一版就是拿设备底噪测的，结果全是 1~5%，完全失真）。
    flac = r"W:\临时结界相关\2of\tofu\Rolling Contact - Euclid Number.flac"
    if not os.path.exists(flac):
        w("测试音频不存在: %s" % flac)
        return
    import av
    c = av.open(flac)
    st_av = c.streams.audio[0]
    res = av.AudioResampler(format="fltp", layout="stereo", rate=48000)
    frames = []
    for fr in c.decode(st_av):
        for f in res.resample(fr):
            frames.append(f.to_ndarray().T.astype(np.float32))
        if len(frames) * 1024 > 48000 * (per + 1):
            break
    c.close()
    pcm = np.concatenate(frames, axis=0)          # (n, 2) float32
    i0 = 48000 * 30                               # 从 30s 处开始（有鼓点的段落）
    seg = pcm[i0:]
    nblk = len(seg) // BLOCK
    w("音频: %s  喂 %d 块（每块 %d 样本，约 %.1fs）"
      % (os.path.basename(flac), nblk, BLOCK, nblk * BLOCK / 48000))
    w("")
    tid = threading.main_thread().native_id

    def run(label, fn):
        st = AudioState()
        an = _Analyzer(48000)
        c0 = thread_cpu_sec(tid)
        t0 = time.perf_counter()
        for i in range(nblk):
            fn(an, st, seg[i * BLOCK:(i + 1) * BLOCK])
        el = time.perf_counter() - t0
        c1 = thread_cpu_sec(tid)
        cpu = (c1 - c0) if (c0 is not None and c1 is not None) else 0.0
        w("   %-30s CPU %.3fs / 墙钟 %.2fs = %.1f ms/块  ≈ %.0f%% 个核（实时 47 块/秒）"
          % (label, cpu, el, cpu * 1000 / max(nblk, 1), cpu / el * 100))
        return cpu / el

    buf = deque(maxlen=48000 * 8)

    a = run("① 只 analyzer.feed()", lambda an, st, d: an.feed(d, st))
    b = run("② feed + tolist()+extend", lambda an, st, d: (an.feed(d, st), buf.extend(d[:, 0].tolist())))

    ring = np.zeros(48000 * 8, dtype=np.float32)
    pos = [0]

    def ring_fn(an, st, d):
        an.feed(d, st)
        n = len(d)
        p = pos[0]
        if p + n <= len(ring):
            ring[p:p + n] = d[:, 0]
        else:
            k = len(ring) - p
            ring[p:] = d[:k, 0]
            ring[:n - k] = d[k:, 0]
        pos[0] = (p + n) % len(ring)
    e_ = run("③ feed + numpy 环形缓冲", ring_fn)

    w("")
    w("=" * 74)
    w('结论（折算成「实时运行时占几个核」）')
    w("=" * 74)
    w("  feed 本体          %.0f%% 个核" % (a * 100))
    w("  + tolist（现状）    %.0f%% 个核   ← tolist 这一步 = %.0f%%" % (b * 100, (b - a) * 100))
    w("  + numpy 环形（改法） %.0f%% 个核   ← 相比现状省 %.0f%%"
      % (e_ * 100, (b - e_) * 100))
    open(r"P:\AutoVJ\_audio_cost.txt", "w", encoding="utf-8").write("\n".join(out))


if __name__ == "__main__":
    main()
