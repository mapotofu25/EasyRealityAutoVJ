# -*- coding: utf-8 -*-
"""把诊断脚本打包成独立 exe（其他电脑无需 Python 环境即可运行）"""
import subprocess
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
DIAG = os.path.join(ROOT, "tools", "diag_audio.py")

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--onefile", "--console",
    "--name", "能量检测工具",
    "--paths", SRC,
    "--hidden-import", "audio_engine",
    "--collect-all", "soundcard",
    "--collect-all", "soundfile",
    "--distpath", ROOT,
    "--workpath", os.path.join(ROOT, "build_diag"),
    "--specpath", os.path.join(ROOT, "build_diag"),
    DIAG,
]
print(" ".join(cmd))
rc = subprocess.call(cmd)
sys.exit(rc)
