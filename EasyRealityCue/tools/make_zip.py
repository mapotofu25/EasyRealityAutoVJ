# -*- coding: utf-8 -*-
r"""打发布压缩包：`EasyRealityCue_<版本>.zip`（版本号与 exe 内严格同源）。

用法：
    venv\Scripts\python tools\make_zip.py              # 打包 dist
    venv\Scripts\python tools\make_zip.py 2026.10.01.01  # 只校验版本是否一致

包内容（**zip 顶层** = exe + _internal + 使用说明.txt）：
    EasyRealityCue.exe
    _internal\...
    使用说明.txt

⚠ 瘦身策略：**写包时按文件名跳过**用不到的 Qt 模块 / 插件，**绝不删 dist 里的东西**。
    原因：本沙箱对批量删除有安全钩子（每回合约 40~50 个文件就被终止），
    "先删再打包"会连累整个打包失败（参考项目踩过）。写包时跳过则完全不碰删除。
    跳过的每一份都是本应用（只用 QtCore/QtGui/QtWidgets）用不到的：
    Quick/Qml 家族、Pdf、Svg、VirtualKeyboard、Network/TLS、OpenGL/opengl32sw、Multimedia。
    跳过后由 tools/smoke_exe.py 对**解压出来的**包再跑一次冒烟，证明裁剪没删错东西。
"""
import os
import shutil
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
sys.path.insert(0, TOOLS)
import build_version                                          # noqa: E402

APP_NAME = "EasyRealityCue"
# ★ 2026-10-11：与 build_exe.py 同源 —— 输出目录可用 `ERCUE_DIST` 覆盖
#   （撞"安全删除钩子"或 dist 被运行中的 exe 占用时，换新目录打包）。
DIST = os.path.join(ROOT, os.environ.get("ERCUE_DIST", "dist"), APP_NAME)
MANUAL = "使用说明.txt"


# —— PySide6 里本应用用不到的 Qt DLL（顶层 _internal/PySide6/ 下，按文件名匹配）——
# 本应用只 import QtCore/QtGui/QtWidgets（逐文件扫过 src/ercue/）。这些 DLL 是
# Qt 各类**插件**沿 PE 导入表拉进来的，--exclude-module 拦不住本体，只能写包时跳过。
_QT_UNUSED_TOP = {
    "qt6quick.dll", "qt6qml.dll", "qt6qmlmodels.dll", "qt6qmlmeta.dll",
    "qt6qmlworkerscript.dll",       # QML/Quick 家族：本应用零使用
    "qt6pdf.dll",                   # 仅 imageformats/qpdf.dll 用
    "qt6svg.dll",                   # 仅 qsvgicon / qsvg 插件用
    "qt6virtualkeyboard.dll",       # 仅 qtvirtualkeyboardplugin 用
    "qt6network.dll",               # 仅 networkinformation / tls 插件用
    "qt6opengl.dll", "qt6openglwidgets.dll",   # 本应用不碰 OpenGL / QOpenGLWidget
    "qt6multimedia.dll", "qt6multimediawidgets.dll",  # 本 wheel 无多媒体后端（实测格式数=0）
    "qt6sql.dll", "qt6test.dll", "qt6designer.dll", "qt6help.dll", "qt6uitools.dll",
    "opengl32sw.dll",               # Qt 自带的 Mesa 软件 OpenGL（20MB），仅 Quick 用
    "qtnetwork.pyd",                # 对应 Python 绑定
}

# —— 相对 PySide6/plugins/ 的 (插件类型目录, 文件名)，本应用用不到 ——
_QT_UNUSED_PLUGINS = {
    ("platforminputcontexts", "qtvirtualkeyboardplugin.dll"),   # 拉进整个 Qml/Quick 家族
    ("imageformats", "qpdf.dll"),
    ("imageformats", "qsvg.dll"),
    ("iconengines", "qsvgicon.dll"),
    ("networkinformation", "qnetworklistmanager.dll"),
    ("tls", "qcertonlybackend.dll"),
    ("tls", "qopensslbackend.dll"),
    ("tls", "qschannelbackend.dll"),
    ("generic", "qtuiotouchplugin.dll"),   # TUIO 触摸设备输入插件（桌面鼠标场景不加载）
}


def _is_qt_unused(path):
    """判断 `path` 是否是 PySide6 里本应用用不到的 Qt DLL / 插件。"""
    p = path.replace("\\", "/").lower()
    if "/pyside6/" not in p:
        return False
    base = os.path.basename(p)
    if base in _QT_UNUSED_TOP:
        return True
    if os.path.basename(os.path.dirname(p)) == "multimedia":
        return True                                            # multimedia 插件整目录（无后端）
    return (os.path.basename(os.path.dirname(p)), base) in _QT_UNUSED_PLUGINS


def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def main():
    date_key, seq, ver = build_version.read_build_info()
    if seq <= 0:
        print("!! 还没打包过（src/ercue/_build_ver.py 里没有版本号），先跑 tools/build_exe.py")
        return 1
    zip_name = build_version.zip_name(date_key, seq)
    zip_path = os.path.join(ROOT, zip_name)

    if len(sys.argv) > 1 and sys.argv[1] != ver:
        print("!! 参数版本 %s 与构建版本 %s 不一致，已按构建版本命名" % (sys.argv[1], ver))

    if not os.path.isdir(DIST):
        print("!! 找不到 dist/%s，先跑 tools/build_exe.py" % APP_NAME)
        return 1
    exe = os.path.join(DIST, APP_NAME + ".exe")
    if not os.path.exists(exe):
        print("!! dist 里没有 %s.exe" % APP_NAME)
        return 1

    # 提示「exe 是否比源码旧」（漏打包最常见的坑）
    exe_mtime = os.path.getmtime(exe)
    stale = []
    for root, dirs, files in os.walk(os.path.join(ROOT, "src")):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith(".py") and os.path.getmtime(os.path.join(root, f)) > exe_mtime:
                stale.append(os.path.relpath(os.path.join(root, f), ROOT))
    if stale:
        print("!! 以下源文件比 exe 新，包内可能不是最新代码：")
        for s in stale[:15]:
            print("     " + s)

    print("版本 %s（%s 第 %d 次生成）" % (ver, date_key, seq))
    print("包名 %s" % zip_name)

    # 把说明书放到 dist 顶层（随包发出；dist 里不保留也无所谓）
    if os.path.exists(os.path.join(ROOT, MANUAL)):
        shutil.copy(os.path.join(ROOT, MANUAL), os.path.join(DIST, MANUAL))

    dist_size = _dir_size(DIST)
    n = 0
    skipped = 0
    skipped_bytes = 0
    skipped_names = []
    t0 = time.time()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for root, _dirs, files in os.walk(DIST):
            for f in files:
                p = os.path.join(root, f)
                if _is_qt_unused(p):
                    skipped += 1
                    try:
                        skipped_bytes += os.path.getsize(p)
                    except OSError:
                        pass
                    skipped_names.append(os.path.relpath(p, DIST))
                    continue
                # zip 顶层 = exe + _internal + 使用说明.txt（扁平，不加外层目录）
                z.write(p, os.path.relpath(p, DIST))
                n += 1

    zip_bytes = os.path.getsize(zip_path)
    print("-" * 60)
    print("体积报告")
    print("  dist 解包合计      : %8.1f MB" % (dist_size / 1048576.0))
    print("  写入 zip 的条目    : %d 个" % n)
    print("  跳过（未用 Qt）    : %d 个 / %.1f MB" % (skipped, skipped_bytes / 1048576.0))
    print("  裁剪后包内解包体积 : %8.1f MB" % ((dist_size - skipped_bytes) / 1048576.0))
    print("  最终 zip           : %8.1f MB  (%s)" % (zip_bytes / 1048576.0, zip_path))
    print("  压缩用时           : %.0f 秒" % (time.time() - t0))
    if skipped_names:
        print("  跳过清单：")
        for name in sorted(skipped_names):
            print("     - " + name)
    print("-" * 60)
    print("下一步：venv\\Scripts\\python tools\\smoke_exe.py --from-zip")
    return 0


if __name__ == "__main__":
    sys.exit(main())
