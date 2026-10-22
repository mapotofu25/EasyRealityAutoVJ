# -*- coding: utf-8 -*-
"""核对 make_zip 在当前 dist 上到底跳过了哪些文件（cyndilib 无用文件 + 未用 Qt 模块）。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import make_zip  # noqa: E402

DIST = make_zip.DIST
print("dist =", DIST)

rows = []
cnt = {"cyndilib": 0, "qt": 0}
sz = {"cyndilib": 0, "qt": 0}
for root, dirs, files in os.walk(DIST):
    for f in files:
        p = os.path.join(root, f)
        s = os.path.getsize(p)
        if make_zip._is_cyndilib_junk(p):
            cnt["cyndilib"] += 1
            sz["cyndilib"] += s
            rows.append(("cyndilib", s, os.path.relpath(p, DIST)))
        elif make_zip._is_qt_unused(p):
            cnt["qt"] += 1
            sz["qt"] += s
            rows.append(("qt", s, os.path.relpath(p, DIST)))

rows.sort(key=lambda r: -r[1])
for kind, s, rel in rows:
    print(f"  [{kind:8s}] {s:>12,}  {rel}")
print()
print(f"cyndilib 跳过 {cnt['cyndilib']} 个文件，{sz['cyndilib']/1048576:.1f} MB")
print(f"Qt 未用   跳过 {cnt['qt']} 个文件，{sz['qt']/1048576:.1f} MB")
print(f"合计省 {((sz['cyndilib']+sz['qt'])/1048576):.1f} MB")
