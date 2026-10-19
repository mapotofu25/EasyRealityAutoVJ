# -*- coding: utf-8 -*-
import json, os, shutil, tempfile, collections
src = r"C:\Users\Administrator\AppData\Local\AutoVJ\config.json"
print("config size", os.path.getsize(src))
# 夹具目录在 finally 里回收（新铁律：mkdtemp 必须清理）
d = tempfile.mkdtemp(prefix="qa3_cfg_")
try:
    dst = os.path.join(d, "config.json")
    shutil.copy2(src, dst)   # 只读复制
    cfg = json.load(open(dst, encoding="utf-8"))
    print("top keys:", sorted(cfg.keys()))
    lays = cfg.get("layers", [])
    print("layers:", len(lays))
    exts = (".mov", ".mp4", ".mkv", ".avi", ".webm")
    allv = []
    for i, lay in enumerate(lays):
        clips = lay.get("clips") or []
        paths = []
        for c in clips:
            p = c if isinstance(c, str) else (c.get("path","") if isinstance(c, dict) else "")
            if p:
                paths.append(p)
        ex = [p for p in paths if os.path.splitext(p)[1].lower() in exts and os.path.exists(p)]
        miss = [p for p in paths if not os.path.exists(p)]
        print("  layer %d: clips=%d exist_video=%d missing=%d name=%r" % (i, len(paths), len(ex), len(miss), lay.get("name","")))
        allv += ex
    print("library entries:", len(cfg.get("library", [])))
    print("total usable videos:", len(allv))
    print("sample:", [os.path.basename(x) for x in allv[:8]])
    media = r"Y:\EasyRealityAutoVJ\素材库"
    cnt = 0
    for root,_,fs in os.walk(media):
        for f in fs:
            if os.path.splitext(f)[1].lower() in exts: cnt += 1
    print("media lib video count:", cnt)
finally:
    shutil.rmtree(d, ignore_errors=True)
