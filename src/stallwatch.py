# -*- coding: utf-8 -*-
"""主线程卡顿看门狗：把「软件未响应」变成可定位的日志。

【为什么需要】
现场反馈是「我就在前台点浏览器，软件就未响应了」，而那一刻：
  · 引擎还在跑（画面在输出、BPM/能量在跳、NDI 也还在发）
  · **只有 GUI 主线程停了**
这种"静默卡死"光靠猜很难定位（2026-09-25 排查了半天，NDI 被实测排除）。
所以放一个看门狗线程：**主线程超过 N 秒没打心跳，就把所有 Python 线程的调用栈
写进日志**（`sys._current_frames()`），下次再卡就能直接看到卡在哪一行，不用猜。

【局限（要知道）】
如果主线程是**持着 GIL 死等**（纯 Python 死循环/自旋），看门狗自己也拿不到 GIL →
这一轮抓不到栈；但主线程一旦恢复，看门狗会立刻记下"刚才卡了多久"。
如果是**等 C 调用**（绝大多数情况：磁盘、GPU、驱动、系统调用），GIL 是放开的 →
看门狗能完整抓到栈。

日志：`%LOCALAPPDATA%\\AutoVJ\\ui_stall.log`
"""
import os
import sys
import threading
import time

STALL_SECS = 3.0        # 主线程超过这么久没心跳 → 判定卡顿
POLL_SECS = 0.5
MAX_STACK = 30          # 每个线程最多回放这么多层栈
SNAP_EVERY = 5.0        # ★ 卡顿**持续**时，每 5 秒补一次快照（原来只记一次，
                        #   结果"永久卡死"看起来和"卡了一下就好了"一模一样 —— 2026-09-26 踩过）
MAX_SNAPS = 40          # 最多补这么多次（约 3.5 分钟），避免日志无限涨
HANG_HINT = 3.0         # 实际间隔 > 计划间隔 × 这个倍数 ⇒ 看门狗自己也被挡住了


def log_path():
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "ui_stall.log")


def log_line(line):
    """从别处往这个日志里写一行（例如 `engine._locked` 的锁获取超时）。

    都是"界面卡住"这一类事件，放同一个文件最方便一起看。
    """
    p = log_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (stamp, line))
    except Exception:                                            # noqa: BLE001
        pass


def _stacks(prev=None):
    """所有 Python 线程的当前调用栈。

    返回 `(输出行, {tid: "文件:行"})`。

    `prev` 给上次的映射时，会逐个线程标注**位置有没有变** —— 这是区分两种卡死的
    关键：**所有线程都"与上次相同" ⇒ 连解码线程都停了 ⇒ GIL 被某个 C 调用长期持住**；
    只有主线程不动、其它照常 ⇒ 只是主线程自己在等（锁/驱动）。
    """
    names = {}
    try:
        for t in threading.enumerate():
            names[t.ident] = t.name
    except Exception:                                            # noqa: BLE001
        pass
    try:
        frames = sys._current_frames()
    except Exception as e:                                       # noqa: BLE001
        return ["      （拿不到线程栈：%s）" % e], {}
    out = []
    cur = {}
    for tid, fr in frames.items():
        pos = "?"
        try:
            pos = "%s:%d" % (os.path.basename(fr.f_code.co_filename), fr.f_lineno)
        except Exception:                                        # noqa: BLE001
            pass
        cur[tid] = pos
        mark = ""
        if prev:
            if tid not in prev:
                mark = "   ← 新出现"
            elif prev[tid] != pos:
                mark = "   ← 变了（上次 %s）" % prev[tid]
            else:
                mark = "   ← 与上次相同"
        out.append("      ── 线程 %s%s" % (names.get(tid, "?"), mark))
        n = 0
        while fr is not None and n < MAX_STACK:
            try:
                out.append("         %s:%d  in %s" % (
                    os.path.basename(fr.f_code.co_filename), fr.f_lineno,
                    fr.f_code.co_name))
            except Exception:                                    # noqa: BLE001
                break
            fr = fr.f_back
            n += 1
        if fr is not None:
            out.append("         …（更外层略）")
    return out, cur


def _proc_snapshot():
    """进程级读数（全只读、微秒级）：线程数 / 句柄数 / 工作集 / 提交 / CPU 累计秒。

    ⚠ 只用 `GetProcessTimes` / `GetProcessMemoryInfo` / 线程快照 —— 全都**不 attach、
    不暂停**任何线程，所以在用户正在演出时也可以安全调用（见 MEMORY.md 的零干扰铁律）。
    """
    r = {}
    try:
        import ctypes
        import ctypes.wintypes as wt
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = wt.HANDLE      # 不设会被当 32 位截断
        # ⚠ 伪句柄是 -1（无符号 0xFFFF_FFFF_FFFF_FFFF）：**必须包成 c_void_p 再传**，
        # 否则 ctypes 对未声明 argtypes 的函数按 32 位 int 取参 →
        # `ArgumentError: OverflowError: int too long to convert`。
        h = ctypes.c_void_p(k32.GetCurrentProcess())

        class FT(ctypes.Structure):
            _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]

        c, e, k, u = FT(), FT(), FT(), FT()
        if k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                               ctypes.byref(k), ctypes.byref(u)):
            r["cpu"] = (((k.hi << 32) | k.lo) + ((u.hi << 32) | u.lo)) / 1e7

        class PMC(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        m = PMC()
        m.cb = ctypes.sizeof(PMC)
        if ctypes.WinDLL("psapi").GetProcessMemoryInfo(h, ctypes.byref(m), m.cb):
            r["ws"] = m.WorkingSetSize / 1048576.0
            r["commit"] = m.PagefileUsage / 1048576.0

        hc = ctypes.c_ulong(0)
        if k32.GetProcessHandleCount(h, ctypes.byref(hc)):
            r["handles"] = hc.value

        s = k32.CreateToolhelp32Snapshot(0x00000004, 0)
        if s and s != -1:
            class TE(ctypes.Structure):
                _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                            ("th32ThreadID", ctypes.c_ulong),
                            ("th32OwnerProcessID", ctypes.c_ulong),
                            ("tpBasePri", ctypes.c_long), ("tpDeltaPri", ctypes.c_long),
                            ("dwFlags", ctypes.c_ulong)]
            te = TE()
            te.dwSize = ctypes.sizeof(TE)
            mypid = k32.GetCurrentProcessId()
            n = 0
            if k32.Thread32First(s, ctypes.byref(te)):
                while True:
                    if te.th32OwnerProcessID == mypid:
                        n += 1
                    if not k32.Thread32Next(s, ctypes.byref(te)):
                        break
            r["threads"] = n
            k32.CloseHandle(s)
    except Exception:                                            # noqa: BLE001
        pass
    return r


def _fmt_snap(r):
    parts = []
    for key, label, unit in (("threads", "线程", ""), ("handles", "句柄", ""),
                             ("ws", "工作集", " MB"), ("commit", "提交", " MB")):
        if key in r:
            v = r[key]
            parts.append("%s %s%s" % (label, "%.0f" % v if isinstance(v, float) else v, unit))
    return "  ".join(parts)


class StallWatchdog(threading.Thread):
    """心跳看门狗。`hb` 是主线程不断刷新的 {"t": perf_counter()}。"""

    def __init__(self, hb, stall_secs=STALL_SECS, poll=POLL_SECS, write=None):
        super().__init__(name="stall-watchdog", daemon=True)
        self.hb = hb
        self.stall_secs = stall_secs
        self.poll = poll
        self._write = write or self._default_write
        self._stop = threading.Event()
        self._reported = False
        self._worst = 0.0
        self._s0 = {}          # 卡顿起点的进程读数（用来算卡顿期间真正烧了几个核）
        self._t0 = 0.0
        self._pos = {}         # 上次各线程的位置（判断卡死期间"谁还在动"）
        self._last_snap = 0.0  # 上次补快照的时刻
        self._snaps = 0        # 本次卡顿已补了几次快照

    def _default_write(self, line):
        log_line(line)

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.wait(self.poll):
            try:
                gap = time.perf_counter() - self.hb.get("t", 0.0)
            except Exception:                                    # noqa: BLE001
                continue
            if gap < self.stall_secs:
                if self._reported:
                    self._write("主线程已恢复（本次卡顿最长约 %.1f 秒）" % self._worst)
                    # ★ 卡顿期间到底烧了几个核？——这是区分「在算」与「在等」的决定性判据：
                    #   CPU 低（<1 核）  ⇒ 主线程在等锁/等驱动/被阻塞，**不是算不过来**（要查锁）
                    #   CPU 高（>=2 核） ⇒ 真的在算东西（要查是不是渲染/解码太重）
                    s1 = _proc_snapshot()
                    dt = time.perf_counter() - self._t0
                    c0, c1 = self._s0.get("cpu"), s1.get("cpu")
                    if dt > 0.05 and c0 is not None and c1 is not None:
                        cores = max(0.0, (c1 - c0) / dt)
                        verdict = ("主线程在等（不烧 CPU）—— 不是算不过来"
                                   if cores < 1.0 else
                                   "进程在算 —— 负载偏重" if cores >= 2.0 else "介于两者之间")
                        self._write("   卡顿期间进程 CPU：%.2f 核  →  %s" % (cores, verdict))
                    d = {k: (s1[k] - self._s0[k]) for k in ("threads", "handles", "ws", "commit")
                         if k in s1 and k in self._s0}
                    if d:
                        self._write("   卡顿前后变化：线程 %+d  句柄 %+d  工作集 %+.0f MB  提交 %+.0f MB"
                                    % (d.get("threads", 0), d.get("handles", 0),
                                       d.get("ws", 0), d.get("commit", 0)))
                    self._write("")
                    self._reported = False
                    self._worst = 0.0
                    self._snaps = 0
                    self._pos = {}
                continue
            if gap > self._worst:
                self._worst = gap
            if not self._reported:
                self._reported = True
                self._write("★ 主线程卡顿：已 %.1f 秒没有响应（判定阈值 %.1f 秒）"
                            % (gap, self.stall_secs))
                self._s0 = _proc_snapshot()
                self._t0 = time.perf_counter()
                snap = _fmt_snap(self._s0)
                if snap:
                    self._write("   进程读数（卡顿起点）：%s" % snap)
                self._write("   以下是当时所有线程的调用栈：")
                lines, self._pos = _stacks(None)
                for line in lines:
                    self._write(line)
                self._last_snap = time.perf_counter()
                self._snaps = 1
            elif (self._snaps < MAX_SNAPS
                  and (time.perf_counter() - self._last_snap) >= SNAP_EVERY):
                # ★ 卡顿**还在持续** —— 补一次快照。
                # 判据的核心：看门狗自己就是普通 Python 线程，**它能把这条写出来就说明拿到了 GIL**。
                #  · 实际间隔 ≈ 计划间隔  ⇒ GIL 流通 ⇒ 主线程是"自己在等"（锁/驱动/系统调用）
                #  · 实际间隔 >> 计划间隔  ⇒ 看门狗也被挡住 ⇒ GIL 被某个 C 调用长期持有
                self._snaps += 1
                now = time.perf_counter()
                real = now - self._last_snap
                self._last_snap = now
                if real > SNAP_EVERY * HANG_HINT:
                    why = ("看门狗自己也写不动（实际间隔是计划的 %.1f 倍）"
                           "⇒ GIL 被长期持有，连看门狗都被挡住" % (real / SNAP_EVERY))
                else:
                    why = ("看门狗照常按时写入 ⇒ GIL 是流通的，"
                           "主线程是自己在等（锁/驱动/系统调用），不是被 GIL 饿死")
                self._write("   —— 仍在卡顿：已 %.1f 秒（第 %d 次快照）" % (gap, self._snaps))
                self._write("      %s" % why)
                s1 = _proc_snapshot()
                dt = now - self._t0
                c0, c1 = self._s0.get("cpu"), s1.get("cpu")
                if dt > 0.05 and c0 is not None and c1 is not None:
                    self._write("      从卡顿起点累计 CPU：%.2f 核" % max(0.0, (c1 - c0) / dt))
                snap = _fmt_snap(s1)
                if snap:
                    self._write("      进程读数：%s" % snap)
                prev = self._pos
                lines, cur = _stacks(prev)
                self._pos = cur
                for line in lines:
                    self._write(line)
                n_same = sum(1 for t, v in cur.items() if prev.get(t) == v)
                n_chg = len(cur) - n_same
                tail = "  ⇒ 所有线程都冻住了（连解码线程都不动）" if n_chg <= 1 else ""
                self._write("      小结：%d 个线程位置未变、%d 个在动%s" % (n_same, n_chg, tail))
