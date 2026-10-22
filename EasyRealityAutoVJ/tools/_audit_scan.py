# -*- coding: utf-8 -*-
"""T1 扩展模式扫描 —— 为 ERAVJ 代码审计建立基线台账。

用 AST（而非正则）精确统计：
  1. 吞异常（except 块里只有 pass/continue，无日志）
  2. while True 循环（是否带退避 sleep）
  3. time.sleep / 休眠调用分布
  4. 线程创建点
  5. 锁操作点
  6. 超长函数（>80 行）
  7. subprocess / QTimer / global 声明

产出落 _audit_20261010/t1_baseline.txt
"""
import ast
import io
import os
import sys

SRC = r"P:\ERAVJ\AutoVJ\src"
OUTDIR = r"P:\ERAVJ\AutoVJ\_audit_20261010"
OUT = os.path.join(OUTDIR, "t1_baseline.txt")

SLEEP_NAMES = {"sleep", "wait", "join", "wait_for", "processEvents",
               "msleep", "usleep", "singleShot"}


def iter_py(root):
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d != "__pycache__"]
        for f in sorted(fn):
            if f.endswith(".py"):
                yield os.path.join(dp, f)


def call_name(node):
    """取调用的名字（支持 a.b.c() 与 f()）"""
    f = node.func
    parts = []
    while isinstance(f, ast.Attribute):
        parts.append(f.attr)
        f = f.value
    if isinstance(f, ast.Name):
        parts.append(f.id)
    return ".".join(reversed(parts))


def has_log_call(body):
    """except 块里有没有日志/上报（print / _log / logger / logging / emit / 写文件）"""
    for n in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(n, ast.Call):
            nm = call_name(n).lower()
            if any(k in nm for k in ("log", "print", "emit", "warn", "debug",
                                     "info", "error", "exception")):
                return True
        if isinstance(n, ast.Raise):
            return True
    return False


class V(ast.NodeVisitor):
    def __init__(self, path):
        self.path = path
        self.rel = os.path.relpath(path, SRC)
        self.swallow = []      # (行, 异常类型, 备注)
        self.whiles = []       # (行, 有无退避)
        self.sleeps = []       # (行, 名字, 所在函数)
        self.threads = []      # (行, 名字)
        self.locks = []        # (行, 名字, 所在函数)
        self.longfns = []      # (函数名, 起, 止, 行数)
        self.subprocs = []
        self.timers = []
        self.funcstack = []

    def visit_FunctionDef(self, node):
        self._fn(node)

    def visit_AsyncFunctionDef(self, node):
        self._fn(node)

    def _fn(self, node):
        n = (node.end_lineno or node.lineno) - node.lineno + 1
        if n > 80:
            self.longfns.append((node.name, node.lineno,
                                 node.end_lineno or node.lineno, n))
        self.funcstack.append(node.name)
        self.generic_visit(node)
        self.funcstack.pop()

    def visit_ExceptHandler(self, node):
        body = node.body
        # 只统计"纯吞"：块内语句全是 pass/continue/break（无其它动作）
        only_silent = bool(body) and all(
            isinstance(b, (ast.Pass, ast.Continue, ast.Break)) for b in body)
        if only_silent and not has_log_call(body):
            et = "?"
            if node.type is not None:
                et = ast.unparse(node.type)
            self.swallow.append((node.lineno, et, ""))
        self.generic_visit(node)

    def visit_While(self, node):
        t = node.test
        if isinstance(t, ast.Constant) and t.value is True:
            # 看循环体里有没有退避
            soft = False
            for n in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                if isinstance(n, ast.Call):
                    nm = call_name(n).split(".")[-1]
                    if nm in SLEEP_NAMES:
                        soft = True
            self.whiles.append((node.lineno, soft))
        self.generic_visit(node)

    def visit_Call(self, node):
        nm = call_name(node)
        tail = nm.split(".")[-1]
        if tail in ("sleep", "msleep", "usleep") and "SingleShot" not in nm:
            fn = self.funcstack[-1] if self.funcstack else "<module>"
            self.sleeps.append((node.lineno, nm, fn))
        if tail == "Thread" or nm.endswith("threading.Thread"):
            self.threads.append((node.lineno, nm))
        if tail in ("acquire", "release"):
            fn = self.funcstack[-1] if self.funcstack else "<module>"
            self.locks.append((node.lineno, nm, fn))
        if nm.startswith("subprocess.") or tail in ("Popen", "run", "check_output") \
                and "subprocess" in nm:
            self.subprocs.append((node.lineno, nm))
        if "Timer" in tail:
            self.timers.append((node.lineno, nm))
        self.generic_visit(node)


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    agg = {"swallow": [], "whiles": [], "sleeps": [], "threads": [],
           "locks": [], "longfns": [], "subprocs": [], "timers": []}
    nfiles = 0
    for p in iter_py(SRC):
        nfiles += 1
        try:
            src = io.open(p, encoding="utf-8").read()
            tree = ast.parse(src)
        except Exception as e:                                   # noqa: BLE001
            print("!! 解析失败", p, e)
            continue
        v = V(p)
        v.visit(tree)
        agg["swallow"] += [(v.rel, a, b, c) for a, b, c in v.swallow]
        agg["whiles"] += [(v.rel, a, b) for a, b in v.whiles]
        agg["sleeps"] += [(v.rel, a, b, c) for a, b, c in v.sleeps]
        agg["threads"] += [(v.rel, a, b) for a, b in v.threads]
        agg["locks"] += [(v.rel, a, b, c) for a, b, c in v.locks]
        agg["longfns"] += [(v.rel, a, b, c, d) for a, b, c, d in v.longfns]
        agg["subprocs"] += [(v.rel, a, b) for a, b in v.subprocs]
        agg["timers"] += [(v.rel, a, b) for a, b in v.timers]

    L = []
    w = L.append
    w("ERAVJ 代码审计 T1 基线台账（AST 精确统计）")
    w("扫描 %d 个文件" % nfiles)
    w("=" * 70)

    w("")
    w("【1】吞异常 —— except 块内只有 pass/continue，且无任何日志")
    w("     判据：块内**所有**语句都是 pass/continue/break，且不含 log/print/emit/raise")
    w("     共 %d 处（每个都该问一句：吞掉之后出问题怎么排查？）" % len(agg["swallow"]))
    w("")
    cur = None
    for rel, ln, et, _ in agg["swallow"]:
        if rel != cur:
            w("  -- %s" % rel)
            cur = rel
        w("     L%-6d except %s" % (ln, et))

    w("")
    w("=" * 70)
    w("【2】while True 循环 —— 每处都要确认'取到空必须退避'")
    w("     共 %d 处" % len(agg["whiles"]))
    for rel, ln, soft in agg["whiles"]:
        w("  %-28s L%-6d  %s" % (rel, ln, "有 sleep/wait 类调用" if soft else "★ 未检出退避调用"))

    w("")
    w("=" * 70)
    w("【3】sleep 调用分布 —— 区分'合理等待'与'热路径误睡'")
    w("     共 %d 处" % len(agg["sleeps"]))
    for rel, ln, nm, fn in agg["sleeps"]:
        w("  %-28s L%-6d %-16s in %s" % (rel, ln, nm, fn))

    w("")
    w("=" * 70)
    w("【4】线程创建点 —— 共 %d 处" % len(agg["threads"]))
    for rel, ln, nm in agg["threads"]:
        w("  %-28s L%-6d %s" % (rel, ln, nm))

    w("")
    w("=" * 70)
    w("【5】锁操作点 —— 共 %d 处（配合 tools/_scan_locks.py 的结论看）" % len(agg["locks"]))
    for rel, ln, nm, fn in agg["locks"]:
        w("  %-28s L%-6d %-20s in %s" % (rel, ln, nm, fn))

    w("")
    w("=" * 70)
    w("【6】超长函数（>80 行）—— 维护性与拆分候选，共 %d 个" % len(agg["longfns"]))
    for rel, nm, a, b, n in sorted(agg["longfns"], key=lambda x: -x[4]):
        w("  %-28s %-38s L%d~%d (%d 行)" % (rel, nm, a, b, n))

    w("")
    w("=" * 70)
    w("【7】subprocess 调用 —— 共 %d 处（每处都要问：带 CREATE_NO_WINDOW 了吗）" % len(agg["subprocs"]))
    for rel, ln, nm in agg["subprocs"]:
        w("  %-28s L%-6d %s" % (rel, ln, nm))

    w("")
    w("【8】QTimer 相关 —— 共 %d 处（GUI 高频路径，逐个核对是否碰引擎锁）" % len(agg["timers"]))
    for rel, ln, nm in agg["timers"]:
        w("  %-28s L%-6d %s" % (rel, ln, nm))

    txt = "\n".join(L)
    io.open(OUT, "w", encoding="utf-8").write(txt)
    print("已写出", OUT, "共", len(txt.splitlines()), "行")
    # 控制台摘要
    print("=" * 60)
    print("吞异常 %d / whileTrue %d（无退避 %d）/ sleep %d / 线程 %d / 锁 %d / 超长函数 %d / subprocess %d / QTimer %d"
          % (len(agg["swallow"]), len(agg["whiles"]),
             sum(1 for _, _, s in agg["whiles"] if not s),
             len(agg["sleeps"]), len(agg["threads"]), len(agg["locks"]),
             len(agg["longfns"]), len(agg["subprocs"]), len(agg["timers"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
