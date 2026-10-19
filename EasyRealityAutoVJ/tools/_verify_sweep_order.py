# -*- coding: utf-8 -*-
"""验证 Bug B：启动清扫时机 —— `cleanup_stale_ascii_copies` 必须在单实例锁**之前**。

不启动真 GUI（不建 QApplication）：用 AST 解析 main.py，断言「临时副本清理」调用点
在源码中的行号 **早于** `lock.tryLock(...)`。同时真跑一次 cleanup 逻辑，确认它在
「第二实例（锁被占、本应 sys.exit）」这条路径之前就已执行（即与锁无关）。

临时目录用 mkdtemp + finally rmtree。
"""
import ast
import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(_ROOT, "src", "main.py")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    src = open(MAIN, "r", encoding="utf-8").read()
    tree = ast.parse(src)

    # 找第一个 main() 顶层函数体
    main_fn = None
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "main":
            main_fn = n
            break
    assert main_fn is not None, "未找到 main()"

    sweep_line = None
    lock_line = None
    sweep_call_line = None
    for n in ast.walk(main_fn):
        if isinstance(n, ast.Call):
            name = None
            if isinstance(n.func, ast.Name):
                name = n.func.id
            elif isinstance(n.func, ast.Attribute):
                name = n.func.attr
            if name == "cleanup_stale_ascii_copies":
                sweep_call_line = n.lineno
            if name == "tryLock":
                if lock_line is None or n.lineno < lock_line:
                    lock_line = n.lineno

    print("cleanup_stale_ascii_copies 调用行 =", sweep_call_line)
    print("lock.tryLock 调用行 =", lock_line)
    assert sweep_call_line is not None, "未找到清扫调用！"
    assert lock_line is not None, "未找到 tryLock！"
    assert sweep_call_line < lock_line, \
        "清扫仍在锁之后（行 %s >= %s）—— Bug B 未修复" % (sweep_call_line, lock_line)
    print("✅ 清扫调用在锁之前（行 %d < %d）" % (sweep_call_line, lock_line))

    # 真跑 cleanup 逻辑（隔离目录，验证它确实能删过期文件、且不依赖锁）
    sys.path.insert(0, os.path.join(_ROOT, "src"))
    import media_manager as mm
    import time

    d = tempfile.mkdtemp(prefix="autovj_qa_sweep_")
    try:
        old = os.path.join(d, "autovj_oldhash_1_1.mov")
        new = os.path.join(d, "autovj_newhash_2_2.mov")
        with open(old, "wb") as f:
            f.write(b"x" * 16)
        with open(new, "wb") as f:
            f.write(b"x" * 16)
        # 把关掉 old 的 mtime 拨到 48 小时前
        past = time.time() - 48 * 3600
        os.utime(old, (past, past))
        rm, lk, keep = mm.cleanup_stale_ascii_copies(ttl_hours=24.0, directory=d)
        print("cleanup(隔离目录) -> removed=%d skipped_locked=%d kept_recent=%d" %
              (rm, lk, keep))
        assert not os.path.exists(old), "过期副本未被删除！"
        assert os.path.exists(new), "最近副本被误删！"
        print("✅ 过期删除 / 最近保留 均正确；清扫与锁无关（纯目录操作）")
        print("\n裁决: PASS")
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    main()
