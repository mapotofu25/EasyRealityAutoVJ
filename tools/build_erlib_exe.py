# -*- coding: utf-8 -*-
r"""打包 erlib 独立小软件（DJ 端曲库分析器）。

## 体积的关键一招
主程序包里最大的单项是 **`assets/models/chinese_clip`（330 MB）** ——
那是"给素材自动打标签"用的，**erlib 完全不需要**。
erlib 只要两个小模型：
- `discogs`（17 MB）—— 曲风识别
- `beat_this`（10 MB）—— 小节头（`phrase_bt`）

⇒ 光是这一刀就省掉 330 MB。

## 为什么用 spec 文件而不是一串命令行
与发送端同一个理由：`--exclude-module` 只能拦 Python 模块，
**拦不住顺着 Qt 插件自动跟随进来的 DLL 本体**（实测白带过 45 MB）。
要对 `a.binaries` / `a.datas` 动手，只有 spec 能做。

## 用法
    venv\Scripts\python.exe tools/build_erlib_exe.py
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
ENTRY = os.path.join(ROOT, "erlib_main.pyw")
# ★ 2026-10-09（用户要求）：软件显示名统一为「ERLIB曲库分析」
#   （窗口标题见 src/erlib/app.py 的 APP_TITLE；这里只管**打包产物名**）
OUT_NAME = "ERLIB曲库分析"

# 只带这两个模型（**不带 chinese_clip**）
_MODELS = ("discogs", "beat_this")

_HIDDEN = [
    "erlib", "erlib.format", "erlib.scanner", "erlib.app", "erlib.transfer",
    "erlib.cover",
    "fp", "beatgrid", "phrase_bt", "music_meta",
    "audio_genre", "genre_lookup", "genre_keywords", "genre_visual_default",
    "config", "soundfile", "mutagen", "onnxruntime",
    # ★★ 这些是**函数内延迟 import** 的（`erlib.cover` / `music_meta` 里），
    #    PyInstaller 的静态分析**看不到** ⇒ 不显式声明就会打出一个
    #    "能启动但读不了封面" 的残包（而且不会报错，极难发现）。
    "PIL", "PIL.Image", "PIL.ImageFile", "PIL.JpegImagePlugin",
    "mutagen.flac", "mutagen.id3", "mutagen.mp4", "mutagen.aiff",
    "mutagen.oggvorbis", "mutagen.wave",
]

# 主程序专用的东西一律不要（erlib 既不播放也不渲染）
_EXCLUDES = [
    "engine", "ui_main", "panels", "glctx", "ndi_out", "asio_engine",
    "lan_audio", "lan_audio_recv", "lan_audio_send", "SpoutGL", "dxvnative",
    "postprocess", "colorfx", "match_engine", "charge_engine", "beatgrid_ui",
    # ★★ 体积杀手：这三个只为"读封面"而存在，而 erlib 用的是自带的轻量版
    #   （`erlib/cover.py`：mutagen + PIL）⇒ 砍掉 cv2(97MB) + av/FFmpeg(63MB)
    "cv2", "av", "music_cover", "media_manager",
    "torch", "matplotlib", "scipy", "pandas", "jupyter", "IPython",
    "PIL.ImageQt",
    "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtPdf", "PySide6.QtSvg",
    "PySide6.QtNetwork", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.Qt3DCore", "PySide6.QtMultimedia", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtBluetooth", "PySide6.QtNfc",
    "PySide6.QtSerialPort", "PySide6.QtTest", "PySide6.QtSql",
]


def _model_datas():
    """逐文件列出要带的模型（PyInstaller 的 datas 不吃通配符）。"""
    out = []
    base = os.path.join(ROOT, "assets", "models")
    for sub in _MODELS:
        d = os.path.join(base, sub)
        if not os.path.isdir(d):
            print("★ 警告：模型目录不存在 %s" % d)
            continue
        for dirpath, _dn, fns in os.walk(d):
            rel = os.path.relpath(dirpath, base)
            dest = os.path.join("assets", "models", rel)
            for fn in fns:
                out.append((os.path.join(dirpath, fn), dest))
    return out


def _make_splash():
    """生成启动画面（onefile 模式启动要解压，没有画面用户会以为"点了没反应"）。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:                                            # noqa: BLE001
        return None
    W, H = 420, 190
    im = Image.new("RGB", (W, H), (22, 26, 34))
    d = ImageDraw.Draw(im)

    def _f(sz):
        for n in ("msyh.ttc", "simhei.ttf", "msyhbd.ttc"):
            try:
                return ImageFont.truetype("C:/Windows/Fonts/" + n, sz)
            except Exception:                                    # noqa: BLE001
                pass
        return ImageFont.load_default()

    d.text((26, 32), "ERLIB", font=_f(40), fill=(236, 240, 248))
    d.text((26, 96), "曲库分析", font=_f(18), fill=(120, 190, 230))
    d.text((26, 126), "首次启动需解压，请稍候几秒…", font=_f(13),
           fill=(140, 148, 162))
    p = os.path.join(ROOT, "tools", "_erlib_splash.png")
    im.save(p)
    return p


def _spec_text(datas, splash_png=None):
    ds = ",\n        ".join("(%r, %r)" % (p.replace("\\", "/"),
                                          d.replace("\\", "/"))
                            for p, d in datas)
    hs = ",\n        ".join("'%s'" % h for h in _HIDDEN)
    es = ",\n        ".join("'%s'" % e for e in _EXCLUDES)
    sp = ""
    sp_arg = ""          # ⚠ 留空即可；**别写 "[]"** —— 后面紧跟 a.binaries 会缺逗号报语法错
    if splash_png:
        sp = '''
splash = Splash(
    %r,
    binaries=a.binaries,
    datas=a.datas,
    text_pos=(26, 156),
    text_size=11,
    always_on_top=True,
)
''' % splash_png.replace("\\", "/")
        sp_arg = "splash, splash.binaries,"
    return '''# -*- mode: python ; coding: utf-8 -*-
a = Analysis(
    [%r],
    pathex=[%r],
    binaries=[],
    datas=[
        %s
    ],
    hiddenimports=[
        %s
    ],
    excludes=[
        %s
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
%s# ★ 单文件：**不调 COLLECT** ⇒ 所有东西打进一个 exe
exe = EXE(
    pyz,
    a.scripts,
    %s
    a.binaries,
    a.datas,
    [],
    name=%r,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
''' % (ENTRY.replace("\\", "/"), SRC.replace("\\", "/"), ds, hs, es,
       sp, sp_arg, OUT_NAME)


def main():
    datas = _model_datas()
    tot = sum(os.path.getsize(p) for p, _d in datas)
    print("模型：%d 个文件 / %.1f MB（只带 %s，**不含 chinese_clip**）"
          % (len(datas), tot / 1e6, "/".join(_MODELS)))
    splash = _make_splash()
    # ⚠⚠ 启动画面**用不了**：PyInstaller 的 `Splash` 依赖 **tkinter**，而这个 venv
    #   没装（实测报 "Your platform does not support the splash screen feature,
    #   since tkinter is not installed"）。为它去装 tkinter 不划算 ——
    #   onefile 的解压发生在 Python 起来**之前**，所以"程序内自己弹个窗"也没用。
    #   ⇒ 改为在**使用说明**里写明"首次启动要等几秒"。
    print("启动画面：跳过（PyInstaller Splash 需要 tkinter，本 venv 没有）"
          if splash else "启动画面：跳过（PIL 不可用）")

    spec = os.path.join(ROOT, "tools", "_erlib.spec")
    with open(spec, "w", encoding="utf-8") as f:
        f.write(_spec_text(datas, None))

    build = os.environ.get("AUTOVJ_BUILD", "build_erlib")
    dist = os.environ.get("AUTOVJ_DIST", "dist_erlib")
    # ⚠ onefile 模式：输出 exe 若已存在，PyInstaller 删旧文件会撞安全删除钩子
    #   （项目里为这个踩过 4 次）⇒ 先把它挪走。
    old_exe = os.path.join(ROOT, dist, OUT_NAME + ".exe")
    if os.path.isfile(old_exe):
        try:
            os.replace(old_exe, old_exe + ".old")
            print("已把旧 exe 挪成 %s.old" % os.path.basename(old_exe))
        except Exception as e:                                   # noqa: BLE001
            print("★ 挪旧 exe 失败（%s），可能被占用" % e)
    # ⚠ 给了 .spec 文件就不能再传 `--specpath`（PyInstaller 会报
    #   "makespec options not valid when a .spec file is given"）
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm",
           "--workpath", build, "--distpath", dist, spec]
    print("运行：%s" % " ".join(cmd))
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        print("★ 打包失败（exit=%d）" % r.returncode)
        return r.returncode
    if os.path.isfile(old_exe):
        sz = os.path.getsize(old_exe)
        print("\n构建完成（**单文件**）：%s" % old_exe)
        print("  体积：%.1f MB" % (sz / 1e6))
        print("  ⚠ onefile：每次启动会先解压到临时目录，首次约 5~15 秒（有启动画面）")
    else:
        print("\n★ 产物缺失：%s" % old_exe)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
