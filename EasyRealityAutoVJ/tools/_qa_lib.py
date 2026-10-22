import sys, os
sys.path.insert(0, "src")
import config
c = config.Config()
lib = c["library"]
print("library total =", len(lib))
exist = [p for p in lib if os.path.exists(p)]
missing = [p for p in lib if not os.path.exists(p)]
print("存在 =", len(exist), " 缺失 =", len(missing))
exts = {}
for p in lib:
    e = os.path.splitext(p)[1].lower()
    exts[e] = exts.get(e, 0) + 1
print("扩展名分布:", dict(sorted(exts.items(), key=lambda x:-x[1])))
if missing:
    print("缺失样例(前5):")
    for p in missing[:5]:
        print("   ", p)
