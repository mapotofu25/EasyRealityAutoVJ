# -*- coding: utf-8 -*-
import sys, os
sys.path.insert(0, "src")
import cv2
import config
from media_manager import _ascii_safe_path

c = config.Config()
vids = [p for p in c["library"] if p.lower().endswith(".mov") and os.path.exists(p)]
n_raw = n_safe = 0
for p in vids:
    cap = cv2.VideoCapture(p)
    ok_raw = cap.isOpened()
    cap.release()
    cap2 = cv2.VideoCapture(_ascii_safe_path(p))
    ok_safe = cap2.isOpened()
    cap2.release()
    n_raw += ok_raw; n_safe += ok_safe
print("视频总数 =", len(vids))
print("原始路径 VideoCapture.isOpened() =", n_raw)
print("_ascii_safe_path isOpened()      =", n_safe)
