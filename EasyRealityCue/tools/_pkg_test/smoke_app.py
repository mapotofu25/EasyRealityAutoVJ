# -*- coding: utf-8 -*-
"""最小打包冒烟 app：验证 PySide6 / av / numpy / mutagen / onnxruntime / soundfile
在 PyInstaller 冻结环境下都能 import 并做一件真实的事。"""
import os, sys, json

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
res = {}

from PySide6.QtWidgets import QApplication, QLabel
from PySide6.QtGui import QImage, QColor, QPainter
from PySide6.QtCore import Qt
app = QApplication([])
img = QImage(64, 64, QImage.Format_ARGB32); img.fill(QColor(10, 20, 30, 255))
p = QPainter(img); p.setCompositionMode(QPainter.CompositionMode_Screen)
p.fillRect(0, 0, 64, 64, QColor(5, 5, 200)); p.end()
c = img.pixelColor(32, 32)
res["qt"] = f"PySide6 OK, screen-blend={c.red(),c.green(),c.blue()}"

import numpy as np
res["numpy"] = np.__version__

try:
    import av
    res["av"] = av.__version__
    mp4 = sorted(__import__("glob").glob("P:/avi~/*.mp4"))
    if mp4:
        with av.open(mp4[0], metadata_errors="replace") as ct:
            st = ct.streams.video[0]
            for f in ct.decode(video=0):
                res["av_decode"] = f.to_ndarray(format="rgb24").shape
                break
except Exception as e:
    res["av"] = "FAIL " + repr(e)

try:
    import cv2
    res["cv2"] = cv2.__version__
except Exception as e:
    res["cv2"] = "FAIL " + repr(e)

try:
    import mutagen
    res["mutagen"] = mutagen.version_string
except Exception as e:
    res["mutagen"] = "FAIL " + repr(e)

try:
    import onnxruntime as ort
    res["onnxruntime"] = ort.__version__ + " providers=" + ",".join(ort.get_available_providers())
except Exception as e:
    res["onnxruntime"] = "FAIL " + repr(e)

try:
    import soundfile as sf
    res["soundfile"] = sf.__version__
    res["libsndfile"] = sf.__libsndfile_version__
except Exception as e:
    res["soundfile"] = "FAIL " + repr(e)

# 模型是否随包（--add-data assets/models → assets/models/...）
try:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    mdl = os.path.join(base, "assets", "models", "beat_this", "small0_fixed.onnx")
    res["model_found"] = os.path.isfile(mdl)
    if res["model_found"]:
        import onnxruntime as ort
        sess = ort.InferenceSession(mdl, providers=["CPUExecutionProvider"])
        res["onnx_session"] = [i.name for i in sess.get_inputs()]
except Exception as e:
    res["model_found"] = "FAIL " + repr(e)

out = json.dumps(res, ensure_ascii=False, indent=1)
print(out)
with open(os.path.join(os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)),
                       "smoke_result.json"), "w", encoding="utf-8") as fh:
    fh.write(out)
