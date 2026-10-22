# -*- coding: utf-8 -*-
"""清理测试遗留的 `autovj_*` **目录**（测试夹具残留）。

背景：本项目多个自测脚本用 `tempfile.mkdtemp(prefix="autovj_xxx_")` 造夹具，
      但历史上很多脚本没有 `finally` 清理 → Temp 里堆积了大量 `autovj_*` 空/小目录。
      （注意：`autovj_*` **文件** 由 media_manager.cleanup_stale_ascii_copies 在
      启动期回收，本工具**只管目录**，互不越界。）

安全设计（宁可少删不可误删）：
  · 只处理 `autovj_*` 且**是目录**的条目；
  · 强制断言目标在 `tempfile.gettempdir()` **之下**（防未来误传路径）；
  · 默认 **dry-run**，只打印清单；要真删必须显式 `--apply`；
  · `--apply` 时逐个 try/except，被占用/无权限的跳过并计数，绝不中断；
  · `--min-age-hours N` 只删 N 小时前未改动的目录（默认 0 = 全部）。

用法：
    python tools/_cleanup_test_dirs.py                # 列出将要删的目录（dry-run）
    python tools/_cleanup_test_dirs.py --apply        # 真删
    python tools/_cleanup_test_dirs.py --min-age-hours 6 --apply
"""
import os
import sys
import glob
import time
import shutil
import argparse
import tempfile


PREFIX = "autovj_"
# 本项目自测脚本使用的**全部**临时前缀（都是"测试夹具/取证输出"这一类的残留）。
# ⚠ avj_catch_ / avj_shot_ 是**人工取证工具**的输出目录（_catch_freeze.py /
#   _grab_now.py），里面是给用户看的截图 ⇒ 本工具**默认不含**它们，需 --include-forensics
#   才纳入（避免误删用户刚要看的取证图）。
ALL_PREFIXES = ("autovj_", "qa3_", "avj_iso", "avj_")
FORENSIC_PREFIXES = ("avj_catch_", "avj_shot_")


def find_dirs(base=None, min_age_hours=0.0, prefixes=None):
    """返回 autovj_*/qa3_*/avj_* **目录**列表（默认排除人工取证工具的输出目录）。"""
    base = base if base is not None else tempfile.gettempdir()
    prefixes = prefixes if prefixes is not None else ALL_PREFIXES
    cutoff = time.time() - min_age_hours * 3600.0
    out = []
    seen = set()
    for pre in prefixes:
        for p in sorted(glob.glob(os.path.join(base, pre + "*"))):
            if p in seen:
                continue
            if not os.path.isdir(p):
                continue                      # 只管目录，文件归 cleanup_stale_ascii_copies
            if any(os.path.basename(p).startswith(f) for f in FORENSIC_PREFIXES):
                continue                      # ⚠ 取证工具的输出目录：是用户资产，不删
            try:
                if os.path.getmtime(p) > cutoff:
                    continue                  # 太新：可能是正在跑的测试，保留
            except OSError:
                continue
            seen.add(p)
            out.append(p)
    return sorted(out)


def dir_size(p):
    tot = 0
    try:
        for dp, _dn, fn in os.walk(p):
            for f in fn:
                try:
                    tot += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
    except OSError:
        pass
    return tot


def main():
    ap = argparse.ArgumentParser(description="清理测试遗留的 autovj_* 目录")
    ap.add_argument("--apply", action="store_true",
                    help="真正删除（默认只 dry-run 列清单）")
    ap.add_argument("--min-age-hours", type=float, default=0.0,
                    help="只处理 N 小时前未修改的目录（默认 0=全部）")
    ap.add_argument("--base", default=None,
                    help="扫描根目录（默认 tempfile.gettempdir()；主要给测试用）")
    args = ap.parse_args()
    base = args.base if args.base is not None else tempfile.gettempdir()
    base_abs = os.path.abspath(base)
    real_tmp = os.path.abspath(tempfile.gettempdir())

    # ⚠ 安全断言：默认用法必须落在真实 Temp 之下；显式 --base 用于测试隔离时为
    #   独立自建目录，也必须是绝对路径且存在（避免相对路径被 cwd 影响）。
    if args.base is None:
        if not base_abs.startswith(real_tmp):
            print("拒绝执行：根目录不在临时目录之下：", base_abs)
            return 2
    elif not os.path.isabs(base_abs) or not os.path.isdir(base_abs):
        print("拒绝执行：--base 必须是存在的绝对目录：", base_abs)
        return 2

    dirs = find_dirs(base=base_abs, min_age_hours=args.min_age_hours)

    print("=" * 70)
    print("扫描根:", base_abs)
    print("前缀  :", ", ".join(ALL_PREFIXES), "（只处理**目录**）")
    print("排除  :", ", ".join(FORENSIC_PREFIXES), "（人工取证输出，是用户资产）")
    print("最小年龄: %.1f 小时" % args.min_age_hours)
    print("待处理目录: %d 个" % len(dirs))
    total = 0
    for p in dirs:
        sz = dir_size(p)
        total += sz
        print("  %8.2f MB  %s" % (sz / 1024**2, os.path.basename(p)))
    print("合计大小: %.2f MB" % (total / 1024**2))

    if not args.apply:
        print("\n[dry-run] 未删除任何东西。加 --apply 才真正删除。")
        return 0

    print("\n执行删除（被占用/无权限的会跳过）...")
    ok = fail = 0
    for p in dirs:
        # 双保险：删除前再确认路径仍在 base 之下
        if not os.path.abspath(p).startswith(base_abs):
            continue
        try:
            shutil.rmtree(p)
            ok += 1
        except OSError as e:
            fail += 1
            print("  跳过 %s：%s" % (os.path.basename(p), e))
    print("\n完成：删除 %d，跳过 %d" % (ok, fail))
    return 0


if __name__ == "__main__":
    sys.exit(main())
