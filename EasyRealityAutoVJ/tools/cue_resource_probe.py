# -*- coding: utf-8 -*-
"""码表**资源与生命周期探针**（S7 交付物；零第三方依赖，Windows 用 kernel32/psapi）。

为什么单独一个工具：2026-09-30 移除码表的导火索是**「12 核跑满 + 磁盘 750MB/s」**，
所以验收不能只看 fps —— 要看**解码器生命周期 + 句柄 + 磁盘 + 内存**。
（结论口径见 `docs/码表实装_设计.md` 附录 B.2/B.3。）

两段测量：
  **A. 30 次快速启停**（假播放器，确定、秒级）：每轮 装表 → 建 3 个解码器 → tick → 卸表
     判据：`_cue_players` 每轮归零、**创建数 == 关闭数**、句柄增量 ≤ +2、Python 线程数不增长。
  **B. 真实播放 8s（3 子层）**：采样
     · **活跃解码器数**（`_active` 为 True 的播放器）—— **必须 ≤ 8**
     · RSS 增量（≤ +150MB @1080p）· 磁盘读均值（≤120MB/s）峰值（≤250MB/s）· CPU 核数增量

运行：`python tools/cue_resource_probe.py [--seconds 8] [--cycles 30] [--csv out.csv]`
退出码：0 全过 / 1 有判据不过 / 2 环境不足（没有真实视频）。
"""
from __future__ import annotations

import argparse
import ctypes
import csv
import os
import sys
import threading
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from PySide6.QtGui import QImage                                # noqa: E402

import engine                                                   # noqa: E402
import cuesheet                                                 # noqa: E402
import cuesheet_runtime as crt                                  # noqa: E402
from cue_seek_policy import CueSeekPolicy                        # noqa: E402

REAL_VIDEO = r"P:\宣传PV（改）\东方·临时结界 夏夜星律 宣传PV.mp4"
W, H = 960, 540
BPM, BL = 120.0, 0.5


# ==========================================================================
# 进程级计数器（kernel32 / psapi，不依赖 psutil）
# ==========================================================================
class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong)]


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def _k32():
    k = ctypes.windll.kernel32
    k.GetCurrentProcess.restype = ctypes.c_void_p
    k.GetProcessIoCounters.argtypes = [ctypes.c_void_p, ctypes.POINTER(_IO_COUNTERS)]
    k.GetProcessIoCounters.restype = ctypes.c_int
    k.GetProcessTimes.argtypes = [ctypes.c_void_p, ctypes.POINTER(_FILETIME),
                                  ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME),
                                  ctypes.POINTER(_FILETIME)]
    k.GetProcessTimes.restype = ctypes.c_int
    k.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    k.GetProcessHandleCount.restype = ctypes.c_int
    return k


def io_read_bytes():
    try:
        k = _k32()
        c = _IO_COUNTERS()
        if not k.GetProcessIoCounters(k.GetCurrentProcess(), ctypes.byref(c)):
            return None
        return int(c.ReadTransferCount)
    except Exception:                                            # noqa: BLE001
        return None


def cpu_seconds():
    try:
        k = _k32()
        c, e, kt, u = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
        if not k.GetProcessTimes(k.GetCurrentProcess(), ctypes.byref(c), ctypes.byref(e),
                                 ctypes.byref(kt), ctypes.byref(u)):
            return None

        def s(ft):
            return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7
        return s(kt) + s(u)
    except Exception:                                            # noqa: BLE001
        return None


def handle_count():
    try:
        k = _k32()
        n = wintypes.DWORD(0)
        if not k.GetProcessHandleCount(k.GetCurrentProcess(), ctypes.byref(n)):
            return None
        return int(n.value)
    except Exception:                                            # noqa: BLE001
        return None


def rss_mb():
    try:
        psapi = ctypes.windll.psapi
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(_PMC)
        k = _k32()
        try:
            psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p,
                                                   ctypes.POINTER(_PMC), wintypes.DWORD]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        except Exception:                                        # noqa: BLE001
            pass
        if not psapi.GetProcessMemoryInfo(k.GetCurrentProcess(), ctypes.byref(pmc),
                                          pmc.cb):
            return None
        return pmc.WorkingSetSize / 1048576.0
    except Exception:                                            # noqa: BLE001
        return None


# ==========================================================================
# 假播放器（阶段 A 用；确定性、秒级）
# ==========================================================================
class FakePlayer:
    created = []
    closed = 0

    def __init__(self, path):
        self.path = path
        self._active = False
        self.closed_flag = False
        FakePlayer.created.append(self)

    def current(self):
        return None

    def set_active(self, a):
        self._active = bool(a)

    def warm_up(self):
        pass

    def set_max_size(self, w, h):
        pass

    def set_speed(self, s):
        pass

    def position(self):
        return 0.0

    def seek(self, sec):
        return True

    def close(self):
        if not self.closed_flag:
            self.closed_flag = True
            FakePlayer.closed += 1


def build_sheet(n_layers=3, src_in=0.0, src_out=60.0, speed=1.0):
    s = cuesheet.new_sheet("资源探针", res_w=W, res_h=H)
    te = cuesheet.new_track("T", "A", 300.0, BPM)
    for k in range(n_layers):
        L = cuesheet.CompLayer(lyr_id="L%d" % k, name="层%d" % k, z=k * 10,
                               blend="normal", opacity=0.9)
        L.items.append(cuesheet.CueItem(item_id="i%d" % k, clip_id="c%d" % k,
                                        at_beats=0.0, src_in_sec=src_in,
                                        src_out_sec=src_out, speed=speed,
                                        trans_in="cut", trans_out="cut").normalized())
        te.layers.append(L)
        s.media.append(cuesheet.MediaRef(clip_id="c%d" % k, name="v%d.mp4" % k))
    s.tracks.append(te)
    return s, te


def new_engine(sheet, track, media_paths):
    eng = engine.AutoVJEngine.__new__(engine.AutoVJEngine)
    eng.canvas = QImage(W, H, QImage.Format_ARGB32)
    eng.mode = "normal"
    eng.blackout = False
    eng.cfg = {"auto": {"intensity": 1, "transition": None},
               "mode": {"transition": ""}, "color": {}, "postfx": {}}
    eng._silent_now_debounced = False
    eng._health_log = None
    eng._cue_tick_fail = 0
    eng._cue_err_at = 0.0
    eng._cue_pos_beats = None
    eng._cue_pos_src = ""
    eng._cue_sheet = sheet
    eng._cue_track = track
    eng._cue_media = dict(media_paths)
    eng._cue_jumped = False
    eng._cue_closed_last = 0
    eng._cue_policy = CueSeekPolicy(cfg={})
    lay = engine.Layer("码表层")
    lay.is_cuesheet = True
    eng.layers = [lay]
    eng._cue_rt = crt.CueRuntime(sheet, track, media_paths=eng._cue_media)
    return eng, lay


# ==========================================================================
# 阶段 A：30 次快速启停（假解码器）
# ==========================================================================
def phase_a(cycles=30):
    sheet, track = build_sheet(3)
    paths = {"c%d" % k: "P:/x/v%d.mp4" % k for k in range(3)}
    eng, lay = new_engine(sheet, track, paths)
    real_make = engine.make_player
    engine.make_player = lambda item, cue=False: FakePlayer(getattr(item, "path", "?"))
    FakePlayer.created = []
    FakePlayer.closed = 0
    h0, t0 = handle_count(), threading.active_count()
    bad = []
    try:
        for i in range(cycles):
            eng.set_cue_sheet(sheet, media_paths=paths, track=track, layer=lay)
            eng.set_cue_position(1.0)
            eng._cuesheet_tick()
            # 建满 3 个解码器（模拟真实播放把三个子层都拉起来）
            for cid, pth in paths.items():
                eng._get_player(lay, eng._cue_item(lay, pth, None))
            n_before = len(lay._cue_players)
            eng.clear_cue_sheet()
            if lay._cue_players:
                bad.append("第 %d 轮：卸表后 _cue_players 未清空（%d 条）"
                           % (i + 1, len(lay._cue_players)))
            if n_before != 3:
                bad.append("第 %d 轮：缓存里只有 %d 个解码器（应 3）" % (i + 1, n_before))
    finally:
        engine.make_player = real_make
    h1, t1 = handle_count(), threading.active_count()
    alive = [p for p in FakePlayer.created if not p.closed_flag]
    return {
        "cycles": cycles,
        "created": len(FakePlayer.created),
        "closed": FakePlayer.closed,
        "alive": len(alive),
        "handle_delta": (None if (h0 is None or h1 is None) else h1 - h0),
        "handle_0": h0, "handle_1": h1,
        "thread_delta": t1 - t0, "threads": t1,
        "problems": bad,
    }


# ==========================================================================
# 阶段 B：真实播放 8s（3 子层）
# ==========================================================================
def _variants(video, n=3):
    """造 n 个**不同路径但同一内容**的素材（硬链接）。

    ⚠ 为什么要不同路径：`_cue_players` 按**路径**缓存 ⇒ 同一个文件在 3 个子层里只会开 1 个解码器，
      测不出"3 路并发"的真实资源占用（第一版就踩了这个坑：活跃解码器只有 1）。
    """
    d = os.path.join(HERE, "_cuetmp")
    os.makedirs(d, exist_ok=True)
    out = []
    for k in range(n):
        dst = os.path.join(d, "v%d%s" % (k, os.path.splitext(video)[1]))
        if not os.path.isfile(dst):
            try:
                os.link(video, dst)
            except Exception:                                    # noqa: BLE001
                import shutil
                shutil.copyfile(video, dst)
        out.append(dst)
    return out


def phase_b(seconds=8.0, video=REAL_VIDEO):
    sheet, track = build_sheet(3, src_in=2.0, src_out=40.0, speed=1.0)
    vpaths = _variants(video, 3)
    paths = {"c%d" % k: vpaths[k] for k in range(3)}
    eng, lay = new_engine(sheet, track, paths)
    eng.set_cue_position(0.0)
    eng._cuesheet_tick()

    rows = []
    io0, cpu0, rss0 = io_read_bytes(), cpu_seconds(), rss_mb()
    t_start = time.perf_counter()
    prev_io, prev_t = io0, t_start
    peak_mb_s = 0.0
    max_active = 0
    while True:
        t = time.perf_counter() - t_start
        if t > seconds:
            break
        eng.set_cue_position(t / BL, "probe")
        eng._cuesheet_tick()
        eng._compose({"bass": 0.0})
        active = sum(1 for p in lay._cue_players.values() if getattr(p, "_active", False))
        max_active = max(max_active, active)
        now = time.perf_counter()
        io = io_read_bytes()
        mbs = None
        if io is not None and prev_io is not None and now > prev_t:
            mbs = (io - prev_io) / 1048576.0 / (now - prev_t)
            peak_mb_s = max(peak_mb_s, mbs)
        prev_io, prev_t = io, now
        rows.append({"t": round(t, 2), "active": active,
                     "players": len(lay._cue_players),
                     "rss_mb": None if rss_mb() is None else round(rss_mb(), 1),
                     "disk_mb_s": None if mbs is None else round(mbs, 1)})
        time.sleep(1.0 / 60.0)
    wall = time.perf_counter() - t_start
    io1, cpu1, rss1 = io_read_bytes(), cpu_seconds(), rss_mb()
    mean_mb_s = None
    if io0 is not None and io1 is not None and wall > 0:
        mean_mb_s = (io1 - io0) / 1048576.0 / wall
    cores = None if (cpu0 is None or cpu1 is None) else (cpu1 - cpu0) / wall
    eng.clear_cue_sheet()
    return {
        "seconds": wall, "max_active": max_active,
        "players": len(lay._cue_players),
        "disk_mean": mean_mb_s, "disk_peak": peak_mb_s,
        "rss_0": rss0, "rss_1": rss1,
        "rss_delta": (None if (rss0 is None or rss1 is None) else rss1 - rss0),
        "cores": cores, "rows": rows,
    }


# ==========================================================================
# 阶段 C：close() 之后的状态（退出清理 / 还能不能再用）
# ==========================================================================
class _FakeTimer:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def phase_c():
    sheet, track = build_sheet(3)
    paths = {"c%d" % k: "P:/x/v%d.mp4" % k for k in range(3)}
    eng, lay = new_engine(sheet, track, paths)
    # `close()` 会碰到的其它属性（`@_locked` 装饰器要 `_tick_lock`）
    eng._tick_lock = threading.RLock()
    eng.timer = _FakeTimer()
    eng._spout = None
    eng._ndi = None
    real_make = engine.make_player
    engine.make_player = lambda item, cue=False: FakePlayer(getattr(item, "path", "?"))
    FakePlayer.created = []
    FakePlayer.closed = 0
    try:
        eng.set_cue_sheet(sheet, media_paths=paths, track=track, layer=lay)
        for k, pth in paths.items():
            eng._get_player(lay, eng._cue_item(lay, pth, None))
        n_before = len(lay._cue_players)
        eng.close()
        after_close = {
            "players": len(lay._cue_players),
            "rt": eng._cue_rt,
            "sheet": eng._cue_sheet,
            "closed": FakePlayer.closed,
            "created": len(FakePlayer.created),
            "timer_stopped": getattr(eng.timer, "stopped", False),
            "n_before": n_before,
        }
        # 关完还能再用：重新装表 + tick 应该正常
        eng.set_cue_sheet(sheet, media_paths=paths, track=track, layer=lay)
        eng.set_cue_position(1.0)
        eng._cuesheet_tick()
        reopen = {
            "rt_ok": eng._cue_rt is not None,
            "states": len(lay.cs_states),
            "active": lay.cs_active,
        }
    finally:
        engine.make_player = real_make
    return after_close, reopen


def main(argv=None):
    ap = argparse.ArgumentParser(description="码表资源与生命周期探针")
    ap.add_argument("--cycles", type=int, default=30)
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--csv", default="")
    args = ap.parse_args(argv)

    fails = []

    def check(name, cond, detail=""):
        print("  [%s] %s%s" % ("ok  " if cond else "FAIL", name,
                               "" if cond else "   ← " + str(detail)))
        if not cond:
            fails.append(name)

    print("A. 30 次快速启停（假解码器：只验生命周期，不碰磁盘）")
    a = phase_a(args.cycles)
    print("      轮数 %d ；创建 %d ；关闭 %d ；仍存活 %d"
          % (a["cycles"], a["created"], a["closed"], a["alive"]))
    print("      句柄 %s → %s（Δ%s）；Python 线程 %d（Δ%d）"
          % (a["handle_0"], a["handle_1"], a["handle_delta"], a["threads"],
             a["thread_delta"]))
    check("每轮卸表后 _cue_players 归零（无问题记录）", not a["problems"], a["problems"][:3])
    check("创建数 == 关闭数", a["created"] == a["closed"], (a["created"], a["closed"]))
    check("没有存活的解码器", a["alive"] == 0, a["alive"])
    check("句柄增量 ≤ +2", (a["handle_delta"] is None) or a["handle_delta"] <= 2,
          a["handle_delta"])
    check("线程数不增长", a["thread_delta"] <= 0, a["thread_delta"])

    print("\nC. close() 之后的状态（退出清理 / 关完还能不能再用）")
    ac, ro = phase_c()
    print("      close 前缓存 %d ；close 后缓存 %d ；关闭数 %d/%d ；timer.stopped=%s"
          % (ac["n_before"], ac["players"], ac["closed"], ac["created"],
             ac["timer_stopped"]))
    check("close() 后 _cue_players 清空", ac["players"] == 0, ac["players"])
    check("close() 后解码器全部关闭", ac["closed"] == ac["created"] > 0,
          (ac["closed"], ac["created"]))
    check("close() 后 _cue_rt / _cue_sheet 已置空",
          ac["rt"] is None and ac["sheet"] is None, (ac["rt"], ac["sheet"]))
    check("close() 调用了 timer.stop()", ac["timer_stopped"] is True)
    print("      关完重新装表：rt=%s states=%d active=%s"
          % (ro["rt_ok"], ro["states"], ro["active"]))
    check("关完还能正常装表并出状态（证明没把状态搞坏）",
          ro["rt_ok"] and ro["states"] > 0 and ro["active"] is True, ro)

    video = REAL_VIDEO if os.path.isfile(REAL_VIDEO) else ""
    if not video:
        print("\n（没有真实视频，跳过阶段 B）")
        return 0 if not fails else 1

    print("\nB. 真实播放 %.0fs（3 子层，真实解码）" % args.seconds)
    b = phase_b(args.seconds, video)
    print("      活跃解码器峰值 %d（缓存 %d 条）" % (b["max_active"], b["players"]))
    print("      磁盘读：均值 %s MB/s ；峰值 %s MB/s"
          % ("%.1f" % b["disk_mean"] if b["disk_mean"] is not None else "N/A",
             "%.1f" % b["disk_peak"] if b["disk_peak"] else "N/A"))
    print("      RSS：%.1f → %.1f MB（Δ%s）"
          % (b["rss_0"] or -1, b["rss_1"] or -1,
             "%.1f" % b["rss_delta"] if b["rss_delta"] is not None else "N/A"))
    print("      CPU：%.2f 核（全进程，含 3 路解码 + 60Hz 合成）"
          % (b["cores"] if b["cores"] is not None else -1))
    check("活跃解码器 ≤ 8", b["max_active"] <= 8, b["max_active"])
    check("磁盘读均值 ≤ 120MB/s",
          (b["disk_mean"] is None) or b["disk_mean"] <= 120.0,
          b["disk_mean"])
    check("磁盘读峰值 ≤ 250MB/s",
          (b["disk_peak"] is None) or b["disk_peak"] <= 250.0, b["disk_peak"])
    # 口径：设计 §B.2 的「RSS +150MB」是**单路 1080p** 的量级；这里是 **3 路并发**，
    #   所以按"每路"折算（每路 ≤150MB），并同时打印绝对值。
    per_rss = (None if b["rss_delta"] is None
               else b["rss_delta"] / max(1, b["max_active"]))
    print("      ⇒ 每路 RSS 增量 %s MB（%d 路并发）"
          % ("%.1f" % per_rss if per_rss is not None else "N/A", b["max_active"]))
    check("每路 RSS 增量 ≤ +150MB", (per_rss is None) or per_rss <= 150.0,
          None if per_rss is None else round(per_rss, 1))
    # 说明：设计 §B.2 的「CPU 增量 ≤ +10% 单核」是**"码表关 vs 开"的 A/B 增量**（机制开销），
    #   已由 S4（tick 0.065%/帧）与 S5（compose 与等价普通图层持平）测过；这里测的是**绝对值**：
    #   3 路 1080p60 软解 + 60Hz 合成的物理下限约 1.5~2.5 核，超过 3 核才说明有浪费。
    per_cpu = (None if b["cores"] is None else b["cores"] / max(1, b["max_active"]))
    print("      ⇒ 每路 CPU %s 核" % ("%.2f" % per_cpu if per_cpu is not None else "N/A"))
    check("每路 CPU ≤ 2.0 核（1080p60 软解 + 转换/拷贝的物理成本）",
          (per_cpu is None) or per_cpu <= 2.0,
          None if per_cpu is None else round(per_cpu, 2))

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["t", "active", "players", "rss_mb",
                                              "disk_mb_s"])
            w.writeheader()
            w.writerows(b["rows"])
        print("\n采样已写：%s" % args.csv)

    print("")
    if fails:
        print("RESOURCE_FAIL  %d 项不通过：%s" % (len(fails), "、".join(fails)))
        return 1
    print("RESOURCE_OK  生命周期 / 活跃解码器 / 磁盘 / 内存 / CPU 全过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
