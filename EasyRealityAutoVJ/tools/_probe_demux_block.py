# -*- coding: utf-8 -*-
"""机制探针：为什么 `_try_gpu` 用 `c.demux(st)` 消费首包后，`_soft_frames` 的
`c.decode(video=0)` 会**永久阻塞**？

在**同一个线程**里复现（与真实播放器一致），并用看门狗在 3s 后打印**主线程栈**，
从而看清阻塞点。每个变体独立进程跑（避免一个变体卡死挡住其它）。

用法：`python -u tools/_probe_demux_block.py <variant>`
变体：abandon / close / gc / seek0 / fresh
"""
import os
import sys
import threading
import time
import traceback

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SRC)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import av                                                          # noqa: E402

LIB = r"Y:\EasyRealityAutoVJ\素材库"
DEFAULT_NAME = "dusk oct ring.mov"


def find(name):
    for root, _d, files in os.walk(LIB):
        for f in files:
            if f.lower() == name.lower():
                return os.path.join(root, f)
    return None


def main():
    variant = sys.argv[1] if len(sys.argv) > 1 else "abandon"
    name = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_NAME
    path = find(name)
    print("变体=%s  文件=%s" % (variant, path))

    c = av.open(path, metadata_errors="replace")
    st = c.streams.video[0]
    print("  codec=%s pix_fmt=%s %dx%d" % (st.codec_context.name,
                                           st.codec_context.pix_fmt,
                                           st.codec_context.width,
                                           st.codec_context.height))
    if variant != "fresh":
        gen = c.demux(st)
        pk = None
        for p in gen:
            if p.size:
                pk = bytes(p)
                break
        print("  已 demux 首包 %s 字节（变体=%s）" % (len(pk) if pk else None, variant))
        if variant == "close":
            gen.close()
        elif variant == "gc":
            del gen
            import gc
            gc.collect()
        elif variant == "seek0":
            c.seek(0)

    # ---- 看门狗：3s 后若主线程还卡着，打印它的栈 ----
    main_tid = threading.main_thread().ident
    stop = threading.Event()

    def watchdog():
        if stop.wait(3.0):
            return
        print("  !! 3s 未出帧 —— 主线程栈：")
        fr = sys._current_frames().get(main_tid)
        if fr is not None:
            for line in traceback.format_stack(fr)[-8:]:
                sys.stdout.write("".join("      " + x for x in [line]))

    threading.Thread(target=watchdog, daemon=True).start()

    n = 0
    t0 = time.perf_counter()
    try:
        for _fr in c.decode(video=0):
            n += 1
            if n >= 5:
                break
    except Exception as e:                                          # noqa: BLE001
        print("  decode 抛异常: %s: %s" % (type(e).__name__, str(e)[:120]))
    stop.set()
    print("  结果：%d 帧，耗时 %.2fs %s" % (n, time.perf_counter() - t0,
                                          "" if n else "  ← **阻塞/无帧**"))
    try:
        c.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
