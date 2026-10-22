"""效果渲染 / 输出各环节的单帧成本（为「要不要搬 GPU」提供依据）

背景：GPU 解码接进去之后，CPU 的大头变成了 Qt 渲染线程（实测 4 图层场景里
Qt6Core 起的线程合计约 0.5 个核，占进程 30%）。那么「把合成 / 颜色 / 后处理 /
输出搬到 GPU」到底能省多少？这里逐环节量出来。

⚠ 测法要点：
  · QPainter 的栅格化是**多线程**的（Qt 池线程）→ 必须看**进程级 CPU**
    （`process_time`），只看墙钟会低估它对整机占用的影响。
  · 单帧耗时 × 帧率 = 核当量（60fps 下 1.0 ms/帧 = 0.06 个核）。

用法：venv\\Scripts\\python.exe tools\\_compose_cost.py
"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(ROOT, "src"))

try:
    import main  # noqa: F401  触发线程数限制，与真实运行一致
except Exception:
    pass

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QImage, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

import cv2  # noqa: E402
import numpy as np  # noqa: E402

W, H = 1920, 1080
OUT = []


def w(s=""):
    print(s, flush=True)
    OUT.append(str(s))


def bench(fn, n=40, warm=6):
    for _ in range(warm):
        fn()
    t0, c0 = time.perf_counter(), time.process_time()
    for _ in range(n):
        fn()
    return ((time.perf_counter() - t0) / n * 1000,
            (time.process_time() - c0) / n * 1000)


def cores(ms_per_frame):
    return ms_per_frame / 1000.0 * 60.0


def make_img(seed):
    rng = np.random.default_rng(seed)
    a = rng.integers(0, 255, (H, W, 4), dtype=np.uint8)
    a[:, :, 3] = 255
    return QImage(a.data, W, H, 4 * W, QImage.Format_ARGB32).copy()


def main():
    src = make_img(1)
    canvas = QImage(W, H, QImage.Format_ARGB32)
    canvas.fill(Qt.black)
    w("=" * 90)
    w("效果渲染 / 输出 单帧成本   画布 %dx%d，60fps 换算核当量" % (W, H))
    w("=" * 90)

    # ---------- 1) QPainter 合成 ----------
    w("")
    w("[1] QPainter 合成（把图层画到画布，全屏缩放 + SourceOver 混合）")
    for n in (1, 3, 6):
        def fn(n=n):
            p = QPainter(canvas)
            for _ in range(n):
                p.drawImage(canvas.rect(), src)
            p.end()
        wall, cpu = bench(fn)
        w("    %d 图层：墙钟 %5.2f ms  进程 CPU %5.2f ms/帧  → %4.2f 核"
          % (n, wall, cpu, cores(cpu)))

    # ---------- 2) 颜色 LUT ----------
    w("")
    w("[2] 颜色渲染（cv2.LUT，1080p BGRA）")
    buf = np.frombuffer(canvas.bits(), dtype=np.uint8, count=W * H * 4).reshape(H, W, 4)
    lut = np.arange(256, dtype=np.uint8).reshape(256, 1)
    wall, cpu = bench(lambda: cv2.LUT(buf, lut), n=60)
    w("    cv2.LUT：墙钟 %5.2f ms  进程 CPU %5.2f ms/帧  → %4.2f 核" % (wall, cpu, cores(cpu)))

    # ---------- 3) 后处理（高斯模糊，代表一个效果）----------
    w("")
    w("[3] 后处理（各挑一个代表效果）")
    bgr = buf[:, :, :3]
    tests = [("高斯模糊 5x5", lambda: cv2.GaussianBlur(bgr, (5, 5), 0)),
             ("色差(通道位移)", lambda: cv2.merge([np.roll(bgr[:, :, i], 3, axis=1) for i in range(3)])),
             ("曝光脉冲(乘加)", lambda: cv2.convertScaleAbs(bgr, alpha=1.2, beta=8))]
    for name, f in tests:
        wall, cpu = bench(f, n=40)
        w("    %-16s 墙钟 %5.2f ms  进程 CPU %5.2f ms/帧  → %4.2f 核"
          % (name, wall, cpu, cores(cpu)))

    # ---------- 4) 画布 → QPixmap / 预览绘制 ----------
    w("")
    w("[4] 输出与预览")
    wall, cpu = bench(lambda: QPixmap.fromImage(canvas), n=40)
    w("    QPixmap.fromImage(1080p)   墙钟 %5.2f ms  CPU %5.2f ms/帧 → %4.2f 核"
      % (wall, cpu, cores(cpu)))
    pm = QPixmap.fromImage(canvas)
    view = QPixmap(720, 405)
    wall, cpu = bench(lambda: (lambda p: (p.drawPixmap(view.rect(), pm), p.end()))(QPainter(view)), n=40)
    w("    预览面板绘制(缩到 720p)    墙钟 %5.2f ms  CPU %5.2f ms/帧 → %4.2f 核"
      % (wall, cpu, cores(cpu)))
    wall, cpu = bench(lambda: canvas.copy(), n=40)
    w("    画布整幅拷贝(QImage.copy)  墙钟 %5.2f ms  CPU %5.2f ms/帧 → %4.2f 核"
      % (wall, cpu, cores(cpu)))

    w("")
    w("=" * 90)
    w("读法：核当量 = 该环节在 60fps 下持续跑所消耗的 CPU。")
    w("      这些环节全部搬 GPU（shader 化 + 纹理直传）能省的是它们的**总和**，")
    w("      但要连带把「回读」也去掉才拿得到全额 —— 否则省下的还不够付回读。")
    w("=" * 90)

    with open(os.path.join(ROOT, "_compose_cost.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    print("\n→ 已写入 _compose_cost.txt")


if __name__ == "__main__":
    main()
