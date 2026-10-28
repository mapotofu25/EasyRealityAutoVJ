# -*- coding: utf-8 -*-
"""能量段落表：曲库里每首歌的「段落边界 + 目标能量 / 档位」。

为什么需要它（用户 2026-10-28 明确要求）
--------------------------------------
现在动效跟着**实测能量**走，而实测能量这条路上有两层延迟：

  · `engine._energy_tier_switch` 要求新档位**稳定 2.0 秒**才认 —— 175 BPM 下
    就是 **5.8 拍**（用户原话「中间要等 5 到 8 拍才到位」）；
  · `engine._auto_switch_tick` 又按 8 / 16 拍排期换素材。

于是「build 一路上行、到 drop 才切高动态池」这件事，进了 drop 之后还要再等 5~8 拍
才真正换池子，画面跟音乐错开一整个乐句。

段落表把「第几秒到第几秒是什么段落、该用什么能量和档位」**预先写进曲库**。
演出时指纹一认到歌 + 位置一对齐，到点就立刻执行，完全不看实测能量的脸色。

坐标约定（两个都是刻意的）
------------------------
* `t0` / `t1` **用秒**，与码表 `CueItem.at_sec` 同口径。秒是音频文件的固有时间轴，
  改 BPM 不会让段落边界漂。反例很具体：`Power Generator` 的 BPM 从 141.6 修成 147.0
  （差 3.8%），若段落存拍数，200 秒后就漂 18 拍，边界全错。
* `tier` **显式写，不从 energy 反推**。现有档位判定是滞回阈值（进 high 要 ≥0.70、
  退回 mid 要 <0.58）。用户本意是 drop 但填了 0.68，反推就会成 mid —— 正是要绕开的东西。
  `energy` 负责「视觉强度 / 过渡时长 / 候选偏好」，`tier` 负责「切哪个池」。

存储位置
--------
`cfg["music_meta"][path]["sections"]`（与 `grid` 并列，随曲库走，不新增文件格式）：

    [{"t0": 0.0,  "t1": 32.0, "energy": 0.30, "tier": "low",  "name": "intro"},
     {"t0": 32.0, "t1": 64.0, "energy": 0.55, "tier": "mid",  "name": "build"},
     {"t0": 64.0, "t1": 96.0, "energy": 0.92, "tier": "high", "name": "drop",
      "trans_ms": 0}]
"""

import math

TIERS = ("low", "mid", "high")
MIN_SEC = 1.0          # 短于 1 秒的段没有意义（也防除零/负长度）
MAX_TRANS_MS = 2000

#: 自动推档位时的阈值 —— 与 `engine._energy_tier_switch` 的**进档**阈值保持一致，
#: 这样「填 0.70 自动判 high」和「实测 0.70 进 high」不会打架。
TIER_HI = 0.70
TIER_LO = 0.45


PHRASE_BEATS = 8       # 一个「八拍」（乐句）多少拍 —— 与 waveform_mark 的八拍头同义


def phrase_snap(sec, anchor=0.0, beat_len=0.0, beats=PHRASE_BEATS):
    """把秒吸附到**最近的八拍头**：`anchor + n × beats × beat_len`。

    `beat_len <= 0`（没有网格 / BPM 未知）⇒ 原样返回，绝不凭空捏一个网格出来。
    这与 `waveform_mark.grid_times()` 用的是同一组参数，所以"编辑器里吸附到哪"
    和"波形上画的八拍线在哪"必然一致 —— 这是用户要求的关键一致性。
    """
    try:
        bl = float(beat_len)
        a = float(anchor)
        v = float(sec)
    except (TypeError, ValueError):
        return float(sec)
    if not (math.isfinite(bl) and math.isfinite(a) and math.isfinite(v)):
        return float(sec)
    if bl <= 1e-6 or beats < 1:
        return v
    step = bl * beats
    return a + round((v - a) / step) * step


def tier_from_energy(e):
    """能量 → 档位（只在用户没写 `tier` 时兜底用）。"""
    if e >= TIER_HI:
        return "high"
    if e < TIER_LO:
        return "low"
    return "mid"


def _clamp01(v):
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def normalize(raw, dur=0.0, grid=None):
    """把任意输入整理成合法段落表。**所有消费方都应该先过这个函数**。

    `grid`: 可选 `{"anchor": 秒, "beat_len": 秒}`。给了就把每个 `t0` **吸附到最近的八拍头**
    （用户 2026-10-28：段落边界必须和波形图的八拍线对齐）。不给就原样保留 —— 
    绝不猜一个网格出来（没 BPM 的歌无从吸附）。

    规则：
      * 非 dict / 坐标不是有限数的项丢掉；
      * 按 `t0` 排序；相邻两段间距 < `MIN_SEC` 的丢掉（保留先出现的）；
      * 首段 `t0` 强制 0；
      * 段与段之间**无缝且不重叠**：前一段 `t1` 一律等于后一段 `t0`（手填的 t1 只作参考）；
      * 末段 `t1` = 曲长（`dur` 有效时），否则沿用填的值、至少 `t0 + MIN_SEC`；
      * `energy` 钳到 0~1；`tier` 不在 TIERS 就从 energy 推；`trans_ms` 钳到 0~2000。

    `dur` <= 0 表示不知道曲长（老曲库没存）：此时末段的 `t1` 就用手填值。
    """
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        try:
            t0 = float(it.get("t0", 0.0))
            t1 = float(it.get("t1", 0.0))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(t0) and math.isfinite(t1)):
            continue
        if t0 < 0.0:
            t0 = 0.0
        try:
            e = float(it.get("energy", 0.5))
        except (TypeError, ValueError):
            e = 0.5
        if not math.isfinite(e):
            e = 0.5
        e = _clamp01(e)
        tier = str(it.get("tier") or "").strip().lower()
        if tier not in TIERS:
            tier = tier_from_energy(e)
        rec = {
            "t0": round(t0, 3),
            "t1": round(t1, 3),
            "energy": round(e, 3),
            "tier": tier,
            "name": str(it.get("name") or "")[:24],
        }
        tms = it.get("trans_ms", None)
        if tms is not None:
            try:
                rec["trans_ms"] = max(0, min(MAX_TRANS_MS, int(tms)))
            except (TypeError, ValueError):
                pass
        out.append(rec)

    if not out:
        return []
    # ---- 八拍吸附（给了网格才做）----
    if isinstance(grid, dict):
        try:
            a = float(grid.get("anchor", 0.0) or 0.0)
            bl = float(grid.get("beat_len", 0.0) or 0.0)
        except (TypeError, ValueError):
            a, bl = 0.0, 0.0
        if bl > 1e-6 and math.isfinite(a) and math.isfinite(bl):
            for r in out:
                if r["t0"] <= 1e-6:
                    continue          # 首段永远 0，不吸附（吸到 -0.3 秒没有意义）
                snapped = phrase_snap(r["t0"], a, bl)
                r["t0"] = round(max(0.0, snapped), 3)

    out.sort(key=lambda r: r["t0"])

    keep = []
    for r in out:
        if keep and r["t0"] < keep[-1]["t0"] + MIN_SEC:
            continue                      # 与上一段太近 ⇒ 丢（保留先出现的那个）
        keep.append(r)

    keep[0]["t0"] = 0.0
    try:
        d = float(dur)
    except (TypeError, ValueError):
        d = 0.0
    if not math.isfinite(d):
        d = 0.0
    for i, r in enumerate(keep):
        if i + 1 < len(keep):
            r["t1"] = keep[i + 1]["t0"]   # 无缝、不重叠
        else:
            need = r["t0"] + MIN_SEC
            r["t1"] = max(need, d) if d > need else max(need, r["t1"])
        r["t1"] = round(float(r["t1"]), 3)
    return keep


def section_at(secs, sec):
    """`sec`（原曲秒）落在哪一段。返回 `(索引, 段)`；空表 → `(-1, None)`。

    越界一律夹到首 / 末段 —— 指纹位置偶尔会比真实值大一点（`position()` 越过末段），
    这时应该继续用最后一段，而不是"没段落 ⇒ 掉回实测能量"（那样 drop 末尾会突然掉档）。
    """
    if not secs:
        return -1, None
    try:
        s = float(sec)
    except (TypeError, ValueError):
        return -1, None
    if not math.isfinite(s):
        return -1, None
    if s <= secs[0]["t0"]:
        return 0, secs[0]
    n = len(secs)
    if s >= secs[-1]["t1"]:
        return n - 1, secs[-1]
    lo, hi = 0, n - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        r = secs[mid]
        if s < r["t0"]:
            hi = mid - 1
        elif s >= r["t1"]:
            lo = mid + 1
        else:
            return mid, r
    i = max(0, min(n - 1, lo))
    return i, secs[i]


def energy_of(secs, sec, default=None):
    _i, r = section_at(secs, sec)
    return default if r is None else float(r["energy"])


def tier_of(secs, sec, default=None):
    _i, r = section_at(secs, sec)
    return default if r is None else str(r["tier"])


def make_default(dur, n=4, grid=None):
    """给"还没有段落表"的歌生成一份可编辑的初稿（**只是模板，不是分析结果**）。

    比例：intro 20% / build 25% / drop 45% / outro 10%。
    这是给用户一个能直接改的骨架 —— 比让他从空白开始填快得多，
    也不会像自动检测那样凭空断言"这里就是 drop"。
    """
    try:
        d = float(dur)
    except (TypeError, ValueError):
        return []
    if not math.isfinite(d) or d < 8.0:
        return []
    if n <= 1:
        return normalize([{"t0": 0.0, "t1": d, "energy": 0.5,
                           "tier": "mid", "name": "全曲"}], d, grid)
    cuts = [0.0, d * 0.20, d * 0.45, d * 0.90, d]
    names = ("intro", "build", "drop", "outro")
    energies = (0.30, 0.55, 0.88, 0.35)
    tiers = ("low", "mid", "high", "mid")
    rows = []
    for i in range(min(4, n)):
        rows.append({"t0": cuts[i], "t1": cuts[i + 1],
                     "energy": energies[i], "tier": tiers[i],
                     "name": names[i]})
    return normalize(rows, d, grid)


def describe(secs, limit=6):
    """一行摘要（日志/取证用）。"""
    if not secs:
        return "(无段落)"
    parts = []
    for r in secs[:limit]:
        parts.append("%.1f-%.1fs %s/%s(%.2f)"
                     % (r["t0"], r["t1"], r["name"] or "-", r["tier"], r["energy"]))
    if len(secs) > limit:
        parts.append("…+%d" % (len(secs) - limit))
    return " | ".join(parts)


def covers(secs, sec):
    """`sec` 是否落在某段里（用于"段落表没覆盖到这个位置"的取证）。"""
    i, r = section_at(secs, sec)
    if r is None:
        return False
    return r["t0"] <= sec < r["t1"] or (i == len(secs) - 1 and sec >= secs[-1]["t0"])
