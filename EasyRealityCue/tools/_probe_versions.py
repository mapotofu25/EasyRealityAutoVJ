# -*- coding: utf-8 -*-
"""实测 1：关键第三方库的版本与许可证（从安装元数据读，不靠猜）。"""
import importlib.metadata as md
import sys

print("python:", sys.version)
print("exe   :", sys.executable)
print("=" * 78)

PKGS = ["PySide6", "PySide6_Essentials", "PySide6-Addons", "shiboken6",
        "av", "opencv-python", "numpy", "mutagen", "onnxruntime",
        "soundfile", "pillow", "pyinstaller", "sounddevice", "py-spy",
        "tinytag", "librosa", "aubio", "madmom", "scipy", "numba", "llvmlite"]

for name in PKGS:
    try:
        d = md.distribution(name)
    except Exception as e:
        print(f"{name:22s} : <未安装> ({type(e).__name__})")
        continue
    meta = d.metadata
    lic = meta.get("License") or ""
    if not lic or len(lic) > 60:
        # 有些包把许可证写在分类符里
        cls = [c for c in meta.get_all("Classifier") or [] if c.startswith("License")]
        lic = "; ".join(c.split("::")[-1].strip() for c in cls) or lic[:60]
    print(f"{name:22s} : {d.version:14s} | {lic}")
print("=" * 78)

# Qt 运行时版本
try:
    from PySide6 import __version__ as ps6v
    from PySide6.QtCore import __version__ as qtv, qVersion
    print("PySide6.__version__ =", ps6v)
    print("QtCore.__version__  =", qtv, "| qVersion() =", qVersion())
except Exception as e:
    print("Qt version probe failed:", e)

# av / ffmpeg 版本
try:
    import av
    print("av.__version__ =", av.__version__)
    print("av ffmpeg libs :", av.library_versions)
except Exception as e:
    print("av probe failed:", e)
