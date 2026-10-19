# -*- coding: utf-8 -*-
"""核验本次改动（第三轮：闪黑屏修复 + 手动 IP + 文案清除）已进 PYZ。

用法：python tools/_verify_r5_markers.py [dist_dir]
"""
import os
import sys
import zipfile

TOKENS = [
    b"autovj-ndi-video",
    b"_vbuf_pool",
    b"peer_hint",
    b"hint_rejects",
    b"\xe7\xab\xaf\xe5\x8f\xa3 / \xe5\xaf\xb9\xe7\xab\xafIP",
]
FORBIDDEN = [
    b"self._vq = (bgra,",
]


def main():
    dist = sys.argv[1] if len(sys.argv) > 1 else "dist_r5"
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", dist,
                        "EasyRealityAutoVJ", "_internal")
    pyz = None
    for d, _sub, files in os.walk(root):
        for fn in files:
            if fn.endswith(".pyz"):
                pyz = os.path.join(d, fn)
                break
        if pyz:
            break
    if not pyz:
        print("找不到 PYZ（%s）" % root)
        return 1
    print("PYZ: %s" % pyz)
    blob = b""
    with zipfile.ZipFile(pyz) as z:
        for n in z.namelist():
            try:
                blob += z.read(n)
            except Exception:
                pass
    print("PYZ 解压后 %d 字节" % len(blob))
    ok = True
    for t in TOKENS:
        hit = t in blob
        print("  %s %s" % ("OK" if hit else "MISS", t.decode("utf-8", "replace")))
        if not hit:
            ok = False
    for t in FORBIDDEN:
        hit = t in blob
        print("  %s 旧标记 %s（应未命中）" % ("BAD" if hit else "OK",
                                          t.decode("utf-8", "replace")))
        if hit:
            ok = False
    print("\n" + ("全部标记核验通过" if ok else "有标记未命中"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
