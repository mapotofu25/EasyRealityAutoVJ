# -*- coding: utf-8 -*-
"""打包 A 机「EasyReality 音频发送端」独立小 exe。

产物：`dist_sender/EasyReality_音频发送端.exe`（单文件，目标 < 30 MB）

## 为什么用 spec 文件而不是一串命令行
PyInstaller 的 `--exclude-module` 只能拦住 **Python 模块**，
拦不住顺着 **Qt 插件**自动跟随进来的 **DLL 本体**（与主程序"包体积"那一课同一根因）。
要砍 DLL 必须在分析完成后、打包前**过滤 `a.binaries`** —— 只有 spec 文件能做。
（2026-10-08 实测：纯命令行版 56.8 MB，其中 opengl32sw/Qt6Quick/Qt6Qml/
 Qt6Pdf/Qt6OpenGL/libcrypto 合计约 45 MB 全是白带的。）

## 用法
    venv\\Scripts\\python.exe tools/build_sender_exe.py

⚠ 必须用 `venv\\Scripts\\python.exe`（PyInstaller 装在 venv 里）。
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXE = "EasyReality_音频发送端"

# ---- 要排除的 Python 模块：发送端只用 soundcard + numpy + 一个小窗口 ----
_EXCL_MODULES = [
    "av", "cv2", "torch",
    # ★★ NDI —— 发送端走自研 UDP 协议，一个字节都不需要 cyndilib。
    #   它混进来是因为源码同目录、PyInstaller 顺着 import 图摸到了 ndi_out。
    "cyndilib", "ndi_out",
    # ★★ 曲风分类（onnxruntime 35 MB）—— 发送端只传音频，不做任何分析
    "onnxruntime", "audio_genre", "audio_engine", "media_manager",
    "engine", "beatgrid", "tagger", "genie", "match_engine", "colorfx",
    "postprocess", "glctx", "phrase_bt", "dxvnative", "music_cover",
    "music_meta", "genre_keywords", "genre_lookup", "genre_visual_default",
    "charge_engine", "stallwatch", "output_window", "ui_main", "panels",
    "hotkeys", "theme", "i18n", "i18n_map", "beatgrid",
    # PySide6 未用模块
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
    "PySide6.Qt3DCore", "PySide6.QtMultimedia", "PySide6.QtPdf",
    "PySide6.QtOpenGL", "PySide6.QtSvg", "PySide6.QtNetwork",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDBus",
    "PySide6.QtXml", "PySide6.QtConcurrent", "PySide6.QtPrintSupport",
    "PySide6.QtSerialPort",
]

# ---- 要排除的 DLL / 数据：匹配"路径里含某子串"即剔除 ----
#     ⚠ 只在 `a.binaries` / `a.datas` 上过滤，别用通配符乱删
_EXCL_BIN_SUBSTR = [
    # 19.7 MB —— 软件 OpenGL 兜底。主程序必须保留（无独显机器），
    #           发送端只是一个普通窗口（平台插件直接走 GDI/D3D），不需要
    "opengl32sw",
    # QML/Quick 运行时（Qt 插件链自动跟随进来的，合计 ~11 MB）
    "qt6quick", "qt6qml", "qt6qmlmodels", "qt6quickcontrols2", "qt6quicktemplates2",
    "qt6quickshapes", "qt6quickparticles", "qt6quickdialogs2", "qt6quickscenegraph",
    # 打印 / OpenGL / SVG 封装
    "qt6pdf", "qt6opengl", "qt6svg",
    # TLS（只用标准库 socket，不需要 Qt 的 TLS；libcrypto/libssl 合计 ~9 MB）
    "qt6network",
    # 一堆用不到的 Qt 插件目录（⚠ 统一用正斜杠 —— `_keep` 里已把路径归一化成 `/`，
    #   写成反斜杠要过两层转义，容易出错）
    "/qml/", "/qmltooling/", "/sqldrivers/", "/multimedia/",
    "/designer/", "/virtualkeyboard/", "/webview/", "/iconengines/",
    "/imageformats/qsvg", "/imageformats/qpdf", "/tls/",
    "/translations/",
    # ★ 追加：这些插件发送端一样用不到，而且**它们正是 libcrypto/libssl(9MB) 的挂载点**
    #   （虚拟键盘插件拖出 Qml 链、样式插件拖出 TLS）。主程序包体积膨胀的元凶也是它。
    #   ⚠ 保守起见只删**确定无关**的：平台插件里只留 qwindows（真窗口必需）、
    #     qminimal/qoffscreen 留着（无头环境下 PyInstaller 自检可能用到，成本极小）。
    "/platforminputcontexts/", "/virtualkeyboard/",
    "/styles/", "/generic/",
    "/imageformats/qgif", "/imageformats/qicns", "/imageformats/qico",
    "/imageformats/qtga", "/imageformats/qtiff", "/imageformats/qwbmp",
    "/imageformats/qwebp", "/imageformats/qjpeg",
]


def _build_spec():
    """生成 spec 文件（内容随排除清单变化，每次重写保证一致）。

    ⚠ 用 `replace` 占位符而**不是** `%` 格式化：spec 正文里有 `%d` / `%s`
      （给运行时打印用），用 `%` 格式化会直接抛 TypeError。
    """
    excl_mod = ",\n        ".join('"%s"' % m for m in _EXCL_MODULES)
    excl_bin = ",\n    ".join('"%s"' % s for s in _EXCL_BIN_SUBSTR)
    # ⚠ 统一用**正斜杠**写进 spec：Windows API 接受正斜杠，
    #   而写反斜杠要经过两层转义（模板 → 文件 → Python 解析），极易出错。
    src_dir = os.path.join(ROOT, "src").replace("\\", "/")
    script = os.path.join(ROOT, "lan_audio_sender.pyw").replace("\\", "/")
    # ★ 应用图标（仅图标相关）：exe 文件图标 + 随包资源（供运行时 setWindowIcon）。
    #   文件缺失 ⇒ icon=None、datas 不含它（打包照常，不影响业务）。
    icon = os.path.join(ROOT, "assets", "send.ico")
    if os.path.isfile(icon):
        icon_lit = repr(icon.replace("\\", "/"))
        datas_lit = "(%r, 'assets')" % icon.replace("\\", "/")
    else:
        icon_lit, datas_lit = "None", ""
    spec = '''# -*- mode: python ; coding: utf-8 -*-
# 本文件由 tools/build_sender_exe.py 自动生成，请勿手改。
import os, sys

a = Analysis(
    ["@@SCRIPT@@"],
    pathex=["@@SRCDIR@@"],
    binaries=[],
    datas=[
        @@DATAS@@
    ],
    hiddenimports=[
        "soundcard", "soundcard.mediafoundation",
        "wasapi_capture", "lan_audio", "lan_audio_send",
        "appicon",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        @@EXCLMOD@@
    ],
    noarchive=False,
)

# ---- ★★ 关键：过滤 DLL / 数据 ----
#  `--exclude-module` 拦不住顺着 Qt 插件自动跟随进来的 DLL 本体，
#  只有在这里对 `a.binaries` / `a.datas` 动手才有效。
_EXCL = [
    @@EXCLBIN@@
]

def _keep(entry):
    # ⚠ 归一化分隔符时**用 chr(92) 拼反斜杠**，不要在自动生成的 spec 里写
    #   反斜杠字面量 —— 那要经过"模板字符串 → spec 文件 → Python 解析"两层转义，
    #   极易写错（2026-10-08 实测踩到 SyntaxError: unterminated string literal）。
    _BS = chr(92)
    name = entry[0].replace(_BS, "/").lower()
    for s in _EXCL:
        if s.replace(_BS, "/") in name:
            return False
    return True

_before = len(a.binaries) + len(a.datas)
a.binaries = [b for b in a.binaries if _keep(b)]
a.datas = [d for d in a.datas if _keep(d)]
print("=== 送端打包：二进制/数据 {} -> {}（剔除 {} 项）==="
      .format(_before, len(a.binaries) + len(a.datas),
              _before - len(a.binaries) - len(a.datas)))

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="@@EXE@@",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=@@ICON@@,
)
'''
    for k, v in (("@@SCRIPT@@", script), ("@@SRCDIR@@", src_dir),
                 ("@@EXCLMOD@@", excl_mod), ("@@EXCLBIN@@", excl_bin),
                 ("@@EXE@@", EXE), ("@@ICON@@", icon_lit),
                 ("@@DATAS@@", datas_lit)):
        spec = spec.replace(k, v)
    path = os.path.join(ROOT, "build_sender", "sender.spec")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(spec)
    return path


def main():
    pyi = os.path.join(ROOT, "venv", "Scripts", "pyinstaller.exe")
    if not os.path.isfile(pyi):
        print("找不到 PyInstaller：%s" % pyi)
        return 1
    # ★ 构建/输出目录支持环境变量覆盖（与 build_exe.py / build_erlib_exe.py 同一约定）：
    #   默认仍是 dist_sender / build_sender；要打去别处（避让正在运行/被占用的旧产物）时
    #   用 `SENDER_DIST=dist_icon SENDER_BUILD=build_icon`。
    build = os.environ.get("SENDER_BUILD", "build_sender")
    dist = os.environ.get("SENDER_DIST", "dist_sender")
    spec = _build_spec()
    args = [
        pyi, "--noconfirm", "--clean",
        "--distpath", os.path.join(ROOT, dist),
        "--workpath", os.path.join(ROOT, build),
        spec,
    ]
    r = subprocess.run(args, cwd=ROOT)
    out = os.path.join(ROOT, dist, EXE + ".exe")
    if os.path.isfile(out):
        print("\n构建完成：%s（%.1f MB）" % (out, os.path.getsize(out) / 1048576.0))
    else:
        print("\n⚠ 未找到产物（exit=%d）" % r.returncode)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
