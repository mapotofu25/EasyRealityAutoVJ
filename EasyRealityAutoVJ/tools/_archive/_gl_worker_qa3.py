# -*- coding: utf-8 -*-
"""QA 第 3 轮（聚焦复验）：B1 并发探测竞态 + B2 降级后就地回切 GPU（Edward）

只看这两处，独立构造角度，不信自报。
  b1_race_fail8    8 线程同时撞冷却结束点（探测失败）→ 只 1 个阻塞满 timeout，余零等待；不误清降级/不误增 n_ok
  b1_race_success  冷却到点、探测会成功的并发场景 → 只授权一次探测、不重复恢复、状态正确
  b1_cv_audit      cancelled 覆盖度（所有给等待者 set() 之处）+ `_cv` 临界区内无阻塞操作（静态+并发压测）
  b2_same_player   同一播放器 降级→（软解重探）→自动回切 GPU：.gpu False→True、n_ok 增长、不卡顿
  b2_flap          连续多次 抖动→恢复→再抖动→再恢复：状态机不粘死
  b2_reprobe_freq  软解重探频率 ≈3s 一次（不是每帧）
  b2_close_switch  软解重探期间 close()/图层切换 → 不崩不泄漏
  b2_zero_create   glctx.py:742 仍唯一 GLDecoder；运行期 create/destroy 仍各 0 次
  regression_all   直接跑 qa1(10) / qa2(6) / hang_test / gpu_player_selftest

用法：venv\\Scripts\\python.exe tools\\_gl_worker_qa3.py
"""
import ast
import ctypes
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
LIB = r"Y:\EasyRealityAutoVJ\素材库"
PY = sys.executable

OUT = []
FAIL = []


def w(s=""):
    print(s, flush=True)
    OUT.append(str(s))


def chk(cond, msg):
    w("    [%s] %s" % ("OK  " if cond else "FAIL", msg))
    if not cond:
        FAIL.append(msg)
    return cond


# ---------------- 进程级读数 ----------------
_PID = os.getpid()
_k32 = ctypes.windll.kernel32
_psapi = ctypes.WinDLL("psapi")
_k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
_k32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
_k32.Thread32First.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_k32.Thread32Next.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_k32.CloseHandle.argtypes = [ctypes.c_void_p]
_k32.GetCurrentProcess.restype = ctypes.c_void_p
_psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]


class _TE(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                ("th32ThreadID", ctypes.c_ulong), ("th32OwnerProcessID", ctypes.c_ulong),
                ("tpBasePri", ctypes.c_long), ("tpDeltaPri", ctypes.c_long),
                ("dwFlags", ctypes.c_ulong)]


class _PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def snap():
    s = _k32.CreateToolhelp32Snapshot(0x00000004, 0)
    n = 0
    te = _TE()
    te.dwSize = ctypes.sizeof(_TE)
    if _k32.Thread32First(s, ctypes.byref(te)):
        while True:
            if te.th32OwnerProcessID == _PID:
                n += 1
            if not _k32.Thread32Next(s, ctypes.byref(te)):
                break
    _k32.CloseHandle(s)
    p = _PMC()
    p.cb = ctypes.sizeof(_PMC)
    _psapi.GetProcessMemoryInfo(_k32.GetCurrentProcess(), ctypes.byref(p), p.cb)
    return {"threads": n, "ws_mb": p.WorkingSetSize / 1048576.0,
            "commit_mb": p.PagefileUsage / 1048576.0}


def log_path():
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "ui_stall.log")


def log_text():
    try:
        with open(log_path(), encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:
        return ""


def count_player(pl, secs):
    last = pl.current()
    n = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < secs:
        img = pl.current()
        if img is not None and img is not last:
            last = img
            n += 1
        time.sleep(0.003)
    dt = time.perf_counter() - t0
    return n, (n / dt if dt else 0.0)


def _dxv_files():
    import av
    import dxvnative
    out = []
    for f in sorted(os.listdir(LIB)):
        if not f.lower().endswith((".mov", ".mp4", ".mkv")):
            continue
        p = os.path.join(LIB, f)
        try:
            c = av.open(p, metadata_errors="replace")
            try:
                st = c.streams.video[0]
                if st.codec_context.name != "dxv":
                    continue
                w, h = int(st.width), int(st.height)
                pk = next(c.demux(st))
                hd = dxvnative.parse_header(bytes(pk))
                fmt = None if hd is None else int(hd[0])
            finally:
                c.close()
        except Exception:
            continue
        out.append((p, fmt, w, h))
    return out


def _pick(fmt_want=5, min_px=1.9e6):
    for p, fmt, w, h in _dxv_files():
        if fmt == fmt_want and w * h >= min_px:
            return p
    for p, fmt, w, h in _dxv_files():
        if fmt in (1, 5) and w * h >= min_px:
            return p
    return None


# ---------------- 可控假解码器 ----------------
class _CTL:
    mode = "ok"
    sleep_s = 0.0


def _mk_worker(recover, timeout):
    import glctx

    class _Fake:
        def __init__(self):
            self.ok = True
            self.err = ""
            self.calls = 0

        def decode(self, blocks, sw, sh, fmt, dw, dh, bgra=False):
            self.calls += 1
            if _CTL.mode == "hang":
                time.sleep(9999)
            elif _CTL.mode == "slow":
                time.sleep(_CTL.sleep_s)
            import numpy as np
            return np.zeros((dh, dw, 4), dtype=np.uint8)

        def destroy(self):
            return False
    glctx.GLWorker._inst = None
    glctx.GLDecoder = _Fake
    wk = glctx.GLWorker.get()
    wk.RECOVER_AFTER = recover
    wk.decode_timeout = timeout
    return wk, wk.ensure()


def _d(wk):
    return wk.decode(b"\x00" * 4096, 64, 64, 1, 64, 64, bgra=True)


# ================================================================ B1
def case_b1_race_fail8():
    import glctx
    w("=" * 94)
    w("B1 用例：8 线程同时撞冷却结束点（探测失败）→ 只 1 个阻塞 timeout，余零等待")
    w("=" * 94)
    if not glctx.preinit():
        chk(False, "glfw 不可用")
        return
    TO, RA = 0.3, 0.8
    wk, ok = _mk_worker(recover=RA, timeout=TO)
    chk(ok, "worker 就绪")
    _CTL.mode, _CTL.sleep_s = "slow", 0.5
    _d(wk)                                     # 第一次超时 → 降级
    chk(wk._degraded, "已降级")
    time.sleep(0.9)                            # 越过冷却
    _CTL.mode, _CTL.sleep_s = "slow", 0.9      # 探测仍会失败
    nok0, nto0 = wk.n_ok, wk.n_timeout
    N = 8
    bar = threading.Barrier(N)
    res = [None] * N
    dts = [0.0] * N

    def job(i):
        bar.wait()
        t = time.perf_counter()
        res[i] = _d(wk)
        dts[i] = time.perf_counter() - t
    ths = [threading.Thread(target=job, args=(i,)) for i in range(N)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    n_block = sum(1 for d in dts if d > TO * 0.6)
    n_zero = sum(1 for d in dts if d < 0.05)
    w("    各线程耗时：%s" % ["%.3f" % d for d in dts])
    w("    阻塞≈timeout 的线程数=%d，零等待线程数=%d，全部返回 None=%s"
      % (n_block, n_zero, all(r is None for r in res)))
    w("    _degraded=%s  n_ok +%d  n_timeout +%d"
      % (wk._degraded, wk.n_ok - nok0, wk.n_timeout - nto0))
    chk(n_block == 1, "★ 恰好**只有 1 个**线程阻塞满 timeout（其余不该等）")
    chk(n_zero == N - 1, "★ 其余 %d 个线程**零等待**返回 None" % (N - 1))
    chk(all(r is None for r in res), "所有并发探测都返回 None（探测失败）")
    chk(wk._degraded is True, "★ 探测失败后 `_degraded` 保持 True（未被误清）")
    chk(wk.n_ok == nok0, "★ `n_ok` 未被误增（误判成功）")
    t = time.perf_counter()
    _d(wk)
    dt = time.perf_counter() - t
    chk(dt < 0.05, "★ 其后请求仍**零等待**（%.4fs）—— 没有变回每帧等 timeout" % dt)


def case_b1_race_success():
    import glctx
    w("=" * 94)
    w("B1 用例：冷却到点、探测会成功的多线程场景 → 只授权一次探测、不重复恢复")
    w("=" * 94)
    if not glctx.preinit():
        chk(False, "glfw 不可用")
        return
    TO, RA = 0.3, 0.8
    wk, ok = _mk_worker(recover=RA, timeout=TO)
    chk(ok, "worker 就绪")
    _CTL.mode, _CTL.sleep_s = "slow", 0.5
    _d(wk)
    chk(wk._degraded, "已降级")
    time.sleep(0.9)
    _CTL.mode = "ok"                            # 探测会成功
    nok0 = wk.n_ok
    base_recover_logs = log_text().count("已自动恢复")
    N = 8
    bar = threading.Barrier(N)
    res = [None] * N

    def job(i):
        bar.wait()
        res[i] = _d(wk)
    ths = [threading.Thread(target=job, args=(i,)) for i in range(N)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    n_ok_res = sum(1 for r in res if r is not None)
    rec_logs = log_text().count("已自动恢复") - base_recover_logs
    w("    8 线程：成功返回帧 %d 个、None %d 个；n_ok +%d；'已自动恢复' 日志 %d 条"
      % (n_ok_res, N - n_ok_res, wk.n_ok - nok0, rec_logs))
    chk(n_ok_res >= 1, "至少一个线程拿到帧")
    chk(wk.n_ok - nok0 == n_ok_res, "n_ok 增量 == 成功返回数（无幽灵成功）")
    chk(rec_logs == 1, "★ '已自动恢复' 恰好 1 条（**只授权一次探测**、不重复恢复）")
    chk(wk._degraded is False, "★ 状态正确恢复（_degraded=False）")
    chk(wk.available() is True, "available()=True")


def case_b1_cv_audit():
    import glctx
    w("=" * 94)
    w("B1 用例：cancelled 覆盖度 + `_cv` 临界区无阻塞（静态审计 + 并发压测）")
    w("=" * 94)
    src = open(os.path.join(SRC, "glctx.py"), encoding="utf-8").read()
    lines = src.splitlines()

    # 1) 所有 event.set() 之处：要么带 cancelled，要么带真实 result（worker 正常完成）
    w("    ---- glctx.py 中所有 `*.event.set()` 之处 ----")
    bad = []
    for i, l in enumerate(lines, 1):
        if re.search(r"\.event\.set\(\)", l):
            ctx = "\n".join(lines[max(0, i - 4):i + 1])
            has_cancel = "cancelled = True" in ctx
            # worker 正常完成路径：前面给 req.result 赋值
            has_result = ("req.result = " in ctx) or ("r.result = " in ctx)
            tag = "cancelled" if has_cancel else ("result" if has_result else "?未知?")
            w("      L%-4d %-14s | %s" % (i, tag, l.strip()[:60]))
            if not (has_cancel or has_result):
                bad.append(i)
    chk(not bad, "所有 event.set() 都有 cancelled 或 result 伴随（无遗漏：%s）" % bad)

    # 2) `with self._cv:` 临界区内不得有阻塞/耗时调用
    w("    ---- `with self._cv:` 临界区内容 ----")
    bad_ops = []
    i = 0
    while i < len(lines):
        if "with self._cv:" in lines[i]:
            ind = len(lines[i]) - len(lines[i].lstrip())
            w("      L%d:" % (i + 1))
            j = i + 1
            while j < len(lines) and (lines[j].strip() == "" or
                                      (len(lines[j]) - len(lines[j].lstrip())) > ind):
                body = lines[j].strip()
                w("        %s" % body[:78])
                # 允许 `self._cv.wait()`（条件变量等待 —— 等待时会释放锁，是正确用法）；
                # 只禁止：sleep / 事件等待 / 真解码 / 文件 IO 等会**持锁阻塞**的操作
                bad = bool(re.search(r"time\.sleep|frame_blocks|open\(|\.read\(", body))
                for m in re.finditer(r"[\w\.]+\.wait\(", body):
                    if m.group(0) != "self._cv.wait(":
                        bad = True
                if bad:
                    bad_ops.append((j + 1, body))
                j += 1
            i = j
        else:
            i += 1
    chk(not bad_ops, "临界区内**无**阻塞/耗时操作（%s）" % bad_ops)

    # 3) 并发压测：8 线程 × 200 快解码不卡死、无异常（证明 `_cv` 没变成重锁）
    if not glctx.preinit():
        chk(False, "glfw 不可用")
        return
    wk, ok = _mk_worker(recover=1.0, timeout=1.0)
    chk(ok, "worker 就绪")
    _CTL.mode = "ok"
    errs = []
    cnt = [0]

    def hammer():
        try:
            for _ in range(200):
                if _d(wk) is not None:
                    cnt[0] += 1
        except Exception as e:                                   # noqa: BLE001
            errs.append(str(e))
    ths = [threading.Thread(target=hammer) for _ in range(8)]
    t0 = time.perf_counter()
    for t in ths:
        t.start()
    for t in ths:
        t.join(30)
    dt = time.perf_counter() - t0
    alive = sum(1 for t in ths if t.is_alive())
    w("    8 线程 × 200 次解码：完成 %d 次，耗时 %.2fs，存活线程 %d，异常 %d"
      % (cnt[0], dt, alive, len(errs)))
    chk(alive == 0 and not errs, "并发压测全部完成、无卡死、无异常")


# ================================================================ B2
def _real_setup(recover, timeout):
    import glctx
    import media_manager as mm
    glctx.preinit()
    wk = glctx.GLWorker.get()
    wk.ensure()
    wk.RECOVER_AFTER = recover
    wk.decode_timeout = timeout
    mm.set_gpu_decode(True)
    mm._GPU_DISABLED = ""
    mm._GPU_STATUS = None
    return wk


def _inject_slow(wk, sleep_s):
    import time as _t
    orig = getattr(wk._dec, "decode")          # 当前 bound method（可能是真实或已包装）
    if not hasattr(wk, "_qa_orig_decode"):
        wk._qa_orig_decode = orig

    def slow(*a, **k):
        _t.sleep(sleep_s)
        return wk._qa_orig_decode(*a, **k)
    wk._dec.decode = slow
    return orig


def _restore(wk):
    if hasattr(wk, "_qa_orig_decode"):
        wk._dec.decode = wk._qa_orig_decode


def case_b2_same_player():
    import av
    import media_manager as mm
    import glctx
    w("=" * 94)
    w("B2 用例：同一播放器 降级→（软解重探）→自动回切 GPU（.gpu False→True、n_ok 增长、不卡顿）")
    w("=" * 94)
    p = _pick(5)
    if not p:
        chk(False, "无素材")
        return
    wk = _real_setup(recover=1.5, timeout=0.35)
    mm._GPU_STATUS = None
    P = mm.GpuDxvPlayer(p, av, out_alpha=True)
    P._GPU_REPROBE_SECS = 0.6                  # 加速重探（默认 3s）
    P.set_active(True)
    n, f = count_player(P, 2.0)
    w("  ① 初始：gpu=%s %d 帧（%.1ffps）" % (P.gpu, n, f))
    chk(P.gpu is True and n > 30, "初始走 GPU 且正常出帧")

    _inject_slow(wk, 0.8)                      # 慢帧 → 降级
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 5:
        if P.gpu is False:
            break
        time.sleep(0.02)
    t_down = time.perf_counter() - t0
    # 降级期间持续出帧
    n2, f2 = count_player(P, 1.5)
    w("  ② %.2fs 后 gpu=False；降级期出帧 %d（%.1ffps）" % (t_down, n2, f2))
    chk(P.gpu is False, "已降级")
    chk(n2 > 25, "降级期间持续出帧（%.1ffps）" % f2)

    # 恢复解码速度 → 播放器应自动回切
    _restore(wk)
    nok_before = wk.n_ok
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 8:
        if P.gpu is True:
            break
        time.sleep(0.02)
    t_up = time.perf_counter() - t0
    w("  ③ 恢复后 %.2fs 内 gpu 回到 True；n_ok 增量=%d" % (t_up, wk.n_ok - nok_before))
    chk(P.gpu is True, "★ 同一播放器**就地自动回切 GPU**（.gpu=True）")
    chk(t_up < 6.0, "回切及时（%.2fs ≤ 冷却+重探间隔）" % t_up)
    n3, f3 = count_player(P, 2.0)
    w("  ④ 回切后出帧 %d（%.1ffps）；n_ok 增量=%d" % (n3, f3, wk.n_ok - nok_before))
    chk(n3 > 40 and f3 > 20, "★ 回切后真的在 GPU 出帧（%.1ffps）" % f3)
    chk(wk.n_ok - nok_before > 30, "★ 回切后 n_ok 明显增长（确实在 GPU 解码）")
    chk("DXT" in P.reason and "GPU" in P.reason, "reason 已改回 GPU：%r" % P.reason)
    P.close()


def case_b2_flap():
    import av
    import media_manager as mm
    w("=" * 94)
    w("B2 用例：连续多次 抖动→恢复→再抖动→再恢复（状态机不粘死）")
    w("=" * 94)
    p = _pick(5)
    if not p:
        chk(False, "无素材")
        return
    wk = _real_setup(recover=1.0, timeout=0.3)
    mm._GPU_STATUS = None
    P = mm.GpuDxvPlayer(p, av, out_alpha=True)
    P._GPU_REPROBE_SECS = 0.5
    P.set_active(True)
    count_player(P, 1.5)
    cycles = 3
    ok_all = True
    for c in range(cycles):
        _inject_slow(wk, 0.7)
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 5 and P.gpu is not False:
            time.sleep(0.02)
        down = P.gpu is False
        _restore(wk)
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 8 and P.gpu is not True:
            time.sleep(0.02)
        up = P.gpu is True
        w("  第 %d 轮：降级=%s 恢复=%s（降级用 %.2fs）" % (c + 1, down, up, 0.0))
        ok_all = ok_all and down and up
    chk(ok_all, "★ %d 轮 抖动/恢复 全部成功（状态机不粘死）" % cycles)
    n, f = count_player(P, 1.5)
    w("  末态 gpu=%s 出帧 %d（%.1ffps）" % (P.gpu, n, f))
    chk(P.gpu is True and n > 25, "末态稳定在 GPU 且出帧正常")
    P.close()


def case_b2_reprobe_freq():
    import av
    import media_manager as mm
    w("=" * 94)
    w("B2 用例：软解重探频率（≈3s 一次，不是每帧）")
    w("=" * 94)
    p = _pick(5)
    if not p:
        chk(False, "无素材")
        return
    wk = _real_setup(recover=999.0, timeout=0.35)     # 冷却极长 → 整个窗口保持降级
    mm._GPU_STATUS = None
    P = mm.GpuDxvPlayer(p, av, out_alpha=True)        # 用默认 _GPU_REPROBE_SECS=3.0
    w("  默认 _GPU_REPROBE_SECS = %s" % P._GPU_REPROBE_SECS)
    P.set_active(True)
    _inject_slow(wk, 0.8)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 5 and P.gpu is not False:
        time.sleep(0.02)
    chk(P.gpu is False, "已降级")
    # 统计窗口内 available() 调用次数
    orig_av = wk.available
    cnt = [0]

    def av_cnt():
        cnt[0] += 1
        return orig_av()
    wk.available = av_cnt
    secs = 12.0
    n, f = count_player(P, secs)
    wk.available = orig_av
    w("  %.0fs 内：出帧 %d（%.1ffps）；worker.available() 调用 %d 次（%.1f 次/s）"
      % (secs, n, f, cnt[0], cnt[0] / secs))
    # 预期：定时重探 ≈ 每 3s 一次（~4 次）+ 每次容器 loop 结束顺带 1 次（很短素材会多几次）
    chk(cnt[0] / secs < 5.0, "★ 状态查询 ≪ 每帧（%.1f 次/s，远低于 %.0f fps）" % (cnt[0] / secs, f))
    chk(cnt[0] < n * 0.15, "★ available() 调用数 ≪ 出帧数（%d 次 vs %d 帧）" % (cnt[0], n))
    chk(n > secs * 15, "降级期出帧未受阻（%.1ffps）" % f)
    P.close()


def case_b2_close_switch():
    import av
    import media_manager as mm
    w("=" * 94)
    w("B2 用例：软解重探期间 close()/图层切换 → 不崩、不泄漏")
    w("=" * 94)
    files = [x[0] for x in _dxv_files() if x[1] in (1, 5) and x[2] * x[3] >= 1.9e6][:6]
    if not files:
        chk(False, "无素材")
        return
    wk = _real_setup(recover=999.0, timeout=0.35)
    mm._GPU_STATUS = None
    _inject_slow(wk, 0.8)                        # 让 worker 保持降级（超时）
    time.sleep(0.5)
    ok = True
    marks = {}
    total = 24
    for i in range(total):                      # 24 次 create→run→close（都在降级重探中）
        try:
            pl = mm.GpuDxvPlayer(files[i % len(files)], av, out_alpha=True)
            pl._GPU_REPROBE_SECS = 0.4
            pl.set_active(True)
            time.sleep(0.3)
            _ = pl.current()
            pl.close()
        except Exception as e:                                   # noqa: BLE001
            ok = False
            w("  第 %d 次异常：%s" % (i + 1, e))
        if i in (total // 2 - 1, total - 1):     # 一半 / 全部结束后各采一次
            time.sleep(2.5)                      # 给异步退出的解码线程/容器时间收敛
            marks[i] = snap()
    chk(ok, "%d 次 create/run/close（在降级重探中）无异常" % total)
    half, fin = marks.get(total // 2 - 1), marks.get(total - 1)
    w("  中途(%d 次)后：线程 %d  工作集 %.0f MB" % (total // 2, half["threads"], half["ws_mb"]))
    w("  全部(%d 次)后：线程 %d  工作集 %.0f MB" % (total, fin["threads"], fin["ws_mb"]))
    chk(fin["threads"] - half["threads"] < 10,
        "★ 后半程线程不再增长（%+d）—— 是收敛不是泄漏" % (fin["threads"] - half["threads"]))
    chk(fin["ws_mb"] - half["ws_mb"] < 80,
        "后半程内存不再增长（%+.0f MB）" % (fin["ws_mb"] - half["ws_mb"]))


def case_b2_probe_hitch():
    import av
    import media_manager as mm
    w("=" * 94)
    w("B2 附加：驱动**持续**迟滞时，回切探测失败的代价（记录每次冷却的卡顿幅度）")
    w("=" * 94)
    p = _pick(5)
    if not p:
        chk(False, "无素材")
        return
    TO = 0.3
    wk = _real_setup(recover=1.0, timeout=TO)
    mm._GPU_STATUS = None
    P = mm.GpuDxvPlayer(p, av, out_alpha=True)
    P._GPU_REPROBE_SECS = 0.4
    P.set_active(True)
    count_player(P, 1.0)
    _inject_slow(wk, 0.8)                        # 探针每次都会超时（对播放器=失败）
    last = P.current()
    t_prev = time.perf_counter()
    gaps = []
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 8.0:
        img = P.current()
        if img is not None and img is not last:
            now = time.perf_counter()
            gaps.append(now - t_prev)
            t_prev = now
            last = img
        time.sleep(0.002)
    gaps = gaps[1:]
    med = sorted(gaps)[len(gaps) // 2] if gaps else 0.0
    mx = max(gaps) if gaps else 0.0
    big = sum(1 for g in gaps if med > 0 and g > med * 3)
    w("  8s 内出帧 %d；帧间隔 中位 %.3fs  最大 %.3fs；>3×中位的卡顿 %d 次"
      % (len(gaps), med, mx, big))
    w("  （产品参数 timeout=1.5s / RECOVER_AFTER=60s ⇒ 每次冷却约 1.5s 卡一下；"
      "探针成功时无此卡顿）")
    chk(len(gaps) > 100, "降级期仍持续出帧（%d 帧 / 8s）" % len(gaps))
    chk(mx < 2.0, "无永久冻结（最大帧间隔 %.3fs < 2s）" % mx)
    chk(wk._degraded is True, "探针持续失败 → 保持降级（不会误恢复）")
    P.close()


def case_b2_zero_create():
    w("=" * 94)
    w("确认：glctx.py:742 仍唯一 GLDecoder；运行期 create/destroy 仍各 0 次")
    w("=" * 94)
    # 静态：唯一 GLDecoder()
    lines = open(os.path.join(SRC, "glctx.py"), encoding="utf-8").read().splitlines()
    hits = [(i + 1, l.strip()) for i, l in enumerate(lines) if "GLDecoder()" in l]
    w("  GLDecoder() 出现：%s" % hits)
    chk(len(hits) == 1 and hits[0][0] == 742, "glctx.py:742 仍是唯一 GLDecoder()")


def case_b2_zero_create2():
    import av
    import glctx
    import media_manager as mm
    w("=" * 94)
    w("确认2：运行期 (降级+回切全过程) glfw.create/destroy_window = 0")
    w("=" * 94)
    import glfw
    p = _pick(5)
    # ⚠ 先完成唯一上下文的**首次**建立（ensure），再装计数器 —— 首次 create 是允许的
    wk = _real_setup(recover=1.0, timeout=0.3)
    mm._GPU_STATUS = None
    cnt = {"c": 0, "d": 0}
    rc, rd = glfw.create_window, glfw.destroy_window
    glfw.create_window = lambda *a, **k: (cnt.__setitem__("c", cnt["c"] + 1), rc(*a, **k))[1]
    glfw.destroy_window = lambda *a, **k: (cnt.__setitem__("d", cnt["d"] + 1), rd(*a, **k))[1]
    try:
        pl = mm.GpuDxvPlayer(p, av, out_alpha=True)
        pl._GPU_REPROBE_SECS = 0.5
        pl.set_active(True)
        count_player(pl, 1.2)
        _inject_slow(wk, 0.7)                 # 降级
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 5 and pl.gpu is not False:
            time.sleep(0.02)
        _restore(wk)
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 8 and pl.gpu is not True:
            time.sleep(0.02)
        count_player(pl, 1.2)
        pl.close()
    finally:
        glfw.create_window, glfw.destroy_window = rc, rd
    w("  ensure 之后运行期：glfw.create_window=%d 次；destroy_window=%d 次"
      % (cnt["c"], cnt["d"]))
    chk(cnt["c"] == 0 and cnt["d"] == 0, "★ 运行期零 create/destroy（含降级+回切）")
    chk(pl.gpu is True, "结束时已回切 GPU（gpu=True）")


# ================================================================ 回归
def _run_script(rel, timeout):
    w("=" * 94)
    w("回归：%s" % rel)
    w("=" * 94)
    try:
        pr = subprocess.run([PY, os.path.join(ROOT, rel)], capture_output=True, text=True,
                            timeout=timeout)
        out = pr.stdout
        w(out[-3500:] if len(out) > 3500 else out)
        if pr.stderr.strip():
            w("--- stderr ---\n" + pr.stderr[-1500:])
        passed = ("全部通过" in out) and (pr.returncode == 0)
        fails = [ln for ln in out.splitlines() if "[FAIL]" in ln or "★不通过" in ln]
        chk(passed, "%s 全部通过（rc=%d）" % (rel, pr.returncode))
        if fails:
            chk(False, "%s 仍有失败标记：%s" % (rel, fails[:5]))
    except subprocess.TimeoutExpired:
        chk(False, "★ %s 超时(%ds)" % (rel, timeout))


def case_regression_all():
    _run_script("tools/_gl_worker_qa.py", 420)
    _run_script("tools/_gl_worker_qa2.py", 500)
    _run_script("tools/_gl_worker_hang_test.py", 120)
    _run_script("tools/gpu_player_selftest.py", 320)


CASES = {
    "b1_race_fail8": case_b1_race_fail8,
    "b1_race_success": case_b1_race_success,
    "b1_cv_audit": case_b1_cv_audit,
    "b2_same_player": case_b2_same_player,
    "b2_flap": case_b2_flap,
    "b2_reprobe_freq": case_b2_reprobe_freq,
    "b2_close_switch": case_b2_close_switch,
    "b2_probe_hitch": case_b2_probe_hitch,
    "b2_zero_create": case_b2_zero_create,
    "b2_zero_create2": case_b2_zero_create2,
    "regression_all": case_regression_all,
}
TIMEOUTS = {
    "b1_race_fail8": 90, "b1_race_success": 90, "b1_cv_audit": 120,
    "b2_same_player": 120, "b2_flap": 150, "b2_reprobe_freq": 120,
    "b2_close_switch": 150, "b2_probe_hitch": 90,
    "b2_zero_create": 60, "b2_zero_create2": 120,
    "regression_all": 1400,
}


def run_one(name):
    sys.path.insert(0, SRC)
    t0 = time.perf_counter()
    try:
        CASES[name]()
    except Exception as e:
        import traceback
        traceback.print_exc()
        FAIL.append("用例抛异常：%s" % e)
    finally:
        dt = time.perf_counter() - t0
        res = {"case": name, "passed": not FAIL, "n_fail": len(FAIL),
               "fails": FAIL[:20], "secs": round(dt, 1)}
        w("")
        w("用例 %s：%s（%d 项失败，%.1fs）"
          % (name, "通过" if res["passed"] else "不通过", res["n_fail"], dt))
        print("##QA_JSON##" + json.dumps(res, ensure_ascii=False), flush=True)


_LOGF = []


def p(s=""):
    print(s, flush=True)
    _LOGF.append(str(s))


def orchestrate(only=None):
    names = list(CASES) if not only else only
    results = {}
    p("AutoVJ QA 第3轮（B1 并发竞态 / B2 就地回切）   %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    p("python=%s" % PY)
    for nm in names:
        p("\n" + "#" * 94)
        p("### 子进程用例：%s（超时 %ds）" % (nm, int(TIMEOUTS[nm])))
        p("#" * 94)
        tmp = tempfile.mkdtemp(prefix="qa3_")
        env = dict(os.environ, LOCALAPPDATA=tmp)
        try:
            pr = subprocess.run([PY, os.path.abspath(__file__), "--one", nm],
                                capture_output=True, text=True, timeout=TIMEOUTS[nm], env=env)
            out = pr.stdout
            tail = ""
            for line in out.splitlines():
                if line.startswith("##QA_JSON##"):
                    tail = line[len("##QA_JSON##"):]
            p(out)
            if pr.stderr.strip():
                p("--- stderr ---")
                p(pr.stderr[-3000:])
            results[nm] = (json.loads(tail) if tail else
                           {"case": nm, "passed": False, "n_fail": -1,
                            "fails": ["无结果 JSON（rc=%s）" % pr.returncode], "secs": None})
        except subprocess.TimeoutExpired as e:
            p("★★ 用例 %s 超时（%ds）—— HANG/不通过" % (nm, int(TIMEOUTS[nm])))
            if e.stdout:
                p((e.stdout if isinstance(e.stdout, str) else e.stdout.decode("utf-8", "replace"))[-3000:])
            results[nm] = {"case": nm, "passed": False, "n_fail": -1,
                           "fails": ["★ 子进程超时/挂死（>%ds）" % int(TIMEOUTS[nm])], "secs": None}

    p("\n" + "=" * 94)
    p("QA 第3轮 汇总结论")
    p("=" * 94)
    allpass = True
    for nm in names:
        r = results.get(nm, {})
        if not r.get("passed"):
            allpass = False
        p("  %-20s %-8s 失败 %s 项  %s"
          % (nm, "通过" if r.get("passed") else "★不通过", r.get("n_fail"),
             ("%.1fs" % r["secs"]) if r.get("secs") else ""))
        for f in (r.get("fails") or [])[:8]:
            p("        · %s" % f)
    p("=" * 94)
    p("总判定：%s" % ("全部通过" if allpass else "存在不通过用例（见上）"))
    with open(os.path.join(ROOT, "_gl_worker_qa3.json"), "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(ROOT, "_gl_worker_qa3.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(_LOGF))
    p("→ 汇总已写入 _gl_worker_qa3.json / _gl_worker_qa3.txt")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--one" and sys.argv[2] in CASES:
        run_one(sys.argv[2])
    else:
        only = [a for a in sys.argv[1:] if a in CASES] or None
        orchestrate(only)
