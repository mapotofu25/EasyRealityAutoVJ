# -*- coding: utf-8 -*-
"""临时把「项目未使用的 Qt 模块」改名/还原（取证用，跑完务必 restore）。

判定依据（全部有据可查）：
  - src/ 里只 import QtCore / QtGui / QtWidgets（grep 结论）；
  - PE 导入表显示 Qt6Gui 只依赖 Qt6Core，Qt6Widgets 只依赖 Qt6Gui/Qt6Core，
    没有任何被加载的模块/插件依赖下列 DLL；
  - 下列 Qt 模块对应的插件（qsvgicon/qsvg/qpdf/qtvirtualkeyboard/network/tls）
    只在用到 SVG/PDF/虚拟键盘/网络时才加载，本项目全部用不到。

用法：
    venv\\Scripts\\python -u tools\\_rename_qt.py off
    venv\\Scripts\\python -u tools\\_rename_qt.py on
    venv\\Scripts\\python -u tools\\_rename_qt.py status
同时作用于 dist_new（exe 用）与 venv site-packages（源码界面自测用）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIRS = [
    os.path.join(ROOT, "dist_new", "EasyRealityAutoVJ", "_internal", "PySide6"),
    os.path.join(ROOT, "venv", "Lib", "site-packages", "PySide6"),
]
# 相对 PySide6/ 的路径
TARGETS = [
    # —— Quick / QML 全家（无 Python 绑定，也无任何插件依赖）——
    "Qt6Quick.dll", "Qt6Qml.dll", "Qt6QmlModels.dll", "Qt6QmlMeta.dll",
    "Qt6QmlWorkerScript.dll",
    # —— PDF（仅 imageformats/qpdf.dll 用）——
    "Qt6Pdf.dll", os.path.join("plugins", "imageformats", "qpdf.dll"),
    # —— SVG（仅 qsvgicon / qsvg 插件用）——
    "Qt6Svg.dll",
    os.path.join("plugins", "iconengines", "qsvgicon.dll"),
    os.path.join("plugins", "imageformats", "qsvg.dll"),
    # —— 虚拟键盘 ——
    "Qt6VirtualKeyboard.dll",
    os.path.join("plugins", "platforminputcontexts", "qtvirtualkeyboardplugin.dll"),
    # —— TUIO 多点触控设备插件（硬依赖 Qt6Network，故一并跳过）——
    os.path.join("plugins", "generic", "qtuiotouchplugin.dll"),
    # —— 网络（含绑定的 QtNetwork.pyd 与 networkinformation / tls 插件）——
    "Qt6Network.dll", "QtNetwork.pyd",
    os.path.join("plugins", "networkinformation", "qnetworklistmanager.dll"),
    os.path.join("plugins", "tls", "qcertonlybackend.dll"),
    os.path.join("plugins", "tls", "qopensslbackend.dll"),
    os.path.join("plugins", "tls", "qschannelbackend.dll"),
    # —— QtOpenGL 模块（项目用 glfw 建 GL 上下文，不碰 QOpenGLWidget）——
    "Qt6OpenGL.dll",
]


def act(mode):
    for base in DIRS:
        tag = "dist " if "dist_new" in base else "venv "
        if mode == "status":
            print(f"[{tag}] {base}")
        for rel in TARGETS:
            orig = os.path.join(base, rel)
            bak = orig + ".bak"
            if mode == "off":
                if os.path.exists(orig):
                    os.rename(orig, bak)
                elif not os.path.exists(bak):
                    print(f"  ! {tag} MISSING: {rel}")
            elif mode == "on":
                if os.path.exists(bak):
                    os.rename(bak, orig)
                elif not os.path.exists(orig):
                    print(f"  ! {tag} MISSING: {rel}")
            else:  # status
                o = os.path.exists(orig)
                b = os.path.exists(bak)
                if not o or b:
                    print(f"    {rel:52s} orig={o!s:5s} bak={b}")
    if mode == "status":
        print()
    else:
        print(f"done: {mode}")


if __name__ == "__main__":
    act(sys.argv[1] if len(sys.argv) > 1 else "status")
