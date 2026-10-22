# -*- coding: utf-8 -*-
"""侦察：手工构造 MainWindow，逐步打点定位「无窗口」的真因。

用法：venv\\Scripts\\python.exe tools\\_recon_win.py
"""
import os
import sys
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
sys.path.insert(0, _SRC)

print("[1] 导入 Qt…", flush=True)
from PySide6.QtWidgets import QApplication                        # noqa: E402
print("    Qt 版本:", __import__("PySide6").__version__, flush=True)

print("[2] 创建 QApplication…", flush=True)
app = QApplication(sys.argv)
print("    platformName =", app.platformName(), flush=True)

print("[3] 导入 MainWindow…", flush=True)
t0 = time.time()
from ercue.ui.window import MainWindow                            # noqa: E402
print("    导入耗时 %.1fs" % (time.time() - t0), flush=True)

print("[4] 构造 MainWindow（可能较慢）…", flush=True)
t0 = time.time()
try:
    win = MainWindow()
    print("    构造完成，耗时 %.1fs" % (time.time() - t0), flush=True)
    print("    isWindow=%s isVisible=%s size=%s title=%r"
          % (win.isWindow(), win.isVisible(), win.size(), win.windowTitle()), flush=True)
except Exception:
    print("    ★ 构造抛异常：", flush=True)
    traceback.print_exc()
    raise SystemExit(2)

print("[5] 调用 show()…", flush=True)
win.show()
print("    show 返回；isVisible=%s" % win.isVisible(), flush=True)

print("[6] processEvents 一轮…", flush=True)
app.processEvents()
print("    isVisible=%s size=%s" % (win.isVisible(), win.size()), flush=True)
print("    winId =", hex(int(win.winId())) if win.winId() else "0", flush=True)

print("[7] 保持 8 秒，供外部枚举窗口…", flush=True)
t0 = time.time()
while time.time() - t0 < 8:
    app.processEvents()
    time.sleep(0.1)
print("[8] 结束（不进入 exec()，避免挂住）", flush=True)
sys.exit(0)
