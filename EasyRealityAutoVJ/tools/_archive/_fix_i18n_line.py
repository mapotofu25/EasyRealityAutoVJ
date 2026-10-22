# -*- coding: utf-8 -*-
"""修 i18n_map 里被 shell 吃掉转义的那一行（用单引号避免嵌套双引号）。"""
import io
import os

P = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "i18n_map.py")
ls = io.open(P, encoding="utf-8").read().split("\n")
fixed = 0
for i, l in enumerate(ls):
    if l.strip().startswith('"已把 cue 从'):
        ls[i] = error = None
        ls[i] = '    "已把 cue 从「{}」移到「{}」的 {}": "Cue moved to a different layer at {}",'
        fixed += 1
io.open(P, "w", encoding="utf-8", newline="").write("\n".join(ls))
print("已修 %d 行" % fixed)
