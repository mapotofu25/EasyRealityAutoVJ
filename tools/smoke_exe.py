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

    ok = alive and not err
    print("")
    print("结论：%s" % ("✅ 冒烟通过" if ok else "★ 冒烟失败，别发这个包"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
