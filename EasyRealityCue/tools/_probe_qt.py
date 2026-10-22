# -*- coding: utf-8 -*-
"""实测 3：Qt 侧能力 —— QPainter 混合模式枚举与真实合成、QtMultimedia 后端可用性。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage, QPainter, QColor
from PySide6.QtCore import Qt

print("=" * 78)
print("A. QPainter.CompositionMode 枚举全表")
print("=" * 78)
names = []
try:
    for m in QPainter.CompositionMode:
        names.append(m.name)
except Exception:
    # 老式枚举：从类属性取
    for k in dir(QPainter):
        if k.startswith("CompositionMode_"):
            names.append(k)
print("共", len(names), "个：")
print(", ".join(sorted(names)))
print()

# 目标：编辑器需要的 7 种混合
need = {
    "normal":  "CompositionMode_SourceOver",
    "add":     "CompositionMode_Plus",
    "subtract":"(无原生)",
    "screen":  "CompositionMode_Screen",
    "multiply":"CompositionMode_Multiply",
    "lighten": "CompositionMode_Lighten",
    "darken":  "CompositionMode_Darken",
}
print("需求覆盖检查：")
for k, v in need.items():
    ok = (v in names) or (v.startswith("("))
    print(f"  {k:9s} -> {v:34s} {'OK' if ok else '缺失'}")
print()

print("=" * 78)
print("B. 真实合成验证：把源色叠到目标色，读回结果像素")
print("=" * 78)

def make(w, h, rgba):
    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.fillRect(0, 0, w, h, QColor(*rgba))
    p.end()
    return img


def blend(mode_name, src_rgba=(200, 60, 60, 255), dst_rgba=(40, 180, 90, 255)):
    """在 dst 上以 mode 叠 src，返回中心像素 (r,g,b,a)。"""
    W = H = 8
    dst = make(W, H, dst_rgba)
    src = make(W, H, src_rgba)
    p = QPainter(dst)
    mode = getattr(QPainter, mode_name, None)
    try:
        p.setCompositionMode(mode)
    except Exception as e:
        p.end()
        return f"<setCompositionMode 失败 {e}>"
    p.drawImage(0, 0, src)
    p.end()
    c = dst.pixelColor(W // 2, H // 2)
    return (c.red(), c.green(), c.blue(), c.alpha())


tests = ["CompositionMode_SourceOver", "CompositionMode_Plus",
         "CompositionMode_Screen", "CompositionMode_Multiply",
         "CompositionMode_Lighten", "CompositionMode_Darken"]
print("目标色 dst=(40,180,90)  源色 src=(200,60,60,255)")
for t in tests:
    print(f"  {t:32s} -> {blend(t)}")

# subtract：Qt 无原生，验证「Difference + 反相源」或逐像素方案
try:
    # 方案：先把源整体求反（255-src），再走 Plus？不通用。
    # AutoVJ 用的是 numpy/cv2 逐像素相减，这里验证 numpy 视图可读写
    import numpy as np
    dst = make(8, 8, (40, 180, 90, 255))
    bpl = dst.bytesPerLine()
    arr = np.frombuffer(dst.bits(), dtype=np.uint8, count=bpl * 8).reshape(8, bpl)[:, :8 * 4].reshape(8, 8, 4)
    print("  位图直读 (BGRA) 可用:", arr.flags["C_CONTIGUOUS"], "shape", arr.shape,
          "| center BGRA:", arr[4, 4].tolist())
    arr[:, :, 0] = 7   # 直接改 B 通道
    c = dst.pixelColor(4, 4)
    print("  写入 B=7 后读回:", (c.red(), c.green(), c.blue(), c.alpha()))
except Exception as e:
    print("  numpy 视图失败:", type(e).__name__, e)

print()
print("=" * 78)
print("C. QtMultimedia 后端可用性")
print("=" * 78)
try:
    from PySide6 import QtMultimedia
    from PySide6.QtMultimedia import (QMediaPlayer, QMediaDevices, QMediaFormat,
                                      QVideoFrame, QAudioOutput)
    print("PySide6.QtMultimedia 导入:", "OK")
    app = None
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    outs = QMediaDevices.audioOutputs()
    print("QMediaDevices.audioOutputs():", len(outs), "个")
    # 支持的解码格式（无后端时为空）
    fmt = QMediaFormat()
    try:
        dec = fmt.supportedFileFormats(QMediaFormat.ConversionMode.Decode)
        print("后端支持的解码文件格式:", len(dec), "个")
    except Exception as e:
        print("supportedFileFormats 失败:", type(e).__name__, e)
    try:
        print("支持的视频编解码:", [c.name for c in fmt.supportedVideoCodecs(QMediaFormat.ConversionMode.Decode)][:12])
    except Exception as e:
        print("supportedVideoCodecs 失败:", type(e).__name__, e)
    pl = QMediaPlayer()
    print("QMediaPlayer() 构造: OK | errorString():", repr(pl.errorString()))
    pl.setSource(__import__("PySide6.QtCore", fromlist=["QUrl"]).QUrl.fromLocalFile(
        "P:/avi~/11-10-2025.mp4"))
    print("设置 mp4 源后 errorString():", repr(pl.errorString()),
          "| mediaStatus:", pl.mediaStatus())
except Exception as e:
    print("QtMultimedia 探测失败:", type(e).__name__, e)
print()
print("插件目录 multimedia/ 是否存在：",
      os.path.isdir(r"P:/AutoVJ/venv/Lib/site-packages/PySide6/plugins/multimedia"))
