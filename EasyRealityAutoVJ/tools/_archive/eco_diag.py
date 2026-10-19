# -*- coding: utf-8 -*-
"""诊断：render_fps=30 时为什么进程几乎不占 CPU（连解码线程都没有）。

用法： python tools/eco_diag.py
"""
import os
import sys
import json
import time
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_CFG = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ", "config.json")
TMP_CFG = os.path.join(ROOT, "config.json")

CHILD_FILE = os.path.join(ROOT, "tools", "eco_child.py")


def run(fps):
    base = json.load(open(SRC_CFG, encoding="utf-8"))
    d = json.loads(json.dumps(base))
    d.setdefault("perf", {})["render_fps"] = fps
    json.dump(d, open(TMP_CFG, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    r = subprocess.run([sys.executable, CHILD_FILE], capture_output=True, text=True, timeout=300)
    print("=" * 76)
    print("render_fps = %d" % fps)
    print("=" * 76)
    for line in (r.stdout or "").splitlines():
        if "Warning" in line or "warnings.warn" in line or "SoundcardRuntime" in line:
            continue
        if "shim" in line or "dirname" in line:
            continue
        print("  " + line)
    err = [l for l in (r.stderr or "").splitlines()
           if l.strip() and "shim" not in l and "dirname" not in l
           and "SoundcardRuntime" not in l and "warnings.warn" not in l]
    if err:
        print("  ---- stderr ----")
        for l in err[:12]:
            print("  " + l)
    print()


if __name__ == "__main__":
    try:
        run(int(sys.argv[1]) if len(sys.argv) > 1 else 30)
    finally:
        try:
            os.remove(TMP_CFG)
        except OSError:
            pass
