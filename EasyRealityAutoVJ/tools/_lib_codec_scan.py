# -*- coding: utf-8 -*-
"""扫素材库：哪些文件**永远走不了 GPU 硬解**（DXV2-LZF / YCG6 等），哪些是非 DXV。

为什么查这个：现场 CPU 8~14 核 + 磁盘 500MB/s，瓶颈就是"DXV 但硬解不了 ⇒ 只能软解"。
软解 1080p DXV 每路约 2.5 核 + 150MB/s 读盘 ⇒ 同时 3~4 路就把机器吃满。

用法：venv/Scripts/python.exe -u tools/_lib_codec_scan.py [最多文件数=600]
"""
import io
import json
import os
import sys
import time

sys.path.insert(0, r"P:\AutoVJ\src")
import av                                                        # noqa: E402
import dxvnative                                                 # noqa: E402

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 600
BUDGET = 120.0
CFG = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ", "config.json")
d = json.load(io.open(CFG, encoding="utf-8"))
lib = list(d.get("library") or [])
print("素材库 %d 个文件，本次最多扫 %d 个（限时 %.0f 秒）" % (len(lib), LIMIT, BUDGET))

stat = {}
bad = []          # 硬解不了的文件：需要转码
n = 0
t0 = time.time()
for p in lib:
    if n >= LIMIT or time.time() - t0 > BUDGET:
        break
    if not os.path.isfile(p):
        stat["(文件不存在)"] = stat.get("(文件不存在)", 0) + 1
        continue
    n += 1
    ext = os.path.splitext(p)[1].lower()
    if ext in (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"):
        stat["图片"] = stat.get("图片", 0) + 1
        continue
    try:
        c = av.open(p, metadata_errors="replace")
        st = c.streams.video[0]
        codec = st.codec_context.name
        w, h = int(st.width), int(st.height)
        if codec != "dxv":
            k = "非DXV:%s" % codec
            stat[k] = stat.get(k, 0) + 1
        else:
            pk = None
            try:
                for pkt in c.demux(st):
                    if pkt.size:
                        pk = bytes(pkt)
                        break
            except Exception:                                       # noqa: BLE001
                pass
            hd = dxvnative.parse_header(pk) if pk else None
            if hd is None:
                stat["★DXV硬解不了(帧头不支持)"] = stat.get("★DXV硬解不了(帧头不支持)", 0) + 1
                bad.append(("%dx%d" % (w, h), p))
            elif int(hd[0]) not in (1, 5):
                stat["★DXV硬解不了(BC格式%s)" % hd[0]] = \
                    stat.get("★DXV硬解不了(BC格式%s)" % hd[0], 0) + 1
                bad.append(("%dx%d" % (w, h), p))
            elif int(hd[0]) == 1 and w * h < 1000000:
                stat["DXT1 且太小(回退软解)"] = stat.get("DXT1 且太小(回退软解)", 0) + 1
            else:
                k = "DXV 可硬解:DXT%s %s" % (5 if int(hd[0]) == 5 else 1,
                                            "4K" if w > 2560 else "≤2K")
                stat[k] = stat.get(k, 0) + 1
        c.close()
    except Exception as e:                                          # noqa: BLE001
        stat["(打开失败)"] = stat.get("(打开失败)", 0) + 1
        print("  !! %s %s" % (os.path.basename(p)[:40], str(e)[:50]))

print("\n=== 分布（扫了 %d 个） ===" % n)
for k, v in sorted(stat.items(), key=lambda x: -x[1]):
    print("  %-34s %4d" % (k, v))

print("\n=== 硬解不了、建议转码的 DXV（最多列 25 个）===")
for sz, p in bad[:25]:
    print("  %-8s %s" % (sz, os.path.basename(p)))
print("  合计 %d 个" % len(bad))
