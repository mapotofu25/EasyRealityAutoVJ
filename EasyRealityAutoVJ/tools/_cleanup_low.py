# -*- coding: utf-8 -*-
"""待办清单「低」三项：① tools 里的一次性历史脚本归档 ② dist_new 旧产物清理 ③ 失效的 bat。

① tools/ 下堆了 200+ 个历史上"打补丁/追加日志/瘦身内存"的一次性脚本（不进发行包，
   但会把真正有用的工具淹没）。按前缀归档到 tools/_archive/，**不删**（万一要查历史）。
   真正在用的（扫描器/离屏自检/构建链）留在原地。
② dist_new/ 下每个 _prev_* 是 ~500MB 的旧产物，累积十几个 ≈ 8GB。
   只保留**最新两个**（新包出问题能马上回滚），其余删除。
③ 更新到新版.bat 指向改名前的 dist\AutoVJ\AutoVJ.exe，已失效（还会打"完成！"假成功）。
"""
import glob
import io
import os
import shutil
import sys
import time

ROOT = r"P:\AutoVJ"
TOOLS = os.path.join(ROOT, "tools")
ARCH = os.path.join(TOOLS, "_archive")

# 归档前缀：历史一次性脚本（**注意不要匹配到在用的**）
ARCHIVE_PREFIX = (
    "_patch_", "_append_log_", "_shrink_memory", "_update_memory", "_engine_fix",
    "_engine_head_snapshot", "_gridfix", "_fix0", "_fix_two_tests", "_fix_demo_dep",
    "_fix_i18n_line", "_strip_", "_backup_cue", "_mem_", "_log_append", "_ui_pages_test",
    "_patch_audit", "_patch_audio_safe", "_patch_dragfix", "_patch_manual_pages",
    "_patch_ui_p", "_patch_seconds", "_patch_i18n_sec", "_patch_perf_fix",
    "_patch_autosave", "_repro_two_sheets", "_cpu_growth_probe", "_grid_first",
)
KEEP = {
    "_scan_dupdef.py", "_scan_locks.py", "_after_strip_smoke.py", "_audioerr.py",
    "_hl_grab.py", "_hl_diag.py", "_hl_live.py", "_proc_info.py", "_raise_shot.py",
    "_win_shot.py", "_show_ranges.py", "_verify_perf_fp.py", "_git_hygiene.py",
    "_fix_perf1.py", "_fix_perf2.py", "_fix_hl_border.py", "_fix_audio_regress.py",
    "_fix_i18n_key.py", "_fix_i18n_key2.py",          # 本轮这批，留作记录
}


def main():
    # ---------------- ① 归档一次性脚本 ----------------
    if not os.path.isdir(ARCH):
        os.makedirs(ARCH)
    moved, kept = 0, 0
    for p in sorted(glob.glob(os.path.join(TOOLS, "*.py"))):
        name = os.path.basename(p)
        if name in KEEP:
            kept += 1
            continue
        if any(name.startswith(x) for x in ARCHIVE_PREFIX):
            dst = os.path.join(ARCH, name)
            try:
                if os.path.exists(dst):
                    os.remove(dst)
                shutil.move(p, dst)
                moved += 1
            except Exception as e:                                  # noqa: BLE001
                print("  !! 归档失败", name, e)
    print("① tools：归档 %d 个一次性脚本 → tools/_archive/（原地保留 %d 个）"
          % (moved, kept))

    # ---------------- ② 旧产物清理 ----------------
    prevs = sorted(glob.glob(os.path.join(ROOT, "dist_new", "_prev_*")),
                   key=lambda x: os.path.getmtime(x), reverse=True)
    print("② dist_new 下旧产物 %d 个" % len(prevs))
    for d in prevs[:2]:
        print("   保留：%s（%.0f MB）"
              % (os.path.basename(d), _size(d) / 1048576))
    freed = 0.0
    for d in prevs[2:]:
        s = _size(d)
        shutil.rmtree(d, ignore_errors=True)
        if not os.path.isdir(d):
            freed += s
            print("   删除：%s（释放 %.0f MB）" % (os.path.basename(d), s / 1048576))
        else:
            print("   !! 删不干净（有文件被占用？）：%s" % os.path.basename(d))
    print("   共释放 %.1f GB" % (freed / 1073741824))

    # ---------------- ③ 失效的 bat ----------------
    for name in ("更新到新版.bat", "更新到新版 .bat"):
        p = os.path.join(ROOT, name)
        if os.path.isfile(p):
            os.remove(p)
            print("③ 已删除失效脚本：%s" % name)
        else:
            print("③ %s 不存在（已清理过）" % name)
    return 0


def _size(d):
    t = 0
    for dp, _dn, fn in os.walk(d):
        for f in fn:
            try:
                t += os.path.getsize(os.path.join(dp, f))
            except Exception:                                       # noqa: BLE001
                pass
    return t


if __name__ == "__main__":
    sys.exit(main())
