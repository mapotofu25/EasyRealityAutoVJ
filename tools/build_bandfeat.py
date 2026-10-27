# -*- coding: utf-8 -*-
"""离线建「频带特征库」（bandfeat.db）——只做一次，运行时只读。

用法：
  venv\\Scripts\\python.exe -u tools/build_bandfeat.py            # 增量（已有跳过）
  venv\\Scripts\\python.exe -u tools/build_bandfeat.py --force    # 全部重建
"""
import hashlib
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))
import bandfeat                                                    # noqa: E402

LOCAL = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ")
CFG = os.path.join(LOCAL, "config.json")

cfg = json.load(open(CFG, encoding="utf-8"))
items = []
for path, v in (cfg.get("music_meta") or {}).items():
    sid = v.get("song_id")
    if sid is None:
        sid = int(hashlib.md5(path.encode("utf-8")).hexdigest()[:8], 16)
    items.append((int(sid), path))

force = "--force" in sys.argv
if force:
    p = bandfeat.db_path(LOCAL)
    if os.path.exists(p):
        os.remove(p)
        print("已删除旧库")

print("曲目 %d 首，开始提取频带特征…" % len(items))
t0 = time.time()


def prog(sid, frames):
    print("  sid=%-11d frames=%d" % (sid, frames), flush=True)


res = bandfeat.build_db(LOCAL, items, progress=prog)
print("\n写入 %d 首 / 跳过 %d 首 / 失败 %d 首，用时 %.0fs"
      % (res["wrote"], res["skipped"], len(res["failed"]), time.time() - t0))
for sid, why in res["failed"][:10]:
    print("  失败 sid=%s : %s" % (sid, why))
print("库文件：%s（%.1f MB）"
      % (res["db"], os.path.getsize(res["db"]) / 1048576.0 if os.path.exists(res["db"]) else 0))
