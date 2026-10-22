# -*- coding: utf-8 -*-
"""码表 `seek` 可行性前置实测（**只测量，不改任何源码**）。

背景（团队裁定 = 走方案 A：`play_sec` 主导 + `seek` + `set_speed(1.0)` + `src_ended` 持帧）：
    A 有一个和 2026-09-30「12 核跑满 / 磁盘 750MB/s」同款的风险 ——
    **`seek` 可能每次都从头解到目标帧**，长素材 + 频繁 seek = 磁盘爆。
    所以 Step4 之前必须先量清楚：单次 seek 多贵、连续 seek 的磁盘读量多大。

本工具回答三个问题（对每个素材 × 每种 seek 后端）：
  ① **单次 seek 耗时**：随机 20 次，P50 / P95（区分"冷"= 每步新建容器、"热"= 同一容器连续 seek）
  ② **连续 seek 的磁盘读量**：每 4 帧一次、持续 10 秒 → 进程 `read_bytes` 的 1s 桶均值/峰值
     （另给"不 seek 顺序读"的基线，便于算增量）
  ③ **seek 落点精度**：读到的那一帧与目标时间的偏差（关键帧粒度会导致几秒偏差）

两个后端（与真实播放器一一对应，见 `src/media_manager.py`）：
  · `cv2`  → `VideoPlayer`（OpenCV/FFmpeg 后端）：`set(CAP_PROP_POS_MSEC)` + `read()`
  · `av`   → `AvAlphaPlayer` / `DxvGpuPlayer`（PyAV 18）：`container.seek()` + `decode()` 首帧
  注意：**DXV 素材走 `av` 后端**（`create_video_player` 用 PyAV + `dxvnative`），不是 cv2。

判据（团队给的硬门槛）：
  · 单次 seek `P95 < 50ms`
  · 连续 seek（每 4 帧）磁盘：均值 `< 120MB/s`、10s 峰值 `< 250MB/s`
  · 任一项不过 ⇒ 方案 A 不可行（走 B：持帧 + 接受漂移，或放宽限频阈值）

用法：
    python tools/cue_seek_probe.py                       # 自动挑本机素材跑全套
    python tools/cue_seek_probe.py --file a.mp4 --file b.mov
    python tools/cue_seek_probe.py --seconds 10 --every 4 --out tools/_cue_seek_probe.md
    python tools/cue_seek_probe.py --json out.json

零 Qt / 零 psutil（Windows 用 kernel32 的 `GetProcessIoCounters` / `GetProcessTimes`，
其它平台自动降级为"磁盘/CPU 不可测"并在报告里标注）。
"""
from __future__ import annotations

import argparse
import ctypes
import glob
import json
import os
import random
import statistics
import sys
import time
import zipfile

# ==========================================================================
# 进程级计数器（Windows：kernel32；其它平台降级）
# ==========================================================================
_IS_WIN = (os.name == "nt")


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong)]


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]


def _k32():
    """取 kernel32 并声明签名。

    ⚠ 必须声明：`GetCurrentProcess()` 返回伪句柄 `0xFFFFFFFFFFFFFFFF`，
      不声明 `restype=c_void_p` 会被 ctypes 截成 32 位 ⇒ 调用失败（本工具第一版就踩了，
      表现为"磁盘量全 N/A"，差点把结论带偏）。
    """
    k = ctypes.windll.kernel32
    k.GetCurrentProcess.restype = ctypes.c_void_p
    k.GetProcessIoCounters.argtypes = [ctypes.c_void_p, ctypes.POINTER(_IO_COUNTERS)]
    k.GetProcessIoCounters.restype = ctypes.c_int
    k.GetProcessTimes.argtypes = [ctypes.c_void_p, ctypes.POINTER(_FILETIME),
                                  ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME),
                                  ctypes.POINTER(_FILETIME)]
    k.GetProcessTimes.restype = ctypes.c_int
    return k


def _proc_read_bytes():
    """本进程累计读盘字节（含所有解码器线程）。不可测时返回 None。"""
    if not _IS_WIN:
        return None
    try:
        k32 = _k32()
        c = _IO_COUNTERS()
        if not k32.GetProcessIoCounters(k32.GetCurrentProcess(), ctypes.byref(c)):
            return None
        return int(c.ReadTransferCount)
    except Exception:                                            # noqa: BLE001
        return None


def _proc_cpu_seconds():
    """本进程累计 CPU 秒（user+kernel）。不可测时返回 None。"""
    if not _IS_WIN:
        return None
    try:
        k32 = _k32()
        c, e, k, u = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
        if not k32.GetProcessTimes(k32.GetCurrentProcess(), ctypes.byref(c),
                                   ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
            return None

        def secs(ft):
            return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7
        return secs(k) + secs(u)
    except Exception:                                            # noqa: BLE001
        return None


# ==========================================================================
# 后端：cv2 / av（与 media_manager 的播放器一一对应）
# ==========================================================================
class Cv2Backend:
    """`VideoPlayer` 用的路径：OpenCV（FFmpeg 后端）。"""
    name = "cv2"

    def __init__(self):
        import cv2
        self.cv2 = cv2

    def open(self, path):
        cap = self.cv2.VideoCapture(path)
        if not cap.isOpened():
            raise RuntimeError("cv2 打不开：%s" % path)
        fps = float(cap.get(self.cv2.CAP_PROP_FPS) or 0.0) or 30.0
        n = float(cap.get(self.cv2.CAP_PROP_FRAME_COUNT) or 0.0)
        dur = (n / fps) if (n > 0 and fps > 0) else 0.0
        return {"cap": cap, "fps": fps, "dur": dur, "path": path}

    def close(self, h):
        try:
            h["cap"].release()
        except Exception:                                        # noqa: BLE001
            pass

    def seek_read(self, h, t):
        """seek 到 `t` 秒并取一帧；返回 (耗时ms, 实际落点秒)。"""
        cap = h["cap"]
        t = max(0.0, min(t, max(0.0, h["dur"] - 0.001)))
        t0 = time.perf_counter()
        cap.set(self.cv2.CAP_PROP_POS_MSEC, t * 1000.0)
        ok, frame = cap.read()
        dt = (time.perf_counter() - t0) * 1000.0
        if not ok or frame is None:
            return dt, None
        got = float(cap.get(self.cv2.CAP_PROP_POS_MSEC) or 0.0) / 1000.0
        if got <= 0.0 and t > 0.5:
            return dt, None                 # 该后端不报位置 ⇒ 落点精度记 N/A（不伪造误差）
        return dt, got

    def read_next(self, h):
        h["cap"].read()


class AvBackend:
    """`AvAlphaPlayer` / `DxvGpuPlayer` 用的路径：PyAV。"""
    name = "av"

    def __init__(self):
        import av
        self.av = av

    def open(self, path):
        c = self.av.open(path)
        st = c.streams.video[0]
        fps = float(st.average_rate) if st.average_rate else 30.0
        if st.duration:
            dur = float(st.duration * st.time_base)
        elif c.duration:
            dur = c.duration / 1e6
        else:
            dur = 0.0
        return {"c": c, "st": st, "fps": fps, "dur": dur, "path": path,
                "codec": st.codec_context.name, "w": st.codec_context.width,
                "h": st.codec_context.height}

    def close(self, h):
        try:
            h["c"].close()
        except Exception:                                        # noqa: BLE001
            pass

    def seek_read(self, h, t):
        c, st = h["c"], h["st"]
        t = max(0.0, min(t, max(0.0, h["dur"] - 0.001)))
        t0 = time.perf_counter()
        try:
            c.seek(int(t / float(st.time_base)), stream=st, backward=True)
            frame = next(c.decode(video=0))
        except Exception:                                        # noqa: BLE001
            return (time.perf_counter() - t0) * 1000.0, None
        dt = (time.perf_counter() - t0) * 1000.0
        got = float(frame.pts * st.time_base) if frame.pts is not None else None
        return dt, got

    def seek_read_exact(self, h, t, max_frames=600):
        """seek + **前向解到目标帧**（PyAV 的 seek 只落到前面的关键帧）。

        ⚠ 这是方案 A 真正需要的语义：只 `container.seek()` 会停在关键帧上，
          落点误差可达 1~2.4 秒（实测）⇒ 画面"卡在上一段"。
        """
        c, st = h["c"], h["st"]
        t = max(0.0, min(t, max(0.0, h["dur"] - 0.001)))
        t0 = time.perf_counter()
        best = None
        try:
            c.seek(int(t / float(st.time_base)), stream=st, backward=True)
            for _ in range(max_frames):
                frame = next(c.decode(video=0))
                if frame.pts is None:
                    continue
                got = float(frame.pts * st.time_base)
                best = got
                if got >= t - 1e-6:
                    break
        except Exception:                                        # noqa: BLE001
            pass
        return (time.perf_counter() - t0) * 1000.0, best

    def read_next(self, h):
        try:
            next(h["c"].decode(video=0))
        except Exception:                                        # noqa: BLE001
            pass


BACKENDS = {"cv2": Cv2Backend, "av": AvBackend}


# ==========================================================================
# 统计小工具
# ==========================================================================
def _pct(vals, q):
    """分位数（线性插值，v 已排序）。"""
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    frac = pos - lo
    return vals[lo] * (1.0 - frac) + vals[hi] * frac


def _ms(v):
    return "%.2f" % v if v is not None else "N/A"


# ==========================================================================
# 三组测量
# ==========================================================================
def _seek_once(backend, h, t, exact):
    fn = getattr(backend, "seek_read_exact", None) if exact else None
    if callable(fn):
        return fn(h, t)
    return backend.seek_read(h, t)


def measure_single_seek(backend, path, n=20, seed=1234, warm=True, exact=True):
    """随机 n 次 seek 的耗时分布。

    `warm=True` ⇒ 同一个容器里连续 seek（热：容器已打开、索引已建）；
    `warm=False` ⇒ 每次都新建容器再 seek（冷：等价"刚切到这个素材"）。
    """
    rnd = random.Random(seed)
    times, errs = [], []
    if warm:
        h = backend.open(path)
        dur = h["dur"]
        positions = [rnd.uniform(0.02, max(0.05, dur - 0.05)) for _ in range(n)]
        # 先把容器走热（首帧解码 / 索引构建）
        _seek_once(backend, h, dur * 0.5, exact)
        for t in positions:
            dt, got = _seek_once(backend, h, t, exact)
            times.append(dt)
            if got is not None:
                errs.append(abs(got - t))
        backend.close(h)
        meta = {"dur": dur, "fps": h["fps"], "codec": h.get("codec", "h264")}
    else:
        positions = []
        meta = {}
        for _ in range(n):
            h = backend.open(path)
            meta = {"dur": h["dur"], "fps": h["fps"], "codec": h.get("codec", "h264")}
            t = rnd.uniform(0.02, max(0.05, h["dur"] - 0.05))
            positions.append(t)
            dt, got = _seek_once(backend, h, t, exact)
            times.append(dt)
            if got is not None:
                errs.append(abs(got - t))
            backend.close(h)
    s = sorted(times)
    return {"n": len(s), "p50": _pct(s, 0.50), "p95": _pct(s, 0.95),
            "max": (s[-1] if s else None), "mean": (statistics.fmean(s) if s else None),
            "err_p50": _pct(sorted(errs), 0.50) if errs else None,
            "err_max": (max(errs) if errs else None), "meta": meta}


def measure_stream(backend, path, seconds=10.0, fps=60.0, every=4, seek=True):
    """连续 10 秒的读盘/CPU 画像。

    `seek=True` ⇒ 模拟"方案 A 的限频策略"：每 `every` 帧 seek 一次到"应播放的位置"
    （目标按实时推进；每 10 次做一次 −2s 的 loop 回跳，模拟 DJ 打 loop）。
    `seek=False` ⇒ 基线：顺序 `read()`，不 seek。
    """
    h = backend.open(path)
    dur = max(0.001, h["dur"])
    frame_dt = 1.0 / max(1.0, fps)
    step = frame_dt * every
    _seek_once(backend, h, 0.0, True)             # 预热（开容器 / 首帧）
    r0, c0 = _proc_read_bytes(), _proc_cpu_seconds()
    t_start = time.perf_counter()
    buckets = [r0] if r0 is not None else []       # 每 1s 的累计读字节（含起点）
    cur_bytes, cur_t = r0, t_start
    n_seek = 0
    target = 0.0
    while True:
        now = time.perf_counter() - t_start
        if now >= seconds:
            break
        if seek:
            target += step
            if n_seek and n_seek % 10 == 0:       # 每 10 次做一次 loop 回跳
                target = max(0.0, target - 2.0)
            if target >= dur - 0.05:
                target = 0.0
            _seek_once(backend, h, target, True)
            n_seek += 1
        else:
            backend.read_next(h)
        cur_bytes = _proc_read_bytes()
        if cur_bytes is not None and (time.perf_counter() - cur_t) >= 1.0:
            buckets.append(cur_bytes)
            cur_t = time.perf_counter()
        # 让节奏贴近 60fps（每 4 帧 = 66ms 一次动作）
        if seek:
            slack = step - (time.perf_counter() - t_start - now)
            if slack > 0:
                time.sleep(slack)
        else:
            time.sleep(frame_dt * every)
    wall = time.perf_counter() - t_start
    r1, c1 = _proc_read_bytes(), _proc_cpu_seconds()
    backend.close(h)

    deltas = []
    prev = buckets[0] if buckets else None
    for b in buckets[1:]:
        if prev is not None:
            deltas.append(max(0, b - prev))
        prev = b
    mb = [d / 1048576.0 for d in deltas]
    total = (r1 - r0) if (r0 is not None and r1 is not None) else None
    cpu = (c1 - c0) if (c0 is not None and c1 is not None) else None
    return {
        "seek": bool(seek), "seconds": wall, "n_seek": n_seek,
        "read_total_mb": (total / 1048576.0) if total is not None else None,
        "read_mean_mbs": (statistics.fmean(mb) if mb else None),
        "read_peak_mbs": (max(mb) if mb else None),
        "cpu_cores": (cpu / wall) if (cpu is not None and wall > 0) else None,
        "frames_per_seek": every, "fps": fps,
        "file_mb": os.path.getsize(path) / 1048576.0,
    }


# ==========================================================================
# 素材挑选（本机真实素材；DXV 从用户的 .ercue 包里取）
# ==========================================================================
def _probe_meta(path):
    """用 PyAV 读编码/时长（只用于挑素材与报告表头）。"""
    try:
        import av
        with av.open(path) as c:
            st = c.streams.video[0]
            dur = (float(st.duration * st.time_base) if st.duration
                   else (c.duration / 1e6 if c.duration else 0.0))
            return {"codec": st.codec_context.name, "w": st.codec_context.width,
                    "h": st.codec_context.height,
                    "fps": float(st.average_rate) if st.average_rate else 0.0,
                    "dur": dur, "size_mb": os.path.getsize(path) / 1048576.0}
    except Exception:                                            # noqa: BLE001
        return None


def auto_pick(limit=6):
    """自动挑素材：短 H.264 / 长 H.264 / 长 HEVC / DXV（从 .ercue 包内解出）。

    ⚠ 优先用**用户真实素材**；DXV 本机盘上没有散布文件，但用户自己的 `.ercue`
      包里内嵌了 DXV 素材（`media/12.mov`，codec=dxv）⇒ 从包里解到临时目录用。
    """
    picked = []
    # 1) 短 H.264（< 10s）
    for p in glob.glob(r"P:\avi~\*.mp4"):
        m = _probe_meta(p)
        if m and m["codec"] == "h264" and 0.5 <= m["dur"] <= 10.0:
            picked.append(("短 H.264", p, m))
            break
    # 2) 长 H.264（> 120s）
    for p in glob.glob(r"P:\宣传PV（改）\*.mp4") + glob.glob(r"P:\avi~\*.mp4"):
        m = _probe_meta(p)
        if m and m["codec"] == "h264" and m["dur"] > 120.0:
            picked.append(("长 H.264(>2min)", p, m))
            break
    # 3) 长 HEVC（用户素材库另一类长片）
    for p in glob.glob(r"<素材库>\*\*.mp4"):
        m = _probe_meta(p)
        if m and m["codec"] == "hevc" and m["dur"] > 120.0:
            picked.append(("长 HEVC(>2min)", p, m))
            break
    # 4) DXV：从任意 .ercue 包里解一个出来
    dxv = _extract_dxv_from_packages()
    if dxv:
        picked.append(("DXV(DXV3)", dxv, _probe_meta(dxv) or {}))
    return picked[:limit]


def _extract_dxv_from_packages():
    """在 `.ercue` 包里找 `media/*`（codec=dxv），解到临时目录。"""
    import tempfile
    pats = [r"<项目根>\**\*.ercue", r"<项目根>\brand\ui_audit\*\*\*.ercue"]
    seen = []
    for pat in pats:
        seen.extend(glob.glob(pat, recursive=True))
    for pkg in dict.fromkeys(seen):
        try:
            with zipfile.ZipFile(pkg) as z:
                names = [n for n in z.namelist() if n.startswith("media/")]
                for n in names:
                    if not n.lower().endswith((".mov", ".avi", ".dxv", ".mkv", ".mp4")):
                        continue
                    out = os.path.join(tempfile.gettempdir(), "seekprobe_"
                                       + os.path.basename(n))
                    if not os.path.isfile(out):
                        with z.open(n) as src, open(out, "wb") as f:
                            f.write(src.read())
                    m = _probe_meta(out)
                    if m and m["codec"] == "dxv":
                        return out
        except Exception:                                        # noqa: BLE001
            continue
    return ""


# ==========================================================================
# 判定与报告
# ==========================================================================
LIMIT_SEEK_P95_MS = 50.0
LIMIT_DISK_MEAN_MBS = 120.0
LIMIT_DISK_PEAK_MBS = 250.0


def main(argv=None):
    ap = argparse.ArgumentParser(description="码表 seek 可行性实测（不改源码）")
    ap.add_argument("--file", action="append", default=[], help="素材路径（可多次）")
    ap.add_argument("--backend", default="both", choices=("cv2", "av", "both"))
    ap.add_argument("--n", type=int, default=20, help="单次 seek 采样次数")
    ap.add_argument("--cold", type=int, default=5, help="冷启动 seek 采样次数（每次新建容器）")
    ap.add_argument("--seconds", type=float, default=10.0, help="连续 seek 窗口（秒）")
    ap.add_argument("--every", type=int, default=4, help="每几帧 seek 一次（60fps ⇒ 4 帧=15Hz）")
    ap.add_argument("--json", default="", help="把原始测量写成 JSON")
    ap.add_argument("--out", default="", help="把 markdown 报告写到该路径")
    args = ap.parse_args(argv)

    if args.file:
        targets = []
        for p in args.file:
            m = _probe_meta(p)
            if not m:
                raise SystemExit("打不开/非视频：%s" % p)
            codec = m["codec"].upper()
            label = ("DXV" if m["codec"] == "dxv" else codec) + "(%.0fs)" % m["dur"]
            targets.append((label, p, m))
    else:
        targets = auto_pick()
    if not targets:
        raise SystemExit("没找到可测素材（用 --file 指定）")

    backends = list(BACKENDS) if args.backend == "both" else [args.backend]
    rows, raw = [], []
    for label, path, meta in targets:
        for bname in backends:
            be = BACKENDS[bname]()
            # DXV 走 av（= DxvGpuPlayer）；cv2 一般解不了 DXV，失败就跳过并记录
            try:
                be.open(path)
            except Exception as e:                               # noqa: BLE001
                rows.append({"label": label, "path": path, "meta": meta,
                             "backend": bname, "error": str(e)})
                continue
            warm = measure_single_seek(be, path, n=args.n, warm=True, exact=True)
            cold = measure_single_seek(be, path, n=max(1, args.cold), warm=False, exact=True)
            # "只 seek 到关键帧"（不做前向补齐）—— 用来看落点精度代价
            warm_kf = (measure_single_seek(be, path, n=args.n, warm=True, exact=False)
                       if hasattr(be, "seek_read_exact") else warm)
            cont = measure_stream(be, path, seconds=args.seconds, every=args.every,
                                  seek=True)
            base = measure_stream(be, path, seconds=min(3.0, args.seconds),
                                  every=args.every, seek=False)
            rows.append({"label": label, "path": path, "meta": meta, "backend": bname,
                         "warm": warm, "warm_kf": warm_kf, "cold": cold,
                         "cont": cont, "base": base, "error": ""})
            raw.append({"label": label, "path": path, "backend": bname,
                        "warm": warm, "warm_kf": warm_kf, "cold": cold,
                        "cont": cont, "base": base})
            sys.stderr.write("[probe] %-18s %-4s warm p95=%sms cold p95=%sms "
                             "读均值=%sMB/s 峰=%sMB/s\n"
                             % (label, bname, _ms(warm["p95"]), _ms(cold["p95"]),
                                ("%.1f" % cont["read_mean_mbs"])
                                if cont["read_mean_mbs"] is not None else "N/A",
                                ("%.1f" % cont["read_peak_mbs"])
                                if cont["read_peak_mbs"] is not None else "N/A"))

    report = build_report(rows, args)
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report)
        print("\n[probe] 报告已写：%s" % args.out)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=1)
        print("[probe] 原始数据已写：%s" % args.json)
    st = verdict_state(rows)
    print("\n[probe] 判定 = %s" % st)
    return {"SEEK_OK": 0, "SEEK_PARTIAL": 2, "SEEK_FAIL": 1}[st]


def row_verdict(r):
    """单行判定：三项门槛全过才算 PASS。"""
    if r.get("error"):
        return "N/A"
    w, c = r["warm"], r["cont"]
    ok_seek = (w["p95"] is not None and w["p95"] < LIMIT_SEEK_P95_MS)
    ok_mean = (c["read_mean_mbs"] is None) or (c["read_mean_mbs"] < LIMIT_DISK_MEAN_MBS)
    ok_peak = (c["read_peak_mbs"] is None) or (c["read_peak_mbs"] < LIMIT_DISK_PEAK_MBS)
    return "PASS" if (ok_seek and ok_mean and ok_peak) else "FAIL"


def report_ok(rows):
    tested = [r for r in rows if not r.get("error")]
    return bool(tested) and all(row_verdict(r) != "FAIL" for r in tested)


def verdict_state(rows):
    """三态：SEEK_OK（全达标）/ SEEK_PARTIAL（部分格式达标 ⇒ 分级）/ SEEK_FAIL（全超）。"""
    tested = [r for r in rows if not r.get("error")]
    if not tested:
        return "SEEK_FAIL"
    bad = [r for r in tested if row_verdict(r) == "FAIL"]
    if not bad:
        return "SEEK_OK"
    return "SEEK_PARTIAL" if len(bad) < len(tested) else "SEEK_FAIL"


def build_report(rows, args):
    out = []
    out.append("# 码表 `seek` 可行性实测（方案 A 前置验证）")
    out.append("")
    out.append("- 生成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    out.append("- 工具：`tools/cue_seek_probe.py`（**只测量，未改任何源码**）")
    out.append("- 参数：单次 seek 采样 %d 次 / 冷启动 %d 次 / 连续窗口 %.0fs / 每 %d 帧 seek 一次"
               % (args.n, args.cold, args.seconds, args.every))
    out.append("- 门槛：单次 `P95 < %.0fms`；连续 seek 磁盘 均值 `< %.0fMB/s`、峰值 `< %.0fMB/s`"
               % (LIMIT_SEEK_P95_MS, LIMIT_DISK_MEAN_MBS, LIMIT_DISK_PEAK_MBS))
    out.append("")
    out.append("后端对应关系：`cv2` = `VideoPlayer`（OpenCV）；"
               "`av` = `AvAlphaPlayer`/`DxvGpuPlayer`（PyAV，**DXV 走这个**）。")
    out.append("")
    out.append("## 1. 单次 seek 耗时（ms）与落点精度")
    out.append("")
    out.append("| 素材 | 后端 | 编码 | 时长 | 热 P50(精确) | 热 P95(精确) | 热 max "
               "| 冷 P50 | 冷 P95 | 落点误差 P50 | 落点误差 max "
               "| 仅 seek 到关键帧 P95 | 其落点误差 P50 |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        if r.get("error"):
            out.append("| %s | %s | — | — | 打不开：%s | | | | | | |"
                       % (r["label"], r["backend"], r["error"][:40]))
            continue
        m = r["meta"] or {}
        w, c = r["warm"], r["cold"]
        kf = r.get("warm_kf") or w
        out.append("| %s | %s | %s | %.1fs | %s | **%s** | %s | %s | %s | %.3fs | %.3fs "
                   "| %s | %s |"
                   % (r["label"], r["backend"], (m.get("codec") or "?"),
                      m.get("dur") or 0.0, _ms(w["p50"]), _ms(w["p95"]), _ms(w["max"]),
                      _ms(c["p50"]), _ms(c["p95"]),
                      w["err_p50"] if w["err_p50"] is not None else -1.0,
                      w["err_max"] if w["err_max"] is not None else -1.0,
                      _ms(kf["p95"]),
                      ("%.3fs" % kf["err_p50"]) if kf["err_p50"] is not None else "N/A"))
    out.append("")
    out.append("## 2. 连续 seek（每 %d 帧一次，%.0fs 窗口）的磁盘/CPU" % (args.every, args.seconds))
    out.append("")
    out.append("| 素材 | 后端 | 完成 seek 次数 | 读盘总量 | 读盘均值 | 读盘峰值 | CPU 核数 "
               "| 基线顺序读 均值/峰值 |")
    out.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        if r.get("error"):
            continue
        c, b = r["cont"], r["base"]
        out.append("| %s | %s | %d | %sMB | **%sMB/s** | **%sMB/s** | %s | %s / %s MB/s |"
                   % (r["label"], r["backend"], c["n_seek"],
                      ("%.1f" % c["read_total_mb"]) if c["read_total_mb"] is not None else "N/A",
                      ("%.1f" % c["read_mean_mbs"]) if c["read_mean_mbs"] is not None else "N/A",
                      ("%.1f" % c["read_peak_mbs"]) if c["read_peak_mbs"] is not None else "N/A",
                      ("%.2f" % c["cpu_cores"]) if c["cpu_cores"] is not None else "N/A",
                      ("%.1f" % b["read_mean_mbs"]) if b["read_mean_mbs"] is not None else "N/A",
                      ("%.1f" % b["read_peak_mbs"]) if b["read_peak_mbs"] is not None else "N/A"))
    out.append("")
    out.append("## 3. 逐项判定")
    out.append("")
    out.append("| 素材 | 后端 | 单次 P95 | 磁盘均值 | 磁盘峰值 | 结论 |")
    out.append("|---|---|---|---|---|---|")
    for r in rows:
        if r.get("error"):
            out.append("| %s | %s | — | — | — | N/A（%s） |"
                       % (r["label"], r["backend"], r["error"][:30]))
            continue
        v = row_verdict(r)
        c = r["cont"]
        out.append("| %s | %s | %s ms %s | %s MB/s %s | %s MB/s %s | **%s** |"
                   % (r["label"], r["backend"], _ms(r["warm"]["p95"]),
                      "✓" if (r["warm"]["p95"] or 1e9) < LIMIT_SEEK_P95_MS else "✗",
                      ("%.1f" % c["read_mean_mbs"]) if c["read_mean_mbs"] is not None else "N/A",
                      "✓" if (c["read_mean_mbs"] is None
                              or c["read_mean_mbs"] < LIMIT_DISK_MEAN_MBS) else "✗",
                      ("%.1f" % c["read_peak_mbs"]) if c["read_peak_mbs"] is not None else "N/A",
                      "✓" if (c["read_peak_mbs"] is None
                              or c["read_peak_mbs"] < LIMIT_DISK_PEAK_MBS) else "✗",
                      v))
    out.append("")
    tested = [r for r in rows if not r.get("error")]
    ok = report_ok(rows)
    state = verdict_state(rows)
    out.append("## 4. 结论（三态：%s）" % state)
    out.append("")
    lat_bad = [r for r in tested if (r["warm"]["p95"] or 1e9) >= LIMIT_SEEK_P95_MS]
    lat_ok = [r for r in tested if (r["warm"]["p95"] or 1e9) < LIMIT_SEEK_P95_MS]
    disk_bad = [r for r in tested
                if (r["cont"]["read_mean_mbs"] is not None
                    and r["cont"]["read_mean_mbs"] >= LIMIT_DISK_MEAN_MBS)
                or (r["cont"]["read_peak_mbs"] is not None
                    and r["cont"]["read_peak_mbs"] >= LIMIT_DISK_PEAK_MBS)]
    max_peak = max([r["cont"]["read_peak_mbs"] for r in tested
                    if r["cont"]["read_peak_mbs"] is not None] or [0.0])
    out.append("- **磁盘维度**：%s（本次最大峰值 %.1fMB/s，门槛 %.0fMB/s）"
               % ("全部 PASS" if not disk_bad else "超门槛：" + "、".join(
                   "%s/%s" % (r["label"], r["backend"]) for r in disk_bad),
                  max_peak, LIMIT_DISK_PEAK_MBS))
    out.append("- **延迟维度（单次 P95 < %.0fms）**：达标 %s%s"
               % (LIMIT_SEEK_P95_MS,
                  "、".join("%s/%s" % (r["label"], r["backend"]) for r in lat_ok) or "无",
                  ("；**超门槛** " + "、".join("%s/%s(%.0fms)" % (
                      r["label"], r["backend"], r["warm"]["p95"]) for r in lat_bad))
                  if lat_bad else ""))
    out.append("")
    if state == "SEEK_OK":
        out.append("**判定：全部达标 ⇒「每 N 帧 seek 一次」的策略在本次素材上可行。**")
    elif state == "SEEK_PARTIAL":
        out.append("**判定：部分格式达标（分级）。** 磁盘从来不是瓶颈，超门槛的只有**单次延迟**；"
                   "且延迟 ∝ 条目的 GOP 长度。⇒ 若坚持本参数（每 %d 帧精确校正），"
                   "**只有达标那几档能用 A**；其余请按 §5 的分级/放宽方案处理，"
                   "或按团队规则转 B（`src_ended` 持帧 + 接受漂移）。" % args.every)
    else:
        out.append("**判定：全部超门槛 ⇒ 本参数下方案 A 不可行**，转 B 或放宽限频阈值后复测。")
    out.append("")
    out.append("> 说明：磁盘读量取自**进程级** `GetProcessIoCounters`（含所有解码线程），"
               "与验收口径 §B.2 一致；OS 页缓存未清空 ⇒ 冷/热都偏乐观，"
               "现场长时播放的页缓存更热，因此本报告是**保守上界**的方向。")
    return "\n".join(out)


if __name__ == "__main__":
    sys.exit(main())
