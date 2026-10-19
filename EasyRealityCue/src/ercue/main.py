# -*- coding: utf-8 -*-
"""EasyRealityCue 入口（**单窗口，无 embedded 双模式**，架构方案 §3.1）。

跑法：
    P:\\AutoVJ\\venv\\Scripts\\python.exe  P:\\EasyRealityCue\\src\\ercue\\main.py

无头环境（自测 / CI）：
    set QT_QPA_PLATFORM=offscreen
"""

from __future__ import annotations

import logging
import os
import sys

# 允许 `python src/ercue/main.py` 直接跑（把 src 加进 sys.path）
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from PySide6.QtWidgets import QApplication                      # noqa: E402

from ercue.ui.window import APP_VERSION, MainWindow              # noqa: E402


def build_app(argv=None) -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(list(argv if argv is not None else sys.argv))
    app.setApplicationName("EasyRealityCue")
    app.setApplicationVersion(APP_VERSION)          # 真实构建号，见 window._app_version()
    # ★ 应用图标（仅图标相关）：窗口 + 任务栏统一用品牌 ER 标。找不到文件就跳过，
    #   绝不影响启动（打包后从 _MEIPASS/assets 取，源码运行从项目根 assets/ 取）。
    try:
        from PySide6.QtGui import QIcon
        from ercue import appicon
        _icon = appicon.icon_path("cue")
        if _icon:
            app.setWindowIcon(QIcon(_icon))
    except Exception:                               # noqa: BLE001
        pass
    return app


def build_window() -> MainWindow:
    return MainWindow()


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    # ★★★ 2026-10-18：**必须有落盘日志**。
    #   打包成 windowed exe 后没有控制台 ⇒ `log.warning/exception` 全丢 ⇒ 现场报
    #   "导出没带指纹"时**一点线索都没有**（只能靠猜）。这里补一个轮转文件日志。
    try:
        import os
        from logging.handlers import RotatingFileHandler
        _dir = os.path.join(os.environ.get("LOCALAPPDATA", ""), "EasyRealityCue")
        os.makedirs(_dir, exist_ok=True)
        _fh = RotatingFileHandler(os.path.join(_dir, "ercue.log"),
                                  maxBytes=2 * 1024 * 1024, backupCount=3,
                                  encoding="utf-8")
        _fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        _fh.setLevel(logging.INFO)
        logging.getLogger().addHandler(_fh)
    except Exception:                                          # noqa: BLE001
        pass                                                   # 日志写不了也绝不挡启动
    build_app(argv)
    win = build_window()
    win.show()
    return build_app().exec()


if __name__ == "__main__":
    raise SystemExit(main())
