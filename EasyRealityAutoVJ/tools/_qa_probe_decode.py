# -*- coding: utf-8 -*-
"""QA 探针 3：确认真实素材能被 make_player 真正解码（_frames 会增长）。"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

import json
from PySide6.QtWidgets import QApplication                          # noqa: E402
app = QApplication.instance() or QApplication([])

import media_manager as mm                                          # noqa: E402

CFG = os.path.join(_ROOT, "config.json")
with open(CFG, "r", encoding="utf-8") as f:
    cfg = json.load(f)

# 收集真实视频路径
vids = []
for lay in cfg.get("layers", []):
    for c in (lay.get("clips") or []):
        p = c if isinstance(c, str) else (c.get("path", "") if isinstance(c, dict) else "")
        if p and os.path.exists(p) and os.path.splitext(p)[1].lower() in (".mov", ".mp4", ".mkv", ".avi", ".webm"):
            vids.append(p)
# library 里的视频
for p in cfg.get("library", [])[:200]:
    if isinstance(p, str) and os.path.exists(p) and os.path.splitext(p)[1].lower() in (".mov", ".mp4", ".mkv"):
        vids.append(p)
seen = []
for v in vids:
    if v not in seen:
        seen.append(v)
vids = seen
print("找到真实视频:", len(vids))
for v in vids[:6]:
    print("   ", v)

if not vids:
    print("没有真实视频可用")
    sys.exit(2)

for v in vids[:4]:
    it = mm.MediaItem(v)
    print("\n-- %s kind=%s thumb=%s alpha=%s" % (os.path.basename(v), it.kind,
                                                 os.path.basename(str(it.thumb)), it._has_alpha))
    p = mm.make_player(it)
    ptype = type(p).__name__
    p.set_max_size(1920, 1080)
    p.set_active(True)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 3.0 and getattr(p, "_frames", 0) < 5:
        time.sleep(0.02)
    print("   播放器=%s  _frames=%d  age=%.3fs  _ended=%s" %
          (ptype, getattr(p, "_frames", 0),
           (time.perf_counter() - p._last_t) if getattr(p, "_last_t", 0) else -1,
           getattr(p, "_ended", None)))
    p.close()
