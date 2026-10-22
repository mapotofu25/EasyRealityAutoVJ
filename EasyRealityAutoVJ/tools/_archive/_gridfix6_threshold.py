# -*- coding: utf-8 -*-
"""第六轮 ②：`GRID_MIN_VOTES` 阈值对照表（给用户做产品取舍用）。

口径与 tools/_gridfix5_accept.py 的 secA 完全一致（真实逐窗投票直方图 + 真实 per-song grid，
闭环重放驱动现役 `_beat_pos`）：220 例 = 20 首 × 11 个起点 [0,3,…,30]。
「误锁」= 引擎产出网格拍位且 |err|>1 拍的累计时长（秒）。

扫描 GRID_MIN_VOTES ∈ {56, 80, 100, 120(现), 140}，每档给：
  · 首次锁定时刻：中位 / p75 / p90 / max；≤5s 比例
  · 220 例：总误锁 s / 最长误锁 s / >5s 例数
  · Can We Believe That (t=0) / The Lost (t=0) 的总误锁
  · 热 cue：Raccoon Memes (t=5) / Demon's Mischief (t=8) 的总误锁 + max|err|

用法： P:\\AutoVJ\\venv\\Scripts\\python.exe tools/_gridfix6_threshold.py
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
DT = g5.DT
DATA = g5.load()
STARTS = [0, 3, 6, 9, 12, 15, 18, 21, 24, 27, 30]
THRESHOLDS = [56, 80, 100, 120, 140]


def _pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return float("nan")
    i = min(len(xs) - 1, int(round((len(xs) - 1) * q)))
    return xs[i]


def run_threshold(v):
    E.AutoVJEngine.GRID_MIN_VOTES = v
    locks = []
    tot = 0.0
    mx_bd = 0.0
    n5 = 0
    for d in DATA:
        for st in STARTS:
            errs, lt = g5.replay_now(d["wins"], d["exp"], d["bl"], d["anchor"], st)
            bd = g5.bad_dur(errs)
            max((abs(e) for _, e in errs), default=0.0)
            tot += bd
            mx_bd = max(mx_bd, bd)
            if bd > 5.0:
                n5 += 1
            locks.append(lt)
    # 重点曲目 / 热 cue（各自指定起点；不进 220 例）
    hot = {}
    for sub, st in [("Can We Believe That", 0), ("The Lost", 0),
                    ("Raccoon Memes", 5), ("Demon's Mischief", 8)]:
        d = find(sub)
        if d is None:
            continue
        errs, lt = g5.replay_now(d["wins"], d["exp"], d["bl"], d["anchor"], st)
        hot[(sub, st)] = (g5.bad_dur(errs), max((abs(e) for _, e in errs), default=0.0), lt)
    got = [x for x in locks if x is not None]
    return {
        "v": v, "tot": tot, "mx_bd": mx_bd, "n5": n5,
        "lock_med": statistics.median(got) if got else float("nan"),
        "lock_p75": _pct(got, 0.75), "lock_p90": _pct(got, 0.90),
        "lock_max": max(got) if got else float("nan"),
        "le5": sum(1 for x in got if x <= 5.0), "nlock": len(got), "n": len(locks),
        "hot": hot,
    }


def find(song_sub):
    for d in DATA:
        if song_sub.lower() in d["song"].lower():
            return d
    return None


def main():
    print("=" * 96)
    print("② GRID_MIN_VOTES 阈值对照表（口径：20 首 × 11 起点 = 220 例；"
          "误锁=|网格拍位 err|>1 拍的累计秒；GRID_STRONG_VOTES 固定 180）")
    print("=" * 96)
    res = []
    for v in THRESHOLDS:
        print("  计算 GRID_MIN_VOTES=%d ..." % v, flush=True)
        res.append(run_threshold(v))

    hdr = "%-14s" % "GRID_MIN_VOTES" + "".join("%14d" % r["v"] for r in res)
    print("\n【锁定速度 / 误锁总览】")
    print(hdr)
    print("%-14s" % "首次锁定中位s" + "".join("%14.1f" % r["lock_med"] for r in res))
    print("%-14s" % "首次锁定p75s" + "".join("%14.1f" % r["lock_p75"] for r in res))
    print("%-14s" % "首次锁定p90s" + "".join("%14.1f" % r["lock_p90"] for r in res))
    print("%-14s" % "首次锁定maxs" + "".join("%14.1f" % r["lock_max"] for r in res))
    print("%-14s" % "≤5s锁定比例" + "".join("%13.0f%%" % (100.0 * r["le5"] / r["n"]) for r in res))
    print("%-14s" % "锁定覆盖" + "".join("%13d/%d" % (r["nlock"], r["n"]) for r in res))
    print("%-14s" % "总误锁 s" + "".join("%14.0f" % r["tot"] for r in res))
    print("%-14s" % "最长误锁 s" + "".join("%14.1f" % r["mx_bd"] for r in res))
    print("%-14s" % ">5s 例数" + "".join("%14d" % r["n5"] for r in res))

    print("\n【重点曲目 / 热 cue（总误锁 s；括号内=max|err| 拍）】")
    specs = [("Can We Believe That", 0), ("The Lost", 0),
             ("Raccoon Memes", 5), ("Demon's Mischief", 8)]
    for sub, st in specs:
        line = "%-24s t=%-3d" % (sub, st)
        for r in res:
            bd, me, lt = r["hot"].get((sub, st), (float("nan"), float("nan"), None))
            line += "%12.1f(%5.1f)" % (bd, me)
        print(line)

    print("\n（取舍：“阈值越高 → 锁定越慢（首次锁定中位更大、≤5s 更少）但误锁更少”）。")
    print("  现役 = 120。")


if __name__ == "__main__":
    main()
