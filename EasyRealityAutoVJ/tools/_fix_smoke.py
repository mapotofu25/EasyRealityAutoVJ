# -*- coding: utf-8 -*-
"""给 tools/smoke_exe.py 补一个**假阴性**判据。

2026-09-30 实测踩到：一边开着「从源码跑的实例」（python.exe main.py），一边跑冒烟 ——
新 exe 启动时发现**单实例锁**被占，弹一句"已在运行"就 `sys.exit(0)`
⇒ 进程提前退出 ⇒ 冒烟判"★ 失败，别发这个包"，**其实是好包**。
（`smoke_exe.py` 只会 taskkill `EasyRealityAutoVJ.exe`，不认 python.exe 那个实例。）

改法（**不杀 python 进程** —— 环境里可能有别的 python，误杀很危险）：
① 启动前检查单实例锁文件，在就高声提醒"这次结果不可信"；
② 若进程提前退出且锁被占，直接把"疑似被本机另一实例占用"写进结论，别再让人误判。
"""
import io
import os
import sys

P = r"P:\AutoVJ\tools\smoke_exe.py"
PAT = []


def rep(old, new, cnt=1):
    PAT.append((old, new, cnt))


# ① 启动前：检查单实例锁
rep("""    print("启动 exe，等待 %d 秒…" % secs)
    p = subprocess.Popen([EXE], cwd=os.path.dirname(EXE), env=env)""",
    """    # ★ 单实例锁：从源码跑的实例（python main.py）也占这把锁，而它**不叫**
    #   EasyRealityAutoVJ.exe ⇒ 上面的 taskkill 清不掉它 ⇒ 新 exe 会以为"已在运行"
    #   直接退出，冒烟就误报失败。这里先探一下，让结论别被误读。
    _lock = os.path.join(tempfile.gettempdir(), "EasyRealityAutoVJ.lock")
    lock_held = os.path.exists(_lock)
    if lock_held:
        print("⚠ 单实例锁已存在（%s）" % _lock)
        print("  ⇒ 若本机还有『从源码跑的实例』或旧实例，新 exe 会判定"
              "「已在运行」并立刻退出 —— 那不是打包失败！")

    print("启动 exe，等待 %d 秒…" % secs)
    p = subprocess.Popen([EXE], cwd=os.path.dirname(EXE), env=env)""")

# ② 结论：把"被别的实例占锁"这条排除掉
rep("""    ok = alive and not err
    print("")
    print("结论：%s" % ("✅ 冒烟通过" if ok else "★ 冒烟失败，别发这个包"))
    return 0 if ok else 1""",
    """    ok = alive and not err
    print("")
    if not ok and lock_held and not err:
        print("结论：⚠ 结果不可信 —— 进程提前退出，但单实例锁在冒烟前就已存在，")
        print("      极可能是被『另一个实例（含从源码跑的 python main.py）』挡住了。")
        print("      请先关掉所有实例再重跑本脚本，再下结论。")
        return 2
    print("结论：%s" % ("✅ 冒烟通过" if ok else "★ 冒烟失败，别发这个包"))
    return 0 if ok else 1""")


def main():
    s = io.open(P, encoding="utf-8").read()
    for i, (old, new, cnt) in enumerate(PAT, 1):
        n = s.count(old)
        if n != cnt:
            print("✗ 第 %d 处命中 %d 次（应为 %d）" % (i, n, cnt))
            return 1
    for old, new, cnt in PAT:
        s = s.replace(old, new)
    io.open(P, "w", encoding="utf-8", newline="\n").write(s)
    print("✓ smoke_exe.py 已补「假阴性」判据")
    import py_compile
    py_compile.compile(P, doraise=True)
    print("语法 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
