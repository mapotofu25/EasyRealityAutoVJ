# -*- coding: utf-8 -*-
"""用 AST 扫全库：同一个类里**重复定义**的方法（后定义静默覆盖前定义）。

这类 bug 的共同症状：改了"看起来对"的那一份却没反应；注释描述的行为不生效。
"""
import ast
import io
import os
import sys

# 项目根：优先按脚本自身位置推导（项目已从 P:\AutoVJ 迁移到 P:\ERAVJ\AutoVJ）
ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
bad = 0
for f in sorted(os.listdir(ROOT)):
    if not f.endswith(".py"):
        continue
    p = os.path.join(ROOT, f)
    try:
        tree = ast.parse(io.open(p, encoding="utf-8").read())
    except SyntaxError as e:
        print("语法错误", f, e)
        continue
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        seen = {}
        for sub in node.body:
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                seen.setdefault(sub.name, []).append(sub.lineno)
        for name, at in seen.items():
            if len(at) > 1:
                bad += 1
                print("%s: class %s 的 %s() 定义了 %d 次 → 行 %s"
                      % (f, node.name, name, len(at), at))
print("\n共 %d 处重复定义" % bad)
sys.exit(1 if bad else 0)
