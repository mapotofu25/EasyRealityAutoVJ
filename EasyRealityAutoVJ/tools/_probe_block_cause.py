# -*- coding: utf-8 -*-
"""机制探针 v2：到底是 `_open()` 的哪一步让后续 `decode()` 永久阻塞？

在与真实播放器**同样的线程模型**下（在独立 daemon 线程里 open→prep→decode），
逐步叠加 `_open()` 里的准备动作，二分定位。

用法：`python -u tools/_probe_block_cause.py <prep> [文件名]`
prep：none / demux / cap / demux_cap / cap_demux / parse
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
import media_manager as mm                                          # noqa: E402

LIB = r"Y:\EasyRealityAutoVJ\素材库"
DEFAULT_NAME = "dusk oct ring.mov"


def find(name):
    for root, _d, files in os.walk(LIB):
        for f in files:
            if f.lower() == name.lower():
                return os.path.join(root, f)
    return None


def main():
    prep = sys.argv[1] if len(sys.argv) > 1 else "none"
    name = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_NAME
    path = find(name)
    print("prep=%s  文件=%s" % (prep, path))

    res = {"n": 0, "t0": 0.0, "t1": 0.0}

    def job():
        c = av.open(path, metadata_errors="replace")
        st = c.streams.video[0]
        print("  codec=%s pix_fmt=%s %dx%d  thread_count=%s"
              % (st.codec_context.name, st.codec_context.pix_fmt,
                 st.codec_context.width, st.codec_context.height,
                 getattr(st.codec_context, "thread_count", "?")))
        if prep in ("demux", "demux_cap"):
            for p in c.demux(st):
                if p.size:
                    print("  demux 首包 %d 字节" % len(bytes(p)))
                    break
        if prep in ("cap", "demux_cap", "cap_demux"):
            mm._cap_decode_threads(st)
            print("  cap: thread_count=%s" % getattr(st.codec_context, "thread_count", "?"))
        if prep == "cap_demux":
            for p in c.demux(st):
                if p.size:
                    print("  demux 首包 %d 字节" % len(bytes(p)))
                    break
        if prep == "parse":
            for p in c.demux(st):
                if p.size:
                    pk = bytes(p)
                    break
            import dxvnative
            print("  parse_header → %r" % (dxvnative.parse_header(pk),))
        res["t0"] = time.perf_counter()
        try:
            for _fr in c.decode(video=0):
                res["n"] += 1
                if res["n"] >= 5:
                    break
        except Exception as e:                                      # noqa: BLE001
            print("  decode 异常: %s: %s" % (type(e).__name__, str(e)[:120]))
        res["t1"] = time.perf_counter()
        try:
            c.close()
        except Exception:
            pass

    th = threading.Thread(target=job, daemon=True, name="probe-dec")
    th.start()
    th.join(4.0)
    if th.is_alive():
        print("  **解码线程 4s 未结束 —— 栈：")
        for line in traceback.format_stack(sys._current_frames()[th.ident])[-6:]:
            sys.stdout.write("".join("      " + x for x in [line]))
        print("  结果：%d 帧（阻塞）" % res["n"])
    else:
        print("  结果：%d 帧，耗时 %.2fs" % (res["n"], res["t1"] - res["t0"]))


if __name__ == "__main__":
    main()
