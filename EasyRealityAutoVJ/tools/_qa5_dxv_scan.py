# -*- coding: utf-8 -*-
"""QA5: 扫真实库的编解码器分布，解释离屏压测里 gpu=0 的原因（D 的诚实说明）。只读。"""
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))
import av  # PyAV

REAL_CFG = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "config.json")
cfg = json.load(open(REAL_CFG, encoding="utf-8"))
lib = cfg.get("library") or []
dxv = []
codecs = {}
t0 = time.perf_counter()
for p in lib:
    if not os.path.exists(p):
        codecs["<missing>"] = codecs.get("<missing>", 0) + 1
        continue
    try:
        with av.open(p, metadata_errors="replace") as c:
            name = c.streams.video[0].codec_context.name
            codecs[name] = codecs.get(name, 0) + 1
            if name == "dxv":
                st = c.streams.video[0]
                dxv.append((os.path.basename(p), int(st.width), int(st.height)))
    except Exception as e:  # noqa: BLE001
        codecs["<err:%s>" % type(e).__name__] = codecs.get("<err:%s>" % type(e).__name__, 0) + 1
print("库条目 %d，耗时 %.1fs" % (len(lib), time.perf_counter() - t0))
print("编解码器分布：%s" % codecs)
print("DXV 素材 %d 个：%s" % (len(dxv), dxv[:15]))
print("gpu_decode 配置=%r；gpu 相关键=%s"
      % (cfg.get("gpu_decode"),
         [k for k in cfg.keys() if "gpu" in k.lower()]))
