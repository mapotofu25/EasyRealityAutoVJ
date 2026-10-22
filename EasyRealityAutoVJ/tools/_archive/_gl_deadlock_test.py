# -*- coding: utf-8 -*-
"""验证 GL 全局串行锁能否消除「解码 与 上下文销毁 并发」导致的驱动死锁。

背景（2026-09-26 现场）：11 个解码线程全部冻结在 GL 调用上
（6 个 glfwDestroyWindow / 4 个 glReadPixels / 1 个 _ensure_fbo），
逐次快照位置完全不变、CPU 持续烧 3.6 核、永久不恢复。
根因：`decode` 没走 GL 串行锁（只有 create/destroy 走了），与 destroy 并发。

用法：venv\\Scripts\\python.exe tools\\_gl_deadlock_test.py [秒数=12]
"""
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import glctx                                              # noqa: E402

W, H = 1920, 1080
FMT = 5
BLOCKS = bytes((W // 4) * (H // 4) * 16)

stop = threading.Event()
counts = {}


def worker(name, dec):
    n = 0
    while not stop.is_set():
        try:
            if dec.decode(BLOCKS, W, H, FMT, W, H, bgra=True) is not None:
                n += 1
        except Exception:
            pass
        counts[name] = n


def churn(name):
    """反复建/毁上下文 —— 制造「destroy 与 decode 并发」的最恶劣场景"""
    n = 0
    while not stop.is_set():
        try:
            d = glctx.GLDecoder()
            if d.ok:
                d.close()
                d.destroy()
                n += 1
            else:
                time.sleep(0.05)
        except Exception:
            time.sleep(0.05)
        counts[name] = n


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
    print("=" * 78)
    print("GL 串行锁 / 死锁验证   跑 %.0f 秒" % secs)
    print("=" * 78)
    print("preinit:", glctx.preinit())
    decs = []
    for i in range(3):
        d = glctx.GLDecoder()
        print("  解码器 %d: ok=%s err=%s" % (i, d.ok, (d.err or "-")[:60]))
        if d.ok:
            decs.append(d)
    if not decs:
        print("GL 上下文创建失败（GPU 可能被占满/驱动异常），无法验证")
        return 1
    for i, d in enumerate(decs):
        threading.Thread(target=worker, args=("decode%d" % i, d), daemon=True).start()
    threading.Thread(target=churn, args=("churn",), daemon=True).start()

    t0 = time.perf_counter()
    last = dict(counts)
    stalls = 0
    while time.perf_counter() - t0 < secs:
        time.sleep(2.0)
        print("  t=%4.1fs  %s" % (time.perf_counter() - t0,
              "  ".join("%s=%d" % (k, v) for k, v in sorted(counts.items()))))
        dead = [k for k, v in counts.items() if last.get(k) == v]
        if dead:
            stalls += 1
            print("     [!] 这两秒没有推进: %s" % ", ".join(dead))
        last = dict(counts)
    stop.set()
    time.sleep(0.5)
    print()
    print("最终计数: %s" % "  ".join("%s=%d" % (k, v) for k, v in sorted(counts.items())))
    ok = bool(counts) and all(v > 0 for v in counts.values()) and stalls == 0
    print()
    print("结论: %s" % ("OK 无死锁（所有线程持续推进）" if ok else "[!] 有卡顿，见上面的标记"))
    for d in decs:
        try:
            d.close()
            d.destroy()
        except Exception:
            pass
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
