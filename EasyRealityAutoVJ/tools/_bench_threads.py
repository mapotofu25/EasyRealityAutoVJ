# -*- coding: utf-8 -*-
"""决定性：构造播放器 → 激活 → 等 3 秒 → 打出**解码线程的栈**与播放器内部状态。

线程栈用 `sys._current_frames()`（能拿到 `_thread.start_new_thread` 起的线程，
`threading.enumerate()` 拿不到它们）。
"""
import os
import sys
import time
import traceback

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SRC)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import av                             # noqa: E402
import media_manager as mm            # noqa: E402

LIB = r"Y:\EasyRealityAutoVJ\素材库"
NAME = sys.argv[1] if len(sys.argv) > 1 else "dusk oct ring.mov"


def find(name):
    for root, _d, files in os.walk(LIB):
        for f in files:
            if f.lower() == name.lower():
                return os.path.join(root, f)
    return None


path = find(NAME)
print("文件:", path)
p = mm.GpuDxvPlayer(path, av)
p.set_max_size(2880, 1620)
p.set_active(True)
print("已激活，等 3 秒…")
time.sleep(3.0)

print()
print("=== 播放器状态 ===")
for k in ("_dead", "_ended", "gave_up_reason", "gpu", "gpu_want", "_fmt", "reason",
          "_frames", "_last_t", "_active", "_warm"):
    print("  %-16s = %r" % (k, getattr(p, k, "<无>")))

print()
print("=== 所有线程栈（含 _thread 起的解码线程）===")
names = {t.ident: t.name for t in __import__("threading").enumerate()}
for tid, fr in list(sys._current_frames().items()):
    print("-" * 70)
    print("线程 tid=%s  name=%s" % (tid, names.get(tid, "(非 threading 管理的线程)")))
    for line in traceback.format_stack(fr)[-6:]:
        sys.stdout.write("".join("    " + x for x in [line]))

for i in range(4):
    time.sleep(0.5)
    print("t=+%.1fs frames=%s 有图=%s" % (3.5 + i * 0.5, getattr(p, "_frames", "?"),
                                        p.current() is not None))
