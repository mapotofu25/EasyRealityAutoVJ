# -*- coding: utf-8 -*-
"""把「单首扫描」耗时**拆开**测：解码 / 指纹纯计算 / 写库 / 曲风，各自到底占多少。

背景：200 首实测墙钟 4.45 秒/首，但 CPU 只 0.8 核、磁盘吞吐只 8.5 MB/s
（HDD 顺序读可达 100+ MB/s）⇒ 两个都没饱和，怀疑卡在 SQLite 写入。

做法：**串行**（1 线程，排除并发干扰）跑 N 首，细粒度计时段：
  t_dec   sf.read 整曲解码          （HDD 读取 + 解码）
  t_fp    fingerprint_audio 纯计算  （CPU/内存，不含任何写库）
  t_del   DELETE WHERE song_id      （看索引是否真的生效）
  t_ins   INSERT 3~4.5 万行 + commit（SQLite 写入）
  t_meta  resolve_music_genre       （本地/在线曲风）
并每 25 首打印一次**分段均值**，看谁在随进度变慢。

指纹写**临时库**，不碰用户数据。

用法：venv\\Scripts\\python.exe tools\\_scan_split_bench.py [样本数=50] [目录]
"""
import ctypes
import ctypes.wintypes as wt
import hashlib
import os
import random
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import fp                                                               # noqa: E402
import genre_lookup as gl                                               # noqa: E402
from music_meta import resolve_music_genre                              # noqa: E402

AUDIO_EXT = (".flac", ".wav", ".mp3", ".m4a", ".aiff", ".aif", ".ogg", ".opus")

N, DIR = 50, None
_num_seen = False
for _a in sys.argv[1:]:
    if _a.isdigit() and not _num_seen:
        N = int(_a); _num_seen = True
    elif os.path.isdir(_a):
        DIR = _a

OUT = os.path.join(ROOT, "_scan_split_bench.txt")
_lines = []


def out(msg=""):
    print(msg, flush=True)
    _lines.append(msg)
    try:
        with open(OUT, "w", encoding="utf-8") as fh:
            fh.write("\n".join(_lines) + "\n")
    except Exception:
        pass


_k32 = ctypes.windll.kernel32
_k32.GetCurrentProcess.restype = wt.HANDLE
_H = ctypes.c_void_p(_k32.GetCurrentProcess())


class _FT(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]


def cpu_self():
    c, e, k, u = _FT(), _FT(), _FT(), _FT()
    _k32.GetProcessTimes(_H, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u))
    return (((k.hi << 32) | k.lo) + ((u.hi << 32) | u.lo)) / 1e7


def main():
    try:
        _k32.SetPriorityClass(_H, 0x00004000)
    except Exception:
        pass
    import numpy as np
    import soundfile as sf
    from fp import fingerprint_audio

    # 本次只为对比"写入模式"，把联网关掉（否则每首还要在曲风上花 4 秒，干扰判断）
    gl.set_online_enabled(False)

    out("=" * 78)
    out("单首扫描耗时**拆解**（串行 1 线程） %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    out("=" * 78)

    allp = []
    for root, dirs, files in os.walk(DIR):
        for f in files:
            if f.lower().endswith(AUDIO_EXT):
                allp.append(os.path.join(root, f))
    allp.sort()
    if not allp:
        out("没找到音频")
        return 1
    random.seed(7)
    paths = sorted(random.sample(allp, min(N, len(allp))))
    total_mb = sum(os.path.getsize(p) for p in paths) / 1048576.0
    out("库 %d 个；抽 %d 首；合计 %.1f MB（平均 %.1f MB/首）"
        % (len(allp), len(paths), total_mb, total_mb / len(paths)))

    tmp = tempfile.mkdtemp(prefix="autovj_split_")
    db = fp.FingerprintDB(os.path.join(tmp, "t.db"))
    BATCH = int(os.environ.get("AUTOVJ_SPLIT_BATCH", "6"))   # 0 = 逐首 commit（旧行为）
    if BATCH:
        db.begin_scan()
    out("指纹库已建（临时库，空库起步，带 idx_song 索引）")
    out("写入模式：%s" % ("批量（攒 %d 首 commit 一次，synchronous=OFF）" % BATCH
                          if BATCH else "逐首 commit（旧行为，用于对比）"))
    out("")
    out(" 进度 |   解码 |  指纹计算 |  DELETE |  INSERT |   曲风 |  合计/首 | 库行数")
    out("-" * 78)

    rows_total = 0
    acc = [0.0] * 5
    batch = [0.0] * 5
    c0, t0 = cpu_self(), time.perf_counter()
    for i, p in enumerate(paths):
        try:
            a = time.perf_counter()
            data, sr = sf.read(p, dtype="float32", always_2d=True)
            sig = np.mean(data, axis=1)
            del data
            b = time.perf_counter()

            hs = fingerprint_audio(sig, sr)          # 纯计算，不碰库
            c = time.perf_counter()

            sid = int(hashlib.md5(p.encode("utf-8")).hexdigest()[:8], 16)
            db.conn.execute("DELETE FROM fingerprints WHERE song_id=?", (sid,))
            d = time.perf_counter()
            if hs:
                db.conn.executemany("INSERT INTO fingerprints VALUES (?,?,?)",
                                    [(h, sid, off) for h, off in hs])
            if BATCH:
                db._pending += 1
                if db._pending >= BATCH:
                    db.conn.commit()
                    db._pending = 0
            else:
                db.conn.commit()
            e = time.perf_counter()

            resolve_music_genre(p)
            f = time.perf_counter()

            vals = (b - a, c - b, d - c, e - d, f - e)
            for k in range(5):
                acc[k] += vals[k]
                batch[k] += vals[k]
            rows_total += len(hs)
        except Exception:
            continue

        if (i + 1) % 25 == 0:
            n = 25.0
            el = time.perf_counter() - t0
            out(" %4d | %6.2fs | %8.2fs | %6.3fs | %6.3fs | %6.2fs | %7.2fs | %d"
                % (i + 1, batch[0] / n, batch[1] / n, batch[2] / n, batch[3] / n,
                   batch[4] / n, sum(batch) / n, rows_total))
            batch = [0.0] * 5

    el = time.perf_counter() - t0
    cores = (cpu_self() - c0) / el
    n = len(paths)
    out("-" * 78)
    out("总计 %d 首 / %.1f 秒 = %.2f 秒/首   （进程占用 %.2f 核）" % (n, el, el / n, cores))
    out("")
    out("单首平均拆分：")
    labels = ("解码（HDD 读 + 解码）", "指纹纯计算（CPU/内存）",
              "DELETE WHERE song_id", "INSERT + commit（SQLite 写）", "曲风解析")
    tot = sum(acc)
    for k in range(5):
        out("  %-26s %7.3f 秒  （占 %.1f%%）" % (labels[k], acc[k] / n, acc[k] / tot * 100))
    out("  %-26s %7.3f 秒" % ("合计", tot / n))
    out("")
    out("库规模：%d 行 / %.1f MB" % (rows_total, os.path.getsize(os.path.join(tmp, "t.db")) / 1048576))
    out("磁盘吞吐（音频字节/墙钟）：%.1f MB/s" % (total_mb / el))
    if BATCH:
        db.end_scan()
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
