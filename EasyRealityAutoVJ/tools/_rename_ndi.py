# -*- coding: utf-8 -*-
"""临时把 cyndilib 的两份可疑 NDI 运行库改名/还原（取证用，跑完务必 restore）。

用法：
    venv\\Scripts\\python -u tools\\_rename_ndi.py off   # 改名为 .bak
    venv\\Scripts\\python -u tools\\_rename_ndi.py on    # 还原
    venv\\Scripts\\python -u tools\\_rename_ndi.py status
同时作用于 dist_new（exe 用）与 venv site-packages（源码 NDI 自测用）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIRS = [
    os.path.join(ROOT, "dist_new", "EasyRealityAutoVJ", "_internal", "cyndilib", "wrapper", "bin"),
    os.path.join(ROOT, "venv", "Lib", "site-packages", "cyndilib", "wrapper", "bin"),
]
FILES = ["Processing.NDI.Lib.DirectShow.x64.dll", "Processing.NDI.Lib.UWP.x64.dll"]


def act(mode):
    for d in DIRS:
        for name in FILES:
            orig = os.path.join(d, name)
            bak = orig + ".bak"
            if mode == "off":
                if os.path.exists(orig):
                    os.rename(orig, bak)
                    print(f"  renamed -> .bak : {orig}")
                elif os.path.exists(bak):
                    print(f"  already .bak    : {orig}")
                else:
                    print(f"  MISSING         : {orig}")
            elif mode == "on":
                if os.path.exists(bak):
                    os.rename(bak, orig)
                    print(f"  restored        : {orig}")
                elif os.path.exists(orig):
                    print(f"  already orig    : {orig}")
                else:
                    print(f"  MISSING         : {orig}")
    if mode == "status":
        for d in DIRS:
            print(f"[{d}]")
            for name in FILES:
                orig = os.path.exists(os.path.join(d, name))
                bak = os.path.exists(os.path.join(d, name + ".bak"))
                print(f"    {name:42s} orig={orig!s:5s} bak={bak}")


if __name__ == "__main__":
    m = sys.argv[1] if len(sys.argv) > 1 else "status"
    act(m)
