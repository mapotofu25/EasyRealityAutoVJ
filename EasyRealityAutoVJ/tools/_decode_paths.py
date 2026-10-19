# -*- coding: utf-8 -*-
"""决定性实验：运行中，每个图层到底用**哪条解码路径**、播的是哪个文件。

回答"是代码问题还是素材问题"：
  · 如果软解的都是"帧头不支持"的 DXV（LZF/YCG6）⇒ 素材问题（这些文件本来就没法硬解）；
  · 如果软解的里面有 **DXT1/DXT5 这种本该硬解的**（`gpu=False` 但材质没问题）
    ⇒ **代码问题**（GPU 路径被踢下来了）。此时再看 GL 工作线程的 stats/last_message。

用法：venv/Scripts/python.exe -u tools/_decode_paths.py [秒=25]
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, _HERE)

from PySide6.QtWidgets import QApplication                       # noqa: E402

app = QApplication([])
import ui_main                                                   # noqa: E402


class _MB:
    @staticmethod
    def warning(*a, **k):
        pass

    @staticmethod
    def information(*a, **k):
        pass


ui_main.QMessageBox = _MB
SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 25.0

w = ui_main.MainWindow()
w.save_cfg = lambda *a, **k: None
w.save_layers = lambda *a, **k: None
w.cfg.save = lambda *a, **k: None
w.show()
app.processEvents()
w.toggle_run()
print("引擎已启动，跑 %.0f 秒…" % SECS)
t0 = time.perf_counter()
while time.perf_counter() - t0 < SECS:
    app.processEvents()
    time.sleep(0.02)

try:
    import glctx
    wk = glctx.GLWorker.get()
    print("\n【GL 工作线程】available=%s  stats=%s\n  last_message=%r"
          % (wk.available(), wk.stats(), wk.last_message()))
except Exception as e:                                           # noqa: BLE001
    print("\nGL 工作线程查询失败：", e)

print("\n【每个图层的解码路径】")
soft_ok = 0        # 本该硬解却在软解 —— 代码问题
soft_bad = 0       # 材质本身解不了 —— 素材问题
for i, lay in enumerate(w.engine.layers):
    pl = getattr(lay, "player", None)
    clips = list(getattr(lay, "clips", []) or [])
    cur = getattr(lay, "cur", -1)
    pth = ""
    if isinstance(cur, int) and 0 <= cur < len(clips):
        pth = os.path.basename(getattr(clips[cur], "path", ""))
    cls = type(pl).__name__ if pl is not None else "(无)"
    gpu = getattr(pl, "gpu", None)
    want = getattr(pl, "gpu_want", None)
    reason = getattr(pl, "reason", "")
    has_alpha = getattr(pl, "out_alpha", None)
    print("\n  [%d] %-12s 播放器=%-14s gpu=%-5s want=%-5s" % (i, lay.name[:12], cls, gpu, want))
    print("      在播文件: %s" % (pth or "(无)"))
    print("      原因    : %s" % (reason or "(空)"))
    if cls == "GpuDxvPlayer" and gpu is not True:
        # 材质能不能硬解？能 ⇒ 是代码把它踢到软解了
        if "帧头不受支持" in reason or "非 DXV" in reason or "BC 格式" in reason \
                or "尺寸偏小" in reason:
            soft_bad += 1
            print("      ⇒ 素材本身解不了（LZF/YCG6 等）")
        else:
            soft_ok += 1
            print("      ⇒ ★ 材质没问题，却被踢到软解 ⇒ **代码问题**")
    elif cls == "VideoPlayer":
        soft_bad += 1
        print("      ⇒ OpenCV 路径（非 DXV 素材）")

print("\n小结：本该硬解却软解 = %d 个；素材本身解不了 = %d 个" % (soft_ok, soft_bad))
