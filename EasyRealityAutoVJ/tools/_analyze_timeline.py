# -*- coding: utf-8 -*-
"""解析 _pyspy_timeline.txt：统计每个采样点里解码线程分别停在哪。"""
import collections
import io
import re
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "_pyspy_timeline.txt"
txt = io.open(path, encoding="utf-8", errors="replace").read()

# 按 === HH:MM:SS === 切块
blocks = re.split(r"^=== (\d\d:\d\d:\d\d) ===\s*$", txt, flags=re.M)
print("采样点 %d 个" % ((len(blocks) - 1) // 2))

INTEREST = ("_loop", "_soft_frames", "_gpu_frames", "_iter_frames", "current",
            "read", "_open", "_mark_frame", "_wait")


def tops(body):
    """每个线程块取栈顶函数名 + 线程名 + 状态。"""
    out = []
    cur = None
    for ln in body.splitlines():
        m = re.match(r'^Thread (\d+) \((\w+)\): "([^"]*)"$', ln)
        if m:
            cur = {"name": m.group(3), "state": m.group(2), "top": None}
            out.append(cur)
            continue
        if cur is not None and cur["top"] is None and ln.startswith("    "):
            cur["top"] = ln.strip()
    return out


rows = []
for i in range(1, len(blocks), 2):
    ts = blocks[i]
    th = tops(blocks[i + 1])
    rows.append((ts, th))

print()
print("=== 逐采样点：解码线程栈顶分布 ===")
print("%-10s %5s %5s  %s" % ("时刻", "线程", "解码", "栈顶分布（只列 media_manager 相关）"))
for ts, th in rows:
    c = collections.Counter()
    for t in th:
        fn = (t["top"] or "").split(" ")[0]
        if fn in INTEREST:
            c[t["top"]] += 1
    print("%-10s %5d %5d  %s" % (ts, len(th), sum(c.values()), dict(c)))

print()
print("=== 汇总：每个「函数(文件:行)」出现的线程·次数 ===")
allc = collections.Counter()
for _ts, th in rows:
    for t in th:
        fn = (t["top"] or "").split(" ")[0]
        if fn in INTEREST:
            allc[t["top"]] += 1
for k, v in allc.most_common():
    print("  %-40s %d" % (k, v))

print()
print("=== 出现过的非解码线程（确认没有卡在别处）===")
other = collections.Counter()
for _ts, th in rows:
    for t in th:
        if t["top"] not in INTEREST:
            other["%s → %s" % (t["name"], t["top"])] += 1
for k, v in other.most_common(20):
    print("  %-40s %d" % (k, v))
