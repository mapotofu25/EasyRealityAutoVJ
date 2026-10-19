# -*- coding: utf-8 -*-
"""决定性探针：把"解不出首帧"的素材直接喂给原生 dxvnative，看格式闸门怎么判。

对每个文件：
  1. PyAV 取第一个非空包 → dxvnative.parse_header(pkt)  → 帧头解析结果
     · None  = "帧头不受支持"（应走软解）
     · [fmt,…] = 判定 DXT 格式；fmt 必须是 1/5 才允许 GPU
  2. 同一包 → dxvnative.frame_blocks(pkt, w, h) → 能否取出 BC 压缩块
     · None = GPU 路径这包拿不到数据（会被 continue 跳过）
只读。
"""
import os
import sys

LIB = r"Y:\EasyRealityAutoVJ\素材库"
# 解不出首帧的（日志里点名的）
BAD = ["dusk oct ring.mov", "4 (2).mov", "30.mov"]
# 能正常播的（对照组）
GOOD = ["4.mov", "17.mov", "8.mov", "001.mov"]


def find(name):
    for root, _d, files in os.walk(LIB):
        for f in files:
            if f.lower() == name.lower():
                return os.path.join(root, f)
    return None


def one(path, tag):
    import av
    import dxvnative
    print("-" * 78)
    print("[%s] %s" % (tag, os.path.basename(path)))
    try:
        c = av.open(path, metadata_errors="replace")
        st = c.streams.video[0]
        print("    PyAV: 编码=%s 解码输出=%s %dx%d"
              % (st.codec_context.name, st.codec_context.pix_fmt,
                 st.codec_context.width, st.codec_context.height))
        pkt = None
        for p in c.demux(st):
            if p.size:
                pkt = bytes(p)
                break
        if pkt is None:
            print("    !! 拿不到非空包")
            c.close()
            return
        print("    首个包大小 = %d 字节" % len(pkt))
        hd = None
        try:
            hd = dxvnative.parse_header(pkt)
        except Exception as e:                                        # noqa: BLE001
            print("    parse_header 抛异常: %s" % e)
        print("    parse_header → %r" % (hd,))
        if hd is not None:
            try:
                print("      帧头字段: 格式/fmt = %r" % (hd[0],))
            except Exception:                                         # noqa: BLE001
                pass
        try:
            fb = dxvnative.frame_blocks(pkt, st.codec_context.width,
                                        st.codec_context.height)
        except Exception as e:                                        # noqa: BLE001
            fb = "抛异常: %s" % e
        if isinstance(fb, tuple) and len(fb) == 2:
            print("    frame_blocks → (fmt=%r, 块长度=%d 字节)" % (fb[0], len(fb[1])))
        else:
            print("    frame_blocks → %r   ← **GPU 路径拿不到数据**" % (fb,))
        c.close()
    except Exception as e:                                            # noqa: BLE001
        print("    !! 打开失败: %s" % e)


def main():
    print("dxvnative 可用:", end=" ")
    try:
        import dxvnative
        print(dxvnative.available(), getattr(dxvnative, "LAST_ERROR", ""))
    except Exception as e:                                            # noqa: BLE001
        print("导入失败:", e)
        return
    for n in BAD:
        p = find(n)
        if p:
            one(p, "解不出的")
        else:
            print("找不到:", n)
    for n in GOOD:
        p = find(n)
        if p:
            one(p, "对照-能播的")
        else:
            print("找不到:", n)
    print("=" * 78)


if __name__ == "__main__":
    sys.exit(main())
