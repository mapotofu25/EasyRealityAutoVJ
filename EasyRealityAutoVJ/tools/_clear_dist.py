# -*- coding: utf-8 -*-
"""提权清空 dist/EasyRealityAutoVJ（PyInstaller COLLECT 被安全钩子拦下时的兜底）。

用法：以管理员身份运行
    venv\\Scripts\\python tools\\_clear_dist.py
"""
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(ROOT, "dist", "EasyRealityAutoVJ")

if not os.path.isdir(TARGET):
    print("目标不存在，无需清理：%s" % TARGET)
    sys.exit(0)

n = 0
for name in os.listdir(TARGET):
    p = os.path.join(TARGET, name)
    try:
        if os.path.isdir(p):
            shutil.rmtree(p)
        else:
            os.remove(p)
        n += 1
    except Exception as e:                                     # noqa: BLE001
        print("删除失败 %s: %s" % (name, e))

print("已删除 %d 个顶层条目" % n)
print("剩余：%r" % (os.listdir(TARGET) if os.path.isdir(TARGET) else "GONE"))
