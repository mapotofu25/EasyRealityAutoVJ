# -*- coding: utf-8 -*-
"""机制探针 v3：用**真实 `_open()`** + 分别用 `threading.Thread` / `_thread.start_new_thread`
（播放器用的那种）跑 open→decode，判定"永久阻塞"与线程种类/进程状态是否相关。

用法：`python -u tools/_probe_block_cause2.py <threading|newthread|newthread2> [文件名]`
  newthread2：同一进程里先后起**两个** `_thread.start_new_thread` 解码（模拟多图层/多播放器）。
"""
import os
import sys
import threading
import time
import traceback
import _thread

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


def make_bare_player(path):
    """不启动解码线程，只为了复用真实 `_open()`。"""
    p = mm.GpuDxvPlayer.__new__(mm.GpuDxvPlayer)
    p._av = av
    p.path = path
    p.fps = 0.0
    p._src_w = 0
    p._src_h = 0
    p.gpu = False
    p.gpu_want = False
    p._fmt = 0
    p.reason = ""
    return p


def work(path, tag, res):
    p = make_bare_player(path)
    try:
        c, st = p._open()
    except Exception as e:                                          # noqa: BLE001
        res[tag] = "open 异常: %s" % e
        return
    res[tag] = "open 完成 reason=%r thread_count=%s" % (p.reason,
                                                        getattr(st.codec_context, "thread_count", "?"))
    n = 0
    t0 = time.perf_counter()
    try:
        for _fr in c.decode(video=0):
            n += 1
            if n >= 5:
                break
    except Exception as e:                                          # noqa: BLE001
        res[tag] = "decode 异常: %s" % e
    res[tag] += " | 解出 %d 帧 %.2fs" % (n, time.perf_counter() - t0)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "threading"
    name = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_NAME
    path = find(name)
    print("mode=%s 文件=%s" % (mode, path))

    frames = {}
    if mode == "threading":
        t = threading.Thread(target=work, args=(path, "T", frames), daemon=True, name="probe-th")
        t.start()
        t.join(5.0)
        alive = t.is_alive()
    elif mode == "newthread":
        done = threading.Event()

        def wrap():
            work(path, "N", frames)
            done.set()

        _thread.start_new_thread(wrap, ())
        alive = not done.wait(5.0)
    elif mode == "newthread2":
        done = threading.Event()

        def wrap():
            work(path, "N", frames)
            done.set()

        def wrap2():
            work(path, "N2", frames)
            done.set()

        _thread.start_new_thread(wrap, ())
        _thread.start_new_thread(wrap2, ())
        alive = not done.wait(5.0)
    else:
        print("未知 mode")
        return

    print("  %s  存活(卡住)=%s" % (frames, alive))
    if alive:
        print("  **栈：")
        for tid, fr in list(sys._current_frames().items()):
            nm = threading._active.get(tid)
            nm = getattr(nm, "name", None) or "(非threading线程)"
            if "probe" in str(nm):
                print("   tid=%s name=%s" % (tid, nm))
                for line in traceback.format_stack(fr)[-6:]:
                    sys.stdout.write("".join("      " + x for x in [line]))


if __name__ == "__main__":
    main()
