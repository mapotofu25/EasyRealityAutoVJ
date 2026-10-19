# -*- coding: utf-8 -*-
"""诊断：用户现在播的这些素材，为什么没走 GPU 解码？

复刻 `GpuDxvPlayer._try_gpu()` 的**材质级判据**（不建 GL 上下文），逐个文件打印：
  编解码器 / 分辨率 / DXV 帧头格式 / 以及"能不能走 GPU + 卡在哪一条"。
用法：venv/Scripts/python.exe -u tools/_gpu_reason.py
"""
import io
import json
import os
import sys

sys.path.insert(0, r"P:\AutoVJ\src")

import av                                                        # noqa: E402
import dxvnative                                                 # noqa: E402

CFG = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ", "config.json")
d = json.load(io.open(CFG, encoding="utf-8"))
layers = d.get("layers") or []

paths = []
for lay in layers:
    for p in (lay.get("clips") or []):
        if p not in paths:
            paths.append(p)

print("配置里有 %d 个素材（去重后）" % len(paths))
print("=" * 92)
kinds = {}
for p in paths:
    name = os.path.basename(p)
    if not os.path.isfile(p):
        print("%-40s  ✗ 文件不存在" % name[:40])
        continue
    try:
        c = av.open(p, metadata_errors="replace")
        st = c.streams.video[0]
        codec = st.codec_context.name
        w, h = int(st.width), int(st.height)
        reason = ""
        gpu = False
        if codec != "dxv":
            reason = "非 DXV（%s）⇒ 本来就走软解/OpenCV" % codec
        elif w % 4 or h % 4:
            reason = "分辨率不是 4 的倍数"
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
                reason = "DXV 帧头不受支持（DXV2-LZF / YCG6 等）"
            elif int(hd[0]) not in (1, 5):
                reason = "BC 格式 %s 不支持" % hd[0]
            else:
                gpu = True
                reason = "DXT%s ✅ 可走 GPU" % (5 if int(hd[0]) == 5 else 1)
        c.close()
        kinds[codec] = kinds.get(codec, 0) + 1
        print("%-40s %-6s %4dx%-4d %s" % (name[:40], codec, w, h, reason))
    except Exception as e:                                          # noqa: BLE001
        print("%-40s  ✗ 打开失败 %s" % (name[:40], str(e)[:60]))
        kinds["(失败)"] = kinds.get("(失败)", 0) + 1

print("=" * 92)
print("编解码器分布：", kinds)
