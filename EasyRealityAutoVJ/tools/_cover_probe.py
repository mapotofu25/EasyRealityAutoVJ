# -*- coding: utf-8 -*-
"""封面来源探针：本机曲库里，封面到底藏在哪？（只读）

三条可能的路子：
  ① 同目录的图片文件（cover.jpg / folder.jpg / front.jpg …）—— 最省事
  ② 音频文件内嵌封面（FLAC 的 PICTURE 块 / MP3 的 APIC / MP4 的 covr）—— 用 PyAV 取
  ③ 都没有 —— 只能给占位图

用法：venv\\Scripts\\python.exe -u tools\\_cover_probe.py
"""

import collections
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
IMG_NAMES = ("cover", "folder", "front", "album", "albumart", "thumb", "artwork", "封面")


def load_lib():
    for p in (os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "config.json"),
              os.path.join(ROOT, "config.json")):
        if os.path.exists(p):
            try:
                d = json.load(open(p, encoding="utf-8"))
                lib = d.get("music_library") or []
                if lib:
                    return p, lib
            except Exception:
                pass
    return None, []


def folder_images(folder):
    try:
        names = os.listdir(folder)
    except Exception:
        return []
    out = []
    for n in names:
        if n.lower().endswith(IMG_EXT):
            out.append(n)
    # 优先挑名字像封面的
    out.sort(key=lambda n: (0 if os.path.splitext(n)[0].lower() in IMG_NAMES else 1,
                            len(n)))
    return out


def embedded_pic(path):
    """用 PyAV 取内嵌封面，返回 (mime, 字节数) 或 None。"""
    try:
        import av
    except Exception as e:
        return ("无 PyAV", str(e))
    try:
        with av.open(path, metadata_errors="ignore") as c:
            for s in c.streams:
                try:
                    if s.disposition and s.disposition.attached_pic:
                        n = 0
                        mime = getattr(s, "metadata", {}).get("mimetype", "") or ""
                        for fr in c.decode(s):
                            if fr.width and fr.height:
                                return (mime or "?", "%dx%d" % (fr.width, fr.height))
                            n += 1
                            if n > 3:
                                break
                        return (mime or "?", "attached_pic（未解出尺寸）")
                except Exception:
                    continue
            # 有些格式把封面放在 container 的 metadata 里
            meta = getattr(c, "metadata", {}) or {}
            if "METADATA_BLOCK_PICTURE" in meta:
                return ("flac-block", "有（metadata 键）")
            if "APIC" in meta or "coverart" in {k.lower() for k in meta}:
                return ("tag", "有（tag 键）")
        return None
    except Exception as e:
        return ("打开失败", str(e)[:60])


def main():
    cfg, lib = load_lib()
    print("配置：%s   曲目 %d 首" % (cfg, len(lib)))
    if not lib:
        print("没有曲库，退出")
        return

    by_dir = collections.defaultdict(list)
    for f in lib:
        by_dir[os.path.dirname(f)].append(f)

    print()
    print("=" * 92)
    print("① 同目录图片文件")
    print("=" * 92)
    hit_dir = 0
    for d, files in sorted(by_dir.items(), key=lambda kv: -len(kv[1])):
        imgs = folder_images(d)
        if imgs:
            hit_dir += 1
        print("  %2d 首  %s" % (len(files), d))
        print("        → %s" % (imgs[:4] if imgs else "（无图片文件）"))
    print("\n  有图片文件的目录：%d / %d" % (hit_dir, len(by_dir)))

    print()
    print("=" * 92)
    print("② 音频内嵌封面（PyAV 实测）")
    print("=" * 92)
    n_emb = 0
    t0 = time.perf_counter()
    kinds = collections.Counter()
    sample = []
    for f in lib:
        if not os.path.exists(f):
            kinds["文件缺失"] += 1
            continue
        r = embedded_pic(f)
        if r:
            n_emb += 1
            kinds[r[0]] += 1
            if len(sample) < 8:
                sample.append((os.path.basename(f)[:44], r))
        else:
            kinds["无内嵌"] += 1
    dt = time.perf_counter() - t0
    print("  有内嵌封面：%d / %d    耗时 %.2f 秒（%.0f ms/首）"
          % (n_emb, len(lib), dt, dt / max(1, len(lib)) * 1000))
    print("  分类：%s" % dict(kinds))
    for nm, r in sample:
        print("     %-46s %s" % (nm, r))


if __name__ == "__main__":
    main()
