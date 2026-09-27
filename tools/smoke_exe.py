"""新 exe 启动冒烟（发布流程第 5 步，每次 build 后必跑）

做的事：删掉旧的 `startup_error.log` → 启动刚打出来的 exe → 等 N 秒 →
检查「进程还活着」且「没有新生成 startup_error.log」→ 收尾结束进程。

为什么不能省：项目里吃过这个亏 —— 没被执行过的代码路径藏着版本兼容 bug
（全局热键那一支从没跑过，一启用就启动即崩），而**崩在窗口出现之前时用户只看到
「任务管理器里有进程、没有窗口」**。这一条 40 秒的冒烟能拦住绝大多数这类问题。

用法：
  venv\\Scripts\\python.exe tools\\smoke_exe.py [等待秒数=40]
  venv\\Scripts\\python.exe tools\\smoke_exe.py 40 --isolated

⚠ 默认模式会 `taskkill` 掉**所有** EasyRealityAutoVJ.exe（含用户正在跑的实例）。
  如果用户正在使用软件（例如演出中），**必须加 `--isolated`**：
  它把新实例的 `LOCALAPPDATA` 指到一个临时目录 —— 既绕开单实例锁、又不动用户数据，
  结束时也只关掉自己启动的那个进程。（顺带等于测了一遍「全新用户首次启动」。）
"""

import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "dist", "EasyRealityAutoVJ", "EasyRealityAutoVJ.exe")


def _default_log():
    return os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                        "AutoVJ", "startup_error.log")


def _dist_pids():
    """列出 **exe 路径就在 `dist/` 下** 的 EasyRealityAutoVJ 进程 PID。

    ⚠ 只认 dist 里那份 —— 用户自己那份装在别的目录（例如 `Y:\\EasyRealityAutoVJ\\`），
      **绝不能误杀**，所以必须按「可执行文件路径」判断，不能只按进程名。
    """
    import ctypes
    import ctypes.wintypes as wt
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = wt.HANDLE
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq EasyRealityAutoVJ.exe",
                          "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace").stdout
    want = os.path.dirname(EXE).lower()
    pids = []
    for line in out.splitlines():
        parts = [x.strip('"') for x in line.split('","')]
        if len(parts) < 2 or not parts[0].lower().startswith("easyreality"):
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        h = k32.OpenProcess(0x1000, False, pid)
        if not h:
            continue
        buf = ctypes.create_unicode_buffer(2048)
        size = ctypes.c_ulong(2048)
        try:
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                if os.path.dirname(buf.value).lower() == want:
                    pids.append(pid)
        finally:
            k32.CloseHandle(h)
    return pids


def _reap_dist_instances(quiet=False):
    """兜底清理：杀掉 dist 里残留的实例。返回清掉的 PID 列表。

    ★ 2026-09-27 补：光靠 `Popen.terminate()` **实测偶尔会残留**一个实例占住 `dist/`，
      后果是**下次打包 `os.rename(dist/…)` 报 `WinError 32 另一个程序正在使用此文件`**，
      排查一次要几分钟。这里按路径再兜一遍，把这个问题挡在冒烟阶段。
    """
    pids = _dist_pids()
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, encoding="utf-8", errors="replace")
    if pids and not quiet:
        print("★ 冒烟收尾：清掉 dist 里残留的实例 %s（否则下次打包会 WinError 32）"
              % ", ".join(str(x) for x in pids))
    return pids


def main():
    args = sys.argv[1:]
    isolated = "--isolated" in args
    secs = 40
    for a in args:
        if a.isdigit():
            secs = int(a)

    print("=" * 80)
    print("启动冒烟  %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 80)
    if not os.path.exists(EXE):
        print("★ 找不到 %s（先跑 tools/build_exe.py）" % EXE)
        return 2

    env = None
    if isolated:
        data_root = os.path.join(tempfile.gettempdir(), "autovj_smoke_%d" % os.getpid())
        os.makedirs(data_root, exist_ok=True)
        env = os.environ.copy()
        env["LOCALAPPDATA"] = data_root
        log = os.path.join(data_root, "AutoVJ", "startup_error.log")
        print("隔离模式：临时数据目录 = %s" % data_root)
        print("  ★ 不结束你正在运行的实例；结束时只关本次启动的进程")
    else:
        log = _default_log()
        # 先确认没有残留进程占着 dist
        subprocess.run(["taskkill", "/F", "/IM", "EasyRealityAutoVJ.exe"],
                       capture_output=True, encoding="utf-8", errors="replace")

    if os.path.exists(log):
        try:
            os.remove(log)
            print("已删除旧的 startup_error.log")
        except OSError as e:
            print("⚠ 删不掉旧日志（可能被占用）：%s" % e)

    print("启动 exe，等待 %d 秒…" % secs)
    p = subprocess.Popen([EXE], cwd=os.path.dirname(EXE), env=env)
    alive, t0 = True, time.time()
    while time.time() - t0 < secs:
        if p.poll() is not None:
            alive = False
            print("★ 进程提前退出，返回码 %s（%.1f 秒）" % (p.returncode, time.time() - t0))
            break
        time.sleep(1)
    if alive:
        print("进程存活超过 %d 秒 ✓" % secs)

    err = ""
    if os.path.exists(log):
        try:
            with open(log, encoding="utf-8", errors="replace") as fh:
                err = fh.read()
        except Exception as e:                                        # noqa: BLE001
            err = "（读取失败：%s）" % e
    print("新生成的 startup_error.log：%s" % ("**有**" if err else "无 ✓"))
    if err:
        print("-" * 80)
        print(err[:2000])
        print("-" * 80)

    # 收尾：隔离模式只杀自己启动的那个 PID，默认模式清掉全部同名进程
    if isolated:
        try:
            p.terminate()
        except Exception:                                             # noqa: BLE001
            pass
        try:
            p.wait(timeout=10)
        except Exception:                                             # noqa: BLE001
            subprocess.run(["taskkill", "/F", "/PID", str(p.pid)],
                           capture_output=True, encoding="utf-8", errors="replace")
    else:
        subprocess.run(["taskkill", "/F", "/IM", "EasyRealityAutoVJ.exe"],
                       capture_output=True, encoding="utf-8", errors="replace")
    time.sleep(1)
    # ★ 兜底：按「可执行文件路径」清掉 dist 里的残留（只动 dist 那份，不碰用户的实例）
    _reap_dist_instances()

    ok = alive and not err
    print("")
    print("结论：%s" % ("✅ 冒烟通过" if ok else "★ 冒烟失败，别发这个包"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
