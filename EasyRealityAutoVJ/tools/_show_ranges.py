# -*- coding: utf-8 -*-
"""打印需要裁剪的区域（带行号），用于确认锚点。用法：python tools/_show_ranges.py <file> a-b c-d ..."""
import io
import sys

p = sys.argv[1]
s = io.open(p, encoding="utf-8").read().splitlines()
for spec in sys.argv[2:]:
    a, b = spec.split("-")
    a, b = int(a), int(b)
    print("===== %s  %d-%d =====" % (p, a, b))
    for i in range(max(0, a - 1), min(len(s), b)):
        print("%5d| %s" % (i + 1, s[i]))
    print()
