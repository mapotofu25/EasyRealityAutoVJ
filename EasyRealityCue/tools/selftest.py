# -*- coding: utf-8 -*-
"""EasyRealityCue 一键自检（M0）。

跑法（Windows，用参考项目的 venv 里已有的解释器即可）：
    P:\\AutoVJ\\venv\\Scripts\\python.exe  P:\\EasyRealityCue\\tools\\selftest.py

覆盖：
  · units     单位换算往返 + 退化（bpm<=0 → 1 拍 = 1 秒）+ 吸附步长 + 时间码
  · features  能力词表（7 支持 / 只增不删）+ requires_features 自动推断
  · cuesheet  规范化边界 + meta_key 逐字 + 四级认歌（含多命中分支）+ 原生往返
  · ercue_io  版本协商四条文案 + zip/裸 JSON 容错 + IO 往返 + 脏数据不崩 + ★双向格式兼容
  · 零 Qt     扫描 model/ 下是否出现 PySide6 / shiboken / PyQt（应为 0）
"""

from __future__ import annotations

import os
import subprocess
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
_MODEL_DIR = os.path.join(_SRC, "ercue", "model")

if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _run_suite(name, fn):
    try:
        ok = bool(fn())
        return (name, ok, "" if ok else "返回非 True")
    except Exception as e:                                     # noqa: BLE001
        return (name, False, "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()))


def zero_qt_check() -> tuple:
    """AST 扫描 model/ 下所有 .py 的 import 语句，确认没有 Qt 绑定。

    只看真实 import（不看注释/文档字符串），返回 `(ok, 命中列表)`。
    """
    import ast
    forbidden = ("PySide6", "PyQt5", "PyQt6", "shiboken")
    hits = []
    for fn in sorted(os.listdir(_MODEL_DIR)):
        if not fn.endswith(".py"):
            continue
        path = os.path.join(_MODEL_DIR, fn)
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            for m in mods:
                if m.split(".")[0] in forbidden:
                    hits.append("%s:%d: import %s" % (fn, node.lineno, m))
    return (not hits), hits


def main() -> int:
    from ercue.model import units, features, cuesheet, ercue_io, fp_pack

    suites = [
        ("units", units.selftest),
        ("features", features.selftest),
        ("cuesheet", cuesheet.selftest),
        ("ercue_io", ercue_io.selftest),
        ("双向格式兼容", ercue_io.compat_selftest),
        ("状态快照≠文件形态", ercue_io.state_vs_file_selftest),
        # ★ 指纹包二进制格式（`fp/<fp_id>.bin`）：往返 / 空列表 / 坏数据反例。
        #   与演出端 `AutoVJ/src/cue_fp_pack.py` 是**同一格式的两份实现**，
        #   跨端一致性由 `tools/_fp_pack_test.py` 的交叉验证兜底。
        ("指纹包格式", fp_pack.selftest),
    ]

    print("=" * 68)
    print("EasyRealityCue M0 自检  (python %s)" % sys.version.split()[0])
    print("=" * 68)

    results = []
    for name, fn in suites:
        print("\n--- %s ---" % name)
        n, ok, detail = _run_suite(name, fn)
        results.append((n, ok, detail))
        if not ok:
            print("  ✗ %s\n%s" % (n, detail))

    # 零 Qt
    print("\n--- 零 Qt（grep model/）---")
    qt_ok, hits = zero_qt_check()
    results.append(("零 Qt", qt_ok, "\n".join(hits)))
    if qt_ok:
        print("  ✓ model/ 下无任何 Qt 绑定 import")
    else:
        print("  ✗ 命中：\n" + "\n".join(hits))

    print("\n" + "=" * 68)
    ok_all = all(ok for _, ok, _ in results)
    for name, ok, _ in results:
        print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
    print("=" * 68)
    print("汇总：%d/%d 通过  ⇒  %s" % (sum(1 for _, ok, _ in results if ok),
                                       len(results), "全绿 ✓" if ok_all else "有失败 ✗"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
