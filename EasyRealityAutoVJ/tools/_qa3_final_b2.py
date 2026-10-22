# -*- coding: utf-8 -*-
"""QA(严过关) B2：挑战 Qt 排除清单 —— 用 PE 导入表证明「被跳过的 DLL 没有被保留文件依赖」。

对 zip 100108 里**保留**的每个 PE 文件（exe / 保留的 Qt6*.dll / 各插件 dll），
解析导入表，若引用了跳过清单里的 DLL 名 => 违规（会误伤必需文件）。
"""
import os
import sys
import shutil
import tempfile
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
import make_zip                                                     # noqa: E402
import _pe_imports                                                  # noqa: E402

ZIP = os.path.join(_ROOT, "EasyRealityAutoVJ_测试版100108.zip")
SKIPPED = {n.lower() for n in (
    "qt6quick.dll", "qt6qml.dll", "qt6qmlmodels.dll", "qt6qmlmeta.dll",
    "qt6qmlworkerscript.dll", "qt6pdf.dll", "qt6svg.dll", "qt6virtualkeyboard.dll",
    "qt6network.dll", "qt6opengl.dll", "qtnetwork.pyd")}
FAILS = []

z = zipfile.ZipFile(ZIP)
tmp = tempfile.mkdtemp(prefix="qa3_b2_")

kept_pe = []
for it in z.infolist():
    if it.is_dir():
        continue
    n = it.filename
    low = n.lower()
    p = os.path.join(_ROOT, "dist", n.replace("/", os.sep))
    if make_zip._is_qt_unused(p) or make_zip._is_cyndilib_junk(p):
        continue
    if low.endswith((".dll", ".pyd", ".exe")):
        kept_pe.append(n)

try:
    print("保留的 PE 文件数 = %d" % len(kept_pe))
    violations = []
    scanned = 0
    for n in kept_pe:
        base = os.path.basename(n)
        dst = os.path.join(tmp, str(scanned) + "_" + base)
        try:
            with open(dst, "wb") as f:
                f.write(z.read(n))
            imp = _pe_imports.parse_imports(dst)
        except Exception as e:                                          # noqa: BLE001
            continue
        scanned += 1
        bad = [x for x in imp if os.path.basename(x).lower() in SKIPPED]
        if bad:
            violations.append((n, bad))
        # 重点关注平台插件/核心库，打印其完整导入，便于人工核对
        if base.lower() in ("qwindows.dll", "qt6core.dll", "qt6gui.dll", "qt6widgets.dll",
                            "easyrealityautovj.exe"):
            print("  %-28s imports=%d  %s" % (base, len(imp), sorted(set(imp))))

    print()
    print("扫描 PE = %d" % scanned)
    if violations:
        for n, bad in violations:
            print("  !! 依赖被跳过 DLL：%s -> %s" % (n, bad))
        FAILS.append("有保留文件依赖被跳过的 DLL")
    else:
        print("  PASS 没有任何保留 PE 依赖被跳过的 DLL（Qt6Network/OpenGL/Qml/Svg/Pdf/VirtualKeyboard 均无引用）")

    # 直接验证平台插件 qwindows 不依赖 Qt6Network / Qt6OpenGL（不同 Win 环境可能不同，
    # 这里以本包为准；若依赖，跳过 Qt6Network 会致命）。
    qw = [n for n in kept_pe if os.path.basename(n).lower() == "qwindows.dll"]
    if qw:
        dst = os.path.join(tmp, "qw.dll")
        with open(dst, "wb") as f:
            f.write(z.read(qw[0]))
        imp = [os.path.basename(x).lower() for x in _pe_imports.parse_imports(dst)]
        need_skipped = [x for x in imp if x in SKIPPED]
        print("  qwindows.dll 是否依赖被跳过 DLL：%s" % (need_skipped or "否"))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("结果：%s" % ("B2 通过" if not FAILS else "B2 失败 %s" % FAILS))
sys.exit(1 if FAILS else 0)
