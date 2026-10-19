# -*- coding: utf-8 -*-
"""全量「悬空依赖」审计：找出**会被打进包**的 PE 文件里，谁 import 了**被跳过**的模块。

原理：DLL 加载器按**文件名**解析依赖，所以只要某个会被打包的 .dll/.pyd/.exe 的 PE
导入表里出现一个「被跳过（不会进包）」的文件名，就是一条悬空依赖（运行时该依赖解析不到）。

被跳过集合直接复用 tools/make_zip.py 的真实判据（_is_cyndilib_junk / _is_qt_unused），
保证审计与打包口径完全一致。

用法：
    AUTOVJ_DIST=dist_new venv\\Scripts\\python -u tools\\_dep_audit.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import make_zip                       # noqa: E402
import _pe_imports                    # noqa: E402

DIST = make_zip.DIST
PE_EXT = (".dll", ".pyd", ".exe")

print("=" * 88)
print("悬空依赖审计   dist =", DIST)
print("=" * 88)

all_files = []
for root, dirs, files in os.walk(DIST):
    for f in files:
        all_files.append(os.path.join(root, f))

skipped = []
packed = []
for p in all_files:
    if make_zip._is_cyndilib_junk(p) or make_zip._is_qt_unused(p):
        skipped.append(p)
    else:
        packed.append(p)

skipped_names = {}
for p in skipped:
    skipped_names[os.path.basename(p).lower()] = p

pe_packed = [p for p in packed if os.path.splitext(p)[1].lower() in PE_EXT]
print(f"dist 文件总数            : {len(all_files)}")
print(f"  其中被跳过（不进包）    : {len(skipped)}")
print(f"  其中会进包              : {len(packed)}")
print(f"  会进包里 PE(.dll/.pyd/.exe): {len(pe_packed)}")
print(f"被跳过模块名（按名字匹配）  : {len(skipped_names)}")
print()

print("--- 被跳过清单（文件 / 字节）---")
tot_skip_bytes = 0
for p in sorted(skipped, key=lambda x: -os.path.getsize(x)):
    s = os.path.getsize(p)
    tot_skip_bytes += s
    print(f"  {s:>12,}  {os.path.relpath(p, DIST)}")
print(f"  —— 合计 {len(skipped)} 个文件 / {tot_skip_bytes/1048576:.1f} MB")
print()

print("--- 悬空依赖：会进包的 PE 文件 → 依赖了被跳过的模块 ---")
dangling = []
parse_fail = []
for p in pe_packed:
    try:
        imports = _pe_imports.parse_imports(p)
    except Exception as e:                                  # noqa: BLE001
        parse_fail.append((p, str(e)))
        continue
    for imp in imports:
        if imp.lower() in skipped_names:
            dangling.append((p, imp, skipped_names[imp.lower()]))

if dangling:
    for p, imp, tgt in sorted(dangling):
        print(f"  ✗ {os.path.relpath(p, DIST)}")
        print(f"      → 依赖被跳过模块 {imp}  (跳过的是 {os.path.relpath(tgt, DIST)})")
else:
    print("  （无）")
print()
print(f"解析失败的文件数: {len(parse_fail)}" + ("（见下）" if parse_fail else "（无）"))
for p, e in parse_fail:
    print(f"    ! {os.path.relpath(p, DIST)} : {e}")
print()
print("=" * 88)
print(f"结论：扫描 {len(pe_packed)} 个会进包的 PE 文件，发现 {len(dangling)} 条悬空依赖")
print("=" * 88)
