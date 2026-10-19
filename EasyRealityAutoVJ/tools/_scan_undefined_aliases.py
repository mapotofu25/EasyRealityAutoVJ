"""扫描「函数里用了某个模块别名，但该名字在该作用域内根本没定义」的 NameError 隐患。

★ 为什么需要它（2026-10-07 实锤）：
  `audio_engine._capture_loop` 里写 `_np.asarray(...)`，但模块顶部是
  `import numpy as np`，`_np` 只在**另一个函数** `_recognize_loop` 内部做了局部
  `import numpy as _np` —— 作用域不覆盖。于是**每块都抛 `NameError: name '_np'
  is not defined`**，还被 `except Exception: pass` 静静吞掉：
  NDI 音频 push 恒为 0、队列永远空，从 1c9ac6e 起"从来没响过"，且毫无日志。
  `py_compile` 查不出来（NameError 只在运行时炸）。

判据（只报**高置信**的）：
  1. 某函数体内出现 `X.y` / `X.y(...)` 形式的名字 X；
  2. X 以 `_` 开头且形如 `_xx`（局部别名惯用名），且**不是** `self`/`cls`；
  3. 在「该函数体 + 模块全局」范围内**都没有** `import ... as X`、`X = ...`、
     `def X`、`for X in`、`with ... as X`、形参 X、`global X`；
  4. 该文件全局也没有该名字。
  ⇒ 报出来。

用法：`python tools/_scan_undefined_aliases.py [src 目录]`
退出码：0=无隐患；1=有；2=脚本自身出错。
"""
import ast
import os
import sys

# 只关心这类"模块别名"习惯用名，避免误报普通变量。
# ⚠ 这是**已知用过的**别名白名单（实测够用）；若要更宽，可加更多 `_xx` 形式，
#   但每加一个都要先跑一遍确认无误报。
ALIAS_RE = ("_np", "_t", "_sc", "_cv", "_pd", "_re", "_os", "_io", "_json", "_time")


def _bound_names(node):
    """收集一个作用域内**定义**的所有名字（粗粒度，宁可少报不误报）。

    ⚠⚠ **不下钻到嵌套函数/类/lambda 的内部** —— 它们的局部绑定不属于本作用域。
    第一版用 `ast.walk(node)` 会钻进 `_recognize_loop` 之类的嵌套体，
    把里面的局部 import 当成"本作用域有定义"，从而漏报（正是本次的 bug 形态）。
    """
    names = set()

    def visit(n, top):
        """top=True 表示这个节点属于本作用域本身（可收集其绑定）。"""
        if isinstance(n, ast.Import):
            for a in n.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                names.add(a.asname or a.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign)):
            for sub in ast.walk(n.target):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(n.name)
            args = n.args
            for a in list(args.args) + list(args.posonlyargs) + list(args.kwonlyargs):
                names.add(a.arg)
            if args.vararg:
                names.add(args.vararg.arg)
            if args.kwarg:
                names.add(args.kwarg.arg)
        elif isinstance(n, ast.ClassDef):
            names.add(n.name)
        elif isinstance(n, (ast.For, ast.AsyncFor)):
            for sub in ast.walk(n.target):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for item in n.items:
                if item.optional_vars is not None:
                    for sub in ast.walk(item.optional_vars):
                        if isinstance(sub, ast.Name):
                            names.add(sub.id)
        elif isinstance(n, ast.ExceptHandler):
            if n.name:
                names.add(n.name)
        elif isinstance(n, ast.comprehension):
            for sub in ast.walk(n.target):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            names.update(n.names)
        elif isinstance(n, ast.Lambda):
            for a in list(n.args.args) + list(n.args.kwonlyargs):
                names.add(a.arg)

        # 下钻：遇到嵌套的函数/类/lambda 一律**不再下钻**（那是另一个作用域）
        for child in ast.iter_child_nodes(n):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                # 但斜杠："def" 本身的名字已在上面处理；其 body 不归本作用域
                if child is not n:
                    # 只在 child 不是当前节点本身时跳过
                    pass
                continue
            visit(child, False)

    # 顶层：node 自身若是函数，先收集它自己的名字/形参，再从"函数体的语句"下钻
    visit(node, True)
    return names


def _used_attr_roots(node):
    """收集本作用域内所有 `X.attr` 的根名字 X；**不下钻嵌套函数/类/lambda**。"""
    used = set()

    def visit(n):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
            used.add(n.value.id)
        for child in ast.iter_child_nodes(n):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue          # 另一个作用域，跳过
            visit(child)

    visit(node)
    return used


def scan_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src, filename=path)
    except SyntaxError as e:
        print("  [语法错误] %s: %s" % (path, e))
        return []

    # 模块全局绑定的名字。
    # ⚠⚠ 必须**只看顶层语句**，绝不能 `ast.walk(tree)` —— 否则会把**其它函数体内的
    #   局部 `import numpy as _np`** 也算成全局名，正好漏掉本次要抓的 bug
    #   （这就是第一版扫描器"对修复前代码报 0 处"的原因）。
    global_names = set()
    for n in tree.body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                global_names.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Name):
                        global_names.add(sub.id)
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign)):
            for sub in ast.walk(n.target):
                if isinstance(sub, ast.Name):
                    global_names.add(sub.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            global_names.add(n.name)
        elif isinstance(n, ast.For) or isinstance(n, ast.AsyncFor):
            for sub in ast.walk(n.target):
                if isinstance(sub, ast.Name):
                    global_names.add(sub.id)
        elif isinstance(n, ast.Try):
            for sub in ast.walk(n):
                if isinstance(sub, ast.ExceptHandler) and sub.name:
                    global_names.add(sub.name)
        elif isinstance(n, (ast.Global,)):
            global_names.update(n.names)

    problems = []
    # 建"父节点"索引，便于对每个函数算出**所有外层函数作用域**（闭包可见性）
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def enclosing_bindings(fn):
        """本函数可见的**外层函数**作用域绑定（闭包规则：逐层向上累加）。

        ⚠ 不做这一步就会把 `def _log()` 里用外层 `import os as _os` 误报成 NameError
          （实测命中 ui_main 两处假阳性）。
        """
        names = set()
        cur = parents.get(fn)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names |= _bound_names(cur)
            cur = parents.get(cur)
        return names

    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        local_names = _bound_names(fn) | enclosing_bindings(fn)
        # 本函数**自身作用域**内所有 `X.attr` 的 X（不下钻嵌套函数 —— 那是别人的作用域）
        used = _used_attr_roots(fn)
        for name in used:
            if name not in ALIAS_RE:
                continue
            if name in local_names or name in global_names:
                continue
            problems.append((fn.name, fn.lineno, name))
    return problems


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "src")
    root = os.path.abspath(root)
    total = 0
    files = 0
    for dirpath, _dirs, filenames in os.walk(root):
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            files += 1
            p = os.path.join(dirpath, fn)
            probs = scan_file(p)
            for fname, lineno, name in probs:
                total += 1
                print("%s:%d  函数 %s() 用了 `%s.`，但该作用域内未见 `%s` 的定义"
                      "  ⇒ 运行时会 NameError（py_compile 查不出）"
                      % (os.path.relpath(p, root), lineno, fname, name, name))
    print("\n扫描 %d 个文件，发现 %d 处「未定义别名」隐患" % (files, total))
    return 1 if total else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:                                          # noqa: BLE001
        print("!! 脚本自身出错：%s: %s" % (type(e).__name__, e))
        raise SystemExit(2)
