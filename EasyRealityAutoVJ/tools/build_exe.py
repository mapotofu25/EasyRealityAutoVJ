# -*- coding: utf-8 -*-
r"""PyInstaller 打包脚本：生成 P:\AutoVJ\dist\EasyRealityAutoVJ\EasyRealityAutoVJ.exe
用法: venv\Scripts\python tools\build_exe.py
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
# 可通过环境变量切换 PyInstaller 工作目录，避开被占用的旧 build 目录。
# 默认仍保持历史路径，便于常规本地打包。
BUILD_ROOT = os.environ.get("AUTOVJ_BUILD", os.path.join(ROOT, "build"))
DIST_ROOT = os.environ.get("AUTOVJ_DIST", os.path.join(ROOT, "dist"))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_version          # noqa: E402  同目录的版本号工具

# 版本号 = 日期 + 当日序号：本次打包即「当天第 N 次生成」，写进 src/_build_ver.py
# （version.py 读它 → 打进 exe 的版本号就是这个，压缩包名也用它）
DATE_KEY, SEQ, VER = build_version.bump()
print("=" * 60)
print(f"版本 {VER}    （{DATE_KEY} 当天第 {SEQ} 次生成）")
print(f"压缩包将命名为 {build_version.zip_name(DATE_KEY, SEQ)}")
print("=" * 60)

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm",
    "--windowed",
    "--name", "EasyRealityAutoVJ",
    "--workpath", BUILD_ROOT,
    "--distpath", DIST_ROOT,
    "--specpath", BUILD_ROOT,
    "--collect-all", "soundcard",
    "--collect-all", "onnxruntime",
    "--collect-all", "mutagen",
    "--collect-all", "av",
    "--hidden-import", "cffi",
    "--hidden-import", "tags_def",
    "--hidden-import", "tagger",
    "--hidden-import", "audio_genre",
    "--hidden-import", "music_meta",
    "--hidden-import", "genre_lookup",
    "--hidden-import", "genre_keywords",
    "--hidden-import", "charge_engine",
    "--hidden-import", "fp",
    "--hidden-import", "match_engine",
    "--hidden-import", "postprocess",
    "--hidden-import", "colorfx",
    "--hidden-import", "i18n",
    "--hidden-import", "i18n_map",
    "--hidden-import", "version",
    "--hidden-import", "_build_ver",
    # ★ 应用图标定位（main.py 在 try 里惰性 `import appicon`，静态分析可能漏 ⇒ 显式给出）
    "--hidden-import", "appicon",
    "--hidden-import", "ndi_out",
    "--hidden-import", "dxvnative",
    "--hidden-import", "asio_engine",
    "--hidden-import", "wasapi_capture",
    # ★ 局域网音频（自研轻量协议）：audio_engine 静态 import 了它们，
    #   正常情况下 PyInstaller 能自动发现；显式列出是为了防止
    #   "某个 import 被挪进 try 里" 之后打包静默漏掉（这类坑本项目踩过多次）。
    "--hidden-import", "lan_audio",
    "--hidden-import", "lan_audio_recv",
    # ★★ 2026-10-08（erlib 第二步）：导入曲库分析包（.eravjlib）。
    #   ⚠ `erlib` 是**包**，必须逐个列子模块 —— PyInstaller 不会自己跟进去。
    #   ⚠ `import erlib.transfer` 会先执行 `erlib/__init__.py`，而它又 import 了
    #     `scanner` / `format` ⇒ 一并列上。scanner 的依赖（fp / beatgrid /
    #     music_meta / music_cover / audio_genre）主程序本来就有，不用重复加。
    #   ⚠ **不列 `erlib.app`** —— 那是 erlib 自己的界面，主程序用不上。
    "--hidden-import", "erlib",
    "--hidden-import", "erlib.format",
    "--hidden-import", "erlib.scanner",
    "--hidden-import", "erlib.transfer",
    "--hidden-import", "imported_lib",
    "--hidden-import", "lan_audio_send",
    "--hidden-import", "glctx",
    "--hidden-import", "SpoutGL",
    "--hidden-import", "SpoutGL.enums",
    # —— 排除项目用不到的 Qt 模块（2026-10-01 取证：src/ 只 import QtCore/QtGui/QtWidgets）——
    #   ⚠ 重要：这些 Qt6*.dll（Quick/Qml/Pdf/Svg/Network/OpenGL/VirtualKeyboard）是
    #   **Qt 各类插件**（qsvgicon / qsvg / qpdf / qtvirtualkeyboard / tls …）的**二进制依赖**，
    #   PyInstaller 会沿 PE 导入表自动跟随，**--exclude-module 拦不住 DLL 本体**，
    #   它只能拦住 Python 绑定（如 PySide6.QtNetwork 的 .pyd）并阻止对应 hook 触发。
    #   因此 DLL 本体另由 tools/make_zip.py 的 _is_qt_unused 在写包时跳过（可靠，且不碰 dist）。
    #   两处配合：这里少收绑定/hook，make_zip 兜底剔除插件与 DLL。取证见 tools/_qt_reach.md。
    "--exclude-module", "PySide6.QtQuick",
    "--exclude-module", "PySide6.QtQml",
    "--exclude-module", "PySide6.QtPdf",
    "--exclude-module", "PySide6.QtSvg",
    "--exclude-module", "PySide6.QtNetwork",
    "--exclude-module", "PySide6.QtOpenGL",
    "--exclude-module", "PySide6.QtOpenGLWidgets",
    "--collect-all", "cyndilib",
    # GPU 解码（实验）：glfw 用来建离屏 GL 上下文（自带 glfw3.dll）；
    # _dxvlz.dll 是自研的 DXV3 LZ 解包（zig 编译，187KB），必须随包。
    # dxvnative.dll_path() 在打包环境下解析到 _internal（= _MEIPASS），与此处目标一致。
    "--collect-all", "glfw",
    "--add-binary", os.path.join(SRC, "_dxvlz.dll") + os.pathsep + ".",
    # ASIO 低延迟输入的原生宿主（zig 编译，~195KB）。asio_engine.dll_path() 在打包
    # 环境下解析到 _MEIPASS（= _internal），与此处目标 `.` 一致。
    # ⚠ 它是用 `-nostdinc++ -fno-exceptions -fno-rtti` 编的，**不依赖任何 C++ 运行时 DLL**
    #   （operator new/delete 在 asiohost.cpp 里自带垫片），所以不需要额外带 libstdc++。
    "--add-binary", os.path.join(SRC, "_asiohost.dll") + os.pathsep + ".",
    "--add-data", os.path.join(ROOT, "assets", "models") + os.pathsep + "assets" + os.path.sep + "models",
]
# —— 应用图标（★ 仅图标相关；文件缺失则自动跳过，绝不影响打包）——
#   --icon 写入 exe 文件图标；--add-data 让运行时能在 _MEIPASS/assets/autovj.ico 取到。
_icon = os.path.join(ROOT, "assets", "autovj.ico")
if os.path.isfile(_icon):
    cmd += ["--icon", _icon, "--add-data", _icon + os.pathsep + "assets"]
    print("图标：%s" % _icon)
else:
    print("★ 图标缺失，跳过（%s）" % _icon)
cmd.append(os.path.join(SRC, "main.py"))
print(" ".join(cmd))
rc = subprocess.call(cmd)

# ---- 打包后瘦身：cyndilib 自带 3 个平台的 NDI 运行库，只用得上 x64 ----
#   Processing.NDI.Lib.x64.dll        28.8MB  ← 在用（Windows x64 运行库）
#   Processing.NDI.Lib.DirectShow.dll 42MB    DirectShow filter：老采集软件经 DShow
#                                     访问 NDI 源用的，本软件不通过 DShow 收发
#   Processing.NDI.Lib.UWP.dll        24MB    UWP/微软商店应用专用，Python 用不了
# 另有 .cpp/.pyx/Linux 占位文件，运行时全用不上 → 共省约 80MB。
if rc == 0:
    internal = os.path.join(DIST_ROOT, "EasyRealityAutoVJ", "_internal", "cyndilib")
    removed = 0
    try:
        if os.path.isdir(internal):
            for root, dirs, files in os.walk(internal):
                for f in files:
                    low = f.lower()
                    p = os.path.join(root, f)
                    if (low.endswith((".cpp", ".pyx", ".pxd", ".html"))
                            or "directshow" in low or "uwp" in low
                            or low.endswith((".lib", ".so", ".a"))):
                        os.remove(p)
                        removed += 1
            for root, dirs, files in os.walk(internal, topdown=False):
                if not os.listdir(root) and root != internal:
                    os.rmdir(root)
        print(f"cyndilib 瘦身: 删除 {removed} 个文件")
    except Exception as e:
        # safe-delete 钩子可能拦截批量删除——瘦身失败不影响打包结果（exe 已生成），
        # 可另行手动瘦身，勿让 rc 变非零。
        print(f"cyndilib 瘦身被拦截（可忽略，手动再删即可）: {e}")
    print(f"打包完成 → 版本 {VER}（{DATE_KEY} 第 {SEQ} 次生成）；"
          f"压缩包命名 {build_version.zip_name(DATE_KEY, SEQ)}")
sys.exit(rc)
