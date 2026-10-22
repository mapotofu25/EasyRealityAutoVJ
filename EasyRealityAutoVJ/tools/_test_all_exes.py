# -*- coding: utf-8 -*-
"""逐个测试 5 个独立软件的 exe：能否启动、12 秒内是否存活、是否生成错误日志。"""

import os
import subprocess
import time

TARGETS = [
    ("ERAVJ 演出端", r"P:\ERAVJ\AutoVJ\dist_r70\EasyRealityAutoVJ\EasyRealityAutoVJ.exe"),
    ("ERCUE 编排端", r"P:\ERAVJ\EasyRealityCue\dist\EasyRealityCue\EasyRealityCue.exe"),
    ("ERLIB 曲库分析", r"P:\ERAVJ\AutoVJ\dist_icon2\ERLIB曲库分析.exe"),
    ("ERSND 音频发送端", r"P:\ERAVJ\AutoVJ\dist_icon2\EasyReality_音频发送端.exe"),
    ("NDI 接收自检", r"P:\ERAVJ\AutoVJ\NDI局域网测试\NDI接收自检.exe"),
]

ERR_LOGS = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "startup_error.log"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "ui_stall.log"),
]


def snapshot():
    return {p: (os.path.getsize(p) if os.path.exists(p) else -1) for p in ERR_LOGS}


def kill_all():
    for n in ("EasyRealityAutoVJ.exe", "EasyRealityCue.exe", "ERLIB曲库分析.exe",
              "EasyReality_音频发送端.exe", "NDI接收自检.exe"):
        subprocess.call(["taskkill", "/F", "/IM", n],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def alive(name):
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq " + name],
                         capture_output=True, text=True, encoding="gbk", errors="replace")
    return name.lower() in (out.stdout or "").lower()


def main():
    results = []
    for label, exe in TARGETS:
        print("\n" + "=" * 62)
        print("测试：%s" % label)
        if not os.path.isfile(exe):
            print("  ❌ 找不到 exe：%s" % exe)
            results.append((label, False, "exe 不存在"))
            continue
        kill_all()
        time.sleep(1.0)
        before = snapshot()
        t0 = time.time()
        try:
            proc = subprocess.Popen([exe], cwd=os.path.dirname(exe))
        except Exception as e:                                    # noqa: BLE001
            print("  ❌ 启动失败：%r" % (e,))
            results.append((label, False, "启动异常"))
            continue
        time.sleep(12)
        rc = proc.poll()
        now = snapshot()
        grew = [os.path.basename(p) for p in ERR_LOGS
                if before[p] >= 0 and now[p] > before[p]]
        newlog = [os.path.basename(p) for p in ERR_LOGS
                  if before[p] < 0 and now[p] >= 0]
        ok = rc is None and not grew and not newlog
        print("  进程存活：%s   退出码：%r   耗时 %.1fs"
              % ("是" if rc is None else "否", rc, time.time() - t0))
        if grew:
            print("  ⚠ 新增错误日志：%s" % ", ".join(grew))
        if newlog:
            print("  ⚠ 新出现日志文件：%s" % ", ".join(newlog))
        print("  结论：%s" % ("✅ 通过（启动并稳定运行 12 秒）" if ok else "❌ 未通过"))
        results.append((label, ok, "exit=%r 日志增长=%s" % (rc, grew or "无")))
        try:
            proc.terminate()
        except Exception:                                         # noqa: BLE001
            pass
        time.sleep(1.0)
        kill_all()

    print("\n" + "=" * 62)
    print("汇总")
    for label, ok, detail in results:
        print("  %s %-18s %s" % ("✅" if ok else "❌", label, detail))
    n_ok = sum(1 for _, ok, _ in results if ok)
    print("\n通过 %d / %d" % (n_ok, len(results)))
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
