# -*- coding: utf-8 -*-
"""GLWorker 单上下文架构 —— QA 独立验证（Edward，2026-09-26）

验证工程师的架构级改动：全进程只建 **1 个** GL 上下文、专属工作线程持有、**永不销毁**，
运行期不再出现 glfwCreateWindow / glfwDestroyWindow。

⚠ 本脚本**不重跑**工程师的测试，而是独立构造边界与真实场景，自己在子进程里跑
（每个用例独立进程 —— 因为 GLWorker 是进程单例，且某用例会把 worker 打成 dead）。

用例：
  grep               静态复核 src/ 与 tools/ 里的 create/destroy 调用者
  correctness        多线程并发解码：结果不串台、尺寸/格式正确
  concurrent_ensure  多个调用方**同时**首次 ensure() 的竞态
  preflight          未就绪时 decode() 立即返回；ensure() 幂等
  okfalse            dec.ok 中途变 False（真实慢回读路径）⇒ available() 立即转 False
  hang_e2e           注入挂死 ⇒ 真实 GpuDxvPlayer 端到端降级软解并**继续出帧**
  startup_fail       拿不到 GL 上下文 ⇒ 优雅降级、reason 说清、不崩不黑屏
  multitrack         真实 4~5 图层并发 30~60s：持续出帧、线程/内存不增长、运行期零 create/destroy
  regression         开关关闭走原路径、反复切换稳定、无残留旧接口

用法：
  venv\\Scripts\\python.exe tools\\_gl_worker_qa.py            # 子进程里依次跑全部
  venv\\Scripts\\python.exe tools\\_gl_worker_qa.py <case>     # 跑单个用例（内部用）
"""
import ctypes
import json
import os
import re
import subprocess
import sys
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


# ---------------------------------------------------------------- 进程级读数
_PID = os.getpid()
_k32 = ctypes.windll.kernel32
_psapi = ctypes.WinDLL("psapi")
# ⚠ 64 位下必须显式声明 argtypes，否则 HANDLE 会被截断成 32 位 → 读数恒为 0
_k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
_k32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
_k32.Thread32First.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_k32.Thread32Next.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_k32.CloseHandle.argtypes = [ctypes.c_void_p]
_k32.GetCurrentProcess.restype = ctypes.c_void_p
_k32.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
_psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
_psapi.GetProcessMemoryInfo.restype = ctypes.c_int


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
    """进程级：活线程数（含驱动线程）/ 工作集 MB / 提交内存 MB / 句柄数"""
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
    h = _k32.GetCurrentProcess()
    p = _PMC()
    p.cb = ctypes.sizeof(_PMC)
    _psapi.GetProcessMemoryInfo(h, ctypes.byref(p), p.cb)
    hc = ctypes.c_ulong()
    _k32.GetProcessHandleCount(h, ctypes.byref(hc))
    return {"threads": n, "ws_mb": p.WorkingSetSize / 1048576.0,
            "commit_mb": p.PagefileUsage / 1048576.0, "handles": hc.value}


# ---------------------------------------------------------------- 素材工具
def count_player(pl, secs, sample_std=10):
    """廉价统计出帧：只比较 `current()` 对象身份（不能每帧跑 np.std，那会把解码线程饿死）。
    非黑只**抽样**测（稀疏切片），最多 sample_std 次。返回 (帧数, fps, 抽样std列表)。"""
    import numpy as np
    last = pl.current()
    n = 0
    stds = []
    t0 = time.perf_counter()
    next_sample = t0
    while time.perf_counter() - t0 < secs:
        img = pl.current()
        if img is not None and img is not last:
            last = img
            n += 1
            now = time.perf_counter()
            if len(stds) < sample_std and now >= next_sample:
                bpl = img.bytesPerLine()
                a = np.frombuffer(img.bits(), dtype=np.uint8, count=bpl * img.height())
                stds.append(float(a[::64].std()))       # 稀疏抽样，代价极低
                next_sample = now + 0.1
        time.sleep(0.003)
    dt = time.perf_counter() - t0
    return n, (n / dt if dt else 0.0), stds


def _dxv_files():
    """按 (fmt, w, h) 列出素材库里所有 DXV，返回 [(path, fmt, w, h)]。"""
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
                try:
                    pk = next(c.demux(st))
                    hd = dxvnative.parse_header(bytes(pk))
                    fmt = None if hd is None else int(hd[0])
                except Exception:
                    fmt = "?"
            finally:
                c.close()
        except Exception:
            continue
        out.append((p, fmt, w, h))
    return out


def _frames(path, want=6):
    """取该素材前 want 帧的 (fmt, blocks)。"""
    import av
    import dxvnative
    c = av.open(path, metadata_errors="replace")
    st = c.streams.video[0]
    w, h = int(st.width), int(st.height)
    got = []
    try:
        for p in c.demux(st):
            if not p.size:
                continue
            r = dxvnative.frame_blocks(bytes(p), w, h)
            if r is None:
                continue
            got.append((int(r[0]), r[1]))
            if len(got) >= want:
                break
    finally:
        c.close()
    return w, h, got


def _pick(fmt_want, min_px=1.9e6, exclude=()):
    for p, fmt, w, h in _dxv_files():
        if fmt == fmt_want and w * h >= min_px and p not in exclude:
            return p, fmt, w, h
    return None


# ================================================================ 用例实现
def case_grep():
    w("=" * 94)
    w("用例 grep：静态复核 create/destroy 调用者（不信工程师结论，自己跑）")
    w("=" * 94)
    pat = re.compile(r"GLDecoder\(|\.destroy\s*\(\)|\.create_window\s*\(|"
                     r"\.destroy_window\s*\(|glfwTerminate|_drop_dec|\._dec\b")
    hits = {"src": [], "tools": []}
    for area, base in (("src", SRC), ("tools", os.path.join(ROOT, "tools"))):
        for dirpath, _, files in os.walk(base):
            if "__pycache__" in dirpath:
                continue
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    with open(fp, encoding="utf-8", errors="replace") as fh:
                        for ln, line in enumerate(fh, 1):
                            if pat.search(line):
                                hits[area].append((os.path.relpath(fp, ROOT), ln,
                                                   line.strip()[:110]))
                except Exception:
                    pass
    for area in ("src", "tools"):
        w("")
        w("---- %s/ ---- (%d 处，含注释/docstring)" % (area, len(hits[area])))
        for rel, ln, txt in hits[area]:
            w("  %s:%d  %s" % (rel, ln, txt))

    # 结论式断言：用 AST 只看**真实代码调用**（排除注释/docstring 误报）
    import ast
    real = {}
    for area, base in (("src", SRC), ("tools", os.path.join(ROOT, "tools"))):
        acc = {"destroy": [], "create_window": [], "destroy_window": [], "gl_decoder": []}
        for dirpath, _, files in os.walk(base):
            if "__pycache__" in dirpath:
                continue
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    tree = ast.parse(open(fp, encoding="utf-8", errors="replace").read())
                except Exception:
                    continue
                for n in ast.walk(tree):
                    if isinstance(n, ast.Call):
                        f = n.func
                        if isinstance(f, ast.Attribute):
                            key = {"destroy": "destroy",
                                   "create_window": "create_window",
                                   "destroy_window": "destroy_window"}.get(f.attr)
                            if key:
                                acc[key].append((os.path.relpath(fp, ROOT), n.lineno))
                        elif isinstance(f, ast.Name) and f.id == "GLDecoder":
                            acc["gl_decoder"].append((os.path.relpath(fp, ROOT), n.lineno))
        real[area] = acc

    s = real["src"]
    w("")
    w("  [AST 真实代码调用] src/  GLDecoder()=%s  create_window=%s  destroy_window=%s  .destroy()=%s"
      % (s["gl_decoder"], s["create_window"], s["destroy_window"], s["destroy"]))
    chk(len(s["gl_decoder"]) == 1 and s["gl_decoder"][0] == ("src\\glctx.py", 742),
        "src 中唯一构造 GLDecoder 之处 = glctx.py:742（GLWorker._run 内），实际 %s"
        % (s["gl_decoder"],))
    chk(len(s["create_window"]) == 2 and all(h[0] == "src\\glctx.py" for h in s["create_window"]),
        "src 中 create_window 仅在 glctx.py 的 _init（2 处），实际 %s" % (s["create_window"],))
    chk(len(s["destroy_window"]) == 1 and s["destroy_window"][0][0] == "src\\glctx.py",
        "src 中 destroy_window 仅在 glctx.py 的 destroy()（1 处），实际 %s" % (s["destroy_window"],))
    chk(not s["destroy"],
        "src 中**无任何** `.destroy()` 真实调用者（destroy() 已是死代码），残留 %s" % (s["destroy"],))
    other = [h for h in s["gl_decoder"] + s["create_window"] + s["destroy_window"]
             if not h[0].endswith("glctx.py")]
    chk(not other, "src 中除 glctx.py 外无任何 create/destroy（残留 %s）" % (other,))
    t = real["tools"]
    w("  [AST 真实代码调用] tools/ GLDecoder()=%s  glfw.create_window=%s  glfw.destroy_window=%s  .destroy()=%s"
      % ([h[0] for h in t["gl_decoder"]], [h[0] for h in t["create_window"]],
         [h[0] for h in t["destroy_window"]], [h[0] for h in t["destroy"]]))
    w("    这些是**独立诊断脚本**（不在软件运行路径上）；单独运行仍会走旧 GLDecoder/destroy 路径。")


def case_correctness():
    import numpy as np
    import glctx
    w("=" * 94)
    w("用例 correctness：多线程并发解码 —— 结果不串台、尺寸/格式正确")
    w("=" * 94)
    if not glctx.preinit():
        chk(False, "glfw 不可用，无法测")
        return
    worker = glctx.GLWorker.get()
    if not chk(worker.ensure(), "worker 就绪（%s）" % worker.last_error):
        return

    s5 = _pick(5)
    s1 = _pick(1)
    chk(s5 is not None, "找到 DXT5 素材")
    chk(s1 is not None, "找到 DXT1 素材")
    if not (s5 and s1):
        return
    w5, h5, f5 = _frames(s5[0], 4)
    w1, h1, f1 = _frames(s1[0], 4)
    w("  源A %s %dx%d fmt=%s 样本%d帧" % (os.path.basename(s5[0]), w5, h5, s5[1], len(f5)))
    w("  源B %s %dx%d fmt=%s 样本%d帧" % (os.path.basename(s1[0]), w1, h1, s1[1], len(f1)))

    # 请求组合：不同源、不同算块、**不同目标尺寸**（专门验 FBO 尺寸/纹理缓存串台）
    sizes = [(1280, 720), (640, 360), (960, 540), (426, 240), (1920, 1080)]
    combos = []
    for i, (fmt, blk) in enumerate(f5):
        combos.append((blk, w5, h5, fmt, sizes[i % len(sizes)]))
    for i, (fmt, blk) in enumerate(f1):
        combos.append((blk, w1, h1, fmt, sizes[(i + 2) % len(sizes)]))

    # 1) 串行求"真值"（确保后续比对基准干净）
    refs = []
    for blk, sw, sh, fmt, (dw, dh) in combos:
        a = worker.decode(blk, sw, sh, fmt, dw, dh, bgra=True)
        refs.append(a)
    ok_ref = sum(1 for a in refs if a is not None)
    chk(ok_ref == len(combos), "串行基准全部成功（%d/%d）" % (ok_ref, len(combos)))
    chk(all(a.shape == (c[4][1], c[4][0], 4) and a.dtype == np.uint8
            for a, c in zip(refs, combos) if a is not None),
        "基准形状 = (dst_h,dst_w,4) uint8")

    # 2) 并发压测：K 个线程各跑多轮，每轮随机取一个组合，比对自身基准
    K, ROUND = 12, 40
    bad_shape, bad_pix, none_cnt = [0], [0], [0]
    lock = threading.Lock()
    errs = []

    def worker_thread(seed):
        import random as _r
        rng = _r.Random(seed)
        for _ in range(ROUND):
            i = rng.randrange(len(combos))
            blk, sw, sh, fmt, (dw, dh) = combos[i]
            got = worker.decode(blk, sw, sh, fmt, dw, dh, bgra=True)
            if got is None:
                with lock:
                    none_cnt[0] += 1
                continue
            ref = refs[i]
            if got.shape != ref.shape or got.dtype != np.uint8:
                with lock:
                    bad_shape[0] += 1
                    errs.append("组合%d 形状 %s ≠ 期望 %s" % (i, got.shape, ref.shape))
                continue
            if not np.array_equal(got, ref):
                mdiff = int(np.abs(got.astype(np.int16) - ref.astype(np.int16)).max())
                with lock:
                    if mdiff > 2:
                        bad_pix[0] += 1
                        errs.append("组合%d 像素不一致 maxdiff=%d" % (i, mdiff))
                    else:
                        errs.append("组合%d 微小偏差 maxdiff=%d（可接受）" % (i, mdiff))
    ths = [threading.Thread(target=worker_thread, args=(k,)) for k in range(K)]
    t0 = time.perf_counter()
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    dt = time.perf_counter() - t0
    total = K * ROUND
    w("")
    w("  %d 线程 × %d 次 = %d 次并发解码，耗时 %.2fs（%.1f 次/s）"
      % (K, ROUND, total, dt, total / dt))
    w("  None=%d  形状错=%d  像素串台=%d" % (none_cnt[0], bad_shape[0], bad_pix[0]))
    chk(none_cnt[0] == 0, "并发请求无一失败（None=%d）" % none_cnt[0])
    chk(bad_shape[0] == 0, "无一返回错误尺寸（=A 拿到 B 的结果）")
    chk(bad_pix[0] == 0, "无一像素串台（每帧与自己基准一致）")
    for e in errs[:8]:
        w("      · %s" % e)
    w("  worker stats = %s" % worker.stats())


def case_concurrent_ensure():
    import glctx
    w("=" * 94)
    w("用例 concurrent_ensure：多个图层**同时**首次 ensure() 的竞态")
    w("=" * 94)
    if not glctx.preinit():
        chk(False, "glfw 不可用")
        return
    glctx.GLWorker._inst = None
    N = 8
    res = [None] * N
    barrier = threading.Barrier(N)

    def job(i):
        barrier.wait()                       # 尽量同一瞬间冲进去
        wk = glctx.GLWorker.get()
        res[i] = (wk.ensure(), id(wk), wk.last_error)

    ths = [threading.Thread(target=job, args=(i,)) for i in range(N)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    oks = [r[0] for r in res]
    ids = {r[1] for r in res}
    w("  8 个并发 ensure() 结果 = %s" % oks)
    chk(all(oks), "全部返回 True")
    chk(len(ids) == 1, "全部拿到**同一个**单例（%d 个不同实例）" % len(ids))
    wk = glctx.GLWorker.get()
    chk(wk._thread is not None and wk._thread.is_alive(), "工作线程存活")
    glw = [t.name for t in threading.enumerate() if t.name == "gl-worker"]
    chk(len(glw) == 1, "threading 里只有 1 个 gl-worker（实际 %d）" % len(glw))
    # 就绪后能真正解码
    s5 = _pick(5)
    if s5:
        sw, sh, fr = _frames(s5[0], 1)
        a = wk.decode(fr[0][1], sw, sh, fr[0][0], 640, 360, bgra=True)
        chk(a is not None, "并发启动后仍能正常解码")


def case_preflight():
    import glctx
    w("=" * 94)
    w("用例 preflight：未就绪即 decode()；ensure() 幂等")
    w("=" * 94)
    if not glctx.preinit():
        chk(False, "glfw 不可用")
        return
    glctx.GLWorker._inst = None
    wk = glctx.GLWorker.get()
    # 尚未 ensure：decode 必须**立即**返回 None（不能等、不能起线程）
    t0 = time.perf_counter()
    r = wk.decode(b"\x00" * 4096, 64, 64, 1, 64, 64, bgra=True)
    d = time.perf_counter() - t0
    w("  未 ensure 时 decode() → %r，耗时 %.4f s；available=%s，stats=%s"
      % (r, d, wk.available(), wk.stats()))
    chk(r is None, "未就绪时 decode() 返回 None")
    chk(d < 0.05, "未就绪时 decode() 零等待（%.4fs）" % d)
    chk(wk.available() is False, "未就绪时 available() == False")
    chk(wk._thread is None, "未就绪时没有偷偷起线程")
    # ensure 幂等
    r1 = wk.ensure()
    r2 = wk.ensure()
    r3 = wk.ensure()
    w("  ensure() 连调 3 次 → %s %s %s" % (r1, r2, r3))
    chk(r1 and r2 and r3, "ensure() 幂等且都成功")
    glw = [t.name for t in threading.enumerate() if t.name == "gl-worker"]
    chk(len(glw) == 1, "只起了 1 个工作线程（%d）" % len(glw))


def case_okfalse():
    import glctx
    w("=" * 94)
    w("用例 okfalse：dec.ok 中途变 False（真实慢回读路径）⇒ available() 立即转 False")
    w("=" * 94)
    if not glctx.preinit():
        chk(False, "glfw 不可用")
        return
    wk = glctx.GLWorker.get()
    if not chk(wk.ensure(), "worker 就绪"):
        return
    s5 = _pick(5)
    if not s5:
        chk(False, "无 DXT5 素材")
        return
    sw, sh, fr = _frames(s5[0], 2)
    a = wk.decode(fr[0][1], sw, sh, fr[0][0], 640, 360, bgra=True)
    chk(a is not None and wk.available(), "初始状态：能解码且 available=True")

    # ① 直接翻 ok：验证 available()/decode() 立刻反应
    dec = wk._dec
    saved = dec.ok
    dec.ok = False
    chk(wk.available() is False, "① dec.ok=False → available() 立即 False")
    t0 = time.perf_counter()
    r = wk.decode(fr[0][1], sw, sh, fr[0][0], 640, 360, bgra=True)
    chk(r is None and (time.perf_counter() - t0) < 0.05,
        "① dec.ok=False → decode() 零等待返回 None")
    chk(bool(wk.last_message()), "① last_message() 有可读原因：%r" % wk.last_message())
    dec.ok = saved
    chk(wk.available() is True, "① 恢复 ok 后 available() 回到 True")

    # ② 走**真实**降级触发（GLDecoder 自己的连续慢回读判定）
    import media_manager as mm  # noqa
    old_ms, old_lim = glctx.SLOW_READBACK_MS, glctx.SLOW_LIMIT
    glctx.SLOW_READBACK_MS, glctx.SLOW_LIMIT = 0.0, 1     # 任何回读都算"慢"，1 次即降级
    try:
        r2 = wk.decode(fr[1][1], sw, sh, fr[1][0], 640, 360, bgra=True)
        w("  ② 触发真实慢回读降级：decode → %r；dec.err=%r" % (r2, getattr(dec, "err", "")))
        chk(r2 is None, "② 慢回读超限 → decode 返回 None（触发 GLDecoder 自降级）")
        chk(dec.ok is False, "② GLDecoder.ok 已被置 False")
        chk(wk.available() is False, "② worker.available() 立刻转 False（上层会切软解）")
        t0 = time.perf_counter()
        r3 = wk.decode(fr[1][1], sw, sh, fr[1][0], 640, 360, bgra=True)
        chk(r3 is None and (time.perf_counter() - t0) < 0.05,
            "② 之后 decode() 零等待返回 None")
        # ③ 端到端：真实 GpuDxvPlayer 在此状态下应**转软解且继续出帧**
        mm.set_gpu_decode(True)
        import av
        pl = mm.GpuDxvPlayer(s5[0], av, out_alpha=True)
        pl.set_active(True)
        frames, fps, stds = count_player(pl, 2.5)
        nonblack = sum(1 for s in stds if s > 3.0)
        gc = getattr(pl, "gpu", None)
        rs = getattr(pl, "reason", "")
        pl.close()
        w("  ③ GpuDxvPlayer：gpu=%s 出帧=%d（%.1ffps）非黑=%d/%d reason=%s"
          % (gc, frames, fps, nonblack, len(stds), rs))
        chk(gc is False, "③ 上层识别到 GPU 失效并转软解（gpu=False）")
        chk(frames > 40, "③ 转软解后**持续出帧**（%d 帧 / %.1ffps，画面未冻）" % (frames, fps))
        chk(nonblack >= max(1, len(stds) * 0.8) if stds else False, "③ 画面非黑（%d/%d）"
            % (nonblack, len(stds)))
    finally:
        glctx.SLOW_READBACK_MS, glctx.SLOW_LIMIT = old_ms, old_lim


def case_hang_e2e():
    import numpy as np
    import glctx
    import media_manager as mm
    import av
    w("=" * 94)
    w("用例 hang_e2e：注入 worker 内部挂死 ⇒ 真实 GpuDxvPlayer 端到端降级并继续出帧")
    w("=" * 94)
    if not glctx.preinit():
        chk(False, "glfw 不可用")
        return
    wk = glctx.GLWorker.get()
    if not chk(wk.ensure(), "worker 就绪"):
        return
    s5 = _pick(5)
    if not s5:
        chk(False, "无 DXV 素材")
        return
    mm.set_gpu_decode(True)
    mm._GPU_DISABLED = ""
    pl = mm.GpuDxvPlayer(s5[0], av, out_alpha=True)
    pl.set_active(True)

    # 1) 先确认它在走 GPU 且正常出帧
    frames, fps, _ = count_player(pl, 2.0)
    w("  ① 注入前：gpu=%s 出帧=%d（%.1ffps）reason=%s" % (pl.gpu, frames, fps, pl.reason))
    chk(pl.gpu is True, "注入前走 GPU 路径")
    chk(frames > 40, "注入前持续出帧（%d 帧 / %.1ffps）" % (frames, fps))

    # 2) 注入挂死：把 worker 的解码器 decode 换成永不返回
    w("  ② 注入挂死（worker._dec.decode → sleep(9999)，模拟驱动永久卡住）")
    wk._dec.decode = lambda *a, **k: time.sleep(9999)
    t_inject = time.perf_counter()

    # 3) 轮询直到切软解，记录切换耗时
    t_switch = None
    while time.perf_counter() - t_inject < 8.0:
        if pl.gpu is False and pl.reason:
            t_switch = time.perf_counter() - t_inject
            break
        time.sleep(0.02)
    w("  ③ 切换耗时 = %s；切换后 gpu=%s reason=%s"
      % ("%.2fs" % t_switch if t_switch else "未切换(!)", pl.gpu, pl.reason))
    chk(t_switch is not None, "在超时后确实切到软解")
    if t_switch is not None:
        chk(1.3 <= t_switch <= 3.0, "切换耗时 ≈ decode_timeout(1.5s)+余量（%.2fs）" % t_switch)
    chk(wk.available() is False, "worker 被标记 dead（available=False）")

    # 4) 切换后必须**继续出帧**（画面不能冻）
    after, afps, stds = count_player(pl, 2.5)
    nonblack = sum(1 for s in stds if s > 3.0)
    w("  ④ 切换后 2.5s 内出帧=%d（%.1ffps）非黑=%d/%d" % (after, afps, nonblack, len(stds)))
    chk(after > 40, "切换后**继续出帧**（%d 帧 / %.1ffps，未冻死）" % (after, afps))
    chk(nonblack >= max(1, len(stds) * 0.8) if stds else False,
        "切换后画面非黑（%d/%d）" % (nonblack, len(stds)))

    # 5) 之后所有请求零等待
    t0 = time.perf_counter()
    for _ in range(20):
        wk.decode(b"\x00" * 4096, 64, 64, 1, 64, 64, bgra=True)
    w("  ⑤ 挂死后 20 次请求共耗时 %.4fs；stats=%s" % (time.perf_counter() - t0, wk.stats()))
    chk(time.perf_counter() - t0 < 0.2, "挂死后请求零等待（总 %.4fs）" % (time.perf_counter() - t0))
    pl.close()


def case_startup_fail():
    import glctx
    import media_manager as mm
    import av
    import numpy as np
    w("=" * 94)
    w("用例 startup_fail：拿不到 GL 上下文 ⇒ 优雅降级、reason 说清、不崩不黑屏")
    w("=" * 94)
    if not glctx.preinit():
        w("  （glfw 本身可用；本用例故意让 GLDecoder 构造失败）")

    class _FailDecoder:
        def __init__(self):
            self.ok = False
            self.err = "模拟：创建离屏 GL 上下文失败（驱动不可用）"

        def decode(self, *a, **k):
            return None

        def destroy(self):
            return False

    glctx.GLWorker._inst = None
    glctx.GLDecoder = _FailDecoder        # 让工作线程"建不出上下文"
    wk = glctx.GLWorker.get()
    ok = wk.ensure()
    w("  ensure()=%s  available=%s  last_error=%r" % (ok, wk.available(), wk.last_error))
    chk(ok is False, "ensure() 返回 False（上下文建不起来）")
    chk(wk.available() is False, "available()=False")
    chk(bool(wk.last_error), "last_error 说清原因：%r" % wk.last_error)

    mm._GPU_DISABLED = ""
    mm._GPU_STATUS = None
    st = mm.gpu_status()
    w("  gpu_status()=%r" % (st,))
    chk(st[0] is False and bool(st[1]), "gpu_status() 返回 (False, 说明)：%r" % (st[1],))

    mm._GPU_STATUS = None
    s5 = _pick(5)
    if s5:
        mm._GPU_DISABLED = ""
        mm.set_gpu_decode(True)
        pl = mm.GpuDxvPlayer(s5[0], av, out_alpha=True)
        pl.set_active(True)
        frames, fps, stds = count_player(pl, 2.5)
        nonblack = sum(1 for s in stds if s > 3.0)
        w("  GpuDxvPlayer：gpu=%s 出帧=%d（%.1ffps）非黑=%d/%d reason=%s"
          % (pl.gpu, frames, fps, nonblack, len(stds), pl.reason))
        chk(pl.gpu is False, "降级到软解（gpu=False）")
        chk(frames > 40, "软解持续出帧（%d 帧 / %.1ffps）" % (frames, fps))
        chk(nonblack >= max(1, len(stds) * 0.8) if stds else False,
            "画面非黑屏（%d/%d）" % (nonblack, len(stds)))
        pl.close()
    w("  → 进程存活到用例结束（未崩溃）")


def case_multitrack():
    import glctx
    import media_manager as mm
    import av
    import numpy as np
    w("=" * 94)
    w("用例 multitrack：真实多图层并发 —— 持续出帧、线程/内存不增长、运行期零 create/destroy")
    w("=" * 94)

    def qstd(img):
        bpl = img.bytesPerLine()
        a = np.frombuffer(img.bits(), dtype=np.uint8, count=bpl * img.height())
        return float(a.std())

    if not glctx.preinit():
        chk(False, "glfw 不可用")
        return
    wk = glctx.GLWorker.get()
    if not chk(wk.ensure(), "worker 就绪（%s）" % wk.last_error):
        return

    # 选 N 个不同的真实 DXV 素材（混 DXT1/DXT5）
    files = [p for p, fmt, ww, hh in _dxv_files()
             if fmt in (1, 5) and ww * hh >= 1.9e6]
    N = int(os.environ.get("QA_LAYERS", "5"))
    T = float(os.environ.get("QA_SECS", "35"))
    if len(files) < N:
        chk(False, "素材不足（%d < %d）" % (len(files), N))
        return
    picks = files[:N]

    # 量单次 1080p→1080p 解码延迟（解释单 worker 串行化的吞吐上限）
    _sw, _sh, _fr = _frames(picks[0], 3)
    for _ in range(10):
        wk.decode(_fr[0][1], _sw, _sh, _fr[0][0], _sw, _sh, bgra=True)
    _t = time.perf_counter()
    _K = 40
    for i in range(_K):
        fmt, blk = _fr[i % len(_fr)]
        wk.decode(blk, _sw, _sh, fmt, _sw, _sh, bgra=True)
    _ms = (time.perf_counter() - _t) / _K * 1000
    w("  单次 %dx%d→%dx%d 解码 ≈ %.2f ms → 单 worker 上限 %.0f 帧/s（%d 层平分≈%.0f fps/层）"
      % (_sw, _sh, _sw, _sh, _ms, 1000 / _ms, N, 1000 / _ms / N))

    # ★ 直接在 glfw 上装计数器：运行期只要有一次 create/destroy 就说明架构没落地
    import glfw
    counters = {"create": 0, "destroy": 0}
    real_create, real_destroy = glfw.create_window, glfw.destroy_window

    def c_create(*a, **k):
        counters["create"] += 1
        return real_create(*a, **k)

    def c_destroy(*a, **k):
        counters["destroy"] += 1
        return real_destroy(*a, **k)

    glfw.create_window = c_create
    glfw.destroy_window = c_destroy

    try:
        mm.set_gpu_decode(True)
        mm._GPU_DISABLED = "QA: forced soft" if os.environ.get("QA_SOFT") else ""
        _mw = int(os.environ.get("QA_MAXW", "0"))
        _mh = int(os.environ.get("QA_MAXH", "0"))
        players = []
        for p in picks:
            pl = mm.GpuDxvPlayer(p, av, out_alpha=True)
            if _mw and _mh:
                pl.set_max_size(_mw, _mh)
            pl.set_active(True)
            players.append(pl)
        if _mw and _mh:
            w("  （已 set_max_size(%d,%d) —— 模拟输出画布比源小）" % (_mw, _mh))
        time.sleep(1.0)
        # 记每层初始帧
        prev = [pl.current() for pl in players]
        cnt = [0] * N
        w("  %d 图层：%s" % (N, [os.path.basename(x)[:18] for x in picks]))
        w("  GPU 判定：%s%s" % ([pl.gpu for pl in players],
                                "（对照组：强制软解）" if os.environ.get("QA_SOFT") else ""))
        if not os.environ.get("QA_SOFT"):
            chk(all(pl.gpu for pl in players), "全部图层走 GPU 路径")

        base = snap()
        w("  基线(运行时)：线程 %4d  工作集 %6.1f MB  提交 %6.1f MB  句柄 %d"
          % (base["threads"], base["ws_mb"], base["commit_mb"], base["handles"]))
        samples = [base]
        fps_curve = []                     # 每个 1s 桶里各层的真实出帧数
        t0 = time.perf_counter()
        last_t = t0
        next_mark = t0 + 1.0
        bucket = [0] * N
        max_gap = 0.0
        n_ok0 = wk.n_ok
        while time.perf_counter() - t0 < T:
            time.sleep(0.003)             # 紧凑循环：只比对象身份（代价极低）
            for k, pl in enumerate(players):
                img = pl.current()
                if img is not None and img is not prev[k]:
                    prev[k] = img
                    cnt[k] += 1
                    bucket[k] += 1
            now = time.perf_counter()
            if now >= next_mark:
                max_gap = max(max_gap, now - last_t)
                last_t = now
                fps_curve.append(list(bucket))
                bucket = [0] * N
                samples.append(snap())
                next_mark += 1.0
        n_ok_total = wk.n_ok - n_ok0
        run_dt = time.perf_counter() - t0
        # 末段 2s 单独量"是否仍在推进"
        tail = [0] * N
        tailprev = [pl.current() for pl in players]
        te = time.perf_counter()
        while time.perf_counter() - te < 2.0:
            for k, pl in enumerate(players):
                img = pl.current()
                if img is not None and img is not tailprev[k]:
                    tailprev[k] = img
                    tail[k] += 1
            time.sleep(0.003)
        samples.append(snap())
        # 各层真实 fps = 稳态桶（去掉前 2s 启动桶）中位数
        steady = fps_curve[2:] if len(fps_curve) > 3 else fps_curve
        per_layer_fps = [float(np.median([b[k] for b in steady])) if steady else 0.0
                         for k in range(N)]
        w("  worker 期间完成解码 = %d 次（≈%.0f 次/s）" % (n_ok_total, n_ok_total / run_dt))
    finally:
        for pl in players:
            pl.close()
        glfw.create_window = real_create
        glfw.destroy_window = real_destroy

    w("")
    w("  运行 %.1fs；稳态各层 fps（1s 桶中位数）= %s"
      % (run_dt, ["%.1f" % f for f in per_layer_fps]))
    w("  每秒各层出帧曲线（前 2 桶含启动）：")
    for i, b in enumerate(fps_curve[:6]):
        w("      t=%ds: %s" % (i + 1, b))
    w("  末段 2s 每层出帧：%s（≈%s fps）"
      % (tail, ["%.1f" % (x / 2.0) for x in tail]))
    w("  采样最大间隔 %.2fs（>2s 说明采样线程被 GIL 阻塞过）" % max_gap)
    ths = [s["threads"] for s in samples]
    wss = [s["ws_mb"] for s in samples]
    cms = [s["commit_mb"] for s in samples]
    w("  线程曲线：%s" % ths)
    w("  工作集 MB：%s" % ["%.0f" % x for x in wss])
    w("  提交  MB：%s" % ["%.0f" % x for x in cms])
    w("")
    w("  ★ 运行期 glfw.create_window 调用 = %d 次；glfw.destroy_window 调用 = %d 次"
      % (counters["create"], counters["destroy"]))
    w("  worker stats = %s" % wk.stats())

    chk(all(f >= 15 for f in per_layer_fps), "每层稳定出帧 ≥15fps（%s）"
        % ["%.1f" % f for f in per_layer_fps])
    below = [i for i, f in enumerate(per_layer_fps) if f < 28]
    if below:
        w("  ⚠ 注意：层 %s 稳态 <28fps（单 worker 串行化的吞吐上限，非冻结）"
          % below)
    chk(all(x > 5 for x in tail), "末段仍在推进（未卡死）：%s" % tail)
    chk(max_gap < 2.5, "无长时间卡顿（最大采样间隔 %.2fs）" % max_gap)
    growth = ths[-1] - ths[0]
    chk(growth < 40, "线程不随时间增长（%d 轮后净增 %+d，从 %d → %d）"
        % (len(ths) - 1, growth, ths[0], ths[-1]))
    chk(max(ths) - min(ths) < 60, "线程波动受控（峰值-谷值 = %d）" % (max(ths) - min(ths)))
    mem_growth = wss[-1] - wss[0]
    chk(mem_growth < 200, "工作集不增长（净增 %+.1f MB）" % mem_growth)
    chk(counters["create"] == 0 and counters["destroy"] == 0,
        "★ 运行期**零** create/destroy（架构目标达成）")


def case_regression():
    w("=" * 94)
    w("用例 regression：开关关闭走原路径、反复切换稳定、无残留旧接口")
    w("=" * 94)
    import media_manager as mm

    class FakeItem:
        def __init__(self, is_dxv, has_alpha=True):
            self.path = r"X:\nonexist.mov"
            self._has_alpha = has_alpha
            self._is_dxv = is_dxv
            self.kind = "video"

    # 1) 开关关闭 → 绝不返回 GpuDxvPlayer；带 alpha→AvAlphaPlayer，无 alpha→VideoPlayer
    mm.set_gpu_decode(False)
    cases = [(True, True, "AvAlphaPlayer"), (False, True, "AvAlphaPlayer"),
             (False, False, "VideoPlayer"), (True, False, "VideoPlayer")]
    for is_dxv, has, want in cases:
        pl = mm.create_video_player(FakeItem(is_dxv, has_alpha=has))
        got = type(pl).__name__
        chk(got == want, "关闭开关：is_dxv=%s alpha=%s → %s（期望 %s）"
            % (is_dxv, has, got, want))
        pl.close()

    # 2) 开关打开 → DXV 走 GpuDxvPlayer（判定入口不受影响）
    mm.set_gpu_decode(True)
    pl = mm.create_video_player(FakeItem(True))
    chk(type(pl).__name__ == "GpuDxvPlayer", "打开开关：DXV → GpuDxvPlayer")
    pl.close()
    mm.set_gpu_decode(False)

    # 3) 反复切换 20 次：不崩、判定稳定
    stable = True
    for i in range(20):
        mm.set_gpu_decode(bool(i % 2))
        want = "GpuDxvPlayer" if i % 2 else "AvAlphaPlayer"
        p = mm.create_video_player(FakeItem(True))
        if type(p).__name__ != want:
            stable = False
        p.close()
    chk(stable, "开关反复切换 20 次判定始终正确、无异常")
    mm.set_gpu_decode(False)

    # 4) 环境变量优先
    os.environ["AUTO_VJ_GPU"] = "0"
    chk(mm.gpu_decode_enabled() is False, "AUTO_VJ_GPU=0 覆盖设置（关）")
    mm.set_gpu_decode(True)
    chk(mm.gpu_decode_enabled() is False, "AUTO_VJ_GPU=0 优先于 set_gpu_decode(True)")
    os.environ["AUTO_VJ_GPU"] = "1"
    mm.set_gpu_decode(False)
    chk(mm.gpu_decode_enabled() is True, "AUTO_VJ_GPU=1 优先于 set_gpu_decode(False)")
    del os.environ["AUTO_VJ_GPU"]

    # 5) 无残留旧接口
    import inspect
    src = inspect.getsource(mm.GpuDxvPlayer)
    chk("_drop_dec" not in src, "GpuDxvPlayer 源码里无 _drop_dec 残留")
    chk("self._dec" not in src, "GpuDxvPlayer 源码里无 self._dec 残留")
    w("  GpuDxvPlayer 是否有 _drop_dec 属性：%s" % hasattr(mm.GpuDxvPlayer, "_drop_dec"))
    chk(not hasattr(mm.GpuDxvPlayer, "_drop_dec"), "类上无 _drop_dec 方法")


def case_throughput():
    import glctx
    import dxvnative
    import av
    w("=" * 94)
    w("用例 throughput：纯 worker.decode 的串行吞吐（复刻工程师口径，独立复现）")
    w("=" * 94)
    if not glctx.preinit():
        chk(False, "glfw 不可用")
        return
    wk = glctx.GLWorker.get()
    if not chk(wk.ensure(), "worker 就绪"):
        return
    s5 = _pick(5) or _pick(1)
    if not s5:
        chk(False, "无素材")
        return
    sw, sh, _ = _frames(s5[0], 1)
    # 提取 8 个 DXV 块
    c = av.open(s5[0], metadata_errors="replace")
    st = c.streams.video[0]
    blocks = []
    for pkt in c.demux(st):
        if not pkt.size:
            continue
        r = dxvnative.frame_blocks(bytes(pkt), sw, sh)
        if r is not None:
            blocks.append((int(r[0]), r[1]))
        if len(blocks) >= 8:
            break
    c.close()
    w("  素材 %s 源 %dx%d，块 %d 个" % (os.path.basename(s5[0]), sw, sh, len(blocks)))
    for (dw, dh) in ((1280, 720), (1920, 1080)):
        for _ in range(15):
            wk.decode(blocks[0][1], sw, sh, blocks[0][0], dw, dh, bgra=True)
        N = 150
        t0 = time.perf_counter()
        for i in range(N):
            fmt, blk = blocks[i % len(blocks)]
            if wk.decode(blk, sw, sh, fmt, dw, dh, bgra=True) is None:
                w("  ★ %dx%d 单线程解码失败" % (dw, dh))
                return
        single = (time.perf_counter() - t0) / N * 1000
        L, per = 4, 60
        latch = threading.Lock()
        lat = []

        def one(k):
            for i in range(per):
                fmt, blk = blocks[(i + k) % len(blocks)]
                t = time.perf_counter()
                wk.decode(blk, sw, sh, fmt, dw, dh, bgra=True)
                with latch:
                    lat.append(time.perf_counter() - t)
        ths = [threading.Thread(target=one, args=(k,)) for k in range(L)]
        t0 = time.perf_counter()
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        wall = time.perf_counter() - t0
        total = L * per
        w("  → %dx%d：单线程 %.3f ms/次（%.0f fps）；%d 路并发合计 %.0f 次/s，"
          "每路 %.1f fps（每次 %.2f ms，最大 %.2f ms）"
          % (dw, dh, single, 1000 / single, L, total / wall, per / wall,
             wall / per * 1000, max(lat) * 1000))
    chk(True, "吞吐已量（见上方数字）")


CASES = {
    "grep": case_grep,
    "throughput": case_throughput,
    "correctness": case_correctness,
    "concurrent_ensure": case_concurrent_ensure,
    "preflight": case_preflight,
    "okfalse": case_okfalse,
    "hang_e2e": case_hang_e2e,
    "startup_fail": case_startup_fail,
    "multitrack": case_multitrack,
    "regression": case_regression,
}

TIMEOUTS = {
    "grep": 60, "throughput": 120, "correctness": 180, "concurrent_ensure": 90, "preflight": 90,
    "okfalse": 180, "hang_e2e": 90, "startup_fail": 120,
    "multitrack": float(os.environ.get("QA_SECS", "35")) + 120, "regression": 90,
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
    p("AutoVJ GLWorker 架构 QA 独立验证   %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    p("python=%s" % PY)
    for nm in names:
        p("\n" + "#" * 94)
        p("### 子进程用例：%s（超时 %ds）" % (nm, int(TIMEOUTS[nm])))
        p("#" * 94)
        try:
            pr = subprocess.run([PY, os.path.abspath(__file__), "--one", nm],
                                capture_output=True, text=True,
                                timeout=TIMEOUTS[nm])
            out = pr.stdout
            tail = ""
            for line in out.splitlines():
                if line.startswith("##QA_JSON##"):
                    tail = line[len("##QA_JSON##"):]
            p(out)
            if pr.stderr.strip():
                p("--- stderr ---")
                p(pr.stderr[-3000:])
            if tail:
                results[nm] = json.loads(tail)
            else:
                results[nm] = {"case": nm, "passed": False, "n_fail": -1,
                               "fails": ["无结果 JSON（进程异常退出 rc=%s）" % pr.returncode],
                               "secs": None}
        except subprocess.TimeoutExpired as e:
            p("★★ 用例 %s 超时（%ds）—— 判定为 HANG/不通过" % (nm, int(TIMEOUTS[nm])))
            if e.stdout:
                p((e.stdout if isinstance(e.stdout, str) else e.stdout.decode("utf-8", "replace"))[-3000:])
            results[nm] = {"case": nm, "passed": False, "n_fail": -1,
                           "fails": ["★ 子进程超时/挂死（>%ds）" % int(TIMEOUTS[nm])],
                           "secs": None}

    p("\n" + "=" * 94)
    p("QA 汇总结论")
    p("=" * 94)
    allpass = True
    for nm in names:
        r = results.get(nm, {})
        flag = "通过" if r.get("passed") else "★不通过"
        if not r.get("passed"):
            allpass = False
        p("  %-18s %-8s 失败 %s 项  %s"
          % (nm, flag, r.get("n_fail"), ("%.1fs" % r["secs"]) if r.get("secs") else ""))
        for f in (r.get("fails") or [])[:6]:
            p("        · %s" % f)
    p("=" * 94)
    p("总判定：%s" % ("全部通过" if allpass else "存在不通过用例"))
    with open(os.path.join(ROOT, "_gl_worker_qa.json"), "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(ROOT, "_gl_worker_qa.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(_LOGF))
    p("→ 汇总已写入 _gl_worker_qa.json / _gl_worker_qa.txt")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--one" and sys.argv[2] in CASES:
        run_one(sys.argv[2])                    # 子进程内跑单个用例
    else:
        only = [a for a in sys.argv[1:] if a in CASES] or None
        orchestrate(only)
