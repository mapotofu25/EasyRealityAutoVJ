# -*- coding: utf-8 -*-
r"""验证「跨线程写库」修复 —— 用户报「扫描结束不显示出来」的根因。

## 根因（2026-10-08 现场）
`LibWriter` 在**界面主线程**里创建（用户点「开始扫描」那一刻），
但真正的写入发生在**扫描线程**（`ScanThread`）里。
`sqlite3` 默认 `check_same_thread=True` ⇒ **每一首 `add_song` 都抛**
`ProgrammingError: SQLite objects created in a thread can only be used in that same thread`，
而调用方（`Scanner.scan_tree`）把异常吞了
⇒ **工作库里一首歌都没有，界面只显示"失败 N 首"**。

## 本脚本验两件事
1. **跨线程写入**（根因）：主线程建 writer → 子线程 add_song
2. **真实目录端到端**：拿用户实际那个专辑目录跑完整 `scan_tree`，
   确认 `ok > 0` 且工作库里真有数据

## 反向复现
`--old`：用 `sqlite3` 的**默认** `check_same_thread=True` 建连接
⇒ 跨线程写必须失败（证明这条判据真能抓住这个 bug）。
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import threading

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from erlib.format import LibReader, LibWriter                       # noqa: E402
from erlib.scanner import Scanner, find_audio                       # noqa: E402

REAL_DIR = (r"P:/[Rolling Contact]/2013.12.30 [LOLI-0039] "
            r"TOUHOU meets HARDCORE 2 [C85]")


def test_cross_thread(old):
    """主线程建 writer、子线程写 —— 修复前必失败。"""
    d = tempfile.mkdtemp(prefix="erlib_xt_")
    p = os.path.join(d, "t.eravjlib")
    w = LibWriter(p, root=d, resume=False, app_ver="xt")
    if old:
        # 关键：把连接换成"默认参数"（check_same_thread=True），重现修复前行为
        try:
            w.conn.close()
        except Exception:                                        # noqa: BLE001
            pass
        w.conn = sqlite3.connect(p)
    err = []

    def worker():
        try:
            w.add_song(123456, {"file_name": "x.flac", "duration": 1.0},
                       [("h1", 1), ("h2", 2)], None)
            w.commit()
        except Exception as e:                                   # noqa: BLE001
            err.append("%s: %s" % (type(e).__name__, e))

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    try:
        w.close()
    except Exception:                                            # noqa: BLE001
        pass
    n = 0
    try:
        r = LibReader(p)
        n = r.count()
        r.close()
    except Exception:                                            # noqa: BLE001
        pass
    shutil.rmtree(d, ignore_errors=True)
    return err, n


def test_real_scan():
    """用用户实际的专辑目录跑完整 scan_tree。"""
    if not os.path.isdir(REAL_DIR):
        return None
    files = find_audio(REAL_DIR)
    if not files:
        return None
    d = tempfile.mkdtemp(prefix="erlib_real_")
    p = os.path.join(d, "real.eravjlib")
    try:
        w = LibWriter(p, root=REAL_DIR, resume=False, app_ver="real")
        sc = Scanner(root=REAL_DIR)
        st = sc.scan_tree(REAL_DIR, w)
        w.close()
        r = LibReader(p)
        n = r.count()
        r.close()
        return st, n, len(files)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    old = "--old" in sys.argv
    print("=" * 74)
    print("跨线程写库验证" + ("   ★ --old（应失败）" if old else ""))
    print("=" * 74)

    print("\n① 跨线程写入（主线程建 writer → 子线程 add_song）")
    err, n = test_cross_thread(old)
    if err:
        print("   抛异常：%s" % err[0])
    print("   写入后库里歌曲数：%d" % n)
    ok1 = (not err) and n == 1
    print("   判定：%s" % ("✓ 通过" if ok1 else "✗ 失败"))

    if old:
        print("\n" + "=" * 74)
        print("反向复现：%s（check_same_thread=True 时跨线程写必失败）"
              % ("✅ 成功" if not ok1 else "❌ 未复现"))
        print("=" * 74)
        return 0 if not ok1 else 1

    print("\n② 真实目录端到端（%s）" % os.path.basename(REAL_DIR))
    r = test_real_scan()
    ok2 = True
    if r is None:
        print("   跳过（目录不存在或没有音频）")
    else:
        st, n, nf = r
        print("   目录里 %d 个音频；扫描 ok=%d fail=%d skip=%d，用时 %.1fs"
              % (nf, st.get("ok", 0), st.get("fail", 0), st.get("skip", 0),
                 st.get("elapsed", 0)))
        for a, b in (st.get("errors") or [])[:3]:
            print("     失败：%s → %s" % (a, b))
        print("   工作库里实际写入：%d 首" % n)
        ok2 = (st.get("ok", 0) > 0 and n > 0)
        print("   判定：%s" % ("✓ 通过" if ok2 else "✗ 失败"))

    print("\n" + "=" * 74)
    allok = ok1 and ok2
    print("结论：%s" % ("✅ 全部通过" if allok else "❌ 有失败"))
    print("=" * 74)
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
