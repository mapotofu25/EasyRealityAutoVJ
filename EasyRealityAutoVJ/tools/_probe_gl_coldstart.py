# -*- coding: utf-8 -*-
"""取证：真实 GL 冷启动耗时（用来回答「为什么 3s 超时不够」）。

在**独立子进程**里跑（脚本自身就带硬超时，由外层 `timeout` 兜底）：
  · 直接构造 `GLDecoder()`（= GLWorker 工作线程里 `_run` 做的同一件事），
    量「从调用到 ok=True」的墙钟时间；
  · 分解出各段耗时：create_window / make_current / load_fns / build_program。
输出一行结论，供报告引用。不依赖 Qt。
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import glctx  # noqa: E402


def main():
    t0 = time.perf_counter()
    pre = glctx.preinit()
    t1 = time.perf_counter()
    print("preinit(): %s  用时 %.3fs" % (pre, t1 - t0))

    t2 = time.perf_counter()
    dec = glctx.GLDecoder()
    t3 = time.perf_counter()
    print("GLDecoder() 构造（含 create_window/make_current/编译着色器/VAO-VBO）: "
          "ok=%s  用时 %.3fs" % (dec.ok, t3 - t2))
    if not dec.ok:
        print("  err=%s" % getattr(dec, "err", ""))
    print("结论：真实冷启动总耗时 = %.3fs（preinit %.3f + 上下文 %.3f）"
          % (t3 - t0, t1 - t0, t3 - t2))
    # 顺带量一次"热"再建（若配额够）用于对照
    if dec.ok:
        t4 = time.perf_counter()
        dec.close()
        t5 = time.perf_counter()
        print("close() 用时 %.3fs" % (t5 - t4))


if __name__ == "__main__":
    main()
