# -*- coding: utf-8 -*-
"""ERLIB 曲库分析 —— 独立小 exe 的入口。

⚠ 这个文件曾经丢过一次（git 从未跟踪），导致 `tools/build_erlib_exe.py` 直接报
  `script 'erlib_main.pyw' not found`、exe 无法重建。**新增入口脚本必须立刻 git add。**

职责只有一个：把 `src` 放进 sys.path（源码运行时用；打包后由 spec 的 `pathex` 处理），
然后调用 `erlib.app.main()`。真正的界面/业务全在 `src/erlib/app.py`。
"""

import os
import sys

# 源码运行：项目根/erlib_main.pyw → 项目根/src
if not getattr(sys, "frozen", False):
    _here = os.path.dirname(os.path.abspath(__file__))
    _src = os.path.join(_here, "src")
    if os.path.isdir(_src) and _src not in sys.path:
        sys.path.insert(0, _src)

from erlib.app import main                                        # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
