# -*- coding: utf-8 -*-
"""第七轮：收紧「多窗一致性」dt 上限 —— 220 例指标复测 + 敏感性对照（3.0/4.0/5.0/6.0）。

口径与 tools/_gridfix5_accept.py **完全一致**（same 220 = 20 首 × 11 起点；真实逐窗投票
直方图 + 真实 per-song grid，同步闭环重放，驱动现役 src/engine.py 的 `_beat_pos`）。

本脚本**仅**复用 _gridfix5_accept 的重放基础设施，把 `_gridfix5_accept.make_now` 换成
「可注入 GRID_CONSIST_MAX_DT」的版本（引擎用 getattr(self,'GRID_CONSIST_MAX_DT',4.0) 读取，
故给 dummy self 设该属性即可扫不同上限）。**不动任何被复用的口径/公式。**

指标（与第五轮一致）：
  · 总误锁 s = Σ 各例「网格拍位 |err|>1 拍」的时长；最长误锁 s = 单例最大 bad_dur。
  · 首次锁定时刻 中位/最大；锁定覆盖 = 有网格模式的例数 / 总例数。

用法： P:\\AutoVJ\\venv\\Scripts\\python.exe tools/_gridfix7_sens.py
"""
import importlib.util
import os
import statistics
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

# 复用第五轮口径（导入即执行模块级 E.time=FT 注入，二者共享同一 engine 模块）
_spec = importlib.util.spec_from_file_location(
    "g5", os.path.join(ROOT, "tools", "_gridfix5_accept.py"))
G5 = importlib.util.module_from_spec(_spec)
sys.modules["g5"] = G5
_spec.loader.exec_module(G5)

DT = G5.DT
_CAP = [4.0]          # 当前注入的 dt 上限


def make_now_cap(exp, bl, anchor):
    """与 G5.make_now 逐字相同，仅额外注入 GRID_CONSIST_MAX_DT=_CAP[0]。"""
    s = G5._base_self()
    s.GRID_CONSIST_MAX_DT = _CAP[0]
    s._song_grid_lookup = {exp: {"beat_len": bl, "anchor": anchor, "bpm": 120.0, "src": "vdj"}}
    for nm in ("_beat_pos", "_beat_pos_grid", "_grid_observe", "_grid_state"):
        setattr(s, nm, types.MethodType(getattr(G5.E.AutoVJEngine, nm), s))
    return s


# 让 G5.replay_now 使用可注入 dt 的 make_now
G5.make_now = make_now_cap


def scan220(data, starts):
    """跑一遍 220 例，返回 (总误锁s, 最长误锁s, >5s例数, 锁定中位s, 锁定最大s, 覆盖n, 总n)。"""
    tot = 0.0
    mx = 0.0
    n5 = 0
    locks = []
    for d in data:
        for st in starts:
            errs, lt = G5.replay_now(d["wins"], d["exp"], d["bl"], d["anchor"], st)
            bd = G5.bad_dur(errs)
            tot += bd
            mx = max(mx, bd)
            if bd > 5.0:
                n5 += 1
            locks.append(lt)
    got = [x for x in locks if x is not None]
    return (tot, mx, n5, statistics.median(got) if got else float("nan"),
            max(got) if got else float("nan"), len(got), len(locks))


def t0_bad_dur(data, key_substr):
    """某首歌 t=0 起播的 bad_dur（CWB / Lost 用）。返回 (song, bad_dur) 或 None。"""
    for d in data:
        if key_substr.lower() in d["song"].lower():
            errs, _ = G5.replay_now(d["wins"], d["exp"], d["bl"], d["anchor"], 0.0)
            return d["song"], G5.bad_dur(errs)
    return None


def main():
    data = G5.load()
    starts = [0, 3, 6, 9, 12, 15, 18, 21, 24, 27, 30]
    print("载入 %d 首；起点 %s（共 %d 例）" % (len(data), starts, len(data) * len(starts)))

    caps = [3.0, 4.0, 5.0, 6.0]
    print("\n" + "=" * 92)
    print("敏感性对照：GRID_CONSIST_MAX_DT ∈ {3.0, 4.0, 5.0, 6.0}（220 例，逐值）")
    print("=" * 92)
    print("  %-8s %10s %12s %8s %14s %10s %10s" % (
        "dt上限", "总误锁s", "最长误锁s", ">5s", "锁定中位s", "锁定最大s", "覆盖"))
    results = {}
    for cap in caps:
        _CAP[0] = cap
        tot, mx, n5, med, mxlock, cov, nall = scan220(data, starts)
        results[cap] = (tot, mx, n5, med, mxlock, cov, nall)
        print("  %-8.1f %10.0f %12.1f %8d %14.1f %10.1f %10s" % (
            cap, tot, mx, n5, med, mxlock, "%d/%d" % (cov, nall)))

    print("\n  —— CWB(Can We Believe That) / Lost(The Lost) t=0 误锁 s ——")
    print("  %-8s %16s %16s" % ("dt上限", "CWB t=0 s", "Lost t=0 s"))
    for cap in caps:
        _CAP[0] = cap
        cwb = t0_bad_dur(data, "Believe")
        lost = t0_bad_dur(data, "Lost")
        print("  %-8.1f %16s %16s" % (
            cap,
            ("%.1f" % cwb[1]) if cwb else "n/a",
            ("%.1f" % lost[1]) if lost else "n/a"))

    # 结论辅助
    print("\n" + "=" * 92)
    print("对照结论")
    print("=" * 92)
    base = results.get(4.0)
    for cap in caps:
        r = results[cap]
        tag = ""
        if base is not None:
            if (r[0], r[1], r[2]) == (base[0], base[1], base[2]):
                tag = "  ← 与 4.0 完全同值（同平台）"
        print("  dt=%.1f：总误锁=%.0fs 最长=%.1fs >5s=%d 覆盖=%d/%d%s"
              % (cap, r[0], r[1], r[2], r[5], r[6], tag))
    print("  说明：若 3.0 与 4.0 同值、5.0/6.0 明显更差 ⇒ 3~4 是**平台**，4.0 可放心用。")


if __name__ == "__main__":
    main()
