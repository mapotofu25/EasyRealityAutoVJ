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

import numpy as np

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


def normalize(raw, dur=0.0, grid=None, beats=None):
    """把任意输入整理成合法段落表。**所有消费方都应该先过这个函数**。

    `grid`: 可选 `{"anchor": 秒, "beat_len": 秒}`。给了就把每个 `t0` **吸附到最近的栅格线**
    （用户 2026-10-28：段落边界必须和波形图的八拍线对齐）。不给就原样保留 ——
    绝不猜一个网格出来（没 BPM 的歌无从吸附）。
    `beats`: 吸附粒度（拍数）。8 = 八拍乐句（默认），4 = 小节。
    ★ 2026-10-29 用户要求：八拍 / 小节**二选一**，由界面上的单选按钮决定。

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
                snapped = phrase_snap(r["t0"], a, bl,
                                      PHRASE_BEATS if beats is None else int(beats))
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


# ==========================================================================
# 自动分段（扫描曲库时调用）
# ==========================================================================
# 设计要点（2026-10-29，用 INTERNET YAMERO 实测定标）：
#
#   ① **判据是"局部落差"，不是"绝对高低"**。
#      用固定阈值切「能量 > 0.62 算高」在这首歌上直接失效：短 drop（0.956）
#      和主歌（0.641）都高于它，整首被切成 2 段。而 drop 的本质是**能量跳变** ——
#      前 4 秒 vs 后 4 秒的均值差。实测四个边界全部命中：
#        参考 25.3 → 检出 25.2（+0.42）   参考 35.7 → 检出 34.5（-0.27）
#        参考 82.4 → 检出 82.1（+0.51）   参考 144.7 → 检出 144.5（-0.52）
#
#   ② **按本曲自己的分位数归一化**，绝不用绝对 dB 阈值。
#      同一批曲目的母带响度能差 12 dB，全局阈值会让一半曲子整首判成同一档。
#
#   ③ **段的"高/低"用段均值的二类聚类定**，不用 `tier_from_energy`。
#      三档（low/mid/high）在 VJ 场景下没有意义 —— 素材池只有"炸"和"收"两种。
#      二类聚类让每首曲子自己决定分界线（实测 drop=0.90~0.98 / 主歌=0.53~0.64）。
AUTO_WIN_SEC = 4.0        # 落差检测的滑动窗（秒）。必须 < 最短 drop 的一半
AUTO_JUMP_THR = 0.15      # 归一化能量（0~1）的落差阈值
AUTO_MERGE_THR = 0.12     # 相邻段均值差小于它就合并（去碎片）
AUTO_MIN_SEC = 6.0        # 最短段长
AUTO_MAX_SEG = 24         # 段数上限（超了合并最短的段）
AUTO_PCT_LO, AUTO_PCT_HI = 5.0, 95.0   # 归一化用的分位数


def _med_filt(x, k):
    k = max(3, int(k) | 1)
    if x.size < k:
        return x
    pad = k // 2
    y = np.pad(x, pad, mode="edge")
    return np.median(np.lib.stride_tricks.sliding_window_view(y, k), axis=1)


def _mov_avg(x, k):
    k = max(1, int(k))
    if k <= 1 or x.size < k:
        return x
    return np.convolve(x, np.ones(k) / float(k), mode="same")


def _pct_norm(x, lo=None, hi=None):
    lo = AUTO_PCT_LO if lo is None else float(lo)
    hi = AUTO_PCT_HI if hi is None else float(hi)
    a, b = float(np.percentile(x, lo)), float(np.percentile(x, hi))
    if not (b > a + 1e-9):
        return np.zeros_like(x)
    return np.clip((x - a) / (b - a), 0.0, 1.0)


def _kmeans(v, k):
    """一维 k-means（初始中心取分位数）。返回**升序**的中心数组。

    段数很少（<25 个），用不着花哨的初始化；分位数起点比 min/max 稳 ——
    后者遇到一个离群段（比如 0.01 的静音尾巴）就会把中心全带偏。
    """
    v = np.asarray(v, dtype=np.float64)
    k = max(1, min(int(k), v.size))
    if v.size == 0:
        return np.array([], dtype=np.float64)
    if k == 1:
        return np.array([float(v.mean())], dtype=np.float64)
    qs = np.linspace(0, 100, k + 2)[1:-1]
    c = np.sort(np.percentile(v, qs)).astype(np.float64)
    for _ in range(60):
        lab = np.argmin(np.abs(v[:, None] - c[None, :]), axis=1)
        nc = np.array([v[lab == j].mean() if bool((lab == j).any()) else c[j]
                       for j in range(k)], dtype=np.float64)
        nc = np.sort(nc)
        if np.allclose(nc, c, atol=1e-6):
            break
        c = nc
    return c


def energy_curve(f):
    """由 beatgrid 的特征字典算感知能量曲线。返回 `(curve_db, dt)`，失败给 `(None, 0.0)`。

    特征 = 三个频带功率之和 → 10·log10（dB）。
    ⚠ 用 dB 不用线性：drop 与 breakdown 的功率差常在 3~10 倍，
      线性轴上同一套阈值既管不了安静段落、也管不了爆炸段落；
      转 dB 后「同样的听感差」对应同样的数值差。
    """
    try:
        el = np.asarray(f["Elow"], dtype=np.float64)
        em = np.asarray(f["Emid"], dtype=np.float64)
        eh = np.asarray(f["Ehigh"], dtype=np.float64)
        dt = float(f["dt"])
    except (KeyError, TypeError, ValueError):
        return None, 0.0
    if el.size < 16 or dt <= 0.0:
        return None, 0.0
    p = el + em + eh
    return 10.0 * np.log10(p + 1e-9), dt


def auto_segments(f, dur=0.0, grid=None, win_sec=None, jump_thr=None,
                  merge_thr=None, min_sec=None, max_seg=None, refine_sec=0.0,
                  snap_beats=None):
    """从音频特征自动切出能量段落。返回 `normalize()` 之后的段落表。

    :param f:    `beatgrid.features()` 的返回值（含 Elow/Emid/Ehigh/dt）
    :param dur:  曲长（秒）。给了就能让末段收尾在曲末。
    :param grid: `{"anchor": 秒, "beat_len": 秒}` —— 给了就把边界吸附到八拍头
    :param 其余: 覆盖模块级默认值（见 `AUTO_*`）

    返回每项：`{"t0","t1","energy","tier","name"}`，`tier` 只取 `"high"/"low"`。
    无法分段（特征缺失 / 曲太短 / 能量太平）时返回**单段**，不是空表 ——
    引擎那边"空表"意味着走回逐拍渐变，而我们要的是"这首没有段落可言，老老实实渐变"。
    """
    win_sec = AUTO_WIN_SEC if win_sec is None else float(win_sec)
    jump_thr = AUTO_JUMP_THR if jump_thr is None else float(jump_thr)
    merge_thr = AUTO_MERGE_THR if merge_thr is None else float(merge_thr)
    min_sec = AUTO_MIN_SEC if min_sec is None else float(min_sec)
    max_seg = AUTO_MAX_SEG if max_seg is None else int(max_seg)

    raw, dt = energy_curve(f)
    if raw is None:
        return normalize([{"t0": 0.0, "t1": max(1.0, float(dur or 0.0)),
                           "energy": 0.5, "tier": "low", "name": "全长"}], dur, grid,
                         beats=snap_beats)
    n = int(raw.size)
    dur_eff = float(dur) if dur and dur > 0 else n * dt

    # 平滑：先中值（去单帧爆音）再均值（抹平拍内起伏）。顺序不能反 ——
    # 先平均会把孤立爆音抹进邻域，中值就抓不出来了。
    sm = _mov_avg(_med_filt(raw, round(0.5 / dt)), round(1.0 / dt))
    cur = _pct_norm(sm)

    # ---- 1. 局部落差：前 w 秒均值 - 后 w 秒均值 ----
    w = max(2, int(round(win_sec / dt)))
    cs = np.concatenate([[0.0], np.cumsum(cur)])

    def _wm(a, b):
        a = 0 if a < 0 else int(a)
        b = n if b > n else int(b)
        if b <= a:
            return 0.0
        return float((cs[b] - cs[a]) / (b - a))

    dif = np.array([_wm(i, i + w) - _wm(i - w, i) for i in range(n)], dtype=np.float64)

    # ---- 2. 局部极大 + 幅度阈值 + 8 秒去重 ----
    cand = [i for i in range(1, n - 1)
            if abs(dif[i]) >= abs(dif[i - 1]) and abs(dif[i]) >= abs(dif[i + 1])
            and abs(dif[i]) >= jump_thr]
    bnd = []
    for i in cand:
        if bnd and (i - bnd[-1]) * dt < 8.0:
            if abs(dif[i]) > abs(dif[bnd[-1]]):
                bnd[-1] = i
        else:
            bnd.append(i)
    if not bnd:
        e = float(cur.mean())
        return normalize([{"t0": 0.0, "t1": dur_eff, "energy": e,
                           "tier": "high" if e >= 0.5 else "low", "name": "全长"}],
                         dur_eff, grid, beats=snap_beats)

    # ---- 3. 合并：相邻两段均值差都小于 merge_thr 的去掉那条边界 ----
    # 判据是「去掉这条边界后，两边能不能看成同一段」——
    # 所以要同时看它和左右两段的关系，只看一侧会把渐变段落越并越大。
    bs = [0] + list(bnd) + [n]
    minn = max(1, int(round(min_sec / dt)))
    changed = True
    while changed and len(bs) > 2:
        changed = False
        for k in range(1, len(bs) - 1):
            left = _wm(bs[k - 1], bs[k])
            right = _wm(bs[k], bs[k + 1])
            if abs(right - left) < merge_thr:
                del bs[k]
                changed = True
                break
    # 最小段长：反复把最短的段并给邻居
    while len(bs) > 2:
        lens = [(bs[k + 1] - bs[k]) for k in range(len(bs) - 1)]
        k = int(np.argmin(lens))
        if lens[k] >= minn:
            break
        j = (k - 1) if (k == len(bs) - 2) else (k + 1)
        a, b = min(k, j), max(k, j)
        del bs[b]
    # 段数封顶
    while len(bs) - 1 > max_seg:
        lens = [(bs[k + 1] - bs[k]) for k in range(len(bs) - 1)]
        k = int(np.argmin(lens))
        j = (k - 1) if (k == len(bs) - 2) else (k + 1)
        a, b = min(k, j), max(k, j)
        del bs[b]

    # ---- 4. 边界精修：在原始曲线上微调，**范围只给 ±1.2 秒** ----
    # ⚠ 一开始给的是 ±4 秒，实测把 35.7 的淡出边界拉到了 31.3（那 4 秒里有
    #   另一个更陡的鼓点下降）—— 平滑边界本来就只偏 0.1~1.2 秒，精修只该做微调，
    #   范围给大了等于让"最陡点"跑到别的段落去。
    # ⚠ 默认**关掉**（`refine_sec=0`）：实测精修把 25.2→24.3、34.5→33.8，
    #   反而更远离参考边界 25.3 / 35.7 —— 因为"最陡的上升"常常是 drop 之前的
    #   那个军鼓填充，而不是 drop 本身。平滑边界（4 秒窗）本来就只有 0.1~1.2 秒误差。
    span = max(2, int(round(float(refine_sec) / dt))) if refine_sec else 0
    edges = [0.0]
    for k in range(1, len(bs) - 1):
        c = bs[k]
        lo, hi = max(1, c - span), min(n - 1, c + span)
        nt = c
        if span > 0 and hi > lo:
            # 方向由**平滑曲线的前后均值**决定（比看单点 dif 稳）
            rising = _wm(c, min(n, c + w)) >= _wm(max(0, c - w), c)
            d1 = np.diff(raw[lo - 1:hi + 1])
            idx = int(np.argmax(d1) if rising else np.argmin(d1))
            nt = lo + idx
        edges.append(nt * dt)
    edges.append(dur_eff)

    # ---- 5. 段的能量与类型：段均值 → 三分类，**只有最高类算 high** ----
    # ⚠ 为什么不是二分类：段均值的分布是连续的（这首歌 9 段：
    #   0.176/0.312/0.478/0.603/0.641/0.790/0.861/0.956/0.975），k=2 会切在
    #   密度最低的 0.6~0.8 之间 ⇒ 主歌（0.641）被判成 high，而用户明确说
    #   35.7 之后是低能量段。三分类的中心落在 0.25 / 0.61 / 0.90，
    #   取最高类（0.861/0.956/0.975）当 high，主歌 0.641 自然落到 low。
    #   先验也支持这么做：一首歌里 drop 是少数、铺垫和 breakdown 是多数。
    means = [float(cur[bs[k]:bs[k + 1]].mean()) for k in range(len(bs) - 1)]
    c3 = _kmeans(means, 3)
    flat = (c3[-1] - c3[0]) < 0.10        # 三段中心都挤在一起 ⇒ 这首没层次
    thr = (c3[-2] + c3[-1]) / 2.0 if len(c3) >= 3 else 0.5
    # 最高类占比过大（>65%）说明整首都很炸 ⇒ 不该有"低能量段"，全部 high
    hi_share = float(np.mean([1.0 if e >= thr else 0.0 for e in means])) if means else 0.0
    all_hi = hi_share > 0.65

    out = []
    hi_n = lo_n = mid_n = 0
    for k, e in enumerate(means):
        if flat:
            tier = "low"
        elif all_hi:
            tier = "high"
        else:
            tier = "high" if e >= thr else "low"
        if tier == "high":
            hi_n += 1
            nm = "drop %d" % hi_n
        else:
            lo_n += 1
            # 低段的命名按位置区分：首段=前奏、末段=尾奏、其余=过渡。
            # 只写"低能量 3"用户看不出它在曲子里的角色（实测给用户看时他要问）。
            if k == 0:
                nm = "前奏"
            elif k == len(means) - 1:
                nm = "尾奏"
            else:
                mid_n += 1
                nm = "过渡 %d" % mid_n
        out.append({"t0": edges[k], "t1": edges[k + 1], "energy": e,
                    "tier": tier, "name": nm})
    # 首段强制 0、末段收尾到曲末、无缝衔接 —— 交给 normalize 统一处理
    out[0]["t0"] = 0.0
    return normalize(out, dur_eff, grid, beats=snap_beats)
