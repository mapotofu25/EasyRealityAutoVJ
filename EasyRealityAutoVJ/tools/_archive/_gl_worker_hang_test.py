# -*- coding: utf-8 -*-
"""GLWorker 降级 + 冷却自恢复测试（R2 / 2026-09-26）

在 worker **内部注入"卡住不返回"**（用可控门闩的假解码器模拟驱动抖动/挂死，
**不真挂 GPU**、不需要 GL），验证：
  A. 一次超时 ⇒ 进入 degraded；后续请求**零等待**返回 None；
  B. 冷却（`RECOVER_AFTER` 临时调小到 2s）过后，探测**成功即自动恢复** GPU 解码；
  C. 探测**仍失败**时重新进入冷却；
  D. 全程**主线程/其他线程不受影响**（心跳线程证明）；
  E. 降级与恢复各写了日志（stallwatch.log_line，本测试重定向到临时目录）。

用法：venv\\Scripts\\python.exe tools\\_gl_worker_hang_test.py
"""
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

# —— 把 stallwatch 日志重定向到临时目录，**不碰用户的 %LOCALAPPDATA%\\AutoVJ\\ui_stall.log** ——
_LOGDIR = tempfile.mkdtemp(prefix="autovj_glw_log_")
os.environ["LOCALAPPDATA"] = _LOGDIR

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


class _GateDecoder:
    """可控"假解码器"：`gate` 未 set 时 decode() 阻塞（模拟驱动卡住），set 后立即返回。"""

    def __init__(self):
        self.ok = True
        self.err = ""
        self.gate = threading.Event()
        self.gate.set()
        self.calls = 0

    def decode(self, *a, **k):
        import numpy as np
        self.calls += 1
        self.gate.wait(5.0)          # 卡住时最多阻塞 5s（模拟抖动），不真挂 GPU
        return np.zeros((8, 8, 4), dtype=np.uint8)


def _log_text():
    p = os.path.join(_LOGDIR, "AutoVJ", "ui_stall.log")
    try:
        with open(p, encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return ""


def main():
    import glctx

    w("=" * 92)
    w("GLWorker 降级 + 冷却自恢复测试   %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    w("=" * 92)

    glctx.GLWorker._inst = None
    glctx.GLDecoder = _GateDecoder          # 工作线程内会构造它
    worker = glctx.GLWorker.get()
    if not chk(worker.ensure(), "GL 工作线程 + 唯一上下文就绪"):
        return
    worker.RECOVER_AFTER = 2.0              # 临时把冷却调到 2s（默认 60s）
    dec = worker._dec
    w("  RECOVER_AFTER = %.0fs（默认 60s，测试临时调小）" % worker.RECOVER_AFTER)

    # 心跳线程：证明"卡住的只是 worker"，其他线程照常推进
    beat = {"n": 0, "stop": False}

    def _hb():
        while not beat["stop"]:
            beat["n"] += 1
            time.sleep(0.005)

    hb = threading.Thread(target=_hb, name="heartbeat", daemon=True)
    hb.start()
    time.sleep(0.1)

    # ---------------- A. 一次超时 → degraded，后续零等待 ----------------
    w("")
    w("A) 注入卡死（gate 清零）→ 首次请求应 ~1.5s 超时并进入 degraded")
    dec.gate.clear()
    base = beat["n"]
    t0 = time.perf_counter()
    r1 = worker.decode(b"\x00" * 4096, 64, 64, 1, 64, 64, bgra=True)
    dt1 = time.perf_counter() - t0
    hb_during = beat["n"] - base
    w("   首次请求 → %r，耗时 %.3f s；期间心跳推进 %d 次" % (r1, dt1, hb_during))
    chk(r1 is None, "超时请求返回 None")
    chk(1.3 <= dt1 <= 2.1, "耗时 ≈ 1.5s 超时上限（实测 %.3fs）" % dt1)
    chk(worker.available() is False, "进入 degraded：available()=False")
    chk(hb_during > 100, "超时期间其他线程照常推进（心跳 %d 次）" % hb_during)

    n_none, worst = 0, 0.0
    for _ in range(20):
        t = time.perf_counter()
        r = worker.decode(b"\x00" * 4096, 64, 64, 1, 64, 64, bgra=True)
        worst = max(worst, time.perf_counter() - t)
        if r is None:
            n_none += 1
    w("   之后 20 次请求：None=%d/20，最慢 %.4f s" % (n_none, worst))
    chk(n_none == 20 and worst < 0.05, "降级期所有请求零等待返回 None（最慢 %.4fs）" % worst)

    # ---------------- B. 冷却后探测仍失败 → 重新进入冷却 ----------------
    w("")
    w("B) 冷却 2s 后探测（gate 仍清零，探测仍会失败）→ 应重新进入冷却")
    dec.gate.set()                          # 先放掉 worker 手里卡住的那次
    time.sleep(0.2)
    time.sleep(2.5)                         # 等过冷却（2s）
    avail_after_cd = worker.available()
    w("   冷却过后 available() = %s（应 True，放开一次探测机会）" % avail_after_cd)
    chk(avail_after_cd is True, "冷却过后允许重试（available()=True）")
    dec.gate.clear()                        # 让探测卡住
    t0 = time.perf_counter()
    r2 = worker.decode(b"\x00" * 4096, 64, 64, 1, 64, 64, bgra=True)
    dt2 = time.perf_counter() - t0
    w("   探测请求 → %r，耗时 %.3f s" % (r2, dt2))
    chk(r2 is None and 1.3 <= dt2 <= 2.1, "探测失败超时（%.3fs）" % dt2)
    chk(worker.available() is False, "探测失败 → 重新进入冷却（available()=False）")

    # ---------------- C. 冷却后探测成功 → 自动恢复 ----------------
    w("")
    w("C) 冷却 2s 后探测（gate 置位，解码恢复正常）→ 应自动恢复 GPU 解码")
    dec.gate.set()
    time.sleep(0.2)
    time.sleep(2.5)
    chk(worker.available() is True, "冷却过后 available()=True")
    t0 = time.perf_counter()
    r3 = worker.decode(b"\x00" * 4096, 64, 64, 1, 64, 64, bgra=True)
    dt3 = time.perf_counter() - t0
    w("   探测请求 → %s，耗时 %.3f s" % ("非 None（成功）" if r3 is not None else "None", dt3))
    chk(r3 is not None, "探测成功返回结果（自动恢复）")
    chk(worker.available() is True, "恢复后 available()=True（GPU 解码已回来）")
    t = time.perf_counter()
    r4 = worker.decode(b"\x00" * 4096, 64, 64, 1, 64, 64, bgra=True)
    chk(r4 is not None and (time.perf_counter() - t) < 0.5, "恢复后正常快速解码")

    # ---------------- D. 主线程/其他线程不受影响 ----------------
    beat["stop"] = True
    hb.join(timeout=1.0)
    w("")
    w("D) 心跳线程全程推进 = %d 次（主线程/A、B、C 三段期间均未被拖住）" % beat["n"])
    chk(beat["n"] > 500, "心跳线程全程正常推进（%d 次）" % beat["n"])

    # ---------------- E. 日志：降级与恢复各一条 ----------------
    txt = _log_text()
    w("")
    w("E) stallwatch 日志（重定向到临时目录）内容：")
    for line in txt.splitlines():
        w("      " + line)
    chk("已临时停用 GPU 解码" in txt, "降级时有日志")
    chk("已自动恢复" in txt, "恢复时有日志")

    w("")
    w("worker stats = %s" % worker.stats())
    w("=" * 92)
    w("结果：%s" % ("全部通过" if not FAIL else "有 %d 项失败" % len(FAIL)))
    for f in FAIL:
        w("  ★ %s" % f)
    with open(os.path.join(ROOT, "_gl_worker_hang_test.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    w("→ 已写入 _gl_worker_hang_test.txt")


if __name__ == "__main__":
    main()
