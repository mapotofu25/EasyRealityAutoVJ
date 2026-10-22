"""强制清空 dist/EasyRealityAutoVJ：cmd 子进程 del /f /s /q + rmdir。

本机 trash 路线对大树会失败（Some operations were aborted），只能走 cmd。
分两步：先删内容（del /f /s /q * ），再删目录本身。
"""
import os
import shutil
import subprocess
import time

DIST = r"P:\ERAVJ\AutoVJ\dist\EasyRealityAutoVJ"


def run(args):
    r = subprocess.run(args, capture_output=True)
    return r.returncode, (r.stdout or b""), (r.stderr or b"")


def main():
    if not os.path.exists(DIST):
        print("目标不存在：%s" % DIST)
        return 0
    before = shutil.disk_usage("P:\\").free
    print("删除前可用空间：%.2f GB" % (before / 1e9))
    t0 = time.time()
    # 1) 删文件
    rc1, _o1, e1 = run(["cmd", "/c", "del", "/f", "/s", "/q", os.path.join(DIST, "*")])
    print("del /f /s /q 退出码=%d" % rc1)
    if e1 and b"denied" in e1.lower():
        print("  警告:", e1.decode("gbk", "replace")[:200])
    # 2) 删空目录树
    rc2, _o2, e2 = run(["cmd", "/c", "rmdir", "/s", "/q", DIST])
    print("rmdir /s /q 退出码=%d（耗时 %.1fs）" % (rc2, time.time() - t0))
    if e2:
        print("  stderr:", e2.decode("gbk", "replace")[:300])
    still = os.path.exists(DIST)
    if still:
        left = []
        for root, _d, files in os.walk(DIST):
            for fn in files:
                left.append(os.path.join(root, fn))
        print("⚠ 仍有 %d 个文件残留，前 10 个：" % len(left))
        for p in left[:10]:
            print("   ", p)
        return 1
    after = shutil.disk_usage("P:\\").free
    print("删除后可用空间：%.2f GB（释放 %.2f GB）" % (after / 1e9, (after - before) / 1e9))
    print("OK：dist/EasyRealityAutoVJ 已清空")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
