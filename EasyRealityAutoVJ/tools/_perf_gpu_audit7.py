# -*- coding: utf-8 -*-
"""七次取样：码表 N 个子层合成的每帧成本（crop 绘制）+ 逐帧 policy 开销。"""
import os, statistics, sys, time
import numpy as np
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src")); os.chdir(ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtGui import QImage, QPainter, QGuiApplication
from PySide6.QtCore import Qt, QRectF
app = QGuiApplication.instance() or QGuiApplication([])

def ms(fn, n=20, warm=3):
    for _ in range(warm): fn()
    ts=[]
    for _ in range(n):
        t=time.perf_counter(); fn(); ts.append((time.perf_counter()-t)*1000)
    return statistics.median(ts)

for (w,h) in ((1280,720),(1920,1080)):
    rng=np.random.RandomState(6)
    imgs=[QImage((rng.rand(h,w,4)*255).astype(np.uint8).data, w,h,4*w, QImage.Format_ARGB32).copy() for _ in range(4)]
    canvas=QImage(w,h,QImage.Format_RGB32)
    def comp(n):
        def f():
            canvas.fill(Qt.black)
            p=QPainter(canvas)
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            for i in range(n):
                p.setOpacity(0.8)
                p.drawImage(QRectF(0,0,w,h), imgs[i%4], QRectF(w*0.05,h*0.05,w*0.9,h*0.9))
            p.end()
        return f
    print("%dx%d 码表合成: 1 层 %.2f / 2 层 %.2f / 4 层 %.2f / 6 层 %.2f ms"
          % (w,h,ms(comp(1)),ms(comp(2)),ms(comp(4)),ms(comp(6))))

# policy 纯 python 开销（用最小替身对象）
import cue_seek_policy as CSP
import inspect
print("\nCueSeekPolicy 方法:", [n for n,_ in inspect.getmembers(CSP.CueSeekPolicy, inspect.isfunction)])
pol = CSP.CueSeekPolicy()
print("构造参数:", inspect.signature(CSP.CueSeekPolicy.__init__))
