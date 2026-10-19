# -*- coding: utf-8 -*-
"""高 DPI 回归测试：不管系统缩放多少，窗口的**物理尺寸**都必须等于设置的分辨率。

判据（用户要求）：设 1600×900 时
  · 100% 缩放 ⇒ 逻辑 1600×900、物理 1600×900
  · 150% 缩放 ⇒ 逻辑 1067×600、物理 **1600×900**（不再超出屏幕）
用法：venv/Scripts/python.exe -u tools/_test_dpi_window.py
"""
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
SELF = os.path.abspath(__file__)
TARGET = (1600, 900)

CHILD = r'''
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"{src}")
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QGuiApplication
app = QApplication([])
import config, output_window
cfg = config.Config()
cfg.save = lambda *a, **k: None                  # ⚠ 绝不写用户配置
cfg["output"]["width"], cfg["output"]["height"] = {tw}, {th}
w = output_window.OutputWindow(cfg)
w._win_geom = None                                # 走"没有历史几何"分支
w.apply_window_size()
scr = w.screen() or QGuiApplication.primaryScreen()
dpr = float(scr.devicePixelRatio())
lg = (w.width(), w.height())
ph = (round(lg[0] * dpr), round(lg[1] * dpr))
sg = scr.availableGeometry()
print("DPR=%.2f 逻辑=%dx%d 物理=%dx%d 屏幕逻辑=%dx%d 屏幕物理=%dx%d"
      % (dpr, lg[0], lg[1], ph[0], ph[1], sg.width(), sg.height(),
         round(sg.width()*dpr), round(sg.height()*dpr)))
assert ph == ({tw}, {th}), "物理尺寸 %s != 期望 {tw}x{th}" % (ph,)
os._exit(0)
'''.format(src=os.path.join(_ROOT, "src"), tw=TARGET[0], th=TARGET[1])


def main():
    fails = []
    for scale in ("1", "1.25", "1.5", "2"):
        env = dict(os.environ)
        env["QT_SCALE_FACTOR"] = scale
        r = subprocess.run([sys.executable, "-u", "-c", CHILD], env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
        out = (r.stdout or "").strip().splitlines()
        err = (r.stderr or "").strip().splitlines()
        tag = "缩放 %-5s" % scale
        if r.returncode == 0 and out:
            print("  ✓ %s  %s" % (tag, out[-1]))
        else:
            print("  ✗ %s  退出码=%s" % (tag, r.returncode))
            for l in (err[-3:] + out[-2:]):
                print("        " + l[:150])
            fails.append(scale)
    print()
    print("结果：%s" % ("全部通过 ✓ 物理尺寸恒等于设置的分辨率" if not fails
                      else "失败 %s" % fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
