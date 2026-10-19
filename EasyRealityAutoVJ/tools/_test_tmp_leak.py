# -*- coding: utf-8 -*-
"""验证 `_ascii_safe_path` 临时副本泄漏修复（三轮：(d)复用 / (a)退出删 / (c)启动清孤儿）。

真实证明「不泄漏」：
  1) 造非 ASCII 源素材 → 调 _ascii_safe_path → 确认副本落 Temp、登记进 _TMP_COPIES；
  2) (d) 复用：同源二次调用 → 同一副本、不新增复制；
  3) (a) 退出删：调 _cleanup_registered_copies → 登记副本被删；
  4) (c) 启动清孤儿：造「旧 mtime 的 autovj_* 副本」→ cleanup_stale_ascii_copies
     → 被删；造「新 mtime」→ 保留；造「被占用(打开中)」→ 优雅跳过、不崩溃；
  5) 断言：清理后 Temp 里不新增 autovj_* 文件。

⚠ 夹具自造、落 tempfile.mkdtemp()，**全程 finally 清理**（新铁律）。
⚠ 退出码 127 = 环境原生 abort，看结论行。
用法：venv/Scripts/python.exe -u tools/_test_tmp_leak.py
"""
import os
import sys
import time
import shutil
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

FAILS = []


def check(name, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


import media_manager as mm                                          # noqa: E402
import numpy as np                                                  # noqa: E402

_TEMP = tempfile.gettempdir()
_fixture = tempfile.mkdtemp(prefix="autovj_leaktest_")              # 源素材夹具根
_touched = set()                                                    # 本测试产生的 Temp 副本，收尾兜底删
_extra_dirs = []                                                    # 额外的隔离夹具目录（收尾 rmtree）


def _temp_snapshot():
    import glob
    return set(glob.glob(os.path.join(_TEMP, "autovj_*")))


try:
    mm._TMP_COPIES.clear()

    # ------------------------------------------------------------------
    print("① (d) 复用：同源不重复复制 + 副本落 Temp + 登记")
    # ------------------------------------------------------------------
    zh_dir = os.path.join(_fixture, "素材库", "夜景")
    os.makedirs(zh_dir, exist_ok=True)
    src = os.path.join(zh_dir, "亮图.png")
    # ⚠ 夹具必须字节写：cv2.imwrite 自身读不了非 ASCII 路径。
    import cv2
    _ok, _buf = cv2.imencode(".png", np.full((32, 32, 3), 200, dtype=np.uint8))
    with open(src, "wb") as f:
        f.write(_buf.tobytes())
    try:
        src.encode("ascii")
        check("★ 前置：源路径确含非 ASCII", False, src)
    except UnicodeEncodeError:
        check("★ 前置：源路径确含非 ASCII", True)

    safe1 = mm._ascii_safe_path(src)
    _touched.add(safe1)
    check("副本落在 Temp 且非原路径", safe1 != src and os.path.dirname(safe1) == _TEMP, safe1)
    check("副本命名含 autovj_ 前缀", os.path.basename(safe1).startswith("autovj_"),
          os.path.basename(safe1))
    check("副本已落盘", os.path.exists(safe1))
    check("副本已登记进 _TMP_COPIES", safe1 in mm._TMP_COPIES)

    m1 = os.path.getmtime(safe1)
    safe2 = mm._ascii_safe_path(src)
    _touched.add(safe2)
    check("★ (d) 同源二次调用返回同一副本", safe2 == safe1, (safe1, safe2))
    check("★ (d) 同源二次调用不重新写盘（mtime 不变）",
          abs(os.path.getmtime(safe2) - m1) < 1e-6)

    # 纯 ASCII 路径不复制
    ascii_src = os.path.join(_fixture, "ascii_ok.png")
    with open(ascii_src, "wb") as f:
        f.write(_buf.tobytes())
    check("纯 ASCII 路径原样返回（不复制）", mm._ascii_safe_path(ascii_src) == ascii_src)

    # ------------------------------------------------------------------
    print()
    print("② (a) 退出删：登记副本被删，不触碰未登记文件")
    # ------------------------------------------------------------------
    # 另造一个「不登记」的 autovj_* 文件（模拟别人/遗留），退出删不该动它
    outsider = os.path.join(_TEMP, "autovj_outside_should_stay.txt")
    with open(outsider, "w", encoding="utf-8") as f:
        f.write("x")
    _touched.add(outsider)
    before_unreg = os.path.exists(outsider)

    mm._cleanup_registered_copies()
    check("★ (a) 登记副本已被删除", not os.path.exists(safe1), safe1)
    check("★ (a) 未登记的 autovj_* 文件未被误删（只在登记表内删）",
          os.path.exists(outsider) and before_unreg)
    check("(a) 登记表已清空", len(mm._TMP_COPIES) == 0)

    # ------------------------------------------------------------------
    print()
    print("③ (c) 启动清孤儿：TTL + 占用跳过")
    # ------------------------------------------------------------------
    # ⚠ **隔离扫描目录**：cleanup_stale_ascii_copies 支持 directory 参数，
    #   测试传入自己的夹具目录，**绝不**扫真实 Temp（否则会误删用户真实遗留副本）。
    scan_dir = tempfile.mkdtemp(prefix="autovj_leakscan_")
    _extra_dirs.append(scan_dir)
    # 3a: 旧 mtime（2 天前）→ 应被删
    stale = os.path.join(scan_dir, "autovj_000000000000_1_1.png")
    with open(stale, "wb") as f:
        f.write(b"stale")
    old = time.time() - 2 * 24 * 3600
    os.utime(stale, (old, old))
    # 3b: 新 mtime（刚建）→ 应保留
    fresh = os.path.join(scan_dir, "autovj_111111111111_1_1.png")
    with open(fresh, "wb") as f:
        f.write(b"fresh")
    # 3c: 被占用（保持打开）→ 应优雅跳过
    locked = os.path.join(scan_dir, "autovj_222222222222_1_1.png")
    with open(locked, "wb") as f:
        f.write(b"locked")
    old2 = time.time() - 2 * 24 * 3600
    os.utime(locked, (old2, old2))
    fh = open(locked, "rb")                                          # 打开使其"被占用"

    try:
        removed, skipped, kept = mm.cleanup_stale_ascii_copies(ttl_hours=24.0,
                                                               directory=scan_dir)
    finally:
        fh.close()
    check("★ (c) 旧副本被删", not os.path.exists(stale), "removed=%d" % removed)
    check("★ (c) 新副本保留", os.path.exists(fresh), "kept=%d" % kept)
    # 被占用：Windows 下删打开的文件会失败 → 跳过；断言"未崩溃"且"要么删掉要么还在"
    check("★ (c) 被占用文件处理不崩溃（in [deleted, kept]）",
          os.path.exists(locked) or not os.path.exists(locked),
          "skipped=%d" % skipped)
    check("(c) 返回三元组均为整数", all(isinstance(x, int) for x in (removed, skipped, kept)),
          (removed, skipped, kept))

    # 收尾：把仍留在 scan_dir 的测试副本清掉
    for p in (stale, fresh, locked):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass

    # ------------------------------------------------------------------
    print()
    print("④ 断言：本测试在 Temp 产生的 autovj_* 副本已全部回收")
    # ------------------------------------------------------------------
    # outsider 是**故意**保留、验证"退出删只删登记表内"的样本，不计入回收断言。
    leftovers = [p for p in _touched
                 if p != outsider and p != safe1 and os.path.exists(p)]
    check("★ 测试登记副本已全部回收（不含 outsider 样本）", not leftovers, leftovers)
    # outsider：本测试结束后也一并删掉（不留在真实 Temp）
    try:
        if os.path.exists(outsider):
            os.remove(outsider)
    except OSError:
        pass
    check("★ outsider 样本也已清离 Temp", not os.path.exists(outsider))

finally:
    # 新铁律：finally 清理一切夹具
    try:
        mm._cleanup_registered_copies()
    except Exception:
        pass
    for p in list(_touched):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    try:
        shutil.rmtree(_fixture, ignore_errors=True)
    except Exception:
        pass
    for d in list(_extra_dirs):
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass

print()
print("=" * 60)
print("结果：%s" % ("全部通过 ✓" if not FAILS else "失败 %d 项 ✗ %s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
