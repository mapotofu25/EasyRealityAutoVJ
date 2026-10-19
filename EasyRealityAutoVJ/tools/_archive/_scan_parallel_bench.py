# -*- coding: utf-8 -*-
"""并行扫描加速实测（**只读**用户曲库里的音频；指纹写临时库；不碰用户数据、不碰 GPU）。

目的：回答「改成并行到底快多少、占多少 CPU」。
做法：同一批音频，分别用 1 线程 / N 线程跑「解码 + 指纹」这两段本地计算，
      对比墙钟耗时，并采样本进程的 CPU 核当量。

用法：venv\\Scripts\\python.exe tools\\_scan_parallel_bench.py [每档样本数=10]
"""
import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np                                                     # noqa: E402
import soundfile as sf                                                 # noqa: E402
import fp                                                              # noqa: E402

N = 10
DIR = None
for _a in sys.argv[1:]:
    if _a.isdigit():
        N = int(_a)
    elif os.path.isdir(_a):
        DIR = _a
AUDIO_EXT = (".flac", ".wav", ".mp3", ".m4a", ".aiff", ".aif", ".ogg", ".opus")


def _cpu_self():
    k32 = ctypes.windll.kernel32
    k32.GetCurrentProcess.restype = wt.HANDLE
    h = ctypes.c_void_p(k32.GetCurrentProcess())

    class FT(ctypes.Structure):
        _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]
    c, e, k, u = FT(), FT(), FT(), FT()
    if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                               ctypes.byref(k), ctypes.byref(u)):
        return None
    return (((k.hi << 32) | k.lo) + ((u.hi << 32) | u.lo)) / 1e7


def _lower_self():
    """把整个测试进程降到「低于正常」优先级 —— 绝不去抢用户正在跑的那份软件的 CPU。"""
    try:
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = wt.HANDLE
        k32.SetPriorityClass(ctypes.c_void_p(k32.GetCurrentProcess()), 0x00004000)  # BELOW_NORMAL
    except Exception:
        pass


def main():
    _lower_self()
    if DIR:
        allp = []
        for root, dirs, files in os.walk(DIR):
            for f in files:
                if f.lower().endswith(AUDIO_EXT):
                    allp.append(os.path.join(root, f))
        allp.sort()
        if not allp:
            print("目录里没找到音频文件：%s" % DIR)
            return 0
        step = max(1, len(allp) // N)
        paths = allp[::step][:N]
        print("目录 %s：音频共 %d 个 → **跨专辑均匀抽样 %d 首**（每 %d 个取 1）"
              % (DIR, len(allp), len(paths), step))
    else:
        cfgp = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "config.json")
        try:
            cfg = json.load(open(cfgp, encoding="utf-8"))
        except Exception as e:                                         # noqa: BLE001
            print("读不到 config.json: %s" % e)
            return 1
        lib = cfg.get("music_library") or []
        paths = [p for p in lib if isinstance(p, str) and os.path.isfile(p)][:N]
        if not paths:
            print("曲库里没有可用音频（或 music_library 为空），跳过")
            return 0

    print("=" * 76)
    print("并行扫描加速实测   （样本 %d 首；**只读**你的音频，指纹写临时库）" % len(paths))
    print("=" * 76)
    print("CPU 核数: %s" % os.cpu_count())
    total_bytes = sum(os.path.getsize(p) for p in paths) / 1048576.0
    print("样本合计: %.1f MB" % total_bytes)

    def work(p, db, lock, out):
        t0 = time.perf_counter()
        data, sr = sf.read(p, dtype="float32", always_2d=True)
        sig = np.mean(data, axis=1)
        t1 = time.perf_counter()
        sid = int(hashlib.md5(p.encode("utf-8")).hexdigest()[:8], 16)
        with lock:
            db.add_song(sid, sig, sr)
        t2 = time.perf_counter()
        out.append((t1 - t0, t2 - t1))

    results = {}
    for n in (1, 4, 8):
        if n > (os.cpu_count() or 1):
            continue
        d = tempfile.mkdtemp(prefix="autovj_bench_")
        db = fp.FingerprintDB(os.path.join(d, "t.db"))
        lock = threading.Lock()
        out = []
        c0 = _cpu_self()
        t = time.perf_counter()
        if n == 1:
            for p in paths:
                work(p, db, lock, out)
        else:
            with ThreadPoolExecutor(max_workers=n) as ex:
                list(ex.map(lambda p: work(p, db, lock, out), paths))
        el = time.perf_counter() - t
        c1 = _cpu_self()
        db.close()
        cores = (c1 - c0) / el if (c0 is not None and c1 is not None and el > 0) else 0.0
        dec = sum(x[0] for x in out) / len(out)
        fpp = sum(x[1] for x in out) / len(out)
        results[n] = el
        print("  %d 线程：墙钟 %6.2f 秒（%.2f 秒/首）  解码 %.3fs + 指纹 %.3fs  "
              "占用 %.2f 核" % (n, el, el / len(out), dec, fpp, cores))

    if 1 in results:
        for n in (4, 8):
            if n in results:
                sp = results[1] / results[n]
                print("  → %d 线程相对 1 线程：**%.2f×** 加速"
                      "（%.1f 秒 → %.1f 秒）" % (n, sp, results[1], results[n]))
    print()
    print("说明：这是**纯本地**（解码+指纹）的加速比；联网曲风查询不受并行影响")
    print("      （它受 Discogs 未认证 25 次/分钟的限流约束，只能串行+节流）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
