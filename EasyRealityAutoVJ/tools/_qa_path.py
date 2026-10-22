# -*- coding: utf-8 -*-
import sys, os
sys.path.insert(0, "src")
import cv2
import config
from media_manager import _ascii_safe_path

c = config.Config()
targets = [p for p in c["library"] if p.endswith(".png") or p.endswith(".jpg")]
print("图片素材共", len(targets))
for p in targets:
    raw_ok = cv2.imread(p) is not None
    safe = _ascii_safe_path(p)
    safe_ok = cv2.imread(safe) is not None
    print("存在=%s | 原路径imread=%s | _ascii_safe_path=%r ok=%s" % (
        os.path.exists(p), raw_ok, safe, safe_ok))
    print("    path =", repr(p))
