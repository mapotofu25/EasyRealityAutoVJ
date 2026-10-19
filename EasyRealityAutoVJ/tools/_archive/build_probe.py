# -*- coding: utf-8 -*-
"""把「能量诊断工具」打包成独立 exe（其他电脑无需 Python 环境）

产物：P:\\AutoVJ\\能量诊断工具.exe（单文件，GUI）
用法：venv\\Scripts\\python tools\\build_probe.py
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
PROBE = os.path.join(ROOT, "tools", "energy_probe.py")

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm", "--onefile", "--windowed",
    "--name", "能量诊断工具",
    "--paths", SRC,
    "--hidden-import", "audio_engine",
    "--hidden-import", "soundfile",
    "--collect-all", "soundcard",
    "--distpath", ROOT,
    "--workpath", os.path.join(ROOT, "build_probe"),
    "--specpath", os.path.join(ROOT, "build_probe"),
    PROBE,
]
print(" ".join(cmd))
rc = subprocess.call(cmd)
exe = os.path.join(ROOT, "能量诊断工具.exe")
if os.path.exists(exe):
    print("产物:", exe, round(os.path.getsize(exe) / 1048576, 1), "MB")
sys.exit(rc)
