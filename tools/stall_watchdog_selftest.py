"""卡顿看门狗自测：故意让主线程"卡住"，验证日志能抓到调用栈。

要点：模拟方式用 `time.sleep(4)` 而**不是**纯 Python 自旋 ——
真实场景里主线程几乎总是卡在 C 调用上（磁盘/GPU/驱动，此时 GIL 是放开的），
所以看门狗能拿到 GIL、抓到栈；而纯 Python 自旋会**持着 GIL**，
看门狗自己也被饿死，反而抓不到（这是已知局限，见 stallwatch.py 的说明）。

用法：venv\\Scripts\\python.exe tools\\stall_watchdog_selftest.py
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import stallwatch                                               # noqa: E402

# ⚠⚠ 自测**必须改到临时日志路径**：`stallwatch.log_path()` 默认指向**用户真实数据目录**
# （`%LOCALAPPDATA%\AutoVJ\ui_stall.log`），而本脚本开头会 `os.remove` 它。
# 2026-09-26 就是这样把用户现场的卡顿记录清空了（当时正在排查真实卡顿，日志里
# 有 16 次卡顿的完整线程栈）。改路径后再动日志，绝不碰用户的文件。
import tempfile                                                 # noqa: E402
_TMP_LOG = os.path.join(tempfile.gettempdir(), "autovj_stall_selftest.log")
stallwatch.log_path = lambda: _TMP_LOG

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("[OK ]" if cond else "[FAIL]", name, ("  " + extra) if extra else ""))


def main():
    log = stallwatch.log_path()
    try:
        if os.path.exists(log):
            os.remove(log)
    except Exception:                                           # noqa: BLE001
        pass

    hb = {"t": time.perf_counter()}
    w = stallwatch.StallWatchdog(hb, stall_secs=1.5, poll=0.3)
    w.start()
    print("看门狗已启动（判定阈值 1.5 秒，日志 %s）" % log)

    print("\n[1] 正常跳动 2 秒 —— 不应报警")
    t0 = time.time()
    while time.time() - t0 < 2.0:
        hb["t"] = time.perf_counter()
        time.sleep(0.2)
    got = os.path.exists(log) and os.path.getsize(log) > 0
    check("正常心跳期间不误报", not got)

    print("\n[2] 故意让主线程卡住 4 秒 —— 应抓到栈（短暂卡顿）")
    time.sleep(4.0)
    hb["t"] = time.perf_counter()      # 恢复心跳
    time.sleep(1.2)

    print("\n[3] 再卡住 14 秒 —— 模拟现场的「永久无响应」")
    print("    应看到：周期性补快照 + GIL 判据 + 各线程位置对比")
    time.sleep(14.0)
    hb["t"] = time.perf_counter()
    time.sleep(1.2)
    w.stop()

    txt = ""
    if os.path.exists(log):
        txt = open(log, encoding="utf-8").read()
    print("\n=== 日志内容（关键部分）===")
    interesting = ("卡顿", "恢复", "快照", "GIL", "读音", "读数", "小结", "─ 线程", "MainThread",
                   "线程 ?", "累计 CPU", "变化")
    shown = 0
    for ln in txt.splitlines():
        if any(k in ln for k in interesting):
            print("  " + ln)
            shown += 1
            if shown >= 40:
                print("  …（略）")
                break
    print()

    check("日志已生成", bool(txt))
    check("记录了卡顿", "卡顿" in txt)
    check("记录了恢复", "已恢复" in txt)
    check("抓到了调用栈", "stall_watchdog_selftest" in txt, "（栈里应出现本文件名）")
    check("持续卡顿有多次快照", "第 2 次快照" in txt, "（★ 修复「永久卡死看起来像好了」）")
    check("给出了 GIL 判据", ("GIL 是流通的" in txt) or ("GIL 被长期持有" in txt))
    check("栈带位置对比标注", ("与上次相同" in txt) or ("变了（上次" in txt) or ("新出现" in txt))
    check("卡顿期间 CPU 判据", "在等" in txt or "核" in txt)

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  - %s" % f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
