# -*- coding: utf-8 -*-
"""Easy Reality AutoVJ 软件入口"""
import sys
import os
import tempfile
import time

# ⚠⚠ 必须在**任何模块 import numpy 之前**执行：numpy 用的是 scipy-openblas，
# 默认按 **CPU 核数**开内部线程池。而这套程序里的矩阵运算全是小规模
# （能量分析的分频段、指纹匹配、曲风特征 88×512 / 96×128 之类），
# 多线程的线程调度开销远大于收益 —— 在 4 核机器上等于把 CPU 铺满、和 VDJ 抢核。
# 实测（tools/cpu_profile.py，2026-09-24）：进程 CPU 里有相当一部分花在
# **Python 看不见的 C 库线程**上。（与 media_manager 里 `cv2.setNumThreads(2)` 同源问题。）
# 改回去：设环境变量 AUTO_VJ_BLAS_THREADS=0（或自己指定线程数）。
_blas_t = os.environ.get("AUTO_VJ_BLAS_THREADS", "2")
if _blas_t and _blas_t != "0":
    for _k in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
               "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
        os.environ.setdefault(_k, _blas_t)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtGui import QFont
from PySide6.QtCore import Qt, QLockFile

import config
import theme
from ui_main import MainWindow


def _make_splash(theme_name):
    """自绘启动画面：让人一眼看到「在加载」，而不是「打不开」。

    背景（2026-09-24 用户报「软件打不开了，但任务管理器里有进程」）：
    以前所有初始化都在 `win.show()` 之前 —— 只要其中任何一步慢（冷启动读几百 MB、
    杀毒扫描、音频设备枚举卡住），用户看到的就是「有进程、没窗口」。
    现在先弹这张图，窗口构造完再收起。
    """
    from PySide6.QtGui import QPixmap, QPainter, QColor, QFont
    dark = (theme_name != "light")
    bg = QColor("#14161b") if dark else QColor("#f5f6f8")
    fg = QColor("#e9eaee") if dark else QColor("#1e2126")
    sub = QColor("#8b93a1") if dark else QColor("#6b7280")
    pm = QPixmap(480, 210)
    pm.fill(bg)
    p = QPainter(pm)
    p.setPen(QColor("#a06cf0"))
    p.drawRect(0, 0, 479, 209)
    p.setPen(fg)
    p.setFont(QFont("Microsoft YaHei UI", 15, QFont.Bold))
    p.drawText(pm.rect().adjusted(0, -34, 0, 0), Qt.AlignCenter, "Easy Reality AutoVJ")
    p.setPen(sub)
    p.setFont(QFont("Microsoft YaHei UI", 9))
    p.drawText(pm.rect().adjusted(0, 26, 0, 0), Qt.AlignCenter, "正在启动…")
    p.setFont(QFont("Microsoft YaHei UI", 8))
    p.drawText(pm.rect().adjusted(0, 56, 0, 0), Qt.AlignCenter,
               "首次启动或刚更新后会慢一些，请稍候")
    p.end()
    return pm


def _log_startup(rows):
    """把启动各阶段耗时追加到启动日志（保留最近 30 次），便于下次直接看数据。"""
    try:
        path = os.path.join(config.app_base_dir(), "startup_time.log")
        old = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                old = f.read().splitlines()
        old.append("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), "   ".join(rows)))
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(old[-30:]) + "\n")
    except Exception:
        pass


def main():
    t_all = time.perf_counter()
    marks = []

    def mark(label, t0):
        marks.append("%s %.2fs" % (label, time.perf_counter() - t0))

    app = QApplication(sys.argv)
    app.setApplicationName("Easy Reality AutoVJ")
    app.setStyle("Fusion")
    font = QFont("Microsoft YaHei UI", 9)
    app.setFont(font)

    # 主题（亮/暗），读用户上次选择
    t = time.perf_counter()
    cfg = config.Config()
    theme.apply(app, cfg["theme"])
    mark("配置", t)

    # 预热 PyAV：第一次 import av 要 ~65ms，如果等到切到 alpha 素材时再导入，
    # 那一下就会卡在渲染线程上（切换卡顿）。启动时先付掉这个钱。
    t = time.perf_counter()
    try:
        import av  # noqa: F401
    except Exception:
        pass
    mark("PyAV", t)

    # 启动画面：先让用户看到东西，再去做重活
    t = time.perf_counter()
    splash = None
    try:
        from PySide6.QtWidgets import QSplashScreen
        splash = QSplashScreen(_make_splash(cfg["theme"]))
        splash.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        splash.show()
        app.processEvents()          # 强制先画出来，否则会被后面的构造阻塞
    except Exception:
        splash = None
    mark("启动画面", t)

    # 单实例锁：启动加载模型需要十几秒，期间重复双击会开出一堆窗口。
    # 上次被强杀/崩溃后锁文件可能残留 → 先试一次，失败就把**残留锁**删掉再试，
    # 避免出现「有进程有锁、但永远起不来、也没有窗口」的死局。
    t = time.perf_counter()
    lock = QLockFile(os.path.join(tempfile.gettempdir(), "EasyRealityAutoVJ.lock"))
    lock.setStaleLockTime(5000)
    if not lock.tryLock(1000):
        if lock.removeStaleLockFile() and lock.tryLock(1000):
            pass
        else:
            if splash is not None:
                splash.close()
            QMessageBox.information(
                None, "已在运行",
                "Easy Reality AutoVJ 已经在运行中（启动加载需要一些时间，请看任务栏）。\n"
                "如确认没有窗口，可在任务管理器结束 EasyRealityAutoVJ.exe 后重试。")
            sys.exit(0)
    mark("单实例锁", t)

    # 启动阶段任何异常都不能「悄悄退出」：写日志 + 弹窗，否则用户只看到
    # 「任务管理器里有进程，但没有窗口，过一会儿自己关了」。
    t = time.perf_counter()
    try:
        win = MainWindow()
    except Exception:
        import traceback
        log = os.path.join(config.app_base_dir(), "startup_error.log")
        try:
            with open(log, "a", encoding="utf-8") as f:
                f.write("[%s]\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                        traceback.format_exc()))
        except Exception:
            log = "(日志写入失败)"
        if splash is not None:
            splash.close()
        QMessageBox.critical(
            None, "启动失败",
            "软件启动时出错，窗口无法打开。\n\n"
            "错误详情已写入：\n%s\n\n"
            "常见原因：素材库/配置数据异常。可以先把下面这个文件改名后重试：\n"
            "%%LOCALAPPDATA%%\\AutoVJ\\config.json（会以默认设置启动）" % log)
        sys.exit(1)
    mark("主窗口", t)

    win.show()
    if splash is not None:
        splash.finish(win)
    mark("合计", t_all)
    _log_startup(marks)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
