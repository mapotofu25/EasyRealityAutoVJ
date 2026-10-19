# -*- coding: utf-8 -*-
"""**完整**扫描流程实测 v2 —— 对齐软件 `MusicScanThread`，并修掉 v1 的内存膨胀。

v1 的问题：进程涨到 **5.6 GB**。原因是整曲解码后的 float32 数组很大
（一首席 46 MB 的 FLAC → 原始 PCM ~92 MB → float32 **~184 MB**），
而 v1 用 `ex.map`（会缓冲结果）+ 没有主动释放、5 线程峰值叠加 ⇒ 内存失控。

v2 的改法：
  · 解码后**立刻 `del data`**（`sig` 只占一半），任务结束再 `del sig`
  · 用 `submit` + `as_completed`（不用会缓冲的 `map`）
  · 每 25 首打印**进程工作集**，把内存曲线暴露出来
  · **随机抽样**（不是等间隔），并输出抽样到的专辑分布
  · 仍然：指纹写**临时库**、不写 config、进程低优先级

用法：venv\\Scripts\\python.exe tools\\_scan_full_bench2.py [样本数=200] [目录] [并发=4]
"""
import ctypes
import ctypes.wintypes as wt
import gc
import hashlib
import os
import random
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np                                                      # noqa: E402
import soundfile as sf                                                  # noqa: E402
import fp                                                               # noqa: E402
import genre_lookup as gl                                               # noqa: E402
from music_meta import resolve_music_genre                              # noqa: E402
from genre_keywords import simplify_genres                              # noqa: E402

AUDIO_EXT = (".flac", ".wav", ".mp3", ".m4a", ".aiff", ".aif", ".ogg", ".opus")

N, DIR, NW = 200, None, 4
_pos = []
for _a in sys.argv[1:]:
    if _a.isdigit():
        if not _pos:
            N = int(_a); _pos.append(0)
        else:
            NW = int(_a)
    elif os.path.isdir(_a):
        DIR = _a

OUT = os.path.join(ROOT, "_scan_full_bench2.txt")
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


class _PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def ws_mb():
    m = _PMC(); m.cb = ctypes.sizeof(_PMC)
    try:
        ctypes.WinDLL("psapi").GetProcessMemoryInfo(_H, ctypes.byref(m), m.cb)
        return m.WorkingSetSize / 1048576.0, m.PeakWorkingSetSize / 1048576.0
    except Exception:
        return 0.0, 0.0


class _FT(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]


def cpu_self():
    c, e, k, u = _FT(), _FT(), _FT(), _FT()
    _k32.GetProcessTimes(_H, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u))
    return (((k.hi << 32) | k.lo) + ((u.hi << 32) | u.lo)) / 1e7


def lower_self():
    try:
        _k32.SetPriorityClass(_H, 0x00004000)   # BELOW_NORMAL
    except Exception:
        pass


def main():
    lower_self()
    t_all0 = time.perf_counter()
    out("=" * 78)
    out("完整扫描流程实测 v2（随机抽样） %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    out("=" * 78)

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

    random.seed(20260926)
    paths = random.sample(allp, min(N, len(allp)))
    paths.sort()
    total_mb = sum(os.path.getsize(p) for p in paths) / 1048576.0
    albums = {os.path.basename(os.path.dirname(p)) for p in paths}
    out("音频库共 %d 个；**随机抽样 %d 首**，覆盖 **%d 张专辑**" % (len(allp), len(paths), len(albums)))
    out("样本合计 %.1f MB（平均 %.1f MB/首）；CPU %d 核；并发 %d；低优先级；单首峰值约 %.0f MB"
        % (total_mb, total_mb / len(paths), os.cpu_count(), NW, total_mb / len(paths) * 4))
    w0, _ = ws_mb()
    out("起始工作集: %.0f MB" % w0)

    tmp = tempfile.mkdtemp(prefix="autovj_fullscan2_")
    db = fp.FingerprintDB(os.path.join(tmp, "t.db"))
    db_lock = threading.Lock()

    gc_ = None
    try:
        from audio_genre import get_genre_classifier
        t = time.perf_counter()
        gc_ = get_genre_classifier()
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
                votes.extend(gc_.classify(s, sr)["tags"])
            except Exception:
                pass
        return [t for t, _ in Counter(votes).most_common(4)]

    stats = []
    srcs = {}

    def one(p):
        """完整单首：解码 → 指纹入库 → 曲风(含在线) → 本地 AI 合并。**主动释放大数组**。"""
        data = sig = None
        try:
            t0 = time.perf_counter()
            data, sr = sf.read(p, dtype="float32", always_2d=True)
            sig = np.mean(data, axis=1)
            del data                                  # ★ 关键：立刻释放整曲数组（~184MB/首）
            t1 = time.perf_counter()
            sid = int(hashlib.md5(p.encode("utf-8")).hexdigest()[:8], 16)
            with db_lock:
                db.add_song(sid, sig, sr)
            t2 = time.perf_counter()
            r = resolve_music_genre(p)
            t3 = time.perf_counter()
            ai = 0.0
            if gc_ is not None and r.get("level") != "track":
                ta = time.perf_counter()
                ai_zh = simplify_genres(local_ai(sig, sr))
                ai = time.perf_counter() - ta
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
            return ((t1 - t0), (t2 - t1), (t3 - t2), ai, (t4 - t3 - ai), r.get("source") or "none")
        except Exception:
            return 0.0, 0.0, 0.0, 0.0, 0.0, "err"
        finally:
            del sig                                   # ★ 任务结束再释放 sig

    # ============ 阶段 1：并行本地（关联网）============
    gl.set_online_enabled(False)
    out("")
    out("--- 阶段 1：并行本地（联网关闭）---")
    c0, t1a = cpu_self(), time.perf_counter()
    done = 0
    with ThreadPoolExecutor(max_workers=NW) as ex:
        futs = [ex.submit(one, p) for p in paths]
        for f in as_completed(futs):
            st = f.result()
            stats.append(st)
            srcs[st[5]] = srcs.get(st[5], 0) + 1
            done += 1
            if done % 25 == 0:
                el = time.perf_counter() - t1a
                w, wp = ws_mb()
                out("  %3d/%d  用时 %6.1fs（%.2f 秒/首，%.1f 首/分）  工作集 %.0f MB（峰值 %.0f）  累计 %.1f 核"
                    % (done, len(paths), el, el / done, done / el * 60, w, wp, (cpu_self() - c0) / el))
                gc.collect()
    t1 = time.perf_counter() - t1a
    w1, wp1 = ws_mb()
    n = max(1, len(stats))
    dec = sum(s[0] for s in stats) / n
    fpp = sum(s[1] for s in stats) / n
    met = sum(s[2] for s in stats) / n
    ai_t = sum(s[3] for s in stats) / n
    mg = sum(s[4] for s in stats) / n
    out("  阶段1 完成：%d 首 / %.1f 秒 = **%.2f 秒/首**（%.1f 首/分钟）"
        % (len(paths), t1, t1 / n, len(paths) / t1 * 60))
    out("  单首平均：解码 %.2fs + 指纹 %.2fs + 曲风(本地) %.2fs + 本地AI %.2fs + 合并 %.2fs = %.2fs"
        % (dec, fpp, met, ai_t, mg, dec + fpp + met + ai_t + mg))
    out("  内存：起始 %.0f MB → 结束 %.0f MB（峰值 %.0f MB）%s"
        % (w0, w1, wp1, "  ** 有累积，需继续查 **" if w1 - w0 > 800 else "  ✅ 平稳"))

    # ============ 阶段 2：串行在线 ============
    out("")
    out("--- 阶段 2：串行在线查询（Discogs/iTunes）---")
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
            out("  已尝试 %d/%d  用时 %.1fs（%.2f 秒/首）" % (i + 1, len(paths), el, el / (i + 1)))
    t2 = time.perf_counter() - t2a
    out("  阶段2：尝试 %d 首 / %.1f 秒%s"
        % (n_probe, t2, "（**被限流，提前结束**，第 %d 首触发）" % blocked_at
           if blocked_at is not None else ""))
    gl.set_online_enabled(True)

    tot = time.perf_counter() - t_all0
    per = t1 / n
    out("")
    out("=" * 78)
    out("汇总（%d 首随机抽样，%d 线程）" % (len(paths), NW))
    out("=" * 78)
    out("  阶段1（并行本地）: %7.1f 秒 = %.2f 秒/首 = %.1f 首/分钟" % (t1, per, 60 / per))
    out("  阶段2（串行在线）: %7.1f 秒（%d 首）" % (t2, n_probe))
    out("  本进程合计      : %7.1f 秒" % tot)
    out("")
    out("  推算（仅本地阶段）:")
    for ns, lb in ((200, "200 首"), (800, "800 首"), (len(allp), "整库 %d 首" % len(allp))):
        out("    %-16s ≈ %6.1f 分钟" % (lb, per * ns / 60))
    out("  曲风来源: %s" % srcs)
    out("  限流: %s" % ("已触发" if gl.online_blocked() else "未触发"))
    wf, wpf = ws_mb()
    out("  最终工作集 %.0f MB（峰值 %.0f MB）" % (wf, wpf))
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
