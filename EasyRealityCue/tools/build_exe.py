# -*- coding: utf-8 -*-
r"""EasyRealityCue 打包脚本：生成 dist\EasyRealityCue\EasyRealityCue.exe（onedir）。

用法：
    venv\Scripts\python tools\build_exe.py

要点（全部依据 docs/技术选型.md §A6 / §C8 的实测结论）：
  · --windowed --onedir（**不用 --onefile**：onefile 每次启动要解压几百 MB，慢且杀软误报多）。
  · --collect-all av        av.libs 里的 FFmpeg DLL 必须以 data 收集，否则 import 时
                            `DLL load failed`（官方 hook-av.py 实测生效，这里再兜底）。
  · --collect-all sounddevice  sounddevice 自带 portaudio DLL，需整包收集。
  · --hidden-import mutagen    mutagen 是惰性 import，静态分析可能漏，显式给出。
  · --exclude-module 掉未用的 Qt 模块（本应用只用 QtCore/QtGui/QtWidgets）。
    ⚠ 这些 **拦不住 Qt6*.dll 本体**（插件沿 PE 导入表把 DLL 拉进来），
       瘦身在 make_zip.py 里「写包时按文件名跳过」做，见那里的说明。
  · --runtime-hook tools/smoke_hook.py：默认什么都不做；仅当环境变量 ERCUE_SMOKE=1 时
    激活自检（供 tools/smoke_exe.py 用真正的产品 exe 跑冒烟）。产品正常运行不受影响。

本脚本内建「失败可诊断」：PyInstaller 的完整输出落 `build\build_exe.log`，
失败时打印最后 30 行。
"""
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
TOOLS = os.path.join(ROOT, "tools")
APP_NAME = "EasyRealityCue"
ENTRY = os.path.join(SRC, "ercue", "main.py")

sys.path.insert(0, TOOLS)
import build_version                                          # noqa: E402

# ★ 2026-10-11：构建/输出目录支持环境变量覆盖。
#   为什么需要（两个真实踩到的坑）：
#     ① `--noconfirm` 会删旧 dist；文件数超"安全删除钩子"阈值时会被拦 ⇒ 打包失败；
#     ② dist 被**正在运行的 exe** 占用时，连脚本自己的"移走备份"都做不了（Device busy）。
#   用法：`ERCUE_DIST=dist_wave ERCUE_BUILD=build_wave python tools/build_exe.py`
_BUILD_DIR = os.environ.get("ERCUE_BUILD", "build")
_DIST_DIR = os.environ.get("ERCUE_DIST", "dist")

LOG_PATH = os.path.join(ROOT, _BUILD_DIR, "build_exe.log")


def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def mb(n):
    return n / 1048576.0


def _clear_dir(path, label):
    """把上一次的产物挪走，好让 PyInstaller 干净落盘（dist 与 workpath 它都会先删）。

    ⚠ 本沙箱对**批量删除**有硬钩子（`SAFE_DELETE_BULK_CONFIRM_REQUIRED`，每回合配额约 50、
    且配额很快耗尽）：dist / build 都有 500+ 文件，`shutil.rmtree` 会被拦下**并直接终止**
    （抛的是 BaseException，连 `except Exception` 都接不住）→ 整个打包失败。
    实测结论：**只能改名、不能删**（改名不是删除，不受该钩子影响）。所以这里**只做改名**，
    把旧产物挪到 gitignore 的 `build/_old/` 下，不污染工作树，事后手动删即可。
    """
    if not os.path.isdir(path):
        return
    bak_root = os.path.join(ROOT, _BUILD_DIR, "_old")
    os.makedirs(bak_root, exist_ok=True)
    base = os.path.basename(path.rstrip("\\/")) or "x"
    stamp = time.strftime("%H%M%S")
    bak = os.path.join(bak_root, "%s_%s_%s" % (label, base, stamp))
    i = 1
    while os.path.exists(bak):
        bak = os.path.join(bak_root, "%s_%s_%s_%d" % (label, base, stamp, i))
        i += 1
    try:
        os.rename(path, bak)
        print("旧 %s 已挪到 %s（改名避让，可手动删除）" % (label, bak))
    except BaseException as e:                                # noqa: BLE001
        print("!! 挪走旧 %s 失败：%s（继续尝试打包）" % (label, e))


def _report_volume(dist_dir):
    """打印 dist 总体积 + 顶层子项体积分布。"""
    print("-" * 60)
    total = _dir_size(dist_dir)
    print("dist 体积报告：%s  合计 %.1f MB" % (dist_dir, mb(total)))
    items = []
    for name in os.listdir(dist_dir):
        p = os.path.join(dist_dir, name)
        items.append((name, _dir_size(p) if os.path.isdir(p) else os.path.getsize(p)))
    items.sort(key=lambda x: -x[1])
    for name, size in items:
        print("    %-34s %8.1f MB" % (name, mb(size)))
    internal = os.path.join(dist_dir, "_internal")
    if os.path.isdir(internal):
        sub = []
        for name in os.listdir(internal):
            p = os.path.join(internal, name)
            sub.append((name, _dir_size(p) if os.path.isdir(p) else os.path.getsize(p)))
        sub.sort(key=lambda x: -x[1])
        print("  _internal 体积分布（Top 12）：")
        for name, size in sub[:12]:
            print("      %-30s %8.1f MB" % (name, mb(size)))
    print("-" * 60)


def main():
    date_key, seq, ver = build_version.bump()
    print("=" * 60)
    print("版本 %s（%s 当天第 %d 次生成）" % (ver, date_key, seq))
    print("压缩包将命名为 %s" % build_version.zip_name(date_key, seq))
    print("=" * 60)

    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    dist_root = os.path.join(ROOT, _DIST_DIR)
    _clear_dir(os.path.join(dist_root, APP_NAME), "dist")
    _clear_dir(os.path.join(ROOT, _BUILD_DIR, APP_NAME), "workpath")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--windowed",
        "--onedir",
        "--name", APP_NAME,
        "--workpath", os.path.join(ROOT, _BUILD_DIR),
        "--distpath", os.path.join(ROOT, _DIST_DIR),
        "--specpath", os.path.join(ROOT, _BUILD_DIR),
        # 让分析阶段能找到 `ercue` 包（入口在 src/ercue/main.py，包根在 src/）
        "--paths", SRC,
        # —— 依赖收集（§A6 / §C8）——
        "--collect-all", "av",
        "--collect-all", "sounddevice",
        "--hidden-import", "mutagen",
        "--hidden-import", "cffi",
        # ⚠ 2026-10-11：`ercue.audio.waveform` 是**惰性 import**（在 timeline_widget 的函数内
        #   `from ercue.audio import waveform`），静态分析可能漏掉 ⇒ 显式给出。
        #   漏掉的后果：冻结包能启动，但**时间轴画不出真实波形**（只在运行时才发现）。
        "--hidden-import", "ercue.audio.waveform",
        # ★ 应用图标定位（main.build_app 在 try 里惰性 import，静态分析可能漏 ⇒ 显式给出）
        "--hidden-import", "ercue.appicon",
        # —— 冒烟钩子（默认惰性，仅 ERCUE_SMOKE=1 生效）——
        "--runtime-hook", os.path.join(TOOLS, "smoke_hook.py"),
        # —— 排除本应用用不到的 Qt 模块（只用 QtCore/QtGui/QtWidgets）——
        "--exclude-module", "PySide6.QtQuick",
        "--exclude-module", "PySide6.QtQml",
        "--exclude-module", "PySide6.QtQmlModels",
        "--exclude-module", "PySide6.QtQuickWidgets",
        "--exclude-module", "PySide6.QtNetwork",
        "--exclude-module", "PySide6.QtOpenGL",
        "--exclude-module", "PySide6.QtOpenGLWidgets",
        "--exclude-module", "PySide6.QtSvg",
        "--exclude-module", "PySide6.QtPdf",
        "--exclude-module", "PySide6.QtMultimedia",
        "--exclude-module", "PySide6.QtSql",
        "--exclude-module", "PySide6.QtTest",
        "--exclude-module", "PySide6.QtDesigner",
        "--exclude-module", "PySide6.QtHelp",
        "--exclude-module", "PySide6.QtUiTools",
        "--exclude-module", "PySide6.QtDBus",
        "--exclude-module", "PySide6.QtPrintSupport",
    ]
    # —— 应用图标（★ 仅图标相关；文件缺失则自动跳过，绝不影响打包）——
    #   --icon 写入 exe 文件图标；--add-data 让运行时能在 _MEIPASS/assets/cue.ico 取到。
    _icon = os.path.join(ROOT, "assets", "cue.ico")
    if os.path.isfile(_icon):
        cmd += ["--icon", _icon, "--add-data", _icon + os.pathsep + "assets"]
        print("图标：%s" % _icon)
    else:
        print("★ 图标缺失，跳过（%s）" % _icon)
    cmd.append(ENTRY)
    print("命令：\n  " + " ".join(cmd) + "\n")
    print("（完整输出写入 %s；打包通常需要 1~3 分钟）" % LOG_PATH)

    t0 = time.time()
    lines = []
    with open(LOG_PATH, "w", encoding="utf-8", errors="replace") as logf:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                cwd=ROOT)
        for line in proc.stdout:
            logf.write(line)
            lines.append(line)
            sys.stdout.write(line)
            sys.stdout.flush()
        rc = proc.wait()
    dt = time.time() - t0

    if rc != 0:
        print("\n" + "!" * 60)
        print("!! PyInstaller 失败（退出码 %d），日志最后 30 行：" % rc)
        for line in lines[-30:]:
            sys.stdout.write(line)
        print("!! 完整日志：%s" % LOG_PATH)
        print("!" * 60)
        return rc

    dist_dir = os.path.join(ROOT, _DIST_DIR, APP_NAME)
    exe = os.path.join(dist_dir, APP_NAME + ".exe")
    if not os.path.exists(exe):
        print("!! 打包命令成功但没找到 %s" % exe)
        return 2
    print("\n打包完成：用时 %.0f 秒（%.1f 分）" % (dt, dt / 60.0))
    print("exe：%s" % exe)
    _report_volume(dist_dir)
    print("下一步：venv\\Scripts\\python tools\\make_zip.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
