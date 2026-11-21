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

# ★ 2026-11-11（用户：「软件正在运行 操作非常卡」）：缩短 Python 线程切换间隔。
#   引擎/解码/音频/识别都是同进程线程，默认 5ms 的 GIL 切换粒度在多线程满载时
#   会让 GUI 线程一次等上百毫秒（现场 ui_stall.log 里大量 [UI] 主线程停顿就发生在
#   引擎 tick 的 Python 段）。1ms 的代价是少量吞吐，换来界面明显更跟手。
try:
    sys.setswitchinterval(0.001)
except Exception:                                              # noqa: BLE001
    pass

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtGui import QFont
from PySide6.QtCore import Qt, QLockFile

import config
import theme
from ui_main import MainWindow

#: 保持打开状态（faulthandler / excepthook 写的就是它），**不能是局部变量**
_CRASH_FP = None


def _install_crash_log():
    """把「Python 异常」和「进程级崩溃（段错误 / 访问冲突）」都落到文件上。

    ★ 2026-11-06（用户：「刚刚在调整行高的时候软件闪退了」）：
      Windows 事件日志只给了 `EasyRealityAutoVJ.exe` + `shiboken6.abi3.dll` +
      `0xc0000005`（访问冲突），没有 Python 栈 ⇒ 完全看不出是哪一行。
      `faulthandler.enable(file=...)` 会在**崩溃那一刻**把当时所有线程的 Python 栈
      写进 `crash.log`；`sys.excepthook` 把未被捕获的 Python 异常写进 `error.log`
      （打包成 --windowed 后 stderr 不可见，等于白丢）。
    失败绝不影响启动（连目录都建不了就直接放弃）。
    """
    global _CRASH_FP
    try:
        import faulthandler
        import traceback
        d = config.app_base_dir()
        os.makedirs(d, exist_ok=True)
        _CRASH_FP = open(os.path.join(d, "crash.log"), "a", buffering=1,
                         encoding="utf-8", errors="replace")
        _CRASH_FP.write("\n===== %s 启动 pid=%d =====\n"
                        % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid()))
        faulthandler.enable(file=_CRASH_FP, all_threads=True)

        def _hook(tp, val, tb):
            try:
                with open(os.path.join(d, "error.log"), "a", encoding="utf-8",
                          errors="replace") as f:
                    f.write("\n===== %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
                    traceback.print_exception(tp, val, tb, file=f)
            except Exception:                                    # noqa: BLE001
                pass
            try:
                sys.__excepthook__(tp, val, tb)
            except Exception:                                    # noqa: BLE001
                pass

        sys.excepthook = _hook
    except Exception:                                            # noqa: BLE001
        _CRASH_FP = None


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
    """把启动各阶段耗时追加到启动日志（保留最近 30 次），便于下次直接看数据。

    ★ 2026-11-17：行尾追加 `版本=` 与 `exe=`。现场经常分不清用户跑的是哪一版
      （同一个桌面快捷方式可能指向旧 dist），排障时只能靠猜。
    """
    try:
        path = os.path.join(config.app_base_dir(), "startup_time.log")
        old = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                old = f.read().splitlines()
        try:
            import version as _ver
            _vk = str(getattr(_ver, "APP_VERSION", "")
                      or getattr(_ver, "VERSION", "")
                      or getattr(_ver, "VER", "") or "")
        except Exception:                                      # noqa: BLE001
            _vk = ""
        old.append("[%s] %s   版本=%s   exe=%s"
                   % (time.strftime("%Y-%m-%d %H:%M:%S"), "   ".join(rows),
                      _vk or "?", sys.executable))
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(old[-30:]) + "\n")
    except Exception:
        pass


def main():
    t_all = time.perf_counter()
    # ★ 2026-11-11（现场：主线程 150~600ms 停顿）：GIL 默认 5ms 切换一次。
    #   10 层 QPainter + 解码线程每帧大量短 Python/C 调用时，GUI 要等 5ms 的整数倍
    #   才能拿到 GIL。降到 1ms，让 GUI/预览线程更及时插进来（吞吐损失 <1%）。
    try:
        sys.setswitchinterval(0.001)
    except Exception:                                          # noqa: BLE001
        pass
    marks = []
    _install_crash_log()      # ★ 必须在最前面：后面任何 import/构造崩了才有栈可查

    def mark(label, t0):
        marks.append("%s %.2fs" % (label, time.perf_counter() - t0))

    # ★ 2026-11-12（用户：Windows 任务栏看不见软件图标）：显式设置
    #   AppUserModelID。Windows 任务栏按 AUMID 归组/取图标；不设置时，
    #   尤其是从快捷方式/不同启动器启动，容易出现“没有图标/白色图标/归到 Python”。
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            ctypes.c_wchar_p("EasyReality.AutoVJ"))
    except Exception:                                          # noqa: BLE001
        pass

    app = QApplication(sys.argv)
    app.setApplicationName("Easy Reality AutoVJ")
    app.setApplicationDisplayName("Easy Reality AutoVJ")
    app.setStyle("Fusion")
    font = QFont("Microsoft YaHei UI", 9)
    app.setFont(font)

    # ★ 应用图标（仅图标相关）：窗口 + 任务栏统一用品牌 ER 标。找不到文件就跳过，
    #   绝不影响启动（打包后从 _MEIPASS/assets 取，源码运行从项目根 assets/ 取）。
    try:
        from PySide6.QtGui import QIcon
        import appicon
        _icon = appicon.icon_path("autovj")
        if _icon:
            app.setWindowIcon(QIcon(_icon))
    except Exception:
        pass

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

    # 临时副本回收（防磁盘泄漏）：**必须在单实例锁之前**！否则「已有实例在跑」时
    # 走到下面的 `sys.exit(0)` 会**直接跳过清扫** —— 实测因此遗留 13GB 孤儿副本
    # （第二实例从不清理、第一实例早就跑过清扫点了）。
    # 清理是**纯删过期文件**（TTL 外），无副作用、不需要任何独占；被占用/太新的副本
    # 优雅跳过。失败静默、绝不影响启动。
    t = time.perf_counter()
    try:
        from media_manager import cleanup_stale_ascii_copies
        # ★ TTL 24h → 6h：崩溃残留最多占盘 6 小时；`.tmp<pid>` 只要 pid 已死会立刻删。
        _rm, _lk, _keep = cleanup_stale_ascii_copies(ttl_hours=6.0)
    except Exception:
        pass
    mark("临时副本清理", t)

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

    # ★ 诊断入口（2026-11-10）：`--open-output-window` / `AUTOVJ_OPEN_OUT_WIN=1`
    #   启动后自动把「输出显示器」切成**窗口模式**并显示输出窗口，用于真机 A/B 取证
    #   （对比"开着输出窗口"与"关闭输出窗口"的 ui_stall.log）。
    #   ⚠ 必须延后到首次事件循环之后：在 __init__ 里同步开窗会卡启动、也会和恢复配置抢顺序。
    #   不带参数时行为与之前**完全一致**（零副作用）。
    try:
        _open_out = ("--open-output-window" in sys.argv
                     or str(os.environ.get("AUTOVJ_OPEN_OUT_WIN", "")).strip() in ("1", "true", "yes"))
    except Exception:                                             # noqa: BLE001
        _open_out = False
    # ★ 诊断入口 2（2026-11-10）：`--auto-start` / `AUTOVJ_AUTO_START=1`
    #   启动后延后触发"开始"，让引擎真的出帧（A/B 取证必须有帧）。
    #   不带参数时行为与之前完全一致。
    try:
        _auto_start = ("--auto-start" in sys.argv
                       or str(os.environ.get("AUTOVJ_AUTO_START", "")).strip() in ("1", "true", "yes"))
    except Exception:                                             # noqa: BLE001
        _auto_start = False
    if _auto_start:
        def _do_auto_start():
            try:
                # 真正的入口是 `MainWindow.toggle_run()`（没有 btn_start 这种东西，
                # 我上一版按 btn_start 找 ⇒ 永远点不到，A/B 才一直是 0 行）
                _fn = getattr(win, "toggle_run", None)
                if callable(_fn) and not bool(getattr(win.engine, "running", False)):
                    _fn()
                    return
                for _name in ("btn_start", "_btn_start", "btn_play"):
                    _b = getattr(win, _name, None)
                    if _b is not None and hasattr(_b, "click"):
                        _b.click()
                        return
            except Exception:                                     # noqa: BLE001
                pass
        from PySide6.QtCore import QTimer as _QT2
        _QT2.singleShot(0, _do_auto_start)

    if _open_out:
        def _do_open_out_window():
            try:
                ow = getattr(win, "out_win", None)
                if ow is None:
                    return
                ow.show_windowed()
                ow.show()
                ow.raise_()
                try:
                    win.engine.set_frame_consumers(
                        bool(win.preview_panel.preview_on()) or ow.isVisible())
                except Exception:                                 # noqa: BLE001
                    pass
            except Exception:                                     # noqa: BLE001
                pass
        from PySide6.QtCore import QTimer as _QT
        _QT.singleShot(0, _do_open_out_window)
    mark("合计", t_all)
    _log_startup(marks)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
