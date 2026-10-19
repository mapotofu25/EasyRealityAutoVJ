# -*- coding: utf-8 -*-
"""QA 探针 2：直接解析 config.json，找真实素材路径 + 层结构。"""
import json
import os

CFG = r"P:\AutoVJ\config.json"
with open(CFG, "r", encoding="utf-8") as f:
    cfg = json.load(f)

print("top keys:", sorted(cfg.keys()))
layers = cfg.get("layers", [])
print("layers:", len(layers))
allp = []
for i, lay in enumerate(layers):
    clips = lay.get("clips") or []
    paths = []
    for c in clips:
        p = c if isinstance(c, str) else (c.get("path", "") if isinstance(c, dict) else "")
        if p:
            paths.append(p)
    n_exist = sum(1 for p in paths if os.path.exists(p))
    print("  layer[%d] name=%-24s visible=%s clips=%d exist=%d is_kv=%s" %
          (i, str(lay.get("name"))[:24], lay.get("visible"), len(paths), n_exist,
           lay.get("is_kv")))
    if paths and n_exist:
        # 找几个不同扩展名的
        for p in paths:
            if os.path.exists(p):
                print("      OK: %s  (%.1f MB)" % (p, os.path.getsize(p) / 1e6))
                allp.append(p)
                break
print("\ntotal existing sample paths:", len(allp))

# 素材库目录
for key in ("library", "media", "media_dirs", "folders", "sources"):
    v = cfg.get(key)
    if v:
        print("%s:" % key, str(v)[:300])
