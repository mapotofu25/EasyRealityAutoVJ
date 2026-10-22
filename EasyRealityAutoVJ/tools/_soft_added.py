# -*- coding: utf-8 -*-
"""那 40 个"硬解不了"的素材，是不是这次新增的 116 个里的？

结论意义：如果"以前 164 个库"里几乎没有它们，那 CPU 从 <40% 涨到 70% 就解释通了
—— 不是代码变了，是**自动匹配池里多了这些只能软解的素材**。
"""
import io
import json
import os
import sys

sys.path.insert(0, r"P:\AutoVJ\src")
import av                                                        # noqa: E402
import dxvnative                                                 # noqa: E402

D = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ")
cur = json.load(io.open(os.path.join(D, "config.json"), encoding="utf-8"))
old = json.load(io.open(os.path.join(D, "config.json.bak_20260928_005849"),
                        encoding="utf-8"))
lib_new = set(cur.get("library") or [])
lib_old = set(old.get("library") or [])
added = lib_new - lib_old
print("素材库：以前 %d，现在 %d，新增 %d" % (len(lib_old), len(lib_new), len(added)))


def is_soft_only(p):
    """True = 这个文件永远走不了 GPU（DXV 但帧头不支持 / 非 DXV / 太小）。"""
    try:
        c = av.open(p, metadata_errors="replace")
        st = c.streams.video[0]
        codec = st.codec_context.name
        w, h = int(st.width), int(st.height)
        if codec != "dxv":
            c.close()
            return "非DXV(%s)" % codec
        pk = None
        try:
            for pkt in c.demux(st):
                if pkt.size:
                    pk = bytes(pkt)
                    break
        except Exception:                                           # noqa: BLE001
            pass
        hd = dxvnative.parse_header(pk) if pk else None
        c.close()
        if hd is None:
            return "DXV帧头不支持"
        if int(hd[0]) not in (1, 5):
            return "BC格式%s" % hd[0]
        if int(hd[0]) == 1 and w * h < 1000000:
            return "DXT1太小"
        return None
    except Exception:                                               # noqa: BLE001
        return "打开失败"


print("\n逐个体检新增的素材（只看前 130 个）：")
soft_new, ok_new, missing = [], 0, 0
for i, p in enumerate(sorted(added)):
    if i >= 130:
        break
    if not os.path.isfile(p):
        missing += 1
        continue
    r = is_soft_only(p)
    if r:
        soft_new.append((r, p))
    else:
        ok_new += 1
print("  新增里：可硬解 %d，**只能软解 %d**，文件不存在 %d" % (ok_new, len(soft_new), missing))
for r, p in soft_new:
    print("     %-16s %s" % (r, os.path.basename(p)))

print("\n老库里有多少只能软解的：")
soft_old = 0
for i, p in enumerate(sorted(lib_old)):
    if i >= 200 or not os.path.isfile(p):
        continue
    if is_soft_only(p):
        soft_old += 1
print("  老库只能软解 = %d 个（老库共 %d）" % (soft_old, len(lib_old)))
