# -*- coding: utf-8 -*-
"""归档 tools/ 里的一次性历史脚本（**移动，不删除**）。

判据（简单且可逆）：一个脚本留在原地，当且仅当
  ① 它被 git 跟踪（= 仓库认可的构建脚本 / selftest），或
  ② 它在当前工作集里（扫描器 / 离屏自检 / 现场取证 / 本轮改动脚本）。
其余一律视为历史探针 → `tools/_archive/`。
"""
import glob
import io
import os
import shutil
import subprocess
import sys

ROOT = r"P:\AutoVJ"
TOOLS = os.path.join(ROOT, "tools")
ARCH = os.path.join(TOOLS, "_archive")

# 当前工作集：本轮及近期真正会用到的
KEEP = {
    "_scan_dupdef.py", "_scan_locks.py",          # 两个静态扫描器
    "_after_strip_smoke.py", "_audioerr.py",      # 摘除自检 / 音频热路径自检
    "_hl_diag.py", "_hl_grab.py", "_hl_live.py",  # 播放高亮取证
    "_proc_info.py", "_perf_watch.py", "_proc_watch.py", "_memwatch.py",
    "_raise_shot.py", "_win_shot.py",             # 截图取证
    "_show_ranges.py",                            # 读代码用
    "_verify_perf_fp.py",                         # 指纹恒等回归
    "_git_hygiene.py", "_cleanup_low.py",
    "_make_release.py",                           # 发 Release
    "_fix_perf1.py", "_fix_perf2.py", "_fix_hl_border.py", "_fix_audio_regress.py",
    "_fix_i18n_key.py", "_fix_i18n_key2.py", "_fix_092911.py", "_fix_092911b.py",
    "_fix_audit_high.py", "_fix_audit_low.py",
    "_vdj_compare.py", "_grid_test.py",           # 节拍网格核对（在用）
}


def tracked():
    r = subprocess.run(["git", "ls-files", "tools"], cwd=ROOT,
                       capture_output=True, text=True)
    return {os.path.basename(l.strip()) for l in r.stdout.splitlines() if l.strip()}


def main():
    keep = KEEP | tracked()
    os.makedirs(ARCH, exist_ok=True)
    moved = []
    for p in sorted(glob.glob(os.path.join(TOOLS, "*.py"))):
        n = os.path.basename(p)
        if n in keep:
            continue
        dst = os.path.join(ARCH, n)
        try:
            if os.path.exists(dst):
                os.remove(dst)
            shutil.move(p, dst)
            moved.append(n)
        except Exception as e:                                      # noqa: BLE001
            print("  !! 失败", n, e)
    # 顺带把同类的一次性输出/日志也搬走（.log/.txt/.json 的探针产物）
    for pat in ("*.log", "*.json", "*_out.txt", "*_rows.txt", ".assetid.txt"):
        for p in glob.glob(os.path.join(TOOLS, pat)):
            n = os.path.basename(p)
            if n.startswith("_"):
                try:
                    shutil.move(p, os.path.join(ARCH, n))
                except Exception:                                   # noqa: BLE001
                    pass
    print("归档 %d 个历史脚本 → tools/_archive/" % len(moved))
    print("原地保留（仓库跟踪 + 当前工作集）：")
    left = sorted(os.path.basename(p) for p in glob.glob(os.path.join(TOOLS, "*.py")))
    print("   ", " ".join(left))
    print("tools 根下现有条目 = %d，归档目录内 = %d"
          % (len(os.listdir(TOOLS)), len(os.listdir(ARCH))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
