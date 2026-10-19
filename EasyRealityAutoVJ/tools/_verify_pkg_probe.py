# -*- coding: utf-8 -*-
"""自查：确认 engine 模块 code 对象里这些名字到底在哪个命名空间。"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "dist", "EasyRealityAutoVJ", "EasyRealityAutoVJ.exe")

from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

d = tempfile.mkdtemp(prefix="avj_verify2_")
try:
    r = CArchiveReader(EXE)
    data = r.extract("PYZ.pyz")
    pyz = os.path.join(d, "PYZ.pyz")
    with open(pyz, "wb") as f:
        f.write(data)
    z = ZlibArchiveReader(pyz)
    code = z.extract("engine")

    def walk(co, depth=0, out=None):
        """递归收集所有代码对象里的 co_names。"""
        if out is None:
            out = set()
        out |= set(co.co_names)
        for c in co.co_consts:
            if hasattr(c, "co_code"):
                walk(c, depth + 1, out)
        return out

    allnames = walk(code)
    print("engine 全模块(含嵌套) 名字总数:", len(allnames))
    for probe in ("SILENT_HIDE_DEBOUNCE", "_silent_hide_tick",
                  "_silent_now_debounced", "_SILENT_SEC_RAW",
                  "_silent_debounce_on", "_silent_since", "_loud_since"):
        print("  ", probe, "->", probe in allnames)

    # 顶层类 AutoVJEngine 的方法名
    print()
    print("---- 顶层 code 对象的直接 co_names（前 40）----")
    print(sorted(list(code.co_names))[:40])
    print()
    classes = [c for c in code.co_consts if hasattr(c, "co_code")]
    print("顶层嵌套代码对象数:", len(classes))
    for c in classes:
        if c.co_name == "AutoVJEngine":
            m = set(c.co_names)
            print("AutoVJEngine.co_names 含 _silent_hide_tick:", "_silent_hide_tick" in m)
            print("AutoVJEngine 方法名里找 silent:",
                  [n for n in sorted(m) if "silent" in n.lower()])
finally:
    import shutil
    shutil.rmtree(d, ignore_errors=True)
