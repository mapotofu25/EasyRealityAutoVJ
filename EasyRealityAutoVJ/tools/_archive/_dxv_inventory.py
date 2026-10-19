"""素材库普查（临时工具）：编码 / 像素格式 / 分辨率分布。

用途：决定 GPU 解码的启用条件 ——
  · 现在每个素材走的是 AvAlphaPlayer(PyAV) 还是 VideoPlayer(cv2)？取决于 pix_fmt；
  · DXT1 / DXT5 各自的分辨率分布（DXT1-1080p 用 GPU 反而略亏，只有大尺寸才启用）。

用法：venv\\Scripts\\python.exe tools\\_dxv_inventory.py
"""

import collections
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import av  # noqa: E402

LIB = r"Y:\EasyRealityAutoVJ\素材库"
VIDEO_EXT = (".mov", ".mp4", ".mkv", ".webm", ".avi", ".m4v")
ALPHA_MARKS = ("rgba", "bgra", "yuva", "argb", "abgr", "ya8", "pal8")

OUT = []


def w(s=""):
    print(s, flush=True)
    OUT.append(s)


def main():
    import dxvnative

    w("素材库 %s" % LIB)
    w("dxvnative.available() = %s" % dxvnative.available())
    w("")

    rows = []
    for f in sorted(os.listdir(LIB)):
        if not f.lower().endswith(VIDEO_EXT):
            continue
        p = os.path.join(LIB, f)
        try:
            c = av.open(p, metadata_errors="replace")
            try:
                st = c.streams.video[0]
                codec = st.codec_context.name
                pf = (st.codec_context.pix_fmt or "")
                info = ""
                if codec == "dxv":
                    try:
                        pk = next(c.demux(st))
                        hd = dxvnative.parse_header(bytes(pk))
                        info = "X" if hd is None else ("DXT%d" % (1 if hd[0] == 1 else 5))
                    except Exception:
                        info = "?"
                rows.append((f, codec, pf, int(st.width), int(st.height), info))
            finally:
                c.close()
        except Exception as e:                                        # noqa: BLE001
            rows.append((f, "ERR", str(e)[:30], 0, 0, ""))

    # ---------- 编码 × 像素格式 ----------
    w("=" * 96)
    w("编码 / 像素格式分布（pix_fmt 含 alpha 标记 ⇒ 走 PyAV；否则走 cv2）")
    w("=" * 96)
    cnt = collections.Counter()
    for f, codec, pf, ww, hh, info in rows:
        has = any(m in pf.lower() for m in ALPHA_MARKS)
        cnt[(codec, pf, "PyAV" if has else "cv2")] += 1
    for (codec, pf, path), n in sorted(cnt.items(), key=lambda kv: -kv[1]):
        w("  %-10s %-12s → %-5s  %3d 个" % (codec, pf, path, n))

    # ---------- DXV 的 BC 格式 × 尺寸 ----------
    w("")
    w("=" * 96)
    w("DXV 素材：BC 格式 × 分辨率（GPU 解码的适用面）")
    w("=" * 96)
    fmt_cnt = collections.Counter()
    for f, codec, pf, ww, hh, info in rows:
        if codec == "dxv":
            fmt_cnt[(info, ww, hh)] += 1
    for (info, ww, hh), n in sorted(fmt_cnt.items()):
        px = ww * hh / 1e6
        w("  %-6s %4dx%-5d (%.1f Mpx)  %3d 个" % (info, ww, hh, px, n))

    w("")
    w("=" * 96)
    w("按 GPU 启用门槛统计（DXT5 一律启用；DXT1 仅在 ≥3.0 Mpx 时启用）")
    w("=" * 96)
    yes = no = other = 0
    for f, codec, pf, ww, hh, info in rows:
        if info == "DXT5":
            yes += 1
        elif info == "DXT1":
            if ww * hh >= 3_000_000:
                yes += 1
            else:
                no += 1
        else:
            other += 1
    w("  GPU 启用 %d 个 / 跳过(DXT1 小尺寸) %d 个 / 不适用 %d 个   [视频共 %d]"
      % (yes, no, other, len(rows)))

    # ---------- 非 DXV 的 mov 明细（降级路径影响面） ----------
    w("")
    nondxv = [(f, codec, pf, ww, hh) for f, codec, pf, ww, hh, i in rows if codec != "dxv"]
    w("非 DXV 视频 %d 个：" % len(nondxv))
    for f, codec, pf, ww, hh in nondxv:
        w("  %-38s %-8s %-12s %dx%d" % (f[:38], codec, pf, ww, hh))

    with open(os.path.join(ROOT, "_dxv_inventory.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    print("\n→ 已写入 _dxv_inventory.txt")


if __name__ == "__main__":
    main()
