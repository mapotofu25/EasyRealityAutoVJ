# -*- coding: utf-8 -*-
"""QA 探针：环境/配置/素材可用性（不修改 src，不落盘到项目下）。

用法：QT_QPA_PLATFORM=offscreen venv/Scripts/python.exe -u tools/_qa_probe_env.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

print("python:", sys.version.replace("\n", " "))
print("frozen:", getattr(sys, "frozen", False))

# --- config 路径 ---
import config                                                   # noqa: E402
print("CONFIG_FILE:", config.CONFIG_FILE)
print("exists:", os.path.exists(config.CONFIG_FILE))
print("exe_dir():", config.exe_dir())
print("app_base_dir():", config.app_base_dir())

# --- 第三方库 ---
for mod in ("cv2", "av", "numpy", "PySide6", "imageio"):
    try:
        m = __import__(mod)
        print("lib %-10s OK  ver=%s" % (mod, getattr(m, "__version__", "?")))
    except Exception as e:                                       # noqa: BLE001
        print("lib %-10s MISSING (%s)" % (mod, e))

# --- 素材库 ---
try:
    cfg = config.Config.load()
    print("\ncfg layers:", len(cfg.get("layers", [])))
    tot = 0
    found = 0
    for i, lay in enumerate(cfg.get("layers", [])):
        clips = lay.get("clips") or []
        if not clips:
            continue
        ex = 0
        for c in clips[:6]:
            p = c if isinstance(c, str) else c.get("path", "")
            tot += 1
            if p and os.path.exists(p):
                found += 1
                if ex < 2:
                    print("  layer[%d] %s -> %s" % (i, lay.get("name"), p))
                ex += 1
        # 完整统计
        allc = [c if isinstance(c, str) else c.get("path", "") for c in clips]
        n_exist = sum(1 for p in allc if p and os.path.exists(p))
        print("  layer[%d] %-22s clips=%d exist=%d" %
              (i, str(lay.get("name"))[:22], len(allc), n_exist))
        tot = tot  # noqa
except Exception as e:                                           # noqa: BLE001
    import traceback
    traceback.print_exc()
    print("config load failed:", e)
