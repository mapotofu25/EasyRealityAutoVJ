# -*- coding: utf-8 -*-
r"""PyInstaller 打包脚本：生成 P:\AutoVJ\dist\EasyRealityAutoVJ\EasyRealityAutoVJ.exe
用法: venv\Scripts\python tools\build_exe.py
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")

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
    "--workpath", os.path.join(ROOT, "build"),
    "--distpath", os.environ.get("AUTOVJ_DIST", os.path.join(ROOT, "dist")),
    "--specpath", os.path.join(ROOT, "build"),
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
    "--hidden-import", "section_analyze",
    "--hidden-import", "postprocess",
    "--hidden-import", "colorfx",
    "--hidden-import", "i18n",
    "--hidden-import", "i18n_map",
    "--hidden-import", "version",
    "--hidden-import", "_build_ver",
    "--hidden-import", "ndi_out",
    "--hidden-import", "dxvnative",
    "--hidden-import", "glctx",
    "--hidden-import", "SpoutGL",
    "--hidden-import", "SpoutGL.enums",
    "--collect-all", "cyndilib",
    # GPU 解码（实验）：glfw 用来建离屏 GL 上下文（自带 glfw3.dll）；
    # _dxvlz.dll 是自研的 DXV3 LZ 解包（zig 编译，187KB），必须随包。
    # dxvnative.dll_path() 在打包环境下解析到 _internal（= _MEIPASS），与此处目标一致。
    "--collect-all", "glfw",
    "--add-binary", os.path.join(SRC, "_dxvlz.dll") + os.pathsep + ".",
    "--add-data", os.path.join(ROOT, "assets", "models") + os.pathsep + "assets" + os.path.sep + "models",
    os.path.join(SRC, "main.py"),
]
print(" ".join(cmd))
rc = subprocess.call(cmd)

# ---- 打包后瘦身：cyndilib 自带 3 个平台的 NDI 运行库，只用得上 x64 ----
#   Processing.NDI.Lib.x64.dll        28.8MB  ← 在用（Windows x64 运行库）
#   Processing.NDI.Lib.DirectShow.dll 42MB    DirectShow filter：老采集软件经 DShow
#                                     访问 NDI 源用的，本软件不通过 DShow 收发
#   Processing.NDI.Lib.UWP.dll        24MB    UWP/微软商店应用专用，Python 用不了
# 另有 .cpp/.pyx/Linux 占位文件，运行时全用不上 → 共省约 80MB。
if rc == 0:
    internal = os.path.join(os.environ.get("AUTOVJ_DIST", os.path.join(ROOT, "dist")),
                            "EasyRealityAutoVJ", "_internal", "cyndilib")
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
