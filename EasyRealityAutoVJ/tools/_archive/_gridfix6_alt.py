# -*- coding: utf-8 -*-
"""③ 附带探查（**超出本轮规定范围，仅供参考**）：锁定判据里「多窗一致性」的 dt 上限。

现状：未锁定时，暂存锚只有在「本窗与上个可信窗一致」时才锁定；`0.05 < dt <= 6.0` 才算
「够近」。但**不一致的窗不会刷新暂存锚** ⇒ 暂存锚可能停留很久；Can We Believe(t=0) 恰好
在 t=0 暂存、t=6.0（dt=6.0，正好卡在上限）才因一个残差窗「一致」而锁定，锁到偏 -1.82s
的坐标，造成 53s 长误锁。这里扫描把 dt 上限收紧的效果（**不改 src**，运行时替换
`_grid_observe`），判断它是否是比「hint/全局背离」更靠谱的方向。

用法： P:\\AutoVJ\\venv\\Scripts\\python.exe tools/_gridfix6_alt.py
"""
import importlib.util
import os
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import engine as E  # noqa: E402


def _load(p, name):
    spec = importlib.util.spec_from_file_location(name, p)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


g5 = _load(os.path.join(ROOT, "tools", "_gridfix5_accept.py"), "g5")
DATA = g5.load()
STARTS = [0, 3, 6, 9, 12, 15, 18, 21, 24, 27, 30]
SRC = open(os.path.join(ROOT, "src", "engine.py"), encoding="utf-8").read()


def _extract(src, name):
    i = src.index("    def %s(" % name)
    j = src.find("\n    def ", i + 1)
    seg = src[i:j] if j != -1 else src[i:]
    lines = seg.rstrip().splitlines()
    return "\n".join(l[4:] if l.startswith("    ") else l for l in lines) + "\n"


def build_observe(dt_cap):
    body = _extract(SRC, "_grid_observe")
    b0 = body
    body = body.replace("if have and 0.05 < dt <= 6.0:", "if have and 0.05 < dt <= %.3f:" % dt_cap)
    body = body.replace("elif (not have) or dt > 6.0:", "elif (not have) or dt > %.3f:" % dt_cap)
    assert body != b0 or abs(dt_cap - 6.0) < 1e-9, "dt 替换未命中"
    ns = {"math": __import__("math")}
    exec(body, ns)
    return ns["_grid_observe"]


def run(dt_cap):
    E.AutoVJEngine._grid_observe = build_observe(dt_cap)
    tot = 0.0
    mx = 0.0
    n5 = 0
    locks = []
    specials = {}
    for d in DATA:
        for st in STARTS:
            errs, lt = g5.replay_now(d["wins"], d["exp"], d["bl"], d["anchor"], st)
            bd = g5.bad_dur(errs)
            tot += bd
            mx = max(mx, bd)
            if bd > 5.0:
                n5 += 1
            locks.append(lt)
    for sub, st in [("Can We Believe That", 0), ("The Lost", 0),
                    ("Raccoon Memes", 5), ("Demon's Mischief", 8)]:
        d = next((x for x in DATA if sub.lower() in x["song"].lower()), None)
        if d is None:
            continue
        errs, lt = g5.replay_now(d["wins"], d["exp"], d["bl"], d["anchor"], st)
        specials[(sub, st)] = (g5.bad_dur(errs), max((abs(e) for _, e in errs), default=0.0),
                               max((abs(e) for _, e in errs), default=0.0))
    got = [x for x in locks if x is not None]
    return (tot, mx, n5, statistics.median(got) if got else float("nan"),
            len(got), len(locks), specials)


def main():
    print("=" * 96)
    print("③附带探查：多窗一致性 dt 上限（默认 6.0s）对 220 例误锁的影响（未改 src 的运行时替换）")
    print("=" * 96)
    print("  %-8s %9s %10s %7s %11s %12s | %s" %
          ("dt上限", "总误锁s", "最长误锁s", ">5s", "锁定中位s", "锁定覆盖",
           "CanWeBelieve/Lost/Raccoon/Demon"))
    for cap in (6.0, 4.0, 3.0, 2.5, 2.0, 1.5, 1.0):
        tot, mx, n5, med, nlock, ncase, sp = run(cap)
        seg = " ".join("%s:%.0f" % (k[0][:6] + "@%d" % k[1], v[0])
                       for k, v in sp.items())
        print("  %-8.1f %9.0f %10.1f %7d %11.1f %9d/%d | %s"
              % (cap, tot, mx, n5, med, nlock, ncase, seg))
    # 还原
    E.AutoVJEngine._grid_observe = build_observe(6.0)


if __name__ == "__main__":
    main()
