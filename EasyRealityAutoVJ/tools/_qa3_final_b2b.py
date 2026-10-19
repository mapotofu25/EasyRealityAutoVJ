# -*- coding: utf-8 -*-
"""QA(严过关) B2b：make_zip 两个判定函数的过宽/漏判单元测试（纯字符串，独立取证）。"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import make_zip                                                     # noqa: E402

FAILS = []
R = r"P:\AutoVJ\dist\EasyRealityAutoVJ"


def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + (("   | " + extra) if extra else ""))
    if not cond:
        FAILS.append(name)


PS = R + r"\_internal\PySide6"

print("== _is_qt_unused：应当跳过 ==")
for f in ("Qt6Quick.dll", "Qt6Qml.dll", "Qt6QmlModels.dll", "Qt6QmlMeta.dll",
          "Qt6QmlWorkerScript.dll", "Qt6Pdf.dll", "Qt6Svg.dll", "Qt6VirtualKeyboard.dll",
          "Qt6Network.dll", "Qt6OpenGL.dll", "QtNetwork.pyd"):
    p = os.path.join(PS, f)
    ck("skip %s" % f, make_zip._is_qt_unused(p) is True)
for sub in ("platforminputcontexts\\qtvirtualkeyboardplugin.dll", "imageformats\\qpdf.dll",
            "imageformats\\qsvg.dll", "iconengines\\qsvgicon.dll",
            "networkinformation\\qnetworklistmanager.dll", "tls\\qcertonlybackend.dll",
            "tls\\qopensslbackend.dll", "tls\\qschannelbackend.dll"):
    p = os.path.join(PS, "plugins", sub)
    ck("skip plugins/%s" % sub.replace("\\", "/"), make_zip._is_qt_unused(p) is True)

print("\n== _is_qt_unused：必须**不**跳过（过宽检查）==")
for f in ("Qt6Core.dll", "Qt6Gui.dll", "Qt6Widgets.dll", "Qt6OpenGLWidgets.dll",
          "Qt6SvgWidgets.dll", "Qt6Concurrent.dll", "Qt6PrintSupport.dll", "Qt6DBus.dll",
          "Qt6Sql.dll", "Qt6Test.dll", "Qt6Xml.dll", "Qt6StateMachine.dll",
          "Qt6Designer.dll", "Qt6Charts.dll", "Qt6Multimedia.dll"):
    p = os.path.join(PS, f)
    ck("keep %s" % f, make_zip._is_qt_unused(p) is False)
ck("keep platforms/qwindows.dll", make_zip._is_qt_unused(os.path.join(PS, "plugins", "platforms", "qwindows.dll")) is False)
ck("keep styles/qwindowsvistastyle.dll",
   make_zip._is_qt_unused(os.path.join(PS, "plugins", "styles", "qwindowsvistastyle.dll")) is False)
ck("非 PySide6 路径下的 Qt6Network.dll 不被本规则处理",
   make_zip._is_qt_unused(r"P:\AutoVJ\dist\EasyRealityAutoVJ\_internal\Qt6Network.dll") is False)

print("\n== _is_cyndilib_junk ==")
CY = R + r"\_internal\cyndilib"
ck("skip cyndilib/*.cpp", make_zip._is_cyndilib_junk(os.path.join(CY, "x.cpp")) is True)
ck("skip cyndilib/... .pyx", make_zip._is_cyndilib_junk(os.path.join(CY, "x.pyx")) is True)
ck("skip cyndilib DirectShow dll",
   make_zip._is_cyndilib_junk(os.path.join(CY, "Processing.NDI.Lib.x64.DirectShow.dll")) is True)
ck("skip cyndilib UWP dll",
   make_zip._is_cyndilib_junk(os.path.join(CY, "Processing.NDI.Lib.x64.UWP.dll")) is True)
ck("**保留** cyndilib 主 NDI dll",
   make_zip._is_cyndilib_junk(os.path.join(CY, "Processing.NDI.Lib.x64.dll")) is False)
ck("保留 cyndilib .pyd", make_zip._is_cyndilib_junk(os.path.join(CY, "callback.cp313-win_amd64.pyd")) is False)
ck("非 cyndilib 目录里含 UWP/directshow 字样不被误伤",
   make_zip._is_cyndilib_junk(R + r"\_internal\foo\my_uwp_directshow_thing.dll") is False)

print()
print("结果：%s" % ("B2b 通过" if not FAILS else "B2b 失败 %s" % FAILS))
sys.exit(1 if FAILS else 0)
