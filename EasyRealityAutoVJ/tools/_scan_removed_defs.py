# -*- coding: utf-8 -*-
"""改动安全性检查：对比 HEAD 与本版，列出**丢失的定义**。

为什么要它（2026-10-01 真实踩到）：
  用「大段文本替换」批量改代码时，如果替换的区间范围算错，
  会把**夹在中间的其他模块级定义一起删掉** —— 这类错误 py_compile **查不出来**
  （NameError 只在运行时才炸），只有真正跑到那条路径才会暴露。
  这次就把 `_SOFT_EMPTY_BACKOFF_AFTER` / `_log_decoder_giveup` 等删掉了，
  靠回归测试才抓到。所以每次大段替换之后都跑一遍本脚本。

用法：venv\\Scripts\\python.exe -u tools/_scan_removed_defs.py [文件...]
      不给参数时检查所有 src/*.py。
"""

import ast
import io
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")

# 有意删掉的旧符号（写成 "文件::符号"）：不是误删，别报警。
#   加进来的每一条都必须在这里说清"为什么删、被什么取代了"。
KNOWN_REMOVALS = {
    # 2026-10-01：全局一次性日志开关 → 改成按 key 去重的 _LOGGED
    #（原来的全局开关会让"第一条日志之后所有错误永久静默"，
    #  直接制造了「NDI 有画面没声音、日志里什么都没有」这种最难查的现场）
    "src/ndi_out.py::_ERR_ONCE",
}


def collect(path_or_text, is_text=False):
    src = path_or_text if is_text else io.open(path_or_text, encoding="utf-8").read()
    try:
        t = ast.parse(src)
    except SyntaxError:
        return None
    out = {}
    for n in t.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[n.name] = "func"
        elif isinstance(n, ast.ClassDef):
            out[n.name] = "class"
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out["%s.%s" % (n.name, m.name)] = "method"
        elif isinstance(n, ast.Assign):
            for tg in n.targets:
                if isinstance(tg, ast.Name):
                    out[tg.id] = "var"
    return out


def head_text(rel):
    r = subprocess.run(["git", "show", "HEAD:%s" % rel], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8")
    return r.stdout if r.returncode == 0 else None


def main():
    files = sys.argv[1:]
    if not files:
        files = sorted(os.path.join("src", f) for f in os.listdir(SRC)
                       if f.endswith(".py"))
    bad = 0
    for rel in files:
        rel = rel.replace("\\", "/")
        old = head_text(rel)
        if not old:
            print("%-28s （HEAD 里没有，跳过）" % rel)
            continue
        new = io.open(os.path.join(ROOT, rel), encoding="utf-8").read()
        a, b = collect(old, True), collect(new, True)
        if a is None or b is None:
            print("%-28s 解析失败" % rel)
            continue
        gone = sorted(x for x in (set(a) - set(b))
                      if ("%s::%s" % (rel, x)) not in KNOWN_REMOVALS)
        added = sorted(set(b) - set(a))
        if gone:
            bad += 1
            print("%-28s !!! 丢失 %d 个定义：%s" % (rel, len(gone), gone))
        else:
            print("%-28s ✓ 无丢失（新增 %d 个：%s）"
                  % (rel, len(added), added[:6] if added else "无"))
    print()
    if bad:
        print("!! 有 %d 个文件丢过定义 —— 很可能是批量替换的区间算错了，请逐条确认" % bad)
        return 1
    print("全部文件：没有丢失任何定义 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
