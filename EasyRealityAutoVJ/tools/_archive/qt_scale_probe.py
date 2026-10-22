# -*- coding: utf-8 -*-
"""验证「Qt 的平滑缩放会不会开线程池」+ 限制 QThreadPool 的效果。

背景：真实运行采样里出现 **7 条 Qt6Core.dll 线程**（合计约 0.96 核，占进程 1/3）。
代码里没有任何 QThreadPool/QtConcurrent 调用，但到处是
`QImage/QPixmap.scaled(..., Qt.SmoothTransformation)` —— 预览面板每帧都要缩放，
怀疑是 Qt 内部平滑缩放的并行路径。

用法： python tools/qt_scale_probe.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtCore import Qt, QThreadPool  # noqa: E402

out = []


def w(s=""):
    print(s)
    out.append(str(s))


def fill(img):
    """给 QImage 填点有变化的内容（全 0 可能走快速路径）"""
    import numpy as np
    h, wd = img.height(), img.width()
    buf = np.random.randint(0, 255, (h, wd, 4), dtype=np.uint8)
    p = img.bits()
    p[:] = buf.tobytes()
    return img


def workload(fn, sec):
    t0 = time.perf_counter()
    n = 0
    while time.perf_counter() - t0 < sec:
        fn()
        n += 1
    return n / (time.perf_counter() - t0)


def main():
    app = QApplication([])
    w("=" * 84)
    w("Qt 缩放 / 线程池探针   Qt %s" % __import__("PySide6").QtCore.qVersion())
    w("=" * 84)
    w("QThreadPool.globalInstance().maxThreadCount() 默认 = %d（本机 %d 核）"
      % (QThreadPool.globalInstance().maxThreadCount(), os.cpu_count()))

    src = fill(QImage(1920, 1080, QImage.Format_ARGB32))
    small = fill(QImage(320, 180, QImage.Format_ARGB32))
    import thread_attrib as ta

    cases = [
        ("① 1080p → 320 宽  SmoothTransformation",
         lambda: src.scaled(320, 180, Qt.KeepAspectRatio, Qt.SmoothTransformation)),
        ("② 1080p → 320 宽  FastTransformation",
         lambda: src.scaled(320, 180, Qt.KeepAspectRatio, Qt.FastTransformation)),
        ("③ 320 → 1080p（放大）Smooth",
         lambda: small.scaled(1920, 1080, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)),
        ("④ 1080p → 960 宽  SmoothTransformation",
         lambda: src.scaled(960, 540, Qt.KeepAspectRatio, Qt.SmoothTransformation)),
    ]
    for tag, fn in cases:
        fps = workload(fn, 1.2)      # 预热
        w("")
        ta.sample(2.5, tag, top=8)
        w("     吞吐 %.0f 次/秒" % fps)

    # ---- 限制 QThreadPool 后再测 ----
    w("")
    w("=" * 84)
    w("把 QThreadPool.globalInstance().setMaxThreadCount(2) 之后")
    w("=" * 84)
    QThreadPool.globalInstance().setMaxThreadCount(2)
    for tag, fn in cases[:1] + cases[3:4]:
        fps = workload(fn, 1.2)
        w("")
        ta.sample(2.5, tag + "（限制后）", top=8)
        w("     吞吐 %.0f 次/秒" % fps)

    open(os.path.join(ROOT, "_qt_scale.txt"), "w", encoding="utf-8").write("\n".join(out))


if __name__ == "__main__":
    main()
