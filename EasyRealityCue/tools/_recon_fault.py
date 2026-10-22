# -*- coding: utf-8 -*-
"""侦察：加载 EasyRealityCue 并定时 dump 所有线程栈，定位"无窗口"卡点。

用法：P:\\ERAVJ\\EasyRealityCue\\venv\\Scripts\\python.exe tools\\_recon_fault.py [秒数]
"""
import faulthandler
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

secs = int(sys.argv[1]) if len(sys.argv) > 1 else 18
print("[recon] %d 秒后将 dump 所有线程栈（若窗口正常出现，可忽略）" % secs, flush=True)
faulthandler.dump_traceback_later(secs, exit=False)

import ercue.main as m                                                # noqa: E402
print("[recon] 进入 main()", flush=True)
raise SystemExit(m.main())
