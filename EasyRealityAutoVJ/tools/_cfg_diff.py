# -*- coding: utf-8 -*-
"""对比"以前"和"现在"的配置，找出 CPU 从 <40% 涨到 70% 的变化点。

手上有 %LOCALAPPDATA%\\AutoVJ\\config.json.bak_20260928_005849（9-28 的备份）——
拿它当"以前"的基准，逐段对比：perf / 图层数与类型 / 素材库规模 / 自动匹配相关设置 /
输出分辨率 / 那 40 个硬解不了的素材是什么时候进库的。
"""
import io
import json
import os
import time

D = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ")
CUR = os.path.join(D, "config.json")
OLD = os.path.join(D, "config.json.bak_20260928_005849")


def load(p):
    try:
        return json.load(io.open(p, encoding="utf-8"))
    except Exception as e:                                          # noqa: BLE001
        print("读不到", p, e)
        return {}


def ts(p):
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(p)))
    except Exception:                                               # noqa: BLE001
        return "?"


a, b = load(OLD), load(CUR)
print("以前 = %s (%s)" % (os.path.basename(OLD), ts(OLD)))
print("现在 = config.json (%s)" % ts(CUR))

print("\n=== 1) perf ===")
print("  以前:", json.dumps(a.get("perf"), ensure_ascii=False))
print("  现在:", json.dumps(b.get("perf"), ensure_ascii=False))

print("\n=== 2) 图层 ===")


def lay_sum(cfg):
    out = []
    for l in (cfg.get("layers") or []):
        out.append("%s(auto=%s,kv=%s,cue=%s,clips=%d,auto_clips=%d)" % (
            l.get("name"), l.get("auto_mode"), l.get("is_kv"),
            bool(l.get("is_cuesheet")), len(l.get("clips") or []),
            len(l.get("auto_clips") or [])))
    return out


for x in lay_sum(a):
    print("  以前:", x)
for x in lay_sum(b):
    print("  现在:", x)

print("\n=== 3) 素材库 / 音乐库规模 ===")
print("  素材库  以前 %d → 现在 %d" % (len(a.get("library") or []), len(b.get("library") or [])))
print("  音乐库  以前 %d → 现在 %d" % (len(a.get("music_library") or []),
                                      len(b.get("music_library") or [])))

print("\n=== 4) 自动/节奏相关 ===")
for k in ("auto", "mode", "beat", "postfx", "color"):
    print("  [%s]\n     以前: %s\n     现在: %s"
          % (k, json.dumps(a.get(k), ensure_ascii=False)[:220],
             json.dumps(b.get(k), ensure_ascii=False)[:220]))

print("\n=== 5) 输出分辨率 ===")
print("  以前:", json.dumps({k: v for k, v in (a.get("output") or {}).items()
                             if k in ("width", "height", "spout_enabled", "ndi_enabled")},
                            ensure_ascii=False))
print("  现在:", json.dumps({k: v for k, v in (b.get("output") or {}).items()
                             if k in ("width", "height", "spout_enabled", "ndi_enabled")},
                            ensure_ascii=False))

print("\n=== 6) 素材库成员差异（以前没有、现在有的）===")
sa, sb = set(a.get("library") or []), set(b.get("library") or [])
added = [p for p in sb - sa]
print("  新增 %d 个；其中最近 3 天改动的：" % len(added))
for p in added[:30]:
    try:
        m = os.path.getmtime(p)
        if time.time() - m < 3 * 86400:
            print("     %s  %s" % (time.strftime("%m-%d %H:%M", time.localtime(m)),
                                   os.path.basename(p)[:44]))
    except Exception:                                               # noqa: BLE001
        pass
