"""alpha 判定审计（临时工具）

修完 `av.open` 的 GBK 元数据问题 + 改成「解一帧确认 alpha 真的不是全 255」之后，
整个素材库的判定会怎么变？这份审计把变化面列清楚，迁移（重建缩略图）才不会误伤。

输出：
  · 现状判定（按缩略图扩展名：PNG=带 alpha）vs 新判定
  · 新判为带 alpha 的（会从 cv2 切到 PyAV：透明生效，但也更贵）
  · 新判为不带 alpha 的（会从 PyAV 切回 cv2：省 CPU，但若真带 alpha 会丢透明 —— 要警惕）
  · 每个素材的编码 / 像素格式 / 分辨率

用法：venv\\Scripts\\python.exe tools\\_alpha_audit.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import av  # noqa: E402

from media_manager import MediaItem, _av_probe_alpha  # noqa: E402

LIB = r"Y:\EasyRealityAutoVJ\素材库"
EXT = (".mov", ".mp4", ".mkv", ".webm", ".avi")
OUT = []


def w(s=""):
    print(s, flush=True)
    OUT.append(str(s))


def full_scan(path, max_frames=240):
    """全片（最多 max_frames 帧）扫 alpha 的真实最小值，用来验证采样是否够。

    返回 (全局最小 alpha, 扫了多少帧, 有透明的帧数)。最小 alpha = 255 就说明整段不透明。
    """
    c = av.open(path, metadata_errors="replace")
    try:
        mn, n, hit = 255, 0, 0
        for fr in c.decode(video=0):
            arr = fr.to_ndarray(format="rgba")
            v = int(arr[:, :, 3].min())
            mn = min(mn, v)
            n += 1
            if v < 255:
                hit += 1
            if n >= max_frames:
                break
        return mn, n, hit
    except Exception as e:                                            # noqa: BLE001
        return -1, 0, 0
    finally:
        c.close()


def main():
    rows = []
    for f in sorted(os.listdir(LIB)):
        if not f.lower().endswith(EXT):
            continue
        p = os.path.join(LIB, f)
        it = MediaItem(p)
        png = os.path.exists(it._thumb_path(".png"))
        jpg = os.path.exists(it._thumb_path(".jpg"))
        try:
            c = av.open(p, metadata_errors="replace")
            try:
                st = c.streams.video[0]
                codec = st.codec_context.name
                pf = st.codec_context.pix_fmt or ""
                ww, hh = int(st.width), int(st.height)
            finally:
                c.close()
        except Exception as e:                                        # noqa: BLE001
            codec, pf, ww, hh = "ERR", str(e)[:20], 0, 0
        _, has = _av_probe_alpha(p)
        rows.append({"f": f, "path": p, "codec": codec, "pf": pf, "w": ww, "h": hh,
                     "png": png, "jpg": jpg, "old": png, "new": bool(has)})

    w("=" * 100)
    w("alpha 判定审计   %d 个视频素材" % len(rows))
    w("=" * 100)
    w("现状判定依据：缩略图是 PNG（旧版逻辑）；新判定：解一帧确认 alpha 不是全 255")
    w("")

    old_n = sum(1 for r in rows if r["old"])
    new_n = sum(1 for r in rows if r["new"])
    w("现状判为带 alpha：%d 个" % old_n)
    w("新判定为带 alpha：%d 个" % new_n)
    w("")

    gain = [r for r in rows if r["new"] and not r["old"]]
    loss = [r for r in rows if r["old"] and not r["new"]]
    same_no = [r for r in rows if not r["new"] and not r["old"]]

    w("-" * 100)
    w("【A】新判为带 alpha（透明将开始生效，同时从 cv2 切到 PyAV）：%d 个" % len(gain))
    w("-" * 100)
    for r in gain:
        w("  %-46s %-6s %-10s %4dx%-5d 缩略图=%s"
          % (r["f"][:46], r["codec"], r["pf"], r["w"], r["h"],
             "PNG" if r["png"] else ("JPG" if r["jpg"] else "无")))
    w("")
    w("-" * 100)
    w("【B】原来是 PNG、新判定说不带 alpha（会切回 cv2 → **可能丢透明**，要人工看）：%d 个"
      % len(loss))
    w("-" * 100)
    for r in loss:
        w("  %-46s %-6s %-10s %4dx%-5d" % (r["f"][:46], r["codec"], r["pf"], r["w"], r["h"]))
    w("")
    w("-" * 100)
    w("【C】一直判为不带 alpha（不受影响）：%d 个" % len(same_no))
    w("-" * 100)
    byc = {}
    for r in same_no:
        byc[r["codec"]] = byc.get(r["codec"], 0) + 1
    w("  按编码：%s" % byc)

    w("")
    w("=" * 100)
    w("去重清单（A 组，用于人工抽查前 10 个）")
    w("=" * 100)
    for r in gain[:10]:
        w("  %s" % r["f"])

    w("")
    w("=" * 100)
    w("抽查：全片 alpha 扫描（验证「采样 24 帧」够不够，max 240 帧）")
    w("=" * 100)
    probe = gain[:2] + loss[:3]
    for r in probe:
        mn, n, hit = full_scan(r["path"])
        verdict = ("整段不透明" if mn >= 255 else "确实有透明（最小 %d）" % mn)
        tag = "A 组(新判有alpha)" if r in gain else "B 组(新判无alpha)"
        w("  %-42s %-18s 扫 %3d 帧，有透明 %3d 帧，最小 alpha %3d → %s"
          % (r["f"][:42], tag, n, hit, mn, verdict))

    with open(os.path.join(ROOT, "_alpha_audit.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    print("\n→ 已写入 _alpha_audit.txt")


if __name__ == "__main__":
    main()
