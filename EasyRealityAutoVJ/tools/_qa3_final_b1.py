# -*- coding: utf-8 -*-
"""QA(严过关) 出包前独立复验 —— B1：make_zip 跳过逻辑（对真实包内容判定）。

zip 100108 是**改动前**打的（line two 跳过规则尚未生效）⇒ 它正好是
"假如规则生效会跳过什么" 的真实输入。直接用 make_zip 的判定函数逐个文件判定，
并用 PE 导入表独立验证 cyndilib 运行时只吃 Processing.NDI.Lib.x64.dll。
"""
import os
import sys
import shutil
import tempfile
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))
import make_zip                                                     # noqa: E402
import _pe_imports                                                  # noqa: E402

ZIP = os.path.join(_ROOT, "EasyRealityAutoVJ_测试版100108.zip")
FAILS = []


def check(name, ok, extra=""):
    print(("  PASS " if ok else "  FAIL ") + name + (("   | " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


z = zipfile.ZipFile(ZIP)
infos = z.infolist()
print("包内条目总数（含目录）=%d" % len(infos))

cy_n = cy_b = qt_n = qt_b = 0
cy_names, qt_names = [], []
for it in infos:
    if it.is_dir():
        continue
    name = it.filename
    # 用与 make_zip 相同的绝对风格路径喂判定函数
    p = os.path.join(_ROOT, "dist", name.replace("/", os.sep))
    if make_zip._is_cyndilib_junk(p):
        cy_n += 1
        cy_b += it.file_size
        cy_names.append(name)
    elif make_zip._is_qt_unused(p):
        qt_n += 1
        qt_b += it.file_size
        qt_names.append(name)

print()
print("== 会被跳过的文件 ==")
print("cyndilib 无用文件：%d 个 / %.1f MB" % (cy_n, cy_b / 1048576))
print("Qt 未用文件    ：%d 个 / %.1f MB" % (qt_n, qt_b / 1048576))
print("合计省         ：%.1f MB" % ((cy_b + qt_b) / 1048576))
print()
print("cyndilib 跳过样例：")
for n in sorted(cy_names)[:12]:
    print("   ", n)
print("Qt 跳过清单：")
for n in sorted(qt_names):
    print("   ", n)

# ---- 关键必需文件绝不能被跳过 ----
names = [i.filename for i in infos if not i.is_dir()]
def find(sub):
    return [n for n in names if sub.lower() in n.lower()]

ndi_dlls = find("ndi.lib") + find("processing.ndi")
print()
print("包内 NDI 相关 dll：", [os.path.basename(n) for n in ndi_dlls])
x64 = [n for n in names if os.path.basename(n).lower() == "processing.ndi.lib.x64.dll"]
check("Processing.NDI.Lib.x64.dll 存在且**不**被跳过",
      bool(x64) and not any(make_zip._is_cyndilib_junk(os.path.join(_ROOT, "dist", n.replace("/", os.sep)))
                            or make_zip._is_qt_unused(os.path.join(_ROOT, "dist", n.replace("/", os.sep)))
                            for n in x64),
      "found=%s" % [os.path.basename(n) for n in x64])

for core in ("Qt6Core.dll", "Qt6Gui.dll", "Qt6Widgets.dll"):
    hit = [n for n in names if os.path.basename(n).lower() == core.lower()]
    skipped = [n for n in hit if make_zip._is_qt_unused(os.path.join(_ROOT, "dist", n.replace("/", os.sep)))]
    check("%s 存在且不被跳过" % core, bool(hit) and not skipped,
          "found=%d skipped=%d" % (len(hit), len(skipped)))

qw = [n for n in names if os.path.basename(n).lower() == "qwindows.dll"]
check("qwindows 平台插件存在且不被跳过",
      bool(qw) and not any(make_zip._is_qt_unused(os.path.join(_ROOT, "dist", n.replace("/", os.sep))) for n in qw),
      "found=%s" % [n for n in qw])

# ---- 独立 PE 验证：cyndilib 的 .pyd 只 import x64 那份 NDI ----
tmp = tempfile.mkdtemp(prefix="qa3_b1_")
try:
    pdbs = [n for n in names if n.lower().endswith(".pyd") and "/cyndilib/" in n.replace("\\", "/").lower()]
    plugin_abs = [n for n in qw]
    extract = [n for n in (pdbs[:5] + plugin_abs) if not n.lower().endswith((".py", ".html"))]
    print()
    print("提取做 PE 导入分析：", [os.path.basename(n) for n in extract])
    for n in extract:
        dst = os.path.join(tmp, os.path.basename(n))
        with open(dst, "wb") as f:
            f.write(z.read(n))
        try:
            imp = _pe_imports.parse_imports(dst)
        except Exception as e:                                         # noqa: BLE001
            print("   !! %s: %s" % (os.path.basename(n), e))
            continue
        ndi = [x for x in imp if "ndi" in x.lower()]
        print("   %-32s imports=%d  NDI=%s" % (os.path.basename(n), len(imp), ndi))
        if ndi:
            check("%s 只引用 Processing.NDI.Lib.x64.dll（不含 DirectShow/UWP）" % os.path.basename(n),
                  all("x64" in x.lower() for x in ndi) and
                  not any(("directshow" in x.lower() or "uwp" in x.lower()) for x in ndi))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("结果：%s" % ("B1 通过" if not FAILS else "B1 失败 %s" % FAILS))
sys.exit(1 if FAILS else 0)
