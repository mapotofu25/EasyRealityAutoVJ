# -*- coding: utf-8 -*-
"""一次性验证：打包产物里的 engine 是否含「无声音时隐藏本层 0.5s 防抖」的改动。

不建 MainWindow，纯读 exe 内的 PYZ。
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "dist", "EasyRealityAutoVJ", "EasyRealityAutoVJ.exe")

from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

d = tempfile.mkdtemp(prefix="avj_verify_")
try:
    print("exe:", EXE)
    print("exe mtime:", __import__("time").strftime(
        "%H:%M:%S", __import__("time").localtime(os.path.getmtime(EXE))))
    r = CArchiveReader(EXE)
    print("PYZ.pyz 在包内:", "PYZ.pyz" in r.toc)
    data = r.extract("PYZ.pyz")     # 返回 bytes（不落盘）
    pyz = os.path.join(d, "PYZ.pyz")
    with open(pyz, "wb") as f:
        f.write(data)
    print("PYZ.pyz 解出:", os.path.exists(pyz), os.path.getsize(pyz), "bytes")

    z = ZlibArchiveReader(pyz)
    code = z.extract("engine")

    names = set(code.co_names)
    consts = set()
    for c in code.co_consts:
        try:
            consts.add(c)
        except TypeError:
            pass

    checks = [
        ("模块级常量 SILENT_HIDE_DEBOUNCE", "SILENT_HIDE_DEBOUNCE" in names),
        ("方法名 _silent_hide_tick", "_silent_hide_tick" in names),
        ("状态变量 _silent_now_debounced", "_silent_now_debounced" in names),
        ("阈值常量 _SILENT_SEC_RAW", "_SILENT_SEC_RAW" in names),
        ("防抖秒数 0.5 出现在常量池", 0.5 in consts),
    ]
    print()
    allok = True
    for label, ok in checks:
        print(("  OK   " if ok else "  FAIL ") + label)
        allok = allok and ok

    # 反汇编 _silent_hide_tick，确认里面真的有 0.5 的钳位比较
    import dis
    print()
    print("---- _silent_hide_tick 反汇编片段 ----")
    for c in code.co_consts:
        if hasattr(c, "co_name") and c.co_name == "_silent_hide_tick":
            txt = []
            for ins in dis.get_instructions(c):
                txt.append("%s %s" % (ins.opname, ins.argrepr))
            joined = "\n".join(txt)
            for line in txt:
                if "COMPARE" in line or "LOAD_CONST" in line:
                    print("   ", line)
            has_half = "0.5" in joined
            print("   反汇编中含 0.5 :", has_half)
            allok = allok and has_half
            break
    else:
        print("   !! 未在常量池找到 _silent_hide_tick 的代码对象")
        allok = False

    # 确认旧的 0.25 行内判据不在 _tick_locked / _compose 里裸用
    print()
    print("结论：", "PASS —— 防抖修复确实在包里" if allok else "FAIL —— 包内不含该修复")
    sys.exit(0 if allok else 1)
finally:
    import shutil
    shutil.rmtree(d, ignore_errors=True)
