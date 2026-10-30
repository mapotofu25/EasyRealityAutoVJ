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
#: 这样「填 0.75 自动判 high」和「实测 0.75 进 high」不会打架。
TIER_HI = 0.75
TIER_LO = 0.40
#   ★ 2026-10-30（用户）：「除了高动态低动态 再把中动态也加进来 0.4到0.8」「高动态提到 0.8」
#     ⇒ 低 < 0.40 / 中 0.40~0.80 / 高 ≥ 0.80。
#   ★ 2026-10-30 晚（用户，`Rolling Contact`）：「中能量改回 0.4 0.75」
#     ⇒ 高门槛 0.80 → **0.75**（0.80 太高：很多歌"确实该算 drop"的段只到 0.76~0.79，
#     被压成中段；用户实测这首歌乐句14/24 的 0.96/0.90 才够高，0.79 那种"半炸"不算）。
#     低门槛保持 0.40。三处必须同步：这里 / `engine._energy_tier_switch`（进档 0.75，
#     出档 0.69，滞回 0.06）/ `match_engine.match_clips`。
ENERGY_GAMMA = 1.5
#   上报能量的伽马压缩（见 `auto_segments` 第 5 步）。母带压缩过的曲子中段只比 drop 低
#   1~2 dB ⇒ 线性归一化后中段读 0.83（用户：「中段的能量太高了 没这么高」）。
#   1.5 下实测：Outburst 中段 0.83→0.73 / drop 0.89→0.83；Lord Help Me 中段 0.76→0.64 /
#   drop 0.96→0.94；Extra Pain 中段 0.79→0.68。只影响显示与档位，不影响切段。


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
AUTO_JUMP_THR = 0.10      # 归一化能量（0~1）的落差阈值
#   ★ 2026-10-30 从 0.15 降到 0.10：实测 Extra Pain 在 150.57s 处（用户指定的分界）
#     前 4s 均值 0.816 → 后 4s 0.928，落差 **0.111** —— 0.15 的门槛够不着，
#     于是 97~217s 一直是同一段，"97.2~150.57 算中能量"根本无法成立。
#     降门槛的副作用由**分级判据**兜住（见 `auto_segments` 第 2 步：中/弱边界
#     必须落在 2 个八拍线上），结构规整的歌不会因此多切。
AUTO_MERGE_THR = 0.05     # 相邻段均值差小于它就合并（去碎片）
#   ★ 2026-10-30 从 0.12 降到 0.08：实测 Extra Pain 在 151.02s 的边界**落差 0.217**
#     （远高于 p80=0.134，早就是"强边界"），却因为前后两段均值 0.78 vs 0.89
#     **差 0.11 < 0.12** 被这一步合并掉了 ⇒ 97~217s 始终是一段，
#     用户要求的"97.2~150.57 算中能量"永远不可能成立。
#     0.08 仍能滤掉真正的碎片（Dust 那首相邻段差都在 0.14 以上）。
AUTO_MIN_SEC = 6.0        # 最短段长（没有网格时的兜底；有网格时改用 AUTO_PHRASE_BEATS）
# ★★ 段落的"最小单位"（2026-10-29 用户实测反馈）：
#   电子音乐**绝大多数是「2 个八拍」或「4 个八拍」一段**。旧实现只把边界吸到
#   "一个八拍"（8 拍）上，于是出现两个问题：
#     ① 一条边界吸到八拍 8、下一条吸到八拍 9 ⇒ 中间只剩 8 拍，甚至出现图里
#        那种 **0.09 秒**的碎片段（量化后两个边界落在同一条线附近）；
#     ② 边界可能落在"半个乐句"上，与编曲结构对不上（用户一眼看出不对）。
#   现在把边界量化到 2 个八拍（16 拍）的整数倍，并保证相邻边界至少隔一个量化步长
#   ⇒ 每段至少 2 个八拍。4 个八拍、8 个八拍的段落天然也是 16 的倍数，不会被改坏。
AUTO_PHRASE_BEATS = 16    # = 2 个八拍（`PHRASE_BEATS`(8) × 2）
PHRASE_ALT_MARGIN = 0.10  # 8 拍线要"赢"16 拍线多少能量落差，才改用它
PHRASE_ALT_MIN_BARS = 4   # 相邻段都 ≥ 这么多个八拍，才允许边界落在"半句"（8 拍）线上
#   ★ 2026-10-30（`Lord Help Me`「drop1 前后各少了两个八拍」）：drop 前面插了一个
#     8 拍 break ⇒ 这段的乐句相位整体偏移一个八拍 ⇒ 16 拍线在两个边界上各往里
#     拉 8 拍，4 个乐句的 drop 被压成 3 个。判据 = 两条候选线两侧各一个八拍的
#     均值差：8 拍线明显更强（≥0.10）就说明"本来就在半句线上"，不硬拉。
#   ★ 但**短段不许**走半句线（2026-10-30 `Dust` 回归）：3 个八拍的前奏边界
#     （12s）能量落差很大，若照能量判据就会留在 12s —— 用户看到的就是
#     「3 个八拍一段」（他 10-29 就为这个报过）。而长段（≥4 个八拍）看起来
#     本来就不像"数错了"，保真更重要。两边各 4 个八拍 = 32 拍，是这两个
#     现场样本的分界：Dust 左边 3 个八拍（拒），Lord Help Me 两边 13/8/17（收）。
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


def smooth_curve(f):
    """感知能量曲线 → 平滑 → 本曲归一化（`auto_segments` 用的就是它）。

    平滑顺序不能反：先中值（去单帧爆音）再均值（抹平拍内起伏）。先平均会把
    孤立爆音抹进邻域，中值就抓不出来了。

    单独抽出来是为了**离线测试和判据函数能拿到同一条曲线** —— 乐句线裁决
    （`_phrase_line`）用的就是它，测试里自己拼一条会和生产口径不一致。
    返回 `(cur, dt)`，特征缺失时 `(None, 0.0)`。
    """
    raw, dt = energy_curve(f)
    if raw is None:
        return None, 0.0
    sm = _mov_avg(_med_filt(raw, round(0.5 / dt)), round(1.0 / dt))
    return _pct_norm(sm), dt


def _grid_ctx(grid):
    """取出乐句网格参数。返回 `(anchor, beat_len, ok)`；`ok=False` 时一切按老逻辑。"""
    if not isinstance(grid, dict):
        return 0.0, 0.0, False
    try:
        a = float(grid.get("anchor", 0.0) or 0.0)
        bl = float(grid.get("beat_len", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0, 0.0, False
    if not (math.isfinite(a) and math.isfinite(bl)) or bl <= 1e-6:
        return 0.0, 0.0, False
    return a, bl, True


def _on_line(t, a, bl, beats, tol=0.35):
    """`t` 是否落在 `beats` 拍网格的某条线上（容差 `tol` 秒）。"""
    step = bl * max(1, int(beats))
    if step <= 1e-6:
        return False
    return abs(t - (a + round((t - a) / step) * step)) <= tol


def _win_mean(cur, dt, t0, t1):
    """`cur` 在 `[t0, t1)` 上的均值；窗口无效时给 0。"""
    if cur is None or dt <= 1e-9:
        return 0.0
    n = int(cur.size)
    i0 = max(0, min(n, int(round(t0 / dt))))
    i1 = max(i0, min(n, int(round(t1 / dt))))
    if i1 <= i0:
        return 0.0
    return float(cur[i0:i1].mean())


def _phrase_step_score(cur, dt, t, bl, sign, beats=PHRASE_BEATS):
    """边界时刻 `t` 两侧**各一个八拍**的均值差，按边界方向（`sign`）对齐后返回正向幅度。

    用途：判断"这条线是不是真的能量分界"。
      · 正值 ⇒ 能量确实在这里按边界的走向跳了一下；
      · 负值 ⇒ 能量走向与边界相反（比如 drop 内部某一拍），吸过去就是错的。
    拿不到曲线（`cur=None`）时返回 0 ⇒ 上层退化成纯距离判据（老行为）。
    """
    if cur is None or dt <= 1e-9 or bl <= 1e-6:
        return 0.0
    n = int(cur.size)
    B = bl * max(1, int(beats))
    i1 = int(round(t / dt))
    i0 = int(round((t - B) / dt))
    j1 = int(round((t + B) / dt))
    i0 = max(0, i0)
    i1 = max(i0, min(n, i1))
    j1 = min(n, max(i1, j1))
    if i1 - i0 < 2 or j1 - i1 < 2:
        return 0.0
    L = float(cur[i0:i1].mean())
    R = float(cur[i1:j1].mean())
    return (R - L) * (1.0 if sign >= 0 else -1.0)


def _phrase_line(t, ga, gbl, cur=None, dt=0.0, sign=1.0, beats=AUTO_PHRASE_BEATS,
                 near=float("inf")):
    """`t` 该吸到哪条乐句线：默认 **16 拍**线；能量证据强且两侧段够长时改用 **8 拍**线。

    ★★ 为什么不能只认 16 拍线（2026-10-30 现场 `Lord Help Me`）：
      drop 前面插了一个 8 拍 break ⇒ 整段 drop 的乐句相位**偏了一个八拍**
      （实测 drop 起点落在第 168 拍 = 10.5 个乐句）。只认 16 拍线就会把两条边界
      各往内拉 8 拍：66.18→69.19、90.51→87.77 ⇒ **4 个乐句的 drop 被压成 3 个**
      （用户：「drop1 前后各少了两个八拍」）。而原始检出本来是对的。
      ⇒ 两条候选线都看，用 `_phrase_step_score`（两侧各一个八拍的均值差）裁决：
        · 原始位置本来就贴着 8 拍线（≤1.5 拍）且那里的落差**不弱于** 16 拍线
          ⇒ 用 8 拍线（真的半句偏移，不硬拉）；
        · 8 拍线的落差比 16 拍线强 `PHRASE_ALT_MARGIN` 以上 ⇒ 也用 8 拍线；
        · 其余情况仍用 16 拍线（保 2/4 个八拍的段落观感，`Dust` 那种碎段照样被吸齐）。
      `cur` 缺省（拿不到能量曲线）⇒ 退化成只看 16 拍线的老行为，不会静默变味。

    :param near: 两侧**较短那段**有多少个八拍（上层算好传进来）。小于
        `PHRASE_ALT_MIN_BARS` 时**不许**走 8 拍线 —— 短段（2/3 个八拍）必须是整乐句，
        否则就是用户看出来的"三个八拍五个八拍"。缺省 `inf` = 不限制（老调用）。
    """
    step16 = gbl * max(1, int(beats))
    l16 = ga + round((t - ga) / step16) * step16
    if cur is None or gbl <= 1e-6:
        return l16
    if near < PHRASE_ALT_MIN_BARS - 1e-6:
        return l16
    # ★ 16 拍线离得够近（≤3 拍）⇒ 老实吸 16 拍线。这一步把触发面收窄到**真正的
    #   半句偏移**（原始边界贴着"奇数八拍"线、离 16 拍线 6 拍以上），否则每条
    #   边界都能找到"更近的 8 拍线"，全库会大面积改写（实测 71/90 首分段变化，
    #   而用户要的只是"别把 drop 边界往里拉一个八拍"）。
    if abs(t - l16) <= gbl * 3.0:
        return l16
    step8 = gbl * PHRASE_BEATS
    l8 = ga + round((t - ga) / step8) * step8
    if abs(l8 - l16) <= 1e-6:                  # 两条线重合 ⇒ 没什么可挑
        return l16
    s16 = _phrase_step_score(cur, dt, l16, gbl, sign)
    s8 = _phrase_step_score(cur, dt, l8, gbl, sign)
    if (abs(t - l8) <= gbl * 1.5 and s8 >= s16 - 0.02) or \
            (s8 >= s16 + PHRASE_ALT_MARGIN):
        return l8
    return l16


def _quantize_phrases(edges, grid, beats=AUTO_PHRASE_BEATS, keep=(), cur=None, dt=0.0):
    """把**最终**的段落边界吸附到「2 个八拍」的网格线上（只微调位置，绝不删边界）。

    2026-10-30 用户第二次反馈（截图：「怎么还是三个八拍五个八拍」）：
      第一版只把边界吸到**一个八拍**（8 拍），于是 `12.26s`（3.27 个八拍）这种
      边界原样留着 —— 用户看到的就是"3 个八拍一段"。而电子音乐实际上按
      **2 个八拍 / 4 个八拍**组织，所以要吸到 **16 拍**（`AUTO_PHRASE_BEATS`）的网格。

    容差 = 0.6 × 16 拍 ≈ 4.5 秒（128BPM）：
      · 距网格线更近 ⇒ 吸过去（`Dust` 实测 12.26 → 16.00，前奏变 4 个八拍）；
      · 更远 ⇒ **保持原位**（"特立独行"的编曲不被硬拉，用户 2026-10-29：
        「不要写死，万一有首歌在进 drop 前有八拍假高潮呢」）。
    吸附后跟相邻边界贴太近（< 半个八拍）时保底挪开，但**绝不删段**。

    ★★ 2026-10-30 第三次反馈（`Lord Help Me`：「drop1 前后各少了两个八拍」）：
      **16 拍网格不能是"唯一真相"**。那首歌 drop 前面插了一个 8 拍的 break
      ⇒ 整段 drop 相对全曲的乐句相位**偏移了一个八拍**（实测 drop 起点在
      第 168 拍 = 10.5 个乐句）。两个边界都被四舍五入**往里**拉了一个八拍：
        66.18 → 69.19、90.51 → 87.77 ⇒ 4 个乐句的 drop 被压成 3 个（少了 6 秒）。
      而原始检出（`dif` 局部极大）本来就是对的（66.18 / 90.51）。
      ⇒ 现在对每条边界同时看 **16 拍线**和**8 拍线**，用 `_phrase_step_score`
      （两侧各一个八拍的均值差）裁决：
        · 原始位置就落在 8 拍线上（≤1.5 拍），且那里的能量落差**不弱于** 16 拍线
          ⇒ 用 8 拍线（"这首就是半句偏移"，不硬拉）；
        · 8 拍线的落差比 16 拍线强 `PHRASE_ALT_MARGIN` 以上 ⇒ 也用 8 拍线；
        · 否则仍然用 16 拍线（保 2/4 个八拍的段落观感，`Dust` 那种碎段照样被吸齐）。
      `cur` 缺省时退化成"只看 16 拍线"的老行为 —— 单测/离线调用不会静默变味。

    `grid` 无效（没 BPM / beat_len<=0）时**原样返回** —— 绝不猜网格。
    """
    if len(edges) <= 2:
        return edges
    a, bl, ok = _grid_ctx(grid)
    if not ok:
        return edges
    half = bl * (PHRASE_BEATS / 2.0)        # 半个八拍（保底间距）
    if half <= 1e-6:
        return edges
    step2 = bl * max(1, int(beats))         # 2 个八拍
    step1 = bl * PHRASE_BEATS               # 1 个八拍
    tol = step2 * 0.6                       # ≈4.5s @128BPM：再远就说明本来不在乐句线上
    q = [float(edges[0])]
    keep_f = [float(k) for k in (keep or ())]
    raw_f = []
    for t in edges:
        try:
            raw_f.append(float(t))
        except (TypeError, ValueError):
            raw_f.append(0.0)
    for _i in range(1, len(raw_f) - 1):
        tf = raw_f[_i]
        # ★ 尖峰段（8 拍假高潮）的两条边界**不吸附** —— 吸过去会把它变成 2 个八拍，
        #   能量被邻段平均掉，用户要的"8 拍假高潮"就没了（2026-10-30 回归）。
        if any(abs(tf - k) <= 0.30 for k in keep_f):
            q.append(tf)
            continue
        e16 = max(0.0, a + round((tf - a) / step2) * step2)
        d16 = abs(e16 - tf)
        # 边界方向由**原始边界**两侧的一个八拍均值决定（不认识方向就没法判强弱）
        sign = 1.0
        if cur is not None:
            _s_lo = _win_mean(cur, dt, max(0.0, tf - step1), tf)
            _s_hi = _win_mean(cur, dt, tf, tf + step1)
            sign = 1.0 if _s_hi >= _s_lo else -1.0
        # 两侧段各有多少个八拍（左用**已定**的上一条边界，右用原始的下一条）
        _bar = max(1e-6, bl * PHRASE_BEATS)
        _near = min((tf - q[-1]) / _bar, (raw_f[_i + 1] - tf) / _bar)
        e = _phrase_line(tf, a, bl, cur, dt, sign, beats=int(beats), near=_near)
        if abs(e - tf) > tol:
            e = tf                                      # 太远 ⇒ 保持原位（不硬拉）
        if e - q[-1] < half:
            e = max(e, q[-1] + half)                # 别与上一条挤在一起（保底，不删）
        q.append(e)
    tail = float(edges[-1])
    if tail - q[-1] < half and len(q) > 1:
        q[-1] = tail - half
    q.append(tail)
    return q


def grid_from_meta(meta):
    """取**生效**网格：`grid_manual.bpm_manual`（用户手改的 BPM）优先于算法 `grid`。

    为什么必须有它（2026-10-30 现场，`Extra Pain`）：
      用户手动把 BPM 从 99.95 改成 **200**，但那只写进 `grid_manual.bpm_manual`
      —— `grid.beat_len` 仍是自动分析出的 0.600272（99.95，半速）。
      而分段算法读的是 `grid` ⇒ 把"2 个八拍"当成 **9.6 秒**（真实是 4.8 秒）
      ⇒ 所有乐句判断慢一倍 ⇒ 用户看到的就是 **"偏差了三拍"** + 整首只切出 2 段。

    `anchor`（第一拍的时刻）不受 BPM 影响 —— 重拍的位置不变，只是拍长变了。
    """
    if not isinstance(meta, dict):
        return None
    g = meta.get("grid") or {}
    gm = meta.get("grid_manual") or {}
    try:
        bl = float(g.get("beat_len") or 0.0)
    except (TypeError, ValueError):
        bl = 0.0
    try:
        a = float(g.get("anchor") or 0.0)
    except (TypeError, ValueError):
        a = 0.0
    try:
        bpm_m = float(gm.get("bpm_manual") or 0.0) if isinstance(gm, dict) else 0.0
    except (TypeError, ValueError):
        bpm_m = 0.0
    if bpm_m > 0:
        return {"anchor": a, "beat_len": round(60.0 / bpm_m, 6), "bpm": bpm_m,
                "manual": True}
    if bl > 1e-6:
        try:
            bpm_a = float(g.get("bpm") or 0.0)
        except (TypeError, ValueError):
            bpm_a = 0.0
        return {"anchor": a, "beat_len": bl, "bpm": bpm_a}
    return None


def auto_segments(f, dur=0.0, grid=None, win_sec=None, jump_thr=None,
                  merge_thr=None, min_sec=None, max_seg=None, refine_sec=0.0,
                  snap_beats=None, phrase_beats=None, phrase_grid=None):
    """从音频特征自动切出能量段落。返回 `normalize()` 之后的段落表。

    :param f:    `beatgrid.features()` 的返回值（含 Elow/Emid/Ehigh/dt）
    :param dur:  曲长（秒）。给了就能让末段收尾在曲末。
    :param grid: `{"anchor": 秒, "beat_len": 秒}` —— 给了就把边界吸附到八拍头
    :param phrase_grid: ★ 2026-10-30：**只用于"乐句判据"、不吸附位置**的网格。
        为什么需要它：曲库同时存 `sections`（吸附后）和 `sections_raw`（原始边界，
        供详情页切换"拍/小节"吸附粒度用）。但旧实现 `raw` 是拿 `grid=None` 算的
        ⇒ 连乐句判据都没有 ⇒ 段数 13，而 `sections` 只有 7 ⇒ **界面显示的和引擎实际用的
        不是同一套分段**（用户 2026-10-30：「怎么还是三个八拍五个八拍」）。
        现在 `raw` 传 `phrase_grid=grid`：乐句判据照用（段数与 `sections` 一致），
        只是**不把边界吸到网格线上**（原始位置，切粒度时才能细化）。
        不传时退化为用 `grid`（老行为）。
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

    cur, dt = smooth_curve(f)
    if cur is None:
        return normalize([{"t0": 0.0, "t1": max(1.0, float(dur or 0.0)),
                           "energy": 0.5, "tier": "low", "name": "全长"}], dur, grid,
                         beats=snap_beats)
    n = int(cur.size)
    dur_eff = float(dur) if dur and dur > 0 else n * dt

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

    # ⚠ 试过「多尺度」（再加一个 win/3 的短窗取幅度更大者）来抓 8 拍假高潮，**实测撤掉**：
    #   Dust 那首的非整八拍段从 3 个涨到 5 个（12.26/151.01/162.26 这些段内起伏被短窗放大
    #   后挤进"强档"）。主窗本来就看得见假高潮的**上下两条边界**（只是幅度被稀释一半），
    #   够用了；短窗纯噪声。要再试必须拿一首**真有 8 拍假高潮**的歌做样本。

    # ---- 2. 候选边界：局部极大 → **用这首歌自己的分布分级** ----
    # ★★ 2026-10-29 用户："电子音乐大部分是四个八拍或者两个八拍一段…**不是必须**，
    #   万一有首歌在进 drop 前有八拍假高潮呢，**是加强算法**"。
    #   ⇒ 绝不写死"段落必须是 N 个八拍"。改用**自适应分级**（阈值随歌变，不是常量）：
    #     · 强边界（落差在自身 top 35%）＝ 真 drop / **8 拍假高潮** / breakdown
    #       ⇒ **无条件保留**，不管它落在第几个八拍；
    #     · 中边界 ＝ 必须落在 2 个八拍（16 拍）线上 —— 电子音乐的常规段落起点；
    #     · 弱边界 ＝ 至少在八拍线上、且落差不能太小（否则就是段内起伏）。
    #   结构规整的歌边界会自然整齐，有奇招的歌也不会被裁掉。
    floor = max(0.03, jump_thr * 0.25)          # 只为滤掉数值噪声，不是"段落判据"
    cand = [i for i in range(1, n - 1)
            if abs(dif[i]) >= abs(dif[i - 1]) and abs(dif[i]) >= abs(dif[i + 1])
            and abs(dif[i]) >= floor]
    ga, gbl, gok = _grid_ctx(phrase_grid if phrase_grid is not None else grid)
    vals = np.array([abs(dif[i]) for i in cand], dtype=np.float64) if cand else np.zeros(0)
    if vals.size >= 5:
        q_hi = float(np.percentile(vals, 80))
        q_mid = float(np.percentile(vals, 45))
    else:
        q_hi = q_mid = float(jump_thr)
    # 绝对下限：强档必须**明显强于**普通落差，否则"分位数"会把所有边界都放进强档
    # （实测 Dust 那首：候选边界多，65 分位只有 0.10 ⇒ 门槛退化成 jump_thr=0.15，
    #   7/5 个八拍的碎段全被保留，等于没约束）。1.6× 是"明显更强"的经验倍数。
    q_hi = max(q_hi, float(jump_thr) * 1.6)
    q_mid = max(q_mid, float(jump_thr))

    # ---- 2b. 线通道：**乐句线本身就是合法段落起点** ----
    # ★★ 2026-10-30 用户（`Rolling Contact`「这首歌中段我感觉还能再分一下」）：
    #   88.38~187.47 一刀切成 99 秒的中段，而里面明明有「两乐句的大声响」：
    #     乐句14 88.38 E=0.99（↑0.39）/ 乐句24 150.31 E=0.94（↑0.43）/
    #     乐句26 162.70 回落到 0.69（dif=0.211）。
    #   为什么漏：峰通道只认「`dif` 的**局部极大**」，而这些线的峰落在离线上
    #   1.86 拍的地方（162.70 的峰在 161.98）⇒ `_on_line` 的 0.35s 容差拒掉
    #   ⇒ 高能量岛只剩左边界，右边界没了 ⇒ 与前后段均值差 0.04 < merge_thr
    #   ⇒ 整岛被 `_merge` 吃掉。
    #   判据：**每条 16 拍线**只要它自己的 |dif| ≥ q_mid（"这条线上真的有落差"）
    #   就作为候选 —— 电子音乐的段落起点本来就在乐句线上，峰落在哪里是平滑窗的
    #   副作用，不该决定"这里算不算边界"。
    #   ⚠ 必须放在分位数算完之后：分位数只能用**峰通道**统计，否则线通道密度
    #     会把门槛拉低（线点比峰点多）。
    #   ⚠ 门槛仍用 q_mid（不是 jump_thr）：q_mid 已含"这首歌自己 45 分位"的自适应，
    #     渐变段落（每乐句只差 0.02）照样进不来。
    #   ⚠ 门槛不能用 `dif`（±4 秒窗）：窗口比一个乐句还长，会把「半个乐句的平台」
    #     放大成边界（实测 218.44 处 dif=0.120 刚过线，但它两侧半句是 0.586/0.616，
    #     根本没有落差 ⇒ 切出 6.17 秒碎段，又被"最小段长"并给后面，drop 起点反而错）。
    #     改用「这条线两侧**各一个八拍**的均值差」——窗口 3.1 秒，正好一个乐句的一半，
    #     量的是"这里真的换段落了吗"。
    if gok and gbl > 1e-6:
        _step = gbl * max(1, int(AUTO_PHRASE_BEATS))
        if _step > 1e-6:
            _k = int(np.floor((0.0 - ga) / _step))
            _extra = []
            while True:
                _t = ga + _k * _step
                if _t > n * dt:
                    break
                if _t > 0.0:
                    _i = int(round(_t / dt))
                    _sc = abs(_phrase_step_score(cur, dt, _t, gbl, 1.0))
                    if 0 < _i < n - 1 and _sc >= q_mid and abs(dif[_i]) >= floor:
                        _extra.append(_i)
                _k += 1
            if _extra:
                cand = sorted(set(cand) | set(_extra))

    cand2 = []
    for i in cand:
        v = abs(dif[i])
        t = i * dt
        if v >= q_hi:
            cand2.append(i)                     # 强：无条件（假高潮就在这一档）
        elif v >= q_mid:
            if (not gok) or _on_line(t, ga, gbl, AUTO_PHRASE_BEATS):
                cand2.append(i)                 # 中：必须 2 个八拍线
        else:
            if not gok:
                if v >= jump_thr:
                    cand2.append(i)
            elif v >= jump_thr * 0.7 and _on_line(t, ga, gbl, AUTO_PHRASE_BEATS):
                cand2.append(i)                 # 弱：也要 2 个八拍线（奇数八拍不是段落起点）
    if not cand2 and cand:
        cand2 = [i for i in cand if abs(dif[i]) >= jump_thr] or list(cand)
    # 去重窗口：有网格时按「1 个八拍」—— 8 拍假高潮（3.75@128BPM）必须能独立成段；
    # 没网格时退回 8 秒（原来就是它）。
    dup_win = 8.0
    if gok:
        dup_win = max(1.5, gbl * PHRASE_BEATS * 0.8)
    bnd = []
    for i in cand2:
        if bnd and (i - bnd[-1]) * dt < dup_win:
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
    if gok:
        # ★ 有网格：最短段按「2 个八拍」算（用户 2026-10-29：常见是 2/4 个八拍一段）。
        #   1 个八拍的段只在**能量尖峰**时豁免（见下面 `_peak_like`）——
        #   那正是"进 drop 前的八拍假高潮"；而"掉下去 1 个八拍"的凹陷要合并回大段。
        minn = max(int(round(1.0 / dt)), int(round(gbl * AUTO_PHRASE_BEATS / dt)))

    def _peak_like(k):
        """bs 的第 k 段是否像「假高潮」：能量明显**高于**左右两侧。

        ⚠ 阈值 0.15 是"明显高于"的经验幅度，不是结构规则 —— 段长/倍数一概不写死，
          靠这首歌自己的能量曲线判断（用户 2026-10-29：「不要写死，万一有首歌
          在进 drop 前有八拍假高潮呢」）。
        """
        if k < 1 or k + 1 >= len(bs):
            return False
        try:
            e = float(cur[bs[k]:bs[k + 1]].mean())
            left = float(cur[bs[k - 1]:bs[k]].mean())
            right = (float(cur[bs[k + 1]:bs[k + 2]].mean())
                     if k + 2 < len(bs) else e)
        except Exception:                                      # noqa: BLE001
            return False
        return e > max(left, right) + 0.15

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
    # 最小段长：反复把最短的段并给邻居（尖峰段豁免）
    while len(bs) > 2:
        lens = [(bs[k + 1] - bs[k]) for k in range(len(bs) - 1)]
        short = [k for k in range(len(lens)) if lens[k] < minn]
        if not short:
            break
        pick = None
        for k in short:
            if not (gok and _peak_like(k)):
                pick = k
                break
        if pick is None:
            break                      # 短段全是尖峰（假高潮/短过门）⇒ 全都保留
        # ★ 凹陷型短段（能量低于左右两侧、且两侧彼此接近）⇒ **两条边界一起删**，
        #   把中间那一小段吃掉。只删一条的话会把整段 drop 切成 7/9 个八拍这种
        #   非乐句长度（实测 Dust 102.26~106.01：0.94 → 0.54 → 0.91）。
        if gok and 0 < pick < len(bs) - 2:
            try:
                _e = float(cur[bs[pick]:bs[pick + 1]].mean())
                _L = float(cur[bs[pick - 1]:bs[pick]].mean())
                _R = float(cur[bs[pick + 1]:bs[pick + 2]].mean())
            except Exception:                                  # noqa: BLE001
                _e = _L = _R = 0.0
            if _e < min(_L, _R) - 0.05 and abs(_L - _R) < merge_thr:
                del bs[pick + 1]
                del bs[pick]
                continue
            # ★★ 2026-10-30（`Outburst Oxygen`「drop1 前面多了 16 拍」）：
            #   短段**明显比自己两侧都安静**（不到较安静一侧的一半）⇒ 并给**前一段**，
            #   绝不并给后面。为什么：这种短段就是 drop 前面的 break/抽低音过门
            #   （实测 55.44~59.19 只剩 0.03，而两侧是 0.70 / 0.90）。并给后面会让 drop
            #   从"开始下沉"那一刻算起 —— 红条比真正的 drop 早 4~16 拍（用户看到的
            #   就是"前面多了 16 拍"）。并给前面之后，drop 边界落在"静音→炸"那一步，
            #   再由乐句量化吸到线上。
            #   ⚠ 判据是**相对深度**（≤较安静一侧的一半），不能只看"比两侧低 0.15"：
            #     Lord Help Me 25.9~29.9 是个 0.46 的过渡八拍（两侧 0.73/0.76，比值 0.64），
            #     按绝对值也会被判成凹陷 ⇒ 前奏被拉长一个八拍（不该动的别动）。
            if _e < 0.5 * min(_L, _R) - 0.02:
                del bs[pick]                # 只删左边那条 ⇒ 这段归上一段
                continue
        j = (pick - 1) if (pick == len(bs) - 2) else (pick + 1)
        a, b = min(pick, j), max(pick, j)
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
    if span > 0:                       # 只有真要精修时才回头取未平滑曲线（省一次）
        raw = energy_curve(f)[0]
        if raw is None:
            span = 0
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

    # ---- 4.5) 乐句量化：边界必须落在「2 个八拍」的整数倍上（见 `_quantize_phrases`）----
    pb = AUTO_PHRASE_BEATS if phrase_beats is None else int(phrase_beats)
    # ★ 尖峰段（进 drop 前的 8 拍假高潮）的两条边界**豁免吸附**：
    #   吸到 2 个八拍网格会把它变成 16 拍，能量被邻段平均掉 ⇒ 假高潮消失
    #   （2026-10-30 单测 (c) 抓到的回归）。
    _keep = []
    if gok:
        try:
            for _k in range(1, len(bs) - 1):
                if (bs[_k + 1] - bs[_k]) < minn and _peak_like(_k):
                    _keep.append(bs[_k] * dt)
                    _keep.append(bs[_k + 1] * dt)
        except Exception:                                      # noqa: BLE001
            _keep = []
    edges = _quantize_phrases(edges, grid, pb, _keep, cur=cur, dt=dt)

    # ---- 5. 段的能量与类型：段均值 → 三分类，**只有最高类算 high** ----
    # ⚠ 为什么不是二分类：段均值的分布是连续的（这首歌 9 段：
    #   0.176/0.312/0.478/0.603/0.641/0.790/0.861/0.956/0.975），k=2 会切在
    #   密度最低的 0.6~0.8 之间 ⇒ 主歌（0.641）被判成 high，而用户明确说
    #   35.7 之后是低能量段。三分类的中心落在 0.25 / 0.61 / 0.90，
    #   取最高类（0.861/0.956/0.975）当 high，主歌 0.641 自然落到 low。
    #   先验也支持这么做：一首歌里 drop 是少数、铺垫和 breakdown 是多数。
    # ★ 段的能量必须用**量化之后**的边界重算：旧代码用 `bs[k]:bs[k+1]`（量化前的
    #   帧索引），边界被吸到乐句线之后两边就对不上了（能量会算到相邻段里去）。
    bis = [0]
    for _e in edges[1:-1]:
        _i = int(round(float(_e) / dt)) if dt > 1e-9 else 0
        bis.append(max(bis[-1] + 1, min(n - 1, _i)))
    bis.append(n)
    means = [float(cur[bis[k]:bis[k + 1]].mean()) for k in range(len(bis) - 1)]

    # ★★ 2026-10-30 第三次（用户）：「除了高动态低动态 再把中动态也加进来 0.4到0.8」
    #   「高动态提到 0.8」「中段的能量太高了 没这么高」。
    #   ⇒ 档位从"本曲 k-means 三分类"改成**绝对阈值**（低 <0.40 / 中 0.40~0.80 / 高 ≥0.80），
    #     并把上报的能量做一次 `ENERGY_GAMMA` 压缩：
    #     母带压过的曲子里中段只比 drop 低 1~2 dB（实测 Outburst Oxygen：49.6 vs 50.3 dB），
    #     线性归一化后中段会读成 0.83 —— 用户一眼看出"没这么高"，而且 0.83 按 0.8 的门槛
    #     该算高动态，自相矛盾。gamma 把上半段拉开：中段 0.83→0.73、drop 0.89→0.83。
    #   ⚠ 只压**上报值**（显示 / 引擎的目标能量 / 档位），不压 `cur` —— 检测与合并用的
    #     是 `cur` 的差值，压了会连带改变切段结果（那是另一件事，不能混在一起改）。
    e_rep = [_clamp01(float(e)) ** ENERGY_GAMMA for e in means]
    tiers = [tier_from_energy(e) for e in e_rep]
    # 兜底：一首歌总要有"最炸的那一段"。整首都很平 / 母带很轻的歌，最高也到不了 0.80，
    # 若就这样判成"没有 drop"，引擎永远进不了高动态池（旧实现"最高类算 high"自带这个性质）。
    if tiers and "high" not in tiers:
        tiers[int(np.argmax(e_rep))] = "high"

    out = []
    hi_n = lo_n = mid_n = 0
    for k, e in enumerate(e_rep):
        tier = tiers[k]
        if tier == "high":
            hi_n += 1
            nm = "drop %d" % hi_n
        elif tier == "mid":
            mid_n += 1
            nm = "中段 %d" % mid_n
        else:
            lo_n += 1
            # 低段的命名按位置区分：首段=前奏、末段=尾奏、其余=过渡。
            if k == 0:
                nm = "前奏"
            elif k == len(means) - 1:
                nm = "尾奏"
            else:
                nm = "过渡 %d" % lo_n
        out.append({"t0": edges[k], "t1": edges[k + 1], "energy": e,
                    "tier": tier, "name": nm})
    # 首段强制 0、末段收尾到曲末、无缝衔接 —— 交给 normalize 统一处理
    out[0]["t0"] = 0.0
    return normalize(out, dur_eff, grid, beats=snap_beats)
