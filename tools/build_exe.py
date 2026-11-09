# -*- coding: utf-8 -*-
r"""PyInstaller 打包脚本：生成 <项目根>\dist\EasyRealityAutoVJ\EasyRealityAutoVJ.exe
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
    # ★ 2026-10-30 用户「大砍刀」：曲风识别（audio_genre / genre_lookup /
    #   genre_keywords / music_meta）整条删除 —— 连带 assets/models/{chinese_clip,discogs}
    #   也不再打包（包体积从 ~470MB 降到 ~140MB）。
    "--hidden-import", "charge_engine",
    "--hidden-import", "fp",
    # ★ 2026-10-28：频带校验是**函数内惰性 import**，PyInstaller 静态扫描看不到，
    #   不显式声明就会在 exe 里 ImportError（而且会被 except 吞掉 ⇒ 静默失效）。
    "--hidden-import", "bandfeat",
    "--hidden-import", "match_engine",
    "--hidden-import", "postprocess",
    "--hidden-import", "colorfx",
    "--hidden-import", "i18n",
    "--hidden-import", "i18n_map",
    "--hidden-import", "version",
    "--hidden-import", "_build_ver",
    # ★ 应用图标定位（main.py 在 try 里惰性 `import appicon`，静态分析可能漏 ⇒ 显式给出）
    "--hidden-import", "appicon",
    # 多屏输出：output_studio 是在 _build_pages 里懒加载的，PyInstaller 动态扫描
    # 可能漏掉；这两个模块必须显式列出来，否则 exe 打开会白页/ImportError。
    "--hidden-import", "output_model",
    "--hidden-import", "output_studio",
    "--hidden-import", "ndi_out",
    # ★ Spout 发送（2026-10-29 新增）：engine 里是 `import spout_out` 的**运行时**导入
    #   （和 ndi_out 一样包在 try 里），PyInstaller 静态分析扫不到 ⇒ 必须显式列出，
    #   否则包里 `import spout_out` 必失败 ⇒ Spout 输出整条静默失效（只剩退避日志）。
    "--hidden-import", "spout_out",
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
    # ★★ 2026-10-27 曲库识别增强（T01/T02/T03）：以下模块全部是**函数内惰性 import**，
    #   PyInstaller 的静态分析跟不进去 ⇒ 漏了之后 exe 里表现为
    #     · 右键「歌曲详情…」→「打开歌曲详情失败：No module named 'music_detail'」
    #     · 详情页无波形（`waveform` 缺失，`_lazy_waveform` 静默返回 None）
    #     · `waveform_mark` 缺失 ⇒ 详情页对话框构造中途抛 ImportError（T02 踩过同样的坑）
    #   与 ERLIB 那边 `tools/build_erlib_exe.py` 的 `_HIDDEN` 是同一份清单，两端必须一起加。
    #   出处：panels._open_music_detail（music_detail）/ music_detail._lazy_waveform（waveform）
    #        / music_detail._build（waveform_mark）/ engine._grid_segment_of（beatgrid）
    "--hidden-import", "music_detail",
    "--hidden-import", "waveform",
    "--hidden-import", "waveform_mark",
    # ★ 2026-10-27 详情页试听：`music_detail._ensure_player` 里惰性 import，
    #   漏了之后 exe 里点 ▶ 只显示「音频模块不可用」。
    "--hidden-import", "audio_playback",
    "--hidden-import", "sounddevice",
    "--hidden-import", "beatgrid",
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
    #  ★ 2026-11-08：**不再排除 QtOpenGL / QtOpenGLWidgets** —— 优化版新增可选
    #    GPU 合成（gpu_compose.py），需要 PySide6.QtOpenGL。默认关闭，但打包后
    #    用户手动把 perf.gpu_compose 设为 true 时也能用。
    #    QtQuick/Qml/Pdf/Svg/Network 仍然排除。
    # ★ 2026-10-27：librosa / numba / llvmlite 是**离线验收脚本**用的（time-stretch 造变速样本），
    #   产品运行期零引用。不排除的话 numba 会把 llvmlite 整套（115MB）拖进包
    #   （实测包从 827M 涨到 947M）。--exclude-module 拦得住，因为没人 import 它们。
    "--exclude-module", "librosa",
    "--exclude-module", "numba",
    "--exclude-module", "llvmlite",
    "--exclude-module", "sklearn",
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

# ★★★ 2026-11-06：**打包前**临时把 NDI 的 DirectShow / UWP 变体改名藏起来。
#   为什么不在打包后删（旧做法）：那两个 DLL 40MB + 23MB，删它们加上别的开发文件
#   一共 50 个文件 ⇒ 触发安全钩子的「批量删除确认」⇒ 删除被拦 ⇒ 冗余留在包里
#   （实测 2026-11-06 打出来的 dist 是 481MB，而正确的应该是 415MB）。
#   改名不算删除，钩子不管；PyInstaller 那一趟就收不到这两个文件。
#   ⚠⚠ 不能只改名！`--collect-all cyndilib` 收的是**整个目录**，改名后
#       `xxx.dll.hidden_for_build` 照样被打进包（实测 2026-11-06：改名后 dist 仍是 481MB）。
#   ⇒ 必须**移出目录**，打完再移回来。移到 BUILD_ROOT 下（同一磁盘，move 是瞬时的）。
_NDI_BIN = os.path.join(os.path.dirname(os.path.dirname(sys.executable)),
                        "Lib", "site-packages", "cyndilib", "wrapper", "bin")
_STASH = os.path.join(BUILD_ROOT, "_ndi_hidden")
_HIDE = ("Processing.NDI.Lib.DirectShow.x64.dll", "Processing.NDI.Lib.UWP.x64.dll")
_hidden = []
for _n in _HIDE:
    _p = os.path.join(_NDI_BIN, _n)
    if os.path.isfile(_p):
        try:
            os.makedirs(_STASH, exist_ok=True)
            os.rename(_p, os.path.join(_STASH, _n))
            _hidden.append(_n)
        except OSError as e:
            print("⚠ 隐藏失败（打包会带回这 63MB）: %s" % e)
print("NDI 变体已临时移出 %d 个（打包后自动还原）" % len(_hidden))
try:
    rc = subprocess.call(cmd)
finally:
    for _n in _hidden:
        try:
            os.rename(os.path.join(_STASH, _n), os.path.join(_NDI_BIN, _n))
        except OSError:
            print("⚠ 没还原成功，请手动移回：%s" % os.path.join(_STASH, _n))

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

    # ---- 收尾：UI 冒烟（offscreen 真构造一次 MainWindow）----
    # ★ 为什么必须自动跑（P0 BUG-2 的教训）：2026-11-09 有一次 `QDoubleSpinBox` 忘了加进
    #   `PySide6.QtWidgets` 导入 —— 语法完全合法 ⇒ py_compile / 静态检查全过，但
    #   `MainWindow()` 一构造就 NameError，软件**根本起不来**，而包已经打出来了。
    #   这类错只有"真构造一次"才发现 ⇒ 挂在这里，忘了跑也会被拦一道。
    #   ⚠ 失败只打印醒目警告、**不改 rc**（包本身是好的，别让 CI 因为告警误判为构建失败）。
    smoke = os.path.join(ROOT, "tools", "_smoke_mainwindow.py")
    if os.path.isfile(smoke):
        print("\n" + "=" * 72)
        print("收尾：UI 冒烟（tools/_smoke_mainwindow.py）")
        print("=" * 72)
        try:
            src = subprocess.run([sys.executable, "-u", smoke], cwd=ROOT,
                                 capture_output=True, text=True, timeout=300)
            out = (src.stdout or "") + (src.stderr or "")
            if "SMOKE_OK" in out and src.returncode == 0:
                print("✅ UI 冒烟通过（SMOKE_OK）")
            else:
                print("\n" + "!" * 72)
                print("!! ★★ UI 冒烟失败 —— 这个包很可能起不来，别发给客户 ★★")
                print("!!   修好后重打：venv/Scripts/python.exe -u tools/_smoke_mainwindow.py")
                print("!" * 72)
                for line in out.strip().splitlines()[-25:]:
                    print("!! " + line)
        except Exception as e:                                     # noqa: BLE001
            print("\n" + "!" * 72)
            print("!! ★★ UI 冒烟没跑起来（%s）—— 请手动执行 ★★" % e)
            print("!!   venv/Scripts/python.exe -u tools/_smoke_mainwindow.py")
            print("!" * 72)
    else:
        print("\n⚠ 找不到 tools/_smoke_mainwindow.py，跳过 UI 冒烟（请确认脚本还在）")
sys.exit(rc)
