# -*- coding: utf-8 -*-
"""验证"硬解不了"的判定稳不稳、可不可信。

两个脚本给出的数量对不上（整库扫 40 个 vs 老库单独扫 39 个），必须先搞清楚是
**文件真的不支持**，还是**我的探测不稳/写错**。做法：同一批文件连测 3 轮，
每轮打印 dxvnative.parse_header 的原始返回、codec、以及前 3 个包的 size。
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
lib = list(cur.get("library") or [])

# 挑几个"被判定为硬解不了"的 + 几个"判定为可硬解"的做对照
names_bad = ("001 (5).mov", "002.mov", "立方体穿梭_Custom.mov", "红Grid_DXV.mov",
             "搬运工 (1).mov", "星空dxv.mov")
names_ok = ("004.mov", "10.mov", "001 (21).mov")

sel = []
for p in lib:
    b = os.path.basename(p)
    if b in names_bad or b in names_ok:
        sel.append(p)
print("选中 %d 个文件做 3 轮重复探测\n" % len(sel))


def probe(p):
    c = av.open(p, metadata_errors="replace")
    st = c.streams.video[0]
    codec = st.codec_context.name
    sizes = []
    pk = None
    for pkt in c.demux(st):
        if pkt.size:
            sizes.append(pkt.size)
            if pk is None:
                pk = bytes(pkt)
            if len(sizes) >= 3:
                break
    hd = dxvnative.parse_header(pk) if pk else None
    c.close()
    return codec, sizes, hd


for p in sel:
    b = os.path.basename(p)
    print("%s" % b)
    for r in range(3):
        try:
            codec, sizes, hd = probe(p)
            print("   第%d轮 codec=%-5s 前3包=%s parse_header=%s"
                  % (r + 1, codec, sizes, hd))
        except Exception as e:                                      # noqa: BLE001
            print("   第%d轮 异常 %s" % (r + 1, str(e)[:60]))
    print()
