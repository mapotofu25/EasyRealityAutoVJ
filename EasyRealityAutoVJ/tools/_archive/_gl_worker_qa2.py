# -*- coding: utf-8 -*-
"""GLWorker「降级→冷却→自恢复」状态机 —— QA 第 2 轮独立复验（Edward，2026-09-26）

复验工程师的 R2/R4 改动，**不重跑他的自测**，重点找边界漏洞：
  statemachine      状态机全边界（用可控假解码器，确定性）：冷却零等待 / 探测失败重新冷却 /
                    恢复后再次抖动 / ok=False 不可恢复 / worker 崩溃仍 dead / **并发探测竞态**
  recover_e2e       真实 GpuDxvPlayer：降级 → 冷却 → 恢复后**新播放器真的恢复走 GPU 出帧**
  gpu_status_rt     R4：worker 降级后 gpu_status() 是否**立即**改报不可用
  longrun_degraded  降级后 5 个真实 DXV 并发跑 30s：画面持续出帧、线程/内存不增长
  regression_qa1    跑上一轮的 tools/_gl_worker_qa.py（不改它）
  regression_selftest 跑 tools/gpu_player_selftest.py

用法：venv\\Scripts\\python.exe tools\\_gl_worker_qa2.py
"""
import ctypes
import json
import os
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


def read_log_tail(n=40):
    try:
        with open(log_path(), encoding="utf-8", errors="replace") as fh:
            return fh.readlines()[-n:]
    except Exception:
        return []


def count_player(pl, secs):
    """廉价统计出帧（只比对象身份），返回 (帧数, fps)。"""
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


# ---------------- 素材 ----------------
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


def _pick(fmt_want, min_px=1.9e6):
    for p, fmt, w, h in _dxv_files():
        if fmt == fmt_want and w * h >= min_px:
            return p
    return None


def _one_block(path):
    import av
    import dxvnative
    c = av.open(path, metadata_errors="replace")
    st = c.streams.video[0]
    sw, sh = int(st.width), int(st.height)
    try:
        for pkt in c.demux(st):
            if not pkt.size:
                continue
            r = dxvnative.frame_blocks(bytes(pkt), sw, sh)
            if r is not None:
                return sw, sh, int(r[0]), r[1]
    finally:
        c.close()
    return None


# ================================================================ 可控假解码器
class _CTL:
    mode = "ok"          # "ok" | "slow" | "hang"
    sleep_s = 0.0


def _mk_fake():
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
    return _Fake


def _mk_worker(recover, timeout):
    import glctx
    glctx.GLWorker._inst = None
    glctx.GLDecoder = _mk_fake()
    wk = glctx.GLWorker.get()
    wk.RECOVER_AFTER = recover
    wk.decode_timeout = timeout
    ok = wk.ensure()
    return wk, ok


# ================================================================ 用例：状态机
def case_statemachine():
    import glctx
    w("=" * 94)
    w("用例 statemachine：降级/冷却/自恢复 全边界（可控假解码器，确定性）")
    w("=" * 94)
    if not glctx.preinit():
        chk(False, "glfw 不可用")
        return

    def d1(wk):
        return wk.decode(b"\x00" * 4096, 64, 64, 1, 64, 64, bgra=True)

    # ---------- 场景 1：超时→冷却（零等待）→探测成功→恢复 ----------
    w("\n--- 场景1：超时→冷却零等待→探测成功→自动恢复 ---")
    wk, ok = _mk_worker(recover=0.8, timeout=0.3)
    chk(ok, "worker 就绪")
    _CTL.mode, _CTL.sleep_s = "slow", 0.75          # > timeout → 超时
    t = time.perf_counter()
    r = d1(wk)
    dt = time.perf_counter() - t
    w("    首次请求返回 %r，耗时 %.2fs；stats=%s" % (r, dt, wk.stats()))
    chk(r is None and 0.25 <= dt <= 0.55, "超时请求返回 None 且阻塞≈timeout（%.2fs）" % dt)
    chk(wk._degraded and not wk.available(), "进入 degraded 且 available()=False")
    t = time.perf_counter()
    for _ in range(50):
        d1(wk)
    tot = time.perf_counter() - t
    w("    冷却期内 50 次请求共 %.4fs" % tot)
    chk(tot < 0.05, "冷却期内请求**零等待**（50 次 %.4fs，不是每帧等 0.3s）" % tot)
    time.sleep(0.85)                                  # 越过冷却
    _CTL.mode = "ok"                                  # 探测将成功
    r = d1(wk)
    w("    冷却后探测返回 %s；_degraded=%s available=%s last_error=%r"
      % (r is not None, wk._degraded, wk.available(), wk.last_error))
    chk(r is not None, "冷却后探测成功返回一帧")
    chk((not wk._degraded) and wk.available(), "★ 自动恢复：degraded 清除、available()=True")

    # ---------- 场景 2：探测失败 → 重新冷却（不是立即重试） ----------
    w("\n--- 场景2：探测失败→重新进入冷却（不立即重试） ---")
    wk, ok = _mk_worker(recover=0.8, timeout=0.3)
    _CTL.mode, _CTL.sleep_s = "slow", 0.75
    d1(wk)
    chk(wk._degraded, "先降级")
    time.sleep(0.85)                                  # 冷却已过
    _CTL.mode, _CTL.sleep_s = "slow", 0.75            # 探测仍会超时
    t = time.perf_counter()
    r = d1(wk)
    dt = time.perf_counter() - t
    chk(r is None and 0.25 <= dt <= 0.55, "探测失败：阻塞≈timeout 后 None（%.2fs）" % dt)
    chk(wk._degraded, "探测失败后仍/重新 degraded")
    t = time.perf_counter()
    for _ in range(20):
        d1(wk)
    tot2 = time.perf_counter() - t
    w("    探测失败后 20 次请求共 %.4fs" % tot2)
    chk(tot2 < 0.03, "★ 失败后**立即回到冷却**：请求又零等待（不是每帧重试，%.4fs）" % tot2)

    # ---------- 场景 3：恢复后再次抖动 → 能再次降级 ----------
    w("\n--- 场景3：恢复后再次抖动→能再次降级（状态机不粘死） ---")
    wk, ok = _mk_worker(recover=0.8, timeout=0.3)
    _CTL.mode, _CTL.sleep_s = "slow", 0.75
    d1(wk)
    time.sleep(0.85)
    _CTL.mode = "ok"
    d1(wk)
    chk((not wk._degraded) and wk.available(), "第一次已恢复")
    _CTL.mode, _CTL.sleep_s = "slow", 0.75            # 再次抖动
    r = d1(wk)
    chk(r is None and wk._degraded and not wk.available(),
        "★ 恢复后再次抖动 → 能再次降级")

    # ---------- 场景 4：ok=False（不可恢复） ----------
    w("\n--- 场景4：上下文失效（dec.ok=False）→ 不可恢复 ---")
    wk, ok = _mk_worker(recover=0.5, timeout=0.3)
    _CTL.mode = "ok"
    wk._dec.ok = False
    time.sleep(0.7)
    chk(not wk.available(), "ok=False → available()=False")
    t = time.perf_counter()
    r = d1(wk)
    dt = time.perf_counter() - t
    chk(r is None and dt < 0.05, "ok=False → 零等待 None（%.4fs）" % dt)
    chk(wk._degraded is False, "ok=False 不会被当成'降级'（是永久失效，另一条路）")

    # ---------- 场景 5：worker 崩溃（dead）→ 不因冷却恢复 ----------
    w("\n--- 场景5：worker 崩溃（dead）→ 冷却也不恢复 ---")
    import glctx as _g

    class _Raising:
        def __init__(self):
            raise RuntimeError("模拟：建上下文崩溃")

    _g.GLWorker._inst = None
    _g.GLDecoder = _Raising
    wk2 = _g.GLWorker.get()
    ok = wk2.ensure()
    chk(ok is False and wk2._dead, "worker 崩溃 → ensure False 且 _dead=True")
    time.sleep(0.6)
    chk(not wk2.available(), "dead 不因时间流逝而 available")
    r = wk2.decode(b"\x00" * 4096, 64, 64, 1, 64, 64, bgra=True)
    chk(r is None, "dead → decode 永久 None")

    # ---------- 场景 6（关键）：多线程同时撞冷却结束点 ----------
    w("\n--- 场景6（关键）：多线程**同时**撞上冷却结束点（并发探测竞态） ---")
    wk, ok = _mk_worker(recover=0.8, timeout=0.3)
    _CTL.mode, _CTL.sleep_s = "slow", 0.9
    d1(wk)                                            # 降级
    chk(wk._degraded, "已降级")
    time.sleep(0.85)                                  # 越过冷却
    _CTL.mode, _CTL.sleep_s = "slow", 0.9             # 探测仍会超时（模拟驱动仍卡）
    nok0 = wk.n_ok
    N = 4
    bar = threading.Barrier(N)
    res = [None] * N
    dts = [0.0] * N

    def job(i):
        bar.wait()                                    # 尽量同一瞬间
        t = time.perf_counter()
        res[i] = d1(wk)
        dts[i] = time.perf_counter() - t
    ths = [threading.Thread(target=job, args=(i,)) for i in range(N)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    deg_after = wk._degraded
    until_after = wk._degrade_until
    now = time.perf_counter()
    w("    4 并发探测：返回=%s 各耗时=%s" % ([r is None for r in res],
                                            ["%.2f" % d for d in dts]))
    w("    探测后 _degraded=%s  _degrade_until-now=%+.2fs  n_ok +%d（n_timeout=%d）"
      % (deg_after, until_after - now, wk.n_ok - nok0, wk.n_timeout))
    t = time.perf_counter()
    r = d1(wk)
    dt2 = time.perf_counter() - t
    w("    紧接着一次请求耗时 %.3fs（若仍处冷却应≈0）" % dt2)
    chk(deg_after is True, "★ 并发探测失败后**仍处于降级**（_degraded 未被误清）")
    chk(dt2 < 0.05, "★ 其后请求**仍零等待**（未被误清成'可用'导致又等 timeout）")


# ================================================================ 用例：真实恢复
def case_recover_e2e():
    import glctx
    import media_manager as mm
    import av
    w("=" * 94)
    w("用例 recover_e2e：真实 GpuDxvPlayer 降级→冷却→**恢复后新播放器真的走 GPU 出帧**")
    w("=" * 94)
    if not glctx.preinit():
        chk(False, "glfw 不可用")
        return
    p = _pick(5) or _pick(1)
    if not p:
        chk(False, "无 DXV 素材")
        return
    mm.set_gpu_decode(True)
    mm._GPU_DISABLED = ""
    wk = glctx.GLWorker.get()
    if not chk(wk.ensure(), "worker 就绪"):
        return
    wk.RECOVER_AFTER = 1.5
    wk.decode_timeout = 0.4
    orig = wk._dec.decode

    # 1) P1 走 GPU
    mm._GPU_STATUS = None
    P1 = mm.GpuDxvPlayer(p, av, out_alpha=True)
    P1.set_active(True)
    n1, f1 = count_player(P1, 2.0)
    w("  ① P1：gpu=%s %d 帧（%.1ffps）reason=%s" % (P1.gpu, n1, f1, P1.reason))
    chk(P1.gpu is True and f1 > 20, "P1 初始走 GPU 且正常出帧")

    # 2) 注入"慢帧"（>timeout，但 worker 线程最终会返回）→ 降级
    def slow(*a, **k):
        time.sleep(0.9)
        return orig(*a, **k)
    wk._dec.decode = slow
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 5:
        if not wk.available():
            break
        time.sleep(0.02)
    dt_deg = time.perf_counter() - t0
    av_now = wk.available()                       # 立刻记，别被后面的窗口跨过冷却混淆
    n2, f2 = count_player(P1, 2.5)
    w("  ② 注入慢帧后 %.2fs 内降级；P1 现 gpu=%s %d 帧（%.1ffps）reason=%s"
      % (dt_deg, P1.gpu, n2, f2, P1.reason))
    chk((av_now is False) and P1.gpu is False, "P1 降级到软解（降级瞬间 available=False）")
    chk(n2 > 50, "降级后 P1 持续出帧（%d 帧 / %.1ffps，画面不断）" % (n2, f2))

    # 3) 越过冷却、恢复解码速度
    time.sleep(1.7)
    wk._dec.decode = orig
    before = wk.available()
    w("  ③ 冷却过后（未探测）available()=%s（乐观放行，交由探测决定）" % before)

    # 4) 新建 P2 → 应恢复走 GPU
    P2 = mm.GpuDxvPlayer(p, av, out_alpha=True)
    P2.set_active(True)
    n3, f3 = count_player(P2, 3.0)
    w("  ④ P2（恢复后新建）：gpu=%s %d 帧（%.1ffps）reason=%s；_degraded=%s"
      % (P2.gpu, n3, f3, P2.reason, wk._degraded))
    chk(P2.gpu is True, "★ 恢复后新播放器**真的走 GPU**（.gpu=True）")
    chk(f3 > 20, "★ P2 以 GPU 水平出帧（%.1ffps）" % f3)
    chk(wk.available() and not wk._degraded, "worker 已清除降级、available()=True")

    # 5) P1 是否也回到 GPU？（预期：不会，已切软解后不回头）
    time.sleep(0.3)
    w("  ⑤ 已降级的 P1 是否自动回 GPU：gpu=%s（现状：切软解后不回头）" % P1.gpu)

    # 6) 日志
    tail = read_log_tail(60)
    joined = "".join(tail)
    w("  ⑥ ui_stall.log 相关行：")
    for line in tail:
        if "GLWorker" in line or "GL 解码" in line:
            w("      %s" % line.rstrip())
    chk("GLWorker 解码超时" in joined or "临时停用 GPU 解码" in joined, "日志有**降级**记录")
    chk("已自动恢复" in joined, "日志有**恢复**记录")
    P1.close()
    P2.close()


# ================================================================ 用例：R4
def case_gpu_status_rt():
    import glctx
    import media_manager as mm
    w("=" * 94)
    w("用例 gpu_status_rt（R4）：worker 降级后 gpu_status() **立即**改报不可用")
    w("=" * 94)
    if not glctx.preinit():
        chk(False, "glfw 不可用")
        return
    p = _pick(5) or _pick(1)
    if not p:
        chk(False, "无素材")
        return
    mm._GPU_DISABLED = ""
    mm._GPU_STATUS = None
    mm.set_gpu_decode(True)
    st1 = mm.gpu_status()
    w("  初始 gpu_status() = %r" % (st1,))
    chk(st1[0] is True, "初始 gpu_status() 报可用")
    wk = glctx.GLWorker.get()
    wk.ensure()
    wk.RECOVER_AFTER = 30.0
    wk.decode_timeout = 0.4
    sw, sh, fmt, blk = _one_block(p)

    def slow(*a, **k):
        time.sleep(0.8)
        return None
    wk._dec.decode = slow
    wk.decode(blk, sw, sh, fmt, 640, 360, bgra=True)      # 触发降级
    st2 = mm.gpu_status()
    w("  降级后 gpu_status() = %r" % (st2,))
    chk(st2[0] is False, "★ 降级后 gpu_status() **立即**改报 (False, ...)")
    chk(bool(st2[1]) and ("停用" in st2[1] or "软解" in st2[1] or "超时" in st2[1]),
        "原因写得清：%r" % st2[1])


# ================================================================ 用例：长跑
def case_longrun_degraded():
    import glctx
    import media_manager as mm
    import av
    w("=" * 94)
    w("用例 longrun_degraded：掉 GPU 后 5 个真实 DXV 并发 30s —— 画面持续、无增长/卡顿")
    w("=" * 94)
    if not glctx.preinit():
        chk(False, "glfw 不可用")
        return
    files = [p for p, fmt, ww, hh in _dxv_files() if fmt in (1, 5) and ww * hh >= 1.9e6]
    N = 5
    T = float(os.environ.get("QA_SECS", "30"))
    if len(files) < N:
        chk(False, "素材不足")
        return
    picks = files[:N]
    wk = glctx.GLWorker.get()
    if not chk(wk.ensure(), "worker 就绪"):
        return
    wk.RECOVER_AFTER = 600.0                # 保持降级，整个长跑都不恢复
    wk.decode_timeout = 0.4
    mm.set_gpu_decode(True)
    mm._GPU_DISABLED = ""

    players = []
    for pth in picks:
        pl = mm.GpuDxvPlayer(pth, av, out_alpha=True)
        pl.set_active(True)
        players.append(pl)
    time.sleep(0.8)
    w("  淘汰前 GPU 判定：%s" % [pl.gpu for pl in players])
    chk(all(pl.gpu for pl in players), "5 层初始走 GPU")

    # 注入永久挂死（模拟驱动真挂）→ 全体降级
    wk._dec.decode = lambda *a, **k: time.sleep(9999)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 5:
        if all(pl.gpu is False for pl in players):
            break
        time.sleep(0.02)
    deg_s = time.perf_counter() - t0
    w("  ★ 注入永久挂死后 **%.2fs** 内 5 层全部降级；gpu=%s" % (deg_s, [pl.gpu for pl in players]))
    chk(all(pl.gpu is False for pl in players), "5 层全部降级到软解")
    chk(deg_s < 3.0, "降级切换很快（%.2fs，非永久冻结）" % deg_s)

    # 长跑
    prev = [pl.current() for pl in players]
    cnt = [0] * N
    base = snap()
    samples = [base]
    t0 = time.perf_counter()
    last_t = t0
    max_gap = 0.0
    while time.perf_counter() - t0 < T:
        time.sleep(0.003)
        for k, pl in enumerate(players):
            img = pl.current()
            if img is not None and img is not prev[k]:
                prev[k] = img
                cnt[k] += 1
        now = time.perf_counter()
        if now - last_t >= 1.0:
            max_gap = max(max_gap, now - last_t)
            last_t = now
            samples.append(snap())
    run_dt = time.perf_counter() - t0
    fps = [c / run_dt for c in cnt]
    ths = [s["threads"] for s in samples]
    wss = [s["ws_mb"] for s in samples]
    w("  降级后跑 %.1fs：各层 %s 帧（≈%s fps）"
      % (run_dt, cnt, ["%.1f" % f for f in fps]))
    w("  线程：%s" % ths)
    w("  工作集 MB：%s" % ["%.0f" % x for x in wss])
    w("  采样最大间隔 %.2fs" % max_gap)
    chk(all(f >= 15 for f in fps), "★ 降级后 5 层**持续出帧** ≥15fps（%s）"
        % ["%.1f" % f for f in fps])
    # 稳态（跳过软解解码器线程池 spin-up 的前几个采样）
    steady = ths[3:] if len(ths) > 5 else ths
    half = max(1, len(steady) // 2)
    f_avg = sum(steady[:half]) / half
    l_avg = sum(steady[half:]) / max(1, len(steady) - half)
    drift = l_avg - f_avg
    w("  稳态线程：均值 前段 %.0f → 后段 %.0f（漂移 %+.0f），峰谷 %d"
      % (f_avg, l_avg, drift, max(steady) - min(steady)))
    chk(abs(drift) < 25, "线程**无单调增长**（稳态前后段均值漂移 %+.0f）" % drift)
    chk(max(steady) - min(steady) < 60, "线程波动受控（峰谷 %d）" % (max(steady) - min(steady)))
    chk(max(wss) - min(wss) < 150, "工作集稳定（峰谷 %+.0f MB）" % (max(wss) - min(wss)))
    chk(max_gap < 2.0, "无卡顿（最大采样间隔 %.2fs）" % max_gap)
    # 关闭后线程应回落 —— 证明前面的高值是活跃解码线程，不是泄漏
    for pl in players:
        pl.close()
    time.sleep(1.8)
    fin = snap()
    w("  关闭 5 个播放器后线程 = %d（运行稳态峰值 %d）" % (fin["threads"], max(steady)))
    chk(fin["threads"] <= max(steady) - 15, "关闭后线程明显回落（%d ≤ 峰值-15）→ 非泄漏"
        % fin["threads"])


# ================================================================ 回归
def _run_script(rel, timeout, keys):
    w("=" * 94)
    w("用例 %s：跑 %s" % (keys, rel))
    w("=" * 94)
    try:
        pr = subprocess.run([PY, os.path.join(ROOT, rel)], capture_output=True, text=True,
                            timeout=timeout)
        out = pr.stdout
        w(out[-4000:] if len(out) > 4000 else out)
        if pr.stderr.strip():
            w("--- stderr ---\n" + pr.stderr[-2000:])
        ok = pr.returncode == 0
        chk(ok, "%s 退出码 %d" % (rel, pr.returncode))
        if "结果：全部通过" in out or "总判定：全部通过" in out:
            chk(True, "%s 自报全部通过" % rel)
        else:
            hits = [ln for ln in out.splitlines() if "★" in ln or "FAIL" in ln]
            chk(not hits, "%s 无失败标记（%d 条）" % (rel, len(hits)))
    except subprocess.TimeoutExpired:
        chk(False, "★ %s 超时（%ds）" % (rel, timeout))


def case_regression_qa1():
    _run_script("tools/_gl_worker_qa.py", 400, "regression_qa1")


def case_regression_selftest():
    _run_script("tools/gpu_player_selftest.py", 300, "regression_selftest")


CASES = {
    "statemachine": case_statemachine,
    "recover_e2e": case_recover_e2e,
    "gpu_status_rt": case_gpu_status_rt,
    "longrun_degraded": case_longrun_degraded,
    "regression_qa1": case_regression_qa1,
    "regression_selftest": case_regression_selftest,
}
TIMEOUTS = {
    "statemachine": 120, "recover_e2e": 120, "gpu_status_rt": 90,
    "longrun_degraded": float(os.environ.get("QA_SECS", "30")) + 120,
    "regression_qa1": 420, "regression_selftest": 320,
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
    p("AutoVJ GLWorker 状态机 QA 第2轮   %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    p("python=%s" % PY)
    for nm in names:
        p("\n" + "#" * 94)
        p("### 子进程用例：%s（超时 %ds）" % (nm, int(TIMEOUTS[nm])))
        p("#" * 94)
        tmp = tempfile.mkdtemp(prefix="qa2_")
        env = dict(os.environ, LOCALAPPDATA=tmp)          # 日志写到临时目录，不碰用户数据
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
            p("★★ 用例 %s 超时（%ds）—— 判定为 HANG/不通过" % (nm, int(TIMEOUTS[nm])))
            if e.stdout:
                p((e.stdout if isinstance(e.stdout, str) else e.stdout.decode("utf-8", "replace"))[-3000:])
            results[nm] = {"case": nm, "passed": False, "n_fail": -1,
                           "fails": ["★ 子进程超时/挂死（>%ds）" % int(TIMEOUTS[nm])], "secs": None}

    p("\n" + "=" * 94)
    p("QA 第2轮 汇总结论")
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
    with open(os.path.join(ROOT, "_gl_worker_qa2.json"), "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(ROOT, "_gl_worker_qa2.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(_LOGF))
    p("→ 汇总已写入 _gl_worker_qa2.json / _gl_worker_qa2.txt")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--one" and sys.argv[2] in CASES:
        run_one(sys.argv[2])
    else:
        only = [a for a in sys.argv[1:] if a in CASES] or None
        orchestrate(only)
