# -*- coding: utf-8 -*-
"""**完整**扫描流程实测 —— 对齐软件里的 `ui_main.MusicScanThread`，只做安全隔离。

复刻的步骤（与软件一致）：
  1) sf.read 整曲解码
  2) 指纹计算 + 入库（add_song）
  3) resolve_music_genre：关键词细分 → ID3 genre → 在线(Discogs/iTunes) → 本地 AI
  4) 本地 AI 曲风（EffNet 三段投票）并与在线结果合并
  5) 阶段 1 并行（关联网）/ 阶段 2 串行（开联网，尊重限流）

与软件**唯一的差异**（为了不破坏你的数据）：
  · 指纹写**临时库**，绝不碰 %LOCALAPPDATA%\\AutoVJ\\fingerprints.db
  · 结果**不写入 config.json**（只统计耗时）
  · 测试进程自带「低于正常」优先级，不抢你正在跑的那份软件

用法：
  venv\\Scripts\\python.exe tools\\_scan_full_bench.py [样本数=200] [音乐目录] [并发数]
"""
import ctypes
import ctypes.wintypes as wt
import hashlib
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np                                                      # noqa: E402
import soundfile as sf                                                  # noqa: E402
import fp                                                               # noqa: E402
import genre_lookup as gl                                               # noqa: E402
from music_meta import resolve_music_genre                              # noqa: E402
from genre_keywords import simplify_genres                              # noqa: E402

AUDIO_EXT = (".flac", ".wav", ".mp3", ".m4a", ".aiff", ".aif", ".ogg", ".opus")

N = 200
DIR = None
WORKERS = None
for _a in sys.argv[1:]:
    if _a.isdigit():
        if N == 200 and WORKERS is None:
            N = int(_a)
        else:
            WORKERS = int(_a)
    elif os.path.isdir(_a):
        DIR = _a

OUT = os.path.join(ROOT, "_scan_full_bench.txt")
_lines = []


def out(msg=""):
    print(msg)
    _lines.append(msg)
    try:
        with open(OUT, "w", encoding="utf-8") as fh:
            fh.write("\n".join(_lines) + "\n")
    except Exception:
        pass


def _lower_self():
    try:
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = wt.HANDLE
        k32.SetPriorityClass(ctypes.c_void_p(k32.GetCurrentProcess()), 0x00004000)  # BELOW_NORMAL
    except Exception:
        pass


def main():
    _lower_self()
    t_all0 = time.perf_counter()
    out("=" * 78)
    out("完整扫描流程实测（对齐软件 MusicScanThread） %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    out("=" * 78)

    # ---- 抽样：跨专辑均匀 ----
    if DIR:
        allp = []
        for root, dirs, files in os.walk(DIR):
            for f in files:
                if f.lower().endswith(AUDIO_EXT):
                    allp.append(os.path.join(root, f))
        allp.sort()
    else:
        import json
        cfg = json.load(open(os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ", "config.json"),
                             encoding="utf-8"))
        allp = sorted(p for p in (cfg.get("music_library") or [])
                      if isinstance(p, str) and os.path.isfile(p))
    if not allp:
        out("没找到音频文件")
        return 1
    step = max(1, len(allp) // N)
    paths = allp[::step][:N]
    nw = WORKERS or max(2, min(6, (os.cpu_count() or 4) // 3))
    total_mb = sum(os.path.getsize(p) for p in paths) / 1048576.0
    out("音频库共 %d 个文件；抽样 **%d 首**（每 %d 个取 1，跨专辑）" % (len(allp), len(paths), step))
    out("样本合计 %.1f MB（平均 %.1f MB/首）；CPU %d 核；并发 %d 线程；测试进程=低优先级"
        % (total_mb, total_mb / len(paths), os.cpu_count(), nw))

    # ---- 指纹库（临时，绝不碰用户数据）----
    tmp = tempfile.mkdtemp(prefix="autovj_fullscan_")
    db = fp.FingerprintDB(os.path.join(tmp, "t.db"))
    db_lock = threading.Lock()

    gc = None
    try:
        from audio_genre import get_genre_classifier
        t = time.perf_counter()
        gc = get_genre_classifier()
        out("本地 AI 模型加载: %.2fs" % (time.perf_counter() - t))
    except Exception as e:                                              # noqa: BLE001
        out("本地 AI 不可用: %s" % e)

    def local_ai(sig, sr):
        from collections import Counter
        seg = int(sr * 6)
        pts = [sig] if len(sig) < seg else [sig[int(sr * f):int(sr * f) + seg]
                                            for f in (0.25, 0.5, 0.75)]
        votes = []
        for s in pts:
            try:
                votes.extend(gc.classify(s, sr)["tags"])
            except Exception:
                pass
        return [t for t, _ in Counter(votes).most_common(4)]

    stats = []          # (解码, 指纹, 曲风含联网, 本地AI)
    net_hits = {"id3": 0, "discogs": 0, "itunes": 0, "localai": 0, "keyword": 0, "none": 0}

    def one(p):
        """与软件 run() 里的 _one 一致（联网开关由阶段控制）。"""
        try:
            t0 = time.perf_counter()
            data, sr = sf.read(p, dtype="float32", always_2d=True)
            sig = np.mean(data, axis=1)
            t1 = time.perf_counter()
            sid = int(hashlib.md5(p.encode("utf-8")).hexdigest()[:8], 16)
            with db_lock:
                db.add_song(sid, sig, sr)
            t2 = time.perf_counter()
            r = resolve_music_genre(p)
            t3 = time.perf_counter()
            t_ai0 = time.perf_counter()
            if gc is not None and r.get("level") != "track":
                ai_zh = simplify_genres(local_ai(sig, sr))
                on = list(r.get("genres") or [])
                if ai_zh:
                    seen, merged = set(), []
                    order = on[:3] + ai_zh[:2] if r.get("level") == "track" else ai_zh[:3] + on[:2]
                    for z in order:
                        if z and z not in seen:
                            seen.add(z); merged.append(z)
                    r["genres"] = merged
                    if not on and ai_zh:
                        r["source"] = "localai"
            t4 = time.perf_counter()
            return (t1 - t0), (t2 - t1), (t3 - t2), (t4 - t_ai0), r.get("source") or "none"
        except Exception:
            return 0.0, 0.0, 0.0, 0.0, "err"

    # ================= 阶段 1：并行（关联网）=================
    gl.set_online_enabled(False)
    out("")
    out("--- 阶段 1：并行本地（联网已关闭，与软件一致）---")
    t1a = time.perf_counter()
    with ThreadPoolExecutor(max_workers=nw) as ex:
        for i, st in enumerate(ex.map(one, paths)):
            stats.append(st)
            net_hits[st[4] if st[4] in net_hits else "none"] = \
                net_hits.get(st[4] if st[4] in net_hits else "none", 0) + 1
            if (i + 1) % 25 == 0:
                el = time.perf_counter() - t1a
                out("  已扫 %d/%d  用时 %.1fs（%.2f 秒/首，%.1f 首/秒）"
                    % (i + 1, len(paths), el, el / (i + 1), (i + 1) / el))
    t1 = time.perf_counter() - t1a
    dec = sum(s[0] for s in stats) / len(stats)
    fpp = sum(s[1] for s in stats) / len(stats)
    met = sum(s[2] for s in stats) / len(stats)
    ai = sum(s[3] for s in stats) / len(stats)
    out("  阶段1 完成：%d 首 / %.1f 秒 = **%.2f 秒/首**（%.1f 首/分钟）"
        % (len(paths), t1, t1 / len(paths), len(paths) / t1 * 60))
    out("  单首平均：解码 %.2fs + 指纹 %.2fs + 曲风(本地) %.2fs + 本地AI %.2fs = %.2fs"
        % (dec, fpp, met, ai, dec + fpp + met + ai))

    # ================= 阶段 2：串行（开联网）=================
    out("")
    out("--- 阶段 2：串行在线查询（Discogs/iTunes，尊重限流）---")
    gl.set_online_enabled(True)
    t2a = time.perf_counter()
    n_probe, blocked_at = 0, None
    for i, p in enumerate(paths):
        if gl.online_blocked():
            blocked_at = i
            break
        try:
            resolve_music_genre(p)
        except Exception:
            pass
        n_probe += 1
        if (i + 1) % 10 == 0:
            el = time.perf_counter() - t2a
            out("  已尝试 %d/%d  用时 %.1fs（%.2f 秒/首）"
                % (i + 1, len(paths), el, el / (i + 1)))
    t2 = time.perf_counter() - t2a
    out("  阶段2 结束：尝试 %d 首 / %.1f 秒%s"
        % (n_probe, t2, "（**已被限流，提前结束**，第 %d 首触发）" % blocked_at
           if blocked_at is not None else ""))
    gl.set_online_enabled(True)

    # ================= 汇总 =================
    tot = time.perf_counter() - t_all0
    out("")
    out("=" * 78)
    out("汇总（%d 首，%d 线程，%.1f MB）" % (len(paths), nw, total_mb))
    out("=" * 78)
    out("  阶段1（并行本地）      : %7.1f 秒  = %.2f 秒/首  = %.1f 首/分钟"
        % (t1, t1 / len(paths), len(paths) / t1 * 60))
    out("  阶段2（串行在线）      : %7.1f 秒  （%d 首）" % (t2, n_probe))
    out("  合计（含模型加载等）    : %7.1f 秒" % tot)
    out("")
    out("  按此推算：")
    for n_song, label in ((200, "200 首"), (800, "800 首"),
                          (len(allp), "整库 %d 首" % len(allp))):
        est = t1 / len(paths) * n_song
        out("    %-14s 仅本地 ≈ %5.1f 分钟；含在线 ≈ %5.1f 分钟"
            % (label, est / 60, (est + t2 / max(1, n_probe) * n_song) / 60))
    out("")
    out("  曲风来源分布: %s" % {k: v for k, v in net_hits.items() if v})
    out("  限流状态: %s" % ("已触发（后续走本地 AI）" if gl.online_blocked() else "未触发"))
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
