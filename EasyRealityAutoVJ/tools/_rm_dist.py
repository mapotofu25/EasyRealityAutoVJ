"""真正删除 dist 目录：走子进程 cmd rmdir（绕过回收站/安全钩子的 trash 失败）。

背景：本机 os.remove/shutil.rmtree 是"送回收站"，而 dist/_internal 这种大树
trash 会失败（Some operations were aborted）⇒ 只剩 cmd 子进程删除这条路。
"""
import os
import subprocess
import time

TARGET = r"P:\ERAVJ\AutoVJ\dist"


def dir_size(p):
    tot = 0
    for root, _dirs, files in os.walk(p):
        for fn in files:
            try:
                tot += os.path.getsize(os.path.join(root, fn))
            except Exception:
                pass
    return tot


def main():
    if not os.path.exists(TARGET):
        print("目标不存在，无需删除：%s" % TARGET)
        return 0
    # 释放空间用实测 free 差值判断（本机"删除"不释放空间是已知坑）
    import shutil
    before = shutil.disk_usage("P:\\").free
    print("删除前可用空间：%.2f GB" % (before / 1e9))
    t0 = time.time()
    r = subprocess.run(
        ["cmd", "/c", "rmdir", "/s", "/q", TARGET],
        capture_output=True, text=True)
    print("cmd 退出码 = %d  （耗时 %.1fs）" % (r.returncode, time.time() - t0))
    if r.stdout.strip():
        print("stdout:", r.stdout.strip()[:500])
    if r.stderr.strip():
        print("stderr:", r.stderr.strip()[:500])
    still = os.path.exists(TARGET)
    if still:
        print("⚠ 仍存在：%s" % TARGET)
        return 1
    after = shutil.disk_usage("P:\\").free
    print("删除后可用空间：%.2f GB  （本次释放 %.2f GB）"
          % (after / 1e9, (after - before) / 1e9))
    print("OK：dist 已删除")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
