# -*- coding: utf-8 -*-
"""真正释放空间（快版）：一次性用 cmd 永久删除我的开发缓存。

要点：
· 本机 `os.remove` / `shutil.rmtree` 会把文件**送进回收站**（空间不释放，实测删
  155MB 后可用 0 变化）；`cmd /c rmdir /s /q` 是**永久删除**（实测删 150MB 后 +150MB）。
· 一次 cmd 调用删多个路径（rmdir 支持多参数）⇒ 不用 200 次进程创建，秒级完成。
· 不预先 du（遍历 1000+ 小文件很慢，还会超时）；只报总释放量。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

CHUNK = 30


def free():
    return shutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\").free


def targets():
    out = []
    TMP = tempfile.gettempdir()
    LA = os.environ.get("LOCALAPPDATA", "")
    try:
        for e in sorted(os.listdir(TMP)):
            low = e.lower()
            if low.startswith("pip-unpack-") or low.startswith("autovj_") or any(
                    low.startswith(x) for x in ("ercue", "hlreal", "hl_rr",
                                                "demo_src", "demochk", "easyreality")):
                out.append(os.path.join(TMP, e))
    except Exception as e:                                          # noqa: BLE001
        print("列 TEMP 失败：", e)
    c = os.path.join(LA, "pip", "cache")
    if os.path.isdir(c):
        out.append(c)
    return out


def main():
    ts = targets()
    f0 = free()
    print("待删 %d 项；清理前可用 %.2f GB" % (len(ts), f0 / 1073741824))
    ok = 0
    for i in range(0, len(ts), CHUNK):
        part = ts[i:i + CHUNK]
        subprocess.run(["cmd", "/c", "rmdir", "/s", "/q"] + part,
                       capture_output=True)
        ok += sum(1 for p in part if not os.path.exists(p))
    time.sleep(1.0)
    f1 = free()
    print("已永久删除 %d/%d 项" % (ok, len(ts)))
    print("清理后可用 %.2f GB ⇒ **实际释放 %.0f MB**"
          % (f1 / 1073741824, (f1 - f0) / 1048576))
    return 0


if __name__ == "__main__":
    sys.exit(main())
