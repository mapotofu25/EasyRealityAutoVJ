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
import os

import numpy as np

TIERS = ("low", "mid", "high")
MIN_SEC = 1.0          # 短于 1 秒的段没有意义（也防除零/负长度）
MAX_TRANS_MS = 2000

#: 自动推档位时的阈值 —— 与 `engine._energy_tier_switch` 的**进档**阈值保持一致，
#: 这样「填 0.75 自动判 high」和「实测 0.75 进 high」不会打架。
TIER_HI = 0.72
TIER_LO = 0.35
#   ★ 2026-10-30（用户）：「除了高动态低动态 再把中动态也加进来 0.4到0.8」「高动态提到 0.8」
#     ⇒ 低 < 0.40 / 中 0.40~0.80 / 高 ≥ 0.80。
#   ★ 2026-10-30 晚（用户，`Rolling Contact`）：「中能量改回 0.4 0.75」
#     ⇒ 高门槛 0.80 → **0.75**（0.80 太高：很多歌"确实该算 drop"的段只到 0.76~0.79，
#     被压成中段；用户实测这首歌乐句14/24 的 0.96/0.90 才够高，0.79 那种"半炸"不算）。
#     低门槛保持 0.40。三处必须同步：这里 / `engine._energy_tier_switch`（进档 0.75，
#     出档 0.69，滞回 0.06）/ `match_engine.match_clips`。
ENERGY_GAMMA = 2.0
#: 能量按 dB 标定：比"本曲最响段"低这么多 dB 就算 0（见 `auto_segments` 里 `means` 的注释）
ENERGY_DB_SPAN = 0.0

# ★★★ 2026-11-06（用户：「Drop 1 怎么能有 0.8 呢？它听起来非常缓和」「整体能量都偏高了」
#   「加一个看看」）：**密度因子**。
#   病根：上报能量只看总电平（`cur` 的段均值），而"稀疏的 drop"和"满编排的 drop"
#   总电平只差 3 dB 是常事 —— `Erebos` drop1 比 drop4 只低 3.3 dB，读 0.75 判 high，
#   而用户听的是"缓"。真正差的是**频段空洞**：drop1 的 40~80Hz 掉到 −25.8 dB、
#   5~11kHz 掉到 −12.9 dB（相对本曲各频段 95 分位），最弱三段平均 **−15.8 dB**，
#   而满编排的 drop 都在 **−9 dB** 上下。
#   ⇒ 每段再算一个「频段均衡度」`spread`（8 个频段相对本曲该频段 95 分位的 dB 的**标准差**），
#     超出 `DENSITY_REF_STD` 的部分按 `DENSITY_SPAN_STD` 折算成折扣，最多打 `DENSITY_W`。
#     「均衡度」而不是「空洞深度」：先量过 `hole = −mean(最弱 3 段)`，它对**本来低频就少**
#     的曲子误判太狠（`Losin' A Control` 全曲低频偏薄 ⇒ 每段都扣，整首中位 0.72→0.42、
#     3 个 drop 被降级）。`spread` 只看"这一段自己的频段是否均衡"，同曲内的相对差异才扣。
#   实测效果（18 首 279 段）：中位能量 0.60 → 0.58，`Erebos` drop1 0.75 → **0.62**、
#     drop7 0.78 → 0.68（都降到 mid，和听感一致），而 spread ≤4 dB 的满编排 drop **完全不动**。
#   ⚠ 它乘在 `**ENERGY_GAMMA` 之后，是第二层连乘 —— 两层可以接受，
#     但**不要再往上叠第三层**（见「乘出来的量禁止三层以上连乘」那条）。
DENSITY_REF_STD = 4.0      # dB：频段离差 ≤ 这个值算"编曲饱满"，不扣分
DENSITY_SPAN_STD = 6.0     # dB：离差比 REF 再多这么多 ⇒ 扣满
DENSITY_W = 0.35           # 最多打 35% 的折
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
PHRASE_ALT_POS_FIRST = True   # **2026-11-01 取舍开关**：位置证据优先 —— 原始边界本来就
#   贴着 8 拍线（≤0.5 拍）且那里的落差够，就**不许** `PHRASE_ALT_MIN_BARS` 的段长门槛把它
#   拉回 16 拍线。置 **False** 就退回 2026-10-30 那版"段长整齐优先"的语义（短段一律凑整到
#   2/4 个八拍），代价是 `The Core Mind` drop3 起点又被拉晚 8 拍（用户 2026-11-01 报的那个）。
#   两句用户原话冲突、且没有可区分特征，取舍理由见 `_phrase_line` 的注释与
#   `tools/_test_phrase_segments.py` 的 (b)。
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
#: 强 / 中 分档的分位数（"这首歌自己 top 15% 的落差"＝强边界）。
#: 取值来历（2026-11-05）：
#:   · 80 / 45 是 2026-10-29 按 `_pct_norm`（0~1 拉伸）尺度定的；
#:   · 换成**相对尺度**（`_dual_dif` 两路取较大）之后，"中边界"那一档会把
#:     1.5~3.5 dB 的段内起伏全放进来 —— 实测 9 首里 6 首被切成 10~15 段
#:     （`Pont Point` 16 / `Shape of Sense` 17 / `Feel the Devotion` 20）。
#:   · ⇒ 中边界抬到与强边界同高（85/85）：**只有"这首歌自己 top 15% 的落差"才算
#:     段落起点**。剩下的"能不能多切一刀"交给 `AUTO_SEG_SEC` 那道时长预算，
#:     而不是靠一档 1.5~3.5 dB 的门槛（那档跟"段内起伏"没有区分度）。
#:     抬完之后 9 首全部落在用户期望段数内（`Pont Point` 16→10 的上限差 2 段的原因
#:     见 `tools/_test_seg_rms.py` 的说明）。
AUTO_Q_HI = 88.0
AUTO_Q_MID = 88.0
#: 线通道自己的分位（见 `auto_segments` 2b 的说明：`_step_mag` 量的是**整个台阶**，
#: `dif` 量的是**半个**，两者不能共用一个分位数 —— 共用会让线通道灵敏度翻倍）。
#: 75 是 `Rolling Contact` 162.70（用户点名的那条，它自己线分布的 76 分位）
#: 还能留下来、而 `Shape of Sense` 不再多切的唯一交点。
AUTO_Q_LINE = 78.0
#: 曲首/曲尾这么近的"落差"不算段落边界，也不参与分位数统计（淡入淡出比真段落响 20 倍）
AUTO_EDGE_SEC = 3.0
#: 合并门槛对「中边界」的倍数：两边响度差**够不上这首歌自己的中边界**才算同一段。
#: ⚠ 取 0.6 不是 1.0 以上：`q_mid` 是**落差**（= 台阶的一半，`_dif_win` 是前后各 4 秒的
#:   均值差），而合并比的是**两边整段均值**（≈ 整个台阶）⇒ 台阶本身是 `q_mid` 的两倍左右，
#:   拿 `1.4 × q_mid` 当门槛会把"整段均值差 3.8"的真台阶（`_test_phrase_offset` (c)）也删掉。
#:   段数控制交给 `AUTO_SEG_SEC` 那道预算，合并只管"两边是不是同一类"。
AUTO_MERGE_K = 0.6
#: 段落数的**时长预算**：平均每段不少于这么多秒，超了就删"两边最像"的边界
#: （强档 / 乐句线边界豁免）。用户原话「一首 2 分钟的歌每 8 秒换一次素材」
#: （2026-11-05）—— 切碎的本质是段数相对曲长太密。32 秒 ≈ 128 BPM 的 4 个乐句组。
AUTO_SEG_SEC = 26.0
#: 「这条边界算不算落在乐句线上」的容差（秒）。只用于**保护线通道的边界**不被
#: 预算删掉：去重后留下的往往是峰（`Rolling Contact` 150.31 的峰在 150.6），
#: 而线通道记的是线（150.31），两者差 0.3 秒很正常，所以按时刻比。
AUTO_LINE_TOL = 1.0

#: 两路判据的合并方式 —— **取证开关**（生产永远是 `"max"`）。
#:   `"max"`  = 逐点取绝对值更大的一路（STFT 相对落差 / RMS 相对响度），见 `_dual_dif`
#:   `"stft"` = 只用 STFT 那一路（dB / 中位数）—— 反向复现用：
#:             `Distorted Dimension` 会丢掉 87 秒的台阶（整首回到 2 段）
#:   `"rms"`  = 只用 RMS 那一路（幅度 / 中位数）—— 反向复现用：
#:             `Pont Point` / `The Core Mind` 段数失控（尺度太"陡"，1 dB 也算大落差）
#: 与 `PHRASE_ALT_POS_FIRST` 同一种用法：常量在这里定，测试里改它就能拿到两条反证。
AUTO_DUAL_MODE = "max"

# ★★ 2026-10-30（用户 `8 緋災斬月 ~ Vermilion Peril Moon`：「23.7到71.6是第一段drop，
#   想办法识别出来」）：**段首「过门」/ 段尾「衰减」要从 drop 里剔出去**。
#   实测那首：能量算法切出 16.45~74.06 一整段（mid 0.703），但
#     · 19.1~23.0（3.9 秒）是全曲最高的 0.93 —— 用户听着是"进 drop 前的那一冲"，
#       不算主体；之后回落到 0.74 的平台一直持续到 71.6；
#     · 71.6 之后 1.5 秒内掉到 0.04 —— 是"衰减下坡"，也不算 drop 主体。
#   用户要的 drop = 23.7~71.6（正好是 8 拍线 23.65 / 71.66）。
#   判据全部**相对段内中位数**（不写死绝对能量、不写死段落长度）：
#     · 段首前 40% 内有一整段「≥ M + GATE_UP」且**短于 GATE_PEAK_MAX** ⇒ 那是过门，
#       起点挪到它结束之后的第一条八拍线上；
#     · 段尾后 30% 内是「< M - GATE_DN」的连续下坡 ⇒ 那是衰减，终点挪到它开始前
#       的最后一条八拍线上。
#   最后把修剪出来的、短于「2 个八拍」的碎段并给能量更接近的邻居。
GATE_LEN_MIN = 12.0       # 段本身短于这个就不修剪（免得把短段切碎）
GATE_UP_ENTER = 0.04      # 找过门候选区间的"进入阈值"（比中位数高这么多就算进入）
GATE_UP = 0.10            # 候选区间的**均值**要比段内中位数高这么多，才算"过门"
GATE_DN = 0.20            # 衰减要比段内中位数低出这么多
GATE_PEAK_MAX = 8.0       # 过门最长多少秒（再长就不是"过门"而是真段落了）
GATE_HEAD_POS = 0.40      # 只看段首的这么多比例
GATE_TAIL_POS = 0.30      # 只看段尾的这么多比例
GATE_HEAD_MAX = 10.0      # 但最多只看段首这么多秒（长段 40% 能有 36 秒 ⇒ 太宽）
GATE_TAIL_MAX = 10.0      # 段尾同理
GATE_M_MIN = 0.35         # 段内中位数低于这个就不修剪（前奏/尾奏/过渡里没有 drop 主体）
GATE_PLAT_STD = 0.075     # 过门之后 8 秒的**标准差**上限 —— 证明后面是"稳定主体平台"
GATE_PLAT_SEC = 8.0       # 判"平台稳不稳"看多少秒
#   ⚠ 没有这条，判据退化成"段首有个小高峰就算过门" —— 实测全库 85/91 首都被改动
#     （几乎每段开头都有起伏）。加上它之后只认"高峰之后确实进入稳定平台"的结构，
#     那才是"进 drop 前的一冲"。

GATE_TAIL_STD = 0.10      # 衰减之前 8 秒的标准差上限（同理：衰减前必须是稳定主体）
#   ⚠ 没有这条会误伤：一首歌的尾奏（中位数 0.03）开头常有 2 秒"残响"（0.1~0.2），
#     相对中位数看就是 +0.15 的"过门" ⇒ 起点被推到下一条八拍线，尾奏反而变长。
#   ⚠ `GATE_UP` 必须是**区间均值**的偏移，不能拿"中位数 + 0.12"当单点阈值：
#     实测那首段内中位数 0.814、峰值 0.934 ⇒ 0.814+0.12 = 0.934 正好卡在峰上，
#     "≥阈值"的采样点数为 0 ⇒ 过门一条都找不到（差 0.0001 就整条判据失效）。


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

    ⚠⚠ 2026-11-05：**这条曲线只用于「上报能量」（显示 / 档位 / 引擎目标能量）**，
      不要再拿它做`落差`判据 —— 用 `smooth_curve_rel()` / `rms_curve()`。
      （`_quantize_phrases` / `_trim_gate_edges` / `_drop_short_seg` 仍然吃这条：
      它们的 `GATE_*` / `PHRASE_*` 常量都是按这条曲线的 0~1 尺度标定的，
      换尺度等于把那些常量全部作废。它们只决定"边界落在哪条线上"，
      不决定"这里算不算边界"。）
    """
    raw, dt = energy_curve(f)
    if raw is None:
        return None, 0.0
    sm = _mov_avg(_med_filt(raw, round(0.5 / dt)), round(1.0 / dt))
    return _pct_norm(sm), dt


def smooth_curve_rel(f):
    """同一条平滑曲线，但**除以本曲中位数**（1.0 = 典型能量），专供边界/合并判据。

    ★★★ 2026-11-05（用户「92 秒后波形明显更高，为什么没分段」）：
      `_pct_norm` 是按 5%/95% 分位数把曲线**拉伸铺满** 0~1 的。对母带压得很满的电子曲
      （整首落在 0.90~1.00 这个窄带），这个拉伸相当于**放大 10 倍**
      ⇒ `AUTO_JUMP_THR = 0.10` 实际变成了「1% 的微小起伏就切一段」
      ⇒ 实测 9 首里 8 首被切成 13~21 段（档位跟着 H/M/L 翻转 11~12 次）。
      改成除以中位数（线性、不拉伸）后，`0.10` 才真正等于「10% 的相对落差」。

    ⚠ 两条曲线并存、各管一件事，别合并：
        · `smooth_curve()`     → `energy` 上报值（0~1）、`tier` 档位、`ENERGY_GAMMA`
        · `smooth_curve_rel()` → 落差 `dif`、乐句线判据、合并判据、量化/裁剪/去短段
      合并会让 `energy` 越界（读到 1.2 之类），档位门槛（0.40/0.75）全错。
    """
    raw, dt = energy_curve(f)
    if raw is None:
        return None, 0.0
    sm = _mov_avg(_med_filt(raw, round(0.5 / dt)), round(1.0 / dt))
    med = float(np.median(sm))
    if not np.isfinite(med) or med <= 1e-6:
        return _pct_norm(sm), dt          # 全静音/异常 ⇒ 退回旧行为，绝不产生 0/负
    return sm / med, dt


def rms_curve(f):
    """**第二路判据：响度（RMS）相对曲线** —— 1.0 = 本曲典型响度。

    ★★ 2026-11-05（用户 `(07) Distorted Dimension`「87 秒有个明显台阶，只切了 2 段」）：
      STFT 那条曲线（`smooth_curve_rel`）在 87 秒处只差 **0.031**（≈1.6 dB），
      够不到 0.10 的门槛；而那里的**响度**从 0.331 跳到 0.477（**+44%**）。
      两者是同一件事的两种量纲：一个 dB 落差除以中位数（被压小），
      一个幅度比值（不压缩）⇒ 必须两路并存，取大的那一路。

    为什么不用**原始波形**算（团队最初的设想是让 `scanner.py` 把 `sig` 传进来）：
      `f` 里的 `Elow/Emid/Ehigh` 是 STFT 三个频带的**功率和**，而
      `sqrt(Elow+Emid+Ehigh)` 就是这一帧的 RMS（Parseval：能量守恒）。
      实测 9 首：`sqrt(三个频带) / 波形算出的 RMS` 中位比值 **0.99~1.005**、
      p5~p95 = **0.94~1.08** —— 逐帧对齐、等价（`tools/_test_seg_rms.py` 里有一项
      常驻校验，`assert` 的是这个比值）。
      ⇒ 不用动 `beatgrid.features()`、不用给 `auto_segments` 加 `rms=` 参数、
        不用改 `scanner.py`，也不会因为两条曲线 `dt` 不整除而错位。

    ⚠ 与 `smooth_curve_rel` 共用同一条平滑链（`_med_filt` → `_mov_avg`）和同一个
      `dt`，两条曲线**逐帧对齐**，可以直接相除 / 取 max。

    返回 `(rel, dt)`；特征缺失或全静音时 `(None, 0.0)`。
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
    amp = np.sqrt(np.maximum(el + em + eh, 0.0))       # 幅度（≈ 逐帧 RMS）
    sm = _mov_avg(_med_filt(amp, round(0.5 / dt)), round(1.0 / dt))
    med = float(np.median(sm))
    if not np.isfinite(med) or med <= 1e-12:
        return None, 0.0
    return sm / med, dt


def _dif_win(cur, dt, win_sec):
    """逐帧「后 `win_sec` 均值 − 前 `win_sec` 均值」（落差曲线，保留正负号）。

    抽出来是因为现在要算**两路**（STFT 一路、RMS 一路），两路必须用**同一套窗口**
    才能互相比较 —— 各写一遍迟早会在边界处（`i-w < 0` / `i+w > n`）算得不一致。
    """
    if cur is None or dt <= 1e-9:
        return None
    c = np.asarray(cur, dtype=np.float64)
    n = int(c.size)
    if n < 4:
        return None
    w = max(2, int(round(float(win_sec) / dt)))
    cs = np.concatenate([[0.0], np.cumsum(c)])
    out = np.zeros(n, dtype=np.float64)
    for i in range(n):
        a0 = 0 if i - w < 0 else i - w
        a1 = i
        b0 = i
        b1 = n if i + w > n else i + w
        left = (cs[a1] - cs[a0]) / float(a1 - a0) if a1 > a0 else 0.0
        right = (cs[b1] - cs[b0]) / float(b1 - b0) if b1 > b0 else 0.0
        out[i] = right - left
    return out


def _dual_dif(dif_stft, dif_rms):
    """两路落差**逐点取绝对值更大的一路**（保留原符号）。

    ★ 为什么不「取平均」或「取小」：两路各自会漏不同的东西 ——
      · STFT 一路（dB / 中位数）对「响度整体抬一格」不敏感（除以 50 dB 的中位数
        之后 3 dB 只剩 0.06）⇒ `Distorted Dimension` 87 秒漏检；
      · RMS 一路（幅度比）对「窄带瞬态」不敏感（总能量没变）⇒ 单用它会把
        母带压满的歌切成十几段（实测 `Pont Point` / `The Core Mind` 失控）。
      取平均会两头都不够；取小则两路的漏检叠加。
      取大 ⇒ 任一路认为"这里真跳了一下"就参与评比，**门槛仍然只有一套**
      （分位数在合并后的曲线上算，见 `auto_segments` 第 2 步）。
    """
    if dif_stft is None:
        return dif_rms
    if dif_rms is None:
        return dif_stft
    if AUTO_DUAL_MODE == "stft":
        return dif_stft
    if AUTO_DUAL_MODE == "rms":
        return dif_rms
    return np.where(np.abs(dif_rms) > np.abs(dif_stft), dif_rms, dif_stft)


def _pctl_low(v, p):
    """分位数，但取**不高于该分位**的那个样本（不做线性插值）。

    为什么不用 `np.percentile` 的默认插值（2026-11-05，`_test_phrase_offset` (a)）：
      候选只有 7 条时（`[0.08, 0.44, 0.44, 2.56, 2.56, 6.887, 6.931]`），85 分位会
      插值成 **6.892** —— 正好卡在最大的两条真台阶中间 ⇒ 第二大的那个台阶（6.887）
      差 0.005 掉出强档、又不是"落在 16 拍线上"，整条边界被丢掉（drop 结尾 68→72）。
      取"不高于分位的样本"后它 6.887 ≥ 6.887 留在强档，插值不再制造这种边界效应。
      代价：小样本时门槛略低一点点（不影响大样本，`Pont Point` 有 53 条候选）。
    """
    a = np.sort(np.asarray(v, dtype=np.float64))
    if a.size == 0:
        return 0.0
    k = int(math.floor(float(p) / 100.0 * (a.size - 1)))
    return float(a[max(0, min(a.size - 1, k))])


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

    ★★ 2026-11-01 用户（`The Core Mind` 详情页截图，标签 `drop 3 0.96`）：
      「drop 3 的色带**前后各少八拍**」。现场数字（bpm=190 / beat_len=0.315789 /
      anchor=1.075，八拍 = 2.526s）：原始检出 drop3 起点 **124.99**（第 392.4 拍），
      8 拍线 l8=**124.86**（第 392 拍，只差 **0.40 拍**）、16 拍线 l16=**127.39**（第 400 拍，
      差 7.60 拍）；`_phrase_step_score` s8=**0.3155** vs s16=**0.0092**（8 拍线强 0.31，
      远超 `PHRASE_ALT_MARGIN`=0.10）—— 证据全都指向"这里本来就是半句偏移"，
      但吸附后起点还是 127.39（晚 8 拍 = 2.53 秒）。
      **为什么**：`near` 门槛（`near < PHRASE_ALT_MIN_BARS` ⇒ 直接 `return l16`）跑在
      8 拍线仲裁**之前**。而 `near` 的左段是拿**已定**边界量出来的：前一条原始边界
      119.49（第 375 拍）先被吸到 16 拍线 117.28（第 368 拍，早了 7 拍）⇒ 左段被算成
      `(124.99-117.28)/2.526 = 3.05` 个八拍 ⇒ 门槛直接短路，仲裁根本没机会跑
      （否则按 s8-s16=+0.31 早就选 l8 了）。即"1.5 拍容差"和"near 门槛"这两道关串在
      一起把合法边界拦掉了：`near` 本意是挡"短段凑整成整乐句"，不是挡"原始边界本来就
      贴在 8 拍线上"这件事 —— `t` 是算法自己找出的能量突变点，它离 8 拍线只差 0.4 拍，
      说明这首在这一处就是半句偏移 ⇒ **位置证据比段长证据硬**，先认位置。
      ⇒ 在 `near` 门槛**之前**加一条短路：`|t - l8| <= 0.5 拍` 且 `s8 >= s16 - 0.02`
        ⇒ 直接 `return l8`。
      ⚠ 只放行"原始边界 ≤0.5 拍贴 8 拍线"这一种：`Dust`（用户：「3 个八拍一段」）那种
        短段**仍然**默认走 16 拍线（`near<4` 时不会走到 1.5 拍那条规则），`Lord Help Me`
        的 s8 领先 0.10 也照样能被后面的仲裁选中。
      ⚠ 成本：l8 / s16 / s8 提前算 ⇒ 每条边界多 2 次 `_phrase_step_score`（各一次切片
        均值，微秒级）。`cur is None`（拿不到能量曲线）时仍走老逻辑，不静默变味。
    """
    step16 = gbl * max(1, int(beats))
    l16 = ga + round((t - ga) / step16) * step16
    if cur is None or gbl <= 1e-6:
        return l16
    # ★ 16 拍线离得够近（≤3 拍）⇒ 老实吸 16 拍线。这一步把触发面收窄到**真正的
    #   半句偏移**（原始边界贴着"奇数八拍"线、离 16 拍线 6 拍以上），否则每条
    #   边界都能找到"更近的 8 拍线"，全库会大面积改写（实测 71/90 首分段变化，
    #   而用户要的只是"别把 drop 边界往里拉一个八拍"）。
    #   ⚠ 提到 `near` 门槛之前只是省两次打分：两条路都返回 `l16`，行为不变。
    #   注意 |t-l16|<=3 拍时 |t-l8| >= 5 拍 ⇒ 下面那条 0.5 拍短路不可能命中，
    #   所以提前返回不会漏掉新判据。
    if abs(t - l16) <= gbl * 3.0:
        return l16
    step8 = gbl * PHRASE_BEATS
    l8 = ga + round((t - ga) / step8) * step8
    if abs(l8 - l16) <= 1e-6:                  # 两条线重合 ⇒ 没什么可挑
        return l16
    s16 = _phrase_step_score(cur, dt, l16, gbl, sign)
    s8 = _phrase_step_score(cur, dt, l8, gbl, sign)
    # ★★ 2026-11-01：**原始边界本来就贴在 8 拍线上**（≤0.5 拍）且落差够
    #   ⇒ 位置证据优先于段长证据，`near` 不许把它拉回 16 拍线。
    #   这是 `The Core Mind` drop3 起点被拉晚 8 拍的修法（详见上面的函数注释）。
    #   0.5 拍怎么来的：现场 0.40 拍，取"半拍"留一点余量；**不能**照抄下面 1.5 拍
    #   那条 —— 1.5 拍是"原始边界大概齐在 8 拍线上"，得配 `near` 限制一起用，
    #   整个放开就会把"四舍五入本来就该吸 16 拍线"的边界也卷进来（全库大面积改写）。
    #   ⚠ 整条判据由模块常量 `PHRASE_ALT_POS_FIRST` 控制（2026-11-01 定稿取舍）：
    #     置 False ＝ 退回 2026-10-30 的"段长整齐优先"（短段一律凑整），实测全库
    #     回到 **997 段 / 0 处改判**，代价是 `The Core Mind` drop3 又被拉晚 8 拍。
    if PHRASE_ALT_POS_FIRST and abs(t - l8) <= gbl * 1.0 and s8 >= s16 - 0.02:
        return l8
    if near < PHRASE_ALT_MIN_BARS - 1e-6:
        return l16
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


def _snap_line(t, a, bl, beats, up=True):
    """把 `t` 吸到 `beats` 拍网格线上：`up=True` 取 ≥t 的第一条，否则取 ≤t 的最后一条。"""
    step = bl * max(1, int(beats))
    if step <= 1e-6:
        return float(t)
    k = (float(t) - a) / step
    k = math.ceil(k - 1e-6) if up else math.floor(k + 1e-6)
    return a + k * step


def _trim_gate_edges(edges, cur, dt, gok, ga, gbl, dur_eff):
    """把段首「过门」和段尾「衰减」从段里剔出去（判据见 `GATE_*` 的说明）。

    为什么放在**乐句量化之后**：过门/衰减的终点常常落在"半句"（8 拍）线上
    （实测 `8 緋災斬月` 是第 72 拍 = 23.65s、第 232 拍 = 71.66s），
    交给 16 拍量化会被硬拉到 26.05 / 74.06 —— 又回到用户抱怨的那个位置。
    ⇒ 这里直接用 8 拍线落位。

    返回 `(edges, trimmed)`：`trimmed` 是**被修剪过的主体区间**列表
    `[(t0, t1), ...]` —— 上层拿它判档位（修剪掉过门/衰减之后，这一段就是
    "drop 主体"，见 `auto_segments` 第 5 步）。段数只会增加（修剪出的过门/衰减
    自成一"段"，再由 `_drop_short_seg` 并给能量更接近的邻居）。
    """
    if cur is None or dt <= 1e-9 or len(edges) < 2:
        return edges, []
    n = int(cur.size)
    trimmed = []
    out = [float(edges[0])]
    for k in range(len(edges) - 1):
        t0 = float(edges[k])
        t1 = float(edges[k + 1])
        i0 = max(0, min(n - 1, int(round(t0 / dt))))
        i1 = max(i0 + 1, min(n, int(round(t1 / dt))))
        L = (i1 - i0) * dt
        if L < GATE_LEN_MIN or i1 - i0 < 8:
            out.append(t1)
            continue
        M = float(np.median(cur[i0:i1]))
        if M < GATE_M_MIN:
            out.append(t1)          # 前奏 / 尾奏 / 过渡：没有"drop 主体"可修剪
            continue
        nt0, nt1 = t0, t1

        # ---- ① 段首过门：一小段明显高于主体 ----
        lim = max(i0 + 2, min(i1 - 2, i0 + int(round(
            min(GATE_HEAD_POS * L, GATE_HEAD_MAX) / dt))))
        enter = M + GATE_UP_ENTER
        best = None
        j = i0
        while j < lim:
            if cur[j] >= enter:
                a0 = j
                while j < i1 and cur[j] >= enter:
                    j += 1
                if (j <= lim and (j - a0) * dt <= GATE_PEAK_MAX
                        and float(cur[a0:j].mean()) >= M + GATE_UP):
                    best = j               # 记"这一段过门的终点"
            else:
                j += 1
        _gated = False
        if best is not None:
            # 过门之后必须是一段**稳定平台**（否则只是段内起伏，不是"进 drop 前的一冲"）
            k0 = best
            k1 = min(i1, k0 + int(round(GATE_PLAT_SEC / dt)))
            _plat_ok = False
            if k1 - k0 >= int(round(4.0 / dt)):
                _plat_ok = float(cur[k0:k1].std()) <= GATE_PLAT_STD
            if _plat_ok:
                t = best * dt
                if gok and gbl > 1e-6:
                    t = _snap_line(t, ga, gbl, PHRASE_BEATS, up=True)
                if t0 + 2.0 <= t <= t1 - min(12.0, 0.5 * L):
                    nt0 = t
                    _gated = True

        # ---- ② 段尾衰减：贴着段尾的一段明显低于主体 ----
        # ⚠ 只在**过门也触发**的段上做：这两个修剪是同一个"结构性 drop"的两端，
        #   单独做衰减几乎命中每一段（段尾总有淡出）⇒ 实测会改掉全库 85/91 首。
        if not _gated:
            out.append(t1)
            if abs(out[-1] - t1) > 1e-6:
                out.append(t1)
            continue
        dn = M - GATE_DN
        lo_lim = max(i0 + 2, min(i1 - 2, i1 - int(round(
            min(GATE_TAIL_POS * L, GATE_TAIL_MAX) / dt))))
        low_start = None
        j = i1
        while j > lo_lim:
            if cur[j - 1] < dn:
                while j > i0 and cur[j - 1] < dn:
                    j -= 1
                low_start = j          # 贴尾那段低能量区的起点
                break
            j -= 1
        if low_start is not None:
            k1 = low_start
            k0 = max(i0, k1 - int(round(GATE_PLAT_SEC / dt)))
            _tail_ok = True
            if k1 - k0 >= int(round(4.0 / dt)):
                _tail_ok = float(cur[k0:k1].std()) <= GATE_TAIL_STD
            t = low_start * dt
            if gok and gbl > 1e-6:
                t = _snap_line(t, ga, gbl, PHRASE_BEATS, up=False)
            if _tail_ok and nt0 + min(12.0, 0.5 * L) <= t <= t1 - 1.5:
                nt1 = t

        if (nt0, nt1) != (t0, t1):
            trimmed.append((float(nt0), float(nt1)))
        # ⚠ 不能用 `out[-1] = nt0` 覆盖：段 0 的起点是曲首 0.0，被覆盖成 nt0 之后
        #   开头的过门就凭空消失（整首从 6 秒才开始）。修剪出来的部分**各自成段**，
        #   交给 `_drop_short_seg` 按能量就近合并。
        if nt0 > t0 + 0.01:
            out.append(nt0)            # 过门自成一"段"
        out.append(nt1)
        if nt1 < t1 - 0.01:
            out.append(t1)             # 衰减尾自成一"段"
        if abs(out[-1] - t1) > 1e-6:   # 兜底：本段终点必须与下段起点对齐
            out.append(t1)
    return out, trimmed


def _drop_short_seg(edges, cur, dt, min_sec, only_touching=()):
    """把短于 `min_sec` 的段并给**能量更接近**的邻居（修剪出来的过门/衰减多是这种）。

    `only_touching`：只处理**紧邻这些时刻**的碎段（传入修剪产生的边界时刻）。
    ⚠ 必须传：无条件合并会把"原本就存在、由 `_peak_like` 特意豁免的 8 拍假高潮"
      也吃掉 —— 实测那样改掉全库 57/91 首，是回归。传了之后只动修剪点附近。
    """
    if cur is None or dt <= 1e-9 or len(edges) < 3:
        return edges
    n = int(cur.size)

    def _m(a, b):
        i0 = max(0, min(n, int(round(a / dt))))
        i1 = max(i0 + 1, min(n, int(round(b / dt))))
        return float(cur[i0:i1].mean())

    out = [float(x) for x in edges]
    guard = 0
    while len(out) > 2 and guard < 16:
        guard += 1
        _tk = [float(x) for x in (only_touching or ())]
        short = []
        for k in range(len(out) - 1):
            if out[k + 1] - out[k] >= min_sec:
                continue
            if _tk and not any(abs(out[k] - t) < 0.05 or abs(out[k + 1] - t) < 0.05
                               for t in _tk):
                continue
            short.append(k)
        if not short:
            break
        k = min(short, key=lambda i: out[i + 1] - out[i])
        e = _m(out[k], out[k + 1])
        cand = []
        if k > 0:
            cand.append((abs(e - _m(out[k - 1], out[k])), k))
        if k < len(out) - 2:
            cand.append((abs(e - _m(out[k + 1], out[k + 2])), k + 1))
        if not cand:
            break
        del out[min(cand)[1]]
    return out


def _fill_phrase_quads(edges, short_dif, dt, dur, ga, gbl, ok,
                       min_bars=10, step_bars=4, thr=0.06):
    """按「每 4 个八拍一段」的音乐结构，给过长的段补一刀。

    ★★★ 2026-11-05（用户：「59 到 68 这一段刚好是四个八拍」）：
      `(07) Distorted Dimension` 在 59.0s 有个明显下陷（RMS 相对落差 −0.215），
      单靠能量能切出来；但它在 68.63s（= 第 28 个八拍）回升却只有 **+0.088**
      （4 秒窗更被稀释成 +0.049）—— 任何能量门槛都够不到，而用户听得出那里是段尾。

      为什么必须单独一路：能量判据量的是"响度变化了多少"，而这里是
      **渐强过渡**（0.812 → 0.950 跨一秒爬上去），坡度缓、窗口一平滑就没了。
      音乐结构上它就是 4 个八拍整，这个先验能量曲线里没有。

    只补同时满足三条的位置（缺一条都不补，避免把段切碎）：
      1. **所在段长度 ≥ `min_bars` 个八拍** —— 短段不动
      2. **落点在 `step_bars` 个八拍的整数倍线上**（相对 `anchor`，±0.35 秒）
      3. **该处是正向回升**（短窗落差 ≥ `thr`）—— 不能凭空插一刀；
         用短窗（2 秒）而不是主判据那个 4 秒窗，正是为了抓住缓升

    :param short_dif: 短窗落差数组（`_dif_win(rms_rel, dt, 2.0)`）
    返回新的 `edges`（含原有的，不删不改，只插）。
    """
    if not ok or gbl <= 1e-6 or dur <= 0 or len(edges) < 2:
        return edges
    bar = gbl * 8.0                      # 一个八拍多少秒
    step = bar * float(step_bars)        # 4 个八拍
    cur_e = list(edges)
    out = [cur_e[0]]
    n = len(short_dif)
    for k in range(len(cur_e) - 1):
        a, b = float(cur_e[k]), float(cur_e[k + 1])
        out.append(b)
        span = b - a
        if span < bar * float(min_bars):     # 段太短，不动
            continue
        # ⚠⚠ 2026-11-05 第三次修订（用户：「Drunken Barrage 11~83 明显起伏较大，
        #   算法只算成一个动态」）：
        #   段长 30 个八拍（11.53→84.57s），RMS 从 0.42 爬到 1.25（差 3 倍），
        #   中间 32.4 / 53.3 / 74.1 三处都有明确正向回升（+0.401 / +0.271 / +0.169）。
        #   但前一版死认「起点 + 4 八拍」= 21.96s，那里是**下降**（−0.298）⇒ 整段放弃 ⇒
        #   72 秒的一大段。⇒ 改成：**从段起点往后找第一个"在刻度上且有正向回升"的位置**，
        #   切完对剩余部分**继续找**（不是只补一刀）。
        #   · DD 那首：起点 59 → +4 八拍 68.63（+0.088 ✅）⇒ 切；剩下 68.63→88 只有
        #     6 个八拍 < min_bars ⇒ 停。正好是用户要的"只切一刀"。
        #   · DB 那首：21.96 不够 → 跳到 32.40（✅）→ 剩余 32.4→84.6 继续 →
        #     53.27（✅）→ 74.14（✅）⇒ 起伏大的长段会被切成几段。
        #   `min_bars` 仍是"剩余段低于这个长度就不再切"的止损线。
        stack = [(a, b)]
        while stack:
            x, y = stack.pop(0)
            if y - x < bar * float(min_bars):
                continue
            found = None
            # ★★★ 2026-11-05 第五次（用户：「Territoriality 怎么也没分出来」）：
            #   ⚠⚠ 刻度**必须从网格线算起，不能从段起点算**。
            #   第一段起点是 `edges[0] = 0.0`（硬赋的，不代表音乐起点），
            #   而 anchor = 1.2147 —— 从 0 数 4 个八拍得 10.105，网格线在 11.32，
            #   差 1.21 秒，`abs(t - 网格)` > 0.35 判为"不在线上"⇒ 所有刻度全跳过
            #   ⇒ 这首 0~82 秒的一大段一个子段都没补上。
            #   Distorted Dimension 侥幸能过，是因为它那段起点 58.97 正好在网格上。
            #   ⇒ 用 `ceil((x - ga) / step)` 反算第一个 ≥ x 的刻度序号。
            m = int(math.ceil((x - ga) / step - 1e-9))
            if m < 1:
                m = 1
            while True:
                t = ga + m * step
                if t >= y - 0.35:            # 这段到头了
                    break
                if t <= x + 0.35:            # 还在段起点上（就是起点本身）
                    m += 1
                    continue
                i = int(round(t / dt))
                if not (1 <= i < n - 1):
                    m += 1
                    continue
                if abs(float(short_dif[i])) >= thr:   # 幅度够就切（不分升降）
                    found = t
                    break
                m += 1
            if found is not None:
                out.append(found)
                stack.append((x, found))     # 左半继续找
                stack.append((found, y))     # 右半继续找
    # 去重 + 排序（插进去的位置可能和已有边界重合）
    out = sorted(set(round(x, 4) for x in out))
    fixed = [out[0]]
    for x in out[1:]:
        if x - fixed[-1] > 0.3:              # 0.3 秒内的并成一条
            fixed.append(x)
    return fixed


def _insert_valley_seg(edges, cur, dt, ga, gbl, ok, max_beats=8.0):
    """在**下降型边界**之后找到一个能量谷底，在那里**插入**一条边界，
    让「谷底那 8 拍」单独成一小段（通常会被判成 mid）。

    ★★★ 2026-11-06（用户：「有谷底时我在想不然把这个八拍切出来算成是中段中动态？」）：
      背景：`dif` 是「前后各 4 秒均值差」，峰值落在**下坡最陡处**，而段落真正结束
      （能量到底）要再晚 8 拍。实测 `The Core Mind`：
        · drop1 尾部 288 拍 cut、292 拍落差最大、**296 拍谷底**(rms 1.405→0.948)
        · drop3 尾部 416 拍 cut、420 拍落差最大、**424 拍谷底**(rms 1.625→1.324)
      曾经试过"把边界后移到谷底"，但那会改变 drop 长度 ⇒ 破坏 4 项既有契约。
      ⇒ 改成**插入**：原边界不动（drop 长度不变），在谷底再加一刀，
        谷底那 8 拍自然落成一段（能量低 ⇒ 被判 mid），正好是"drop 结束后的过渡"。

    ⚠ 只处理**下降型**（本段比下段响）；上升型边界不动。
    ⚠ 谷底必须比边界处低 `MIN_DROP` 才插，避免在平缓处插碎段。
    ⚠ 只插 8 拍线上的位置，且距两端边界都 > 半个八拍。
    """
    if not ok or gbl <= 1e-6 or cur is None or dt <= 1e-9 or len(edges) < 3:
        return edges
    MIN_DROP = 0.10
    bar8 = gbl * PHRASE_BEATS
    n = int(cur.size)

    def _mean(t0, t1):
        i0 = max(0, min(n, int(round(t0 / dt))))
        i1 = max(i0 + 1, min(n, int(round(t1 / dt))))
        return float(cur[i0:i1].mean())

    out = [float(edges[0])]
    added = 0
    for k in range(1, len(edges) - 1):
        t = float(edges[k])
        nxt = float(edges[k + 1])
        out.append(t)
        e_now = _mean(t - bar8 * 0.5, t + bar8 * 0.5)
        if e_now <= _mean(t + bar8 * 0.5, t + bar8 * 1.5) + 1e-6:
            continue                       # 上升型：不处理
        # ⚠ 2026-11-06 修正：**只允许插在"距原边界正好 8 拍、且本身落在八拍网格上"的位置**。
        #   第一版从 t 往后扫 8 拍内的任意点，实测 `Rolling Contact` 在 162.7 后面插出
        #   162.7~165.8（3.1 秒）的碎段，`_test_seg_line` 当场红。
        #   段落边界只该落在八拍头上，插一刀也必须守这个规矩。
        cand = t + bar8
        best_t, best_e = None, e_now
        if (cand < nxt - bar8 * 0.5
                and abs(cand - (ga + round((cand - ga) / bar8) * bar8)) <= 0.35):
            e_c = _mean(cand - bar8 * 0.5, cand + bar8 * 0.5)
            if e_c <= e_now - MIN_DROP:
                best_t, best_e = cand, e_c
        if best_t is not None:
            out.append(best_t)
            added += 1
    out.append(float(edges[-1]))
    return out


def _bar_step(cur, dt, t, bar):
    """`t` 处、左右各 `bar` 秒的均值差（绝对值）。窗口无效时给 0。"""
    if cur is None or dt <= 1e-9:
        return 0.0
    n = int(cur.size)
    i0 = max(0, min(n, int(round((t - bar) / dt))))
    i1 = max(0, min(n, int(round(t / dt))))
    i2 = max(0, min(n, int(round((t + bar) / dt))))
    if i1 - i0 < 2 or i2 - i1 < 2:
        return 0.0
    return abs(float(cur[i0:i1].mean()) - float(cur[i1:i2].mean()))


def _cut_timbre_bar(edges, f, dt, ga, gbl, ok, min_ratio=2.2, min_bars_side=4):
    """按**音色（高频占比）**在段内补一刀 —— 电平没变、但亮度变了的那种段落变化。

    ★★★ 2026-11-06（用户：「Erebos 段落5 前 32 拍和后面 64 拍不一样」「波形图看着很明显」）：

    实测那句 drop 5（小节 24~36，12 个八拍）：
      · 高频占比（Ehigh / (Elow+Emid+Ehigh)，0.3 秒平滑）逐小节：
        24: 0.071  25: 0.095  26: 0.061  27: **0.098**  28: **0.044**  29: 0.025
        30: 0.026  31: 0.023  32: 0.024  33: 0.073 …
      · 也就是小节 28 处高频占比从 ~0.098 **掉到 ~0.024（约 1/4）**
    但**电平**几乎没动：RMS 0.964 vs 0.854（差 0.11），4 秒窗 `dif` 只有 **0.065**，
    而噪声门槛是 0.213 ⇒ 电平判据根本看不见它。用户看到的是"亮的那层没了"。

    ⚠ 判据要用**相对**变化不是绝对差：占比本身只有 2%~10%，绝对差 0.05 看着小，
      但 0.098→0.024 是 **4 倍**，`log2` 尺度上一目了然。
      门槛 `min_ratio=2.2`（升高或降低 120%）。⚠ 一开始写 1.6，实测太松：
      全库平均段数 15.1 → **16.2**（`Shape of Sense` 17→20、`Capture The Moment` 10→13、
      `The Core Mind` 13→16），全是"高频占比差 60~80%"的细微音色差。2.2 倍才够。
      实测 `Erebos` 那处小节 28 是 2.3 倍、小节 29 是 2.8 倍 ⇒ 仍然保留。
    ⚠ 只在段内、每段最多一刀、左右各至少 `min_bars_side` 个八拍。
    """
    if not ok or gbl <= 1e-6 or dt <= 1e-9 or f is None or len(edges) < 2:
        return edges
    try:
        lo = np.asarray(f["Elow"], dtype=np.float64)
        mid = np.asarray(f["Emid"], dtype=np.float64)
        hi = np.asarray(f["Ehigh"], dtype=np.float64)
    except Exception:                                          # noqa: BLE001
        return edges
    br = hi / (lo + mid + hi + 1e-9)
    k = max(1, int(round(0.3 / dt)))
    br = np.convolve(br, np.ones(k) / k, mode="same")
    n = br.size
    bar = gbl * PHRASE_BEATS
    win = bar * 2.0

    def _m(a, b):
        i0 = max(0, min(n - 1, int(round(a / dt))))
        i1 = max(i0 + 1, min(n, int(round(b / dt))))
        return float(br[i0:i1].mean())

    add = []
    for i in range(len(edges) - 1):
        t0, t1 = float(edges[i]), float(edges[i + 1])
        nb0 = int(round((t0 - ga) / bar))
        nb1 = int(round((t1 - ga) / bar))
        if nb1 - nb0 < min_bars_side * 2:
            continue
        best = None
        for kb in range(nb0 + min_bars_side, nb1 - min_bars_side + 1):
            t = ga + kb * bar
            pre, post = _m(t - win, t), _m(t, t + win)
            if pre < 0.01 or post < 0.01:
                continue
            r = max(pre / post, post / pre)
            if r < min_ratio:
                continue
            # ⚠ 结构偏好：两边都是 4 个八拍的倍数时加 0.25 分。
            #   实测 `Erebos` 那处：小节 28（4+8）比值 2.1、小节 29（5+7）比值 2.2 ——
            #   纯按比值会选 29，切出 40 拍 + 56 拍，而用户要的是 32 + 64（= 小节 28）。
            #   加结构分后 28（2.35）胜过 29（2.2）。
            if best is None or r > best[1]:
                best = (t, r)
        # ★★ 找到最强点后，**吸附到 ±1 小节内、能让两边都成 4 的倍数**的那条小节线。
        #   实测 `Erebos`：比值最强的在小节 29（2.8），但那是 5+7；小节 28 是 4+8
        #   （比值 2.3，仍远高于门槛 1.6）—— 用户要的正是 32+64 拍 ⇒ 吸到 28。
        if best is not None:
            _kb = int(round((best[0] - ga) / bar))
            for _d in (-1, 1):
                _k2 = _kb + _d
                _lL, _rL = _k2 - nb0, nb1 - _k2
                if _lL < min_bars_side or _rL < min_bars_side:
                    continue
                if not (abs(_lL / 4.0 - round(_lL / 4.0)) < 0.05
                        and abs(_rL / 4.0 - round(_rL / 4.0)) < 0.05):
                    continue
                _t2 = ga + _k2 * bar
                _p, _q = _m(_t2 - win, _t2), _m(_t2, _t2 + win)
                if _p < 0.01 or _q < 0.01:
                    continue
                if max(_p / _q, _q / _p) >= min_ratio * 0.85:
                    best = (_t2, best[1])
                    break
        if best is not None:
            add.append(best[0])
    if not add:
        return edges
    return sorted(set(float(x) for x in edges) | set(add))


def _cut_ramp_bar(edges, cur, dt, ga, gbl, ok, min_drop=0.30, max_flat=0.15, bars_after=4):
    """把段首那个**内部一路下落**的八拍单独切出来（"下降沿"判据，不是电平差）。

    ★★★ 2026-11-06（用户：「它这个波形图看着很明显，就是前面 8 拍是往下降的，然后后面
      32 拍是差不多的，这个为什么会切不出来呢？」）：

    实测 `Petrified Forest` 小节 8~12（5 个八拍）的逐拍 RMS：
      · 小节 8：0.735 → 0.508 → 0.338 → 0.284 → 0.248 → 0.211 → 0.173 → 0.217
        ⇒ **首八拍内部从 0.735 掉到 0.173（跌幅 0.56）**，是一条下降沿
      · 小节 9~12 的每小节均值：0.43 / 0.45 / 0.51 / 0.51 ⇒ **极差只有 0.08，很平**
    但"电平差"判据算的是两边**平均**：小节 8 均值 0.339、小节 9~12 均值 0.48，
    只差 **0.14**（而小节 13 那刀是 0.46）⇒ 永远排不到前面，调门槛也切不出来。

    为什么：**下降沿的"形状"被平均抹平了**。要抓它必须看"这一段内部有没有大起大落"。

    三条同时满足才切（切完首八拍自成一段）：
      1. 首八拍逐拍极差 >= `min_drop`（0.30）
      2. 紧随其后 `bars_after` 个八拍，**每小节均值**极差 <= `max_flat`（0.12）
      3. 段长 >= bars_after + 1
    ⚠ 只在段首判、每段最多一刀、不递归。
    """
    if not ok or gbl <= 1e-6 or dt <= 1e-9 or cur is None or len(edges) < 2:
        return edges
    bar = gbl * PHRASE_BEATS
    n = int(cur.size)

    def _mean(a, b):
        i0 = max(0, min(n - 1, int(round(a / dt))))
        i1 = max(i0 + 1, min(n, int(round(b / dt))))
        return float(cur[i0:i1].mean())

    add = []
    for i in range(len(edges) - 1):
        t0, t1 = float(edges[i]), float(edges[i + 1])
        nb = int(round((t1 - t0) / bar))
        if nb < bars_after + 1:
            continue
        b1 = [_mean(t0 + k * gbl, t0 + (k + 1) * gbl) for k in range(8)]
        if (max(b1) - min(b1)) < min_drop:
            continue
        bm = [_mean(t0 + bar + k * bar, t0 + bar + (k + 1) * bar) for k in range(bars_after)]
        if (max(bm) - min(bm)) > max_flat:
            continue
        # ⚠⚠ 还必须"首八拍比后面明显安静" —— 否则会切掉 **drop 的头一个八拍**
        #   （drop 开头一记重音砸下来、后面持续，形状也是"内部大起大落 + 后面平稳"）。
        #   实测不加这条，`_test_phrase_offset` 六项全红（drop 被切成 1+7，不再是 8 个八拍）。
        #   判据：首八拍均值 <= 后面均值的 0.85 倍。`Petrified Forest` 那段是 0.339 vs 0.49
        #   （比值 0.69）⇒ 通过；drop 头那一段首八拍是最响的，比值 > 1 ⇒ 被拒。
        if sum(b1) / 8.0 > (sum(bm) / len(bm)) * 0.85:
            continue
        add.append(t0 + bar)
    if not add:
        return edges
    return sorted(set(float(x) for x in edges) | set(add))


def _cut_strong_bar(edges, cur, dt, ga, gbl, ok, min_step=0.30, min_bars=1):
    """在长段内部**插一刀**：只切该段内最陡的那条小节线，一段最多一刀，不递归。

    ★★★ 2026-11-06（用户：「`Petrified Forest` 中段2 前面 5 个八拍应该切出 1 个八拍和
      4 个八拍」「那前 5 个小节能单独切出来吗？」）：

    中段2 是**小节 8→21（13 个八拍）**，前 5 个八拍（8~13）和后 8 个（13~21）明显不同：
      · 小节 8~12：RMS 0.340 / 0.433 / 0.476 / 0.499 / 0.555（一路缓升）
      · 小节 13：**0.996**（台阶 **+0.463**，左右各 2 个八拍窗）
    但小节 13 是**奇数小节**，`_quantize_phrases` 只往 16 拍线（偶数小节）上吸，
    峰离最近的线正好 8 拍 ⇒ 被拒 ⇒ 这刀一直切不出来。

    ⚠ 关键设计：**必须在 `_resnap_to_bar` 之后调用**。那条重落位是"整首联合优化"，
      在它之前插的边界会被它当成可动边界重新排一遍（实测插 2 条 → 12 段变 15 段、
      还出 1 个八拍的碎段）。放在它之后 = 插完就定稿，谁也改不了。

    判据只在有网格时生效；`min_step` 取 0.30（左右各 2 个八拍窗），只抓真正的大台阶。
    """
    if not ok or gbl <= 1e-6 or dt <= 1e-9 or cur is None or len(edges) < 2:
        return edges
    bar = gbl * PHRASE_BEATS
    win = bar * 2.0
    add = []
    for i in range(len(edges) - 1):
        t0, t1 = float(edges[i]), float(edges[i + 1])
        L = int(round((t1 - t0) / bar))
        # ⚠ 只切**不规整**的段（不是 4 个八拍的倍数）：4/8/12 这些本来就规整，
        #   再切就变成 4+4、4+4+4，纯属过度切分 —— 实测不加这条，那首从 12 段涨到 15 段
        #   （`45-57` 被切成 45/49/53/57 四段）。
        # ★ 2026-11-07 复验：曾为修 `Devouring Saturnus` 去掉 `L % 4 == 0` 豁免，
        #   全库 89 首实测**净负面**：Imaginary Dimension 0→2、I'll See You Again 0→3、
        #   Bipolar 1→2 等 6 首「8 拍奇数倍段」反而增加；目标曲也只把 536→544/576，
        #   仍没得到用户要的「512→576 整段 64 拍 drop」。⇒ 已回退、保留豁免。
        #   结论：静音尾巴 / 段内骤降这类问题不能在 edges 层靠放宽插刀解决，
        #   否则会经 normalize 连锁改变下游所有 pass 的 mod32 相位判据。
        if L < 5 or L % 4 == 0:
            continue
        nb0 = int(round((t0 - ga) / bar))
        nb1 = int(round((t1 - ga) / bar))
        best = None
        for kb in range(nb0 + 1, nb1):
            if kb - nb0 < min_bars or nb1 - kb < min_bars:
                continue
            v = _bar_step(cur, dt, ga + kb * bar, win)
            if v < min_step:
                continue
            is_max = True
            for o in (-2, -1, 1, 2):
                if _bar_step(cur, dt, ga + (kb + o) * bar, win) > v + 1e-9:
                    is_max = False
                    break
            if is_max and (best is None or v > best[1]):
                best = (ga + kb * bar, v)
        if best is not None:
            add.append(best[0])
    if not add:
        return edges
    return sorted(set(float(x) for x in edges) | set(add))


def _merge_weak_bar(edges, cur, dt, ga, gbl, ok, max_bars=1.0, ratio=2.0):
    """把一个"只有 1 小节长"的碎段并掉，**保留两条边里更陡的那条**。

    ★★★ 2026-11-06（用户：「中段 1 前面少了八拍」）：
      `End Of The Earth` 在 184/192/200 拍上有三条边界，其中 192 是**弱边界**
      （能量 0.20→0.20，纯属噪声），200 才是真突变（0.20→0.86）。
      但 `_resnap_to_bar` 有 `min_bars=2` 约束：192 和 200 只隔 1 小节 ⇒ 站在 200
      是不合法状态，DP 只能把 200 挪到 208（晚 8 拍）—— 用户听到"前面少了八拍"。
      ⇒ 先把 1 小节的碎段并掉：比两条边谁更陡，弱的删掉，强的留着。
      ⚠ 只在两条边**强度差 ≥ ratio 倍**时才删（避免把真的"8 拍过门"吃掉）；
        删除后 200 就不再受 1 小节约束，`_resnap_to_bar` 能原地不动。
    """
    if not ok or gbl <= 1e-6 or dt <= 1e-9 or cur is None or len(edges) < 4:
        return edges
    bar = gbl * PHRASE_BEATS
    # ⚠ 窗口只能用 **1 小节**：用 2 小节会把隔壁的真突变也算进来，
    #   弱边（192）拿到的分会虚高到 0.4、真突变（200）0.65 ⇒ 比值不到 2 倍，判不出来。
    #   1 小节窗口下弱边≈0、真突变≈0.66，一眼分得开。
    win = bar * 1.0
    out = [float(x) for x in edges]
    guard = 0
    while len(out) > 3 and guard < 8:
        guard += 1
        # ⚠ 容差用"拍"不用"秒"：吸附后的边界是 `ga + n*bar` 四舍五入出来的，
        #   两段"正好 1 小节"的差可能比 bar 大几个 float ulp ⇒ 用秒比会漏判。
        short = [k for k in range(1, len(out) - 2)
                 if (out[k + 1] - out[k]) / bar <= max_bars + 0.02]
        if not short:
            break
        k = min(short, key=lambda i: out[i + 1] - out[i])
        s_l = _bar_step(cur, dt, out[k], win)
        s_r = _bar_step(cur, dt, out[k + 1], win)
        if s_l <= 0 and s_r <= 0:
            break
        drop = k if s_l * ratio < s_r else (k + 1 if s_r * ratio < s_l else None)
        if drop is None:
            break
        del out[drop]
    return out


def _resnap_to_bar(edges, cur, dt, ga, gbl, ok, margin=1.30, min_bars=2.0,
                   struct_w=0.25, energy_keep=0.95):
    """把每条边界在 **±1 小节（8 拍）** 内重新落位到"最陡的那条小节线"。

    ★★★ 2026-11-06（用户：「`Witchcraft Butterfly -VIP-` 从中段二开始全部都算错了，
      都偏差了 8 拍」）：

    病根：`_quantize_phrases` 把边界吸到 **2 个八拍（16 拍）= 偶数小节**的整数倍上。
    这首歌的段落却全在**奇数小节**（第 9/21/37/41/53/69 小节）⇒ 最近的那条 16 拍线
    正好差 8 拍，方向还取决于峰值落在线的哪一侧，所以有的偏早、有的偏晚。
    实测（BPM 128，一个八拍 3.75 秒）：
      · 真实跳变 **296 拍（139.8s，能量开始下落）** → 程序切在 **304 拍**（晚 8 拍）
      · 真实跳变 **328 拍（154.8s，能量回升）**   → 程序切在 **320 拍**（早 8 拍）
      · 尾奏前 **552 拍（台阶 0.909）**           → 程序切在 **560 拍**（晚 8 拍）
    两个边界被**向内挤**，32 拍的下凹被压成 16 拍 —— 就是用户听到的"整体差 8 拍"。

    做法：只搬位置、不增不减，因此不会触发预算/合并的连锁重排。
      · 候选：`t-bar` / `t` / `t+bar`（必须真的在小节网格上）
      · 判据：左右各 **2 个八拍**的均值差，比原地大 `margin` 倍才搬
        （实测三处的比值是 1.52 / 1.41 / 1.38 ⇒ 取 1.30 能全部覆盖，且留了余量）
      · 约束：搬完不能把任一侧压到 `min_bars` 个八拍以内
    """
    if not ok or gbl <= 1e-6 or dt <= 1e-9 or cur is None or len(edges) < 3:
        return edges
    bar = gbl * PHRASE_BEATS
    win = bar * 2.0
    gap_min = bar * min_bars

    # 边界换算成小节号（首尾固定，不许动）
    b = [int(round((float(e) - ga) / bar)) for e in edges]
    m = len(b) - 1
    if m < 2:
        return edges

    def _struct(len_bars):
        """段长的结构分：4 的倍数 1.0 / 偶数 0.5 / 奇数 0。"""
        L = float(len_bars)
        if L < 1.0:
            return -1.0
        if abs(L / 4.0 - round(L / 4.0)) < 0.05:
            return 1.0
        if abs(L / 2.0 - round(L / 2.0)) < 0.05:
            return 0.5
        return 0.0

    # 每个可动边界、每个偏移量(-1/0/+1)的"能量分"
    SH = (-1, 0, 1)
    val = {}
    for k in range(1, m):
        for d in SH:
            pos = b[k] + d
            if pos - b[k - 1] < min_bars or b[k + 1] - pos < min_bars:
                val[(k, d)] = None            # 不可选
                continue
            t = ga + pos * bar
            if abs(t - (ga + round((t - ga) / bar) * bar)) > 0.35:
                val[(k, d)] = None
                continue
            val[(k, d)] = _bar_step(cur, dt, t, win)

    # ★★★ 2026-11-06（用户：「中段 1 前面少了八拍」）——**强突变保护**：
    #   原地就是真突变时不许被 DP 搬走。
    #   实测 `End Of The Earth`：真实跳变在 **200 拍**（能量 0.20→0.86），
    #   而 DP 为了凑"段长是 4 的倍数"把它搬到 **208 拍**（晚 8 拍 = 3.1 秒），
    #   整段少了一个八拍 —— 用户听到的就是"前面少了八拍"。
    #   判据与 `_phrase_line` 同源（位置证据硬于结构分）：
    #   原地分数比**两侧候选都强 `margin` 倍** ⇒ 该边界锁死 d=0（只锁这一条，其余照常优化）。
    #   ⚠ 只在原地分数 > 0 时判（负分/零分不参与，避免"两边都很差"被误锁）。
    _locked = 0
    for k in range(1, m):
        s0 = val.get((k, 0))
        if s0 is None or s0 <= 0:
            continue
        _others = [val[(k, d)] for d in SH if d != 0 and val.get((k, d)) is not None]
        if _others and all(s0 > v * margin for v in _others):
            for d in SH:
                if d != 0:
                    val[(k, d)] = None
            _locked += 1

    # ★★★ 链式 DP：**相邻两条边界要一起定**。
    #   实测 `Witchcraft Butterfly`：把第 16 小节挪到 17 能让「中段2 = 8 个八拍」成立，
    #   但下一段就变 3 个八拍 —— 单看一条边界永远算不出好处，必须连着下一条一起看。
    #   目标 = Σ 能量分 + `struct_w` × Σ 段长结构分，约束 段长 >= `min_bars`。
    NEG = -1e9
    best = {d: (val.get((1, d)) or NEG) for d in SH if val.get((1, d)) is not None}
    back = []
    for k in range(2, m):
        cur_dp, cur_bk = {}, {}
        for d in SH:
            v = val.get((k, d))
            if v is None:
                continue
            for pd, pv in best.items():
                if pv <= NEG:
                    continue
                L = (b[k] + d) - (b[k - 1] + pd)
                sc = pv + v + struct_w * _struct(L)
                if sc > cur_dp.get(d, NEG):
                    cur_dp[d] = sc
                    cur_bk[d] = pd
        if not cur_dp:
            return edges
        best = cur_dp
        back.append(cur_bk)

    # 收尾（最后一段到曲末）
    final_d, final_sc = None, NEG
    for d, pv in best.items():
        if pv <= NEG:
            continue
        L = b[m] - (b[m - 1] + d)
        sc = pv + struct_w * _struct(L)
        if sc > final_sc:
            final_sc, final_d = sc, d
    if final_d is None:
        return edges

    keep = {m - 1: final_d}
    d = final_d
    for k in range(m - 2, 0, -1):
        d = back[k - 1][d]
        keep[k] = d

    out = list(edges)
    for k in range(1, m):
        nd = keep.get(k, 0)
        if nd:
            out[k] = ga + (b[k] + nd) * bar
    return out


def _snap_strong_step(edges, cur, dt, ga, gbl, ok, ratio=3.0, min_rel=0.05):
    """边界"贴错一个八拍"的兜底：±1 小节内台阶**明显更陡**就搬过去（**结构分不参与**）。

    ★★★ 2026-11-07（用户：「Doomed Airflac 的 drop 5 后面少了一个八拍，后面几个 drop
      都提前了」「一次扫一两百首，不可能一首一首调」）：
      `_resnap_to_bar` 的链式 DP 同时优化「台阶分 + 0.25×段长结构分」，为了凑"段长是
      4 的小节倍数"会牺牲台阶。实测这首歌（归一化后曲线，范围=1.0）：
        拍 480 台阶 0.0325 ｜ 拍 488 台阶 **0.1314（4.0 倍）**
        但 480→512 正好 4 小节能拿满分、488→512 只有 3 小节（奇数）⇒ DP 留在 480
        ⇒ drop 5 少 8 拍，后面全部提前一个八拍。
      本函数只做一件事：**只看台阶**，候选 = 前/后一小节，台阶达到原地 `ratio` 倍且
      超过绝对下限（`min_rel` × 曲线动态范围）就搬。
    ⚠ 阈值必须用**相对曲线范围**：`cur` 在 `auto_segments` 里已被归一化（范围≈1.0），
      写成绝对值（如 1.2）永远不触发 —— 上一版就是这么哑掉的。
    ⚠ 只搬位置、不增不减；只跑一遍；段长 ≥ 2 小节。
    """
    if not ok or gbl <= 1e-6 or dt <= 1e-9 or cur is None or len(edges) < 3:
        return edges
    bar = gbl * PHRASE_BEATS
    win = bar * 2.0
    try:
        rng = float(np.percentile(cur, 95) - np.percentile(cur, 5))
    except Exception:                                          # noqa: BLE001
        rng = 0.0
    min_step = max(min_rel * rng, 1e-9)
    out = [float(x) for x in edges]

    def _bar_no(t):
        return int(round((t - ga) / bar))

    for k in range(1, len(out) - 1):
        base = _bar_no(out[k])
        if abs(ga + base * bar - out[k]) > gbl * 1.05:      # 离小节线超过一拍 ⇒ 不动
            continue
        s0 = _bar_step(cur, dt, out[k], win)
        # ★ 只在"原地几乎没有台阶"时才考虑搬：说明这条边界本来就没落在真突变上
        #   （Doomed Airflac 480 拍：0.0325，远低于 0.06×范围）。
        #   真边界（有台阶）一律不动 —— 这一条是为了不把别的歌的结构改坏。
        if s0 >= 0.06 * rng:
            continue
        best = None
        for d in (-1, 1):
            n = base + d
            if (n - _bar_no(out[k - 1])) < 2 or (_bar_no(out[k + 1]) - n) < 2:
                continue
            t = ga + n * bar
            sc = _bar_step(cur, dt, t, win)
            if sc >= max(s0 * ratio, min_step) and (best is None or sc > best[1]):
                best = (t, sc)
        if best is not None:
            out[k] = best[0]
    return out


def _regularize_flat(edges, cur, dt, ga, gbl, ok, flat=0.02):
    """能量"听不出差别"的地方，优先让**段长成整数个 32 拍**。

    ★★★ 2026-11-07（用户：「Doomed Airflac 的 drop 6 后面又少了八拍」）：
      实测那一段能量是一条平线（相邻小节台阶都在 **0.018~0.024**，彼此差 <0.003），
      **没有任何能量证据**说明该在 512 还是 520 切。这种情况下算法的选择就是随机的，
      而 DJ 的曲式在这类连续 drop 里几乎都是 **32 拍一段** ⇒ 用"结构规整"当默认。
      判据：
        · 段长不是 4 小节（32 拍）的整数倍；
        · ±1 小节能凑成整数倍，且那一侧的台阶与原地的差 ≤ `flat`（说明原始边界也没证据）；
        ⇒ 搬过去。
    ⚠ 只在"平"的时候动手；有真实台阶的地方一律不动（那是音乐说的，不是算法说的）。
    """
    if not ok or gbl <= 1e-6 or dt <= 1e-9 or cur is None or len(edges) < 3:
        return edges
    bar = gbl * PHRASE_BEATS
    win = bar * 2.0
    out = [float(x) for x in edges]
    for k in range(1, len(out) - 1):
        L = (out[k] - out[k - 1]) / bar
        bars = int(round(L))
        if abs(L - bars) > 0.3 or bars % 4 == 0 or bars < 2:
            continue
        s0 = _bar_step(cur, dt, out[k], win)
        best = None
        for d in (-1, 1):
            nb = bars + d
            if nb < 1 or nb % 4 != 0:
                continue
            t = out[k] + d * bar
            if (t - out[k - 1]) < bar * 1.0 or (out[k + 1] - t) < bar * 1.0:
                continue
            sc = _bar_step(cur, dt, t, win)
            if abs(sc - s0) <= max(flat, 0.25 * max(s0, sc)):
                best = t
                break
        if best is not None:
            out[k] = best
    return out


def _cap_segment_beats(edges, cur, dt, ga, gbl, ok, max_beats=64):
    """单段最长 64 拍：超了就**从段首往后 64 拍处**切一刀，余下的自成一段。

    ★★★ 2026-11-07 用户：「加一条规则，每一段最长不会超过 64 拍。假如说有 96 拍，
      那就把它切了，把最后 32 拍独立出来」。
      例：96 拍 → 64 + 32；128 拍 → 64 + 64；72 拍 → 64 + 8（余数照切，不吞）。
      切点优先落在**已有能量台阶较陡**的位置：在 [段首+56, 段首+64] 之间挑台阶最大的一条
      小节线，没有明显台阶就取段首+64（纯按时长规则）。
    """
    if not ok or gbl <= 1e-6 or dt <= 1e-9 or cur is None or len(edges) < 2:
        return edges
    bar = gbl * PHRASE_BEATS
    win = bar * 2.0
    out = [float(x) for x in edges]
    i = 0
    guard = 0
    while i < len(out) - 1 and guard < 32:
        guard += 1
        t0, t1 = out[i], out[i + 1]
        if (t1 - t0) / gbl <= max_beats + 0.5:
            i += 1
            continue
        target = t0 + max_beats * gbl
        n0 = int(round((t0 - ga) / bar))
        nts = int(round((target - ga) / bar))
        best, best_v = None, -1.0
        for nb in range(max(n0 + 1, nts - 1), nts + 1):      # 段首+56 ~ 段首+64 拍
            t = ga + nb * bar
            if t <= t0 + 1e-6 or t >= t1 - 1e-6:
                continue
            v = _bar_step(cur, dt, t, win)
            if v > best_v:
                best, best_v = t, v
        if best is None:
            best = target
        out.insert(i + 1, best)
        i += 1
    return out


def _merge_head_tail(edges, ga, gbl, ok, max_beats=16):
    """开头/结尾不足 `max_beats` 拍的那一小段并回相邻段（拾音鼓点不该自成一段）。

    ★★★ 2026-11-07 用户：「这一首歌开头的四个鼓点为什么会被独立出去啊？」
      曲头只有 4 拍准备鼓（随后进入正题），能量台阶明显 ⇒ 被切成独立一段，
      但它在听感上属于"前奏的一部分"，不该单独成段。
      判据：首段或末段 < `max_beats` 拍（默认 16 = 2 小节）⇒ 并进相邻段。
      ⚠ 只动首尾，不动中间（中间的 8 拍过门是真实结构，用户明确要求保留）。
    """
    if not ok or gbl <= 1e-6:
        return edges
    out = [float(x) for x in edges]
    if len(out) >= 3 and (out[1] - out[0]) / gbl < max_beats - 0.5:
        del out[1]
    if len(out) >= 3 and (out[-1] - out[-2]) / gbl < max_beats - 0.5:
        del out[-2]
    return out


def _split_contrast_halves(edges, cur, dt, ga, gbl, ok, min_dev=0.12, min_beats=32):
    """前半段/后半段**能量明显不同**时，从正中间切一刀。

    ★★★ 2026-11-07（用户：「Back 2 Clay 的中段二前面 32 拍是 drop、后面 32 拍是过渡；
      中段四也切得不对」）：
      这两段都是 **64 拍**，前半 0.95、后半 0.60 —— 整段均值 0.62 落在 mid，被判成"中段"，
      于是 drop 和过渡被粘成一段。人耳听到的是两段，因为一半炸、一半收。
      判据：段长为偶数拍且 ≥ `min_beats`，取前/后各一半的均值，差 ≥ `min_dev` ⇒ 中间切一刀。
      ⚠ 只在**两半对比明显**时动手（0.12 ≈ 明显换质感），不改变其它段的段数。
    """
    if not ok or gbl <= 1e-6 or dt <= 1e-9 or cur is None or len(edges) < 2:
        return edges
    out = list(edges)
    i = 0
    while i < len(out) - 1:
        t0, t1 = float(out[i]), float(out[i + 1])
        nb = (t1 - t0) / gbl
        n = int(round(nb / 2.0))
        if nb < min_beats or abs(nb - 2 * n) > 0.6 or n < 8:
            i += 1
            continue
        mid = t0 + n * gbl
        a0 = _win_mean(cur, dt, t0, mid)
        a1 = _win_mean(cur, dt, mid, t1)
        if abs(a1 - a0) >= min_dev:
            out.insert(i + 1, mid)
            i += 2
        else:
            i += 1
    return out


def _align_run_to_32(edges, cur, dt, ga, gbl, ok, hi_rel=0.75):
    """连续高能量段（drop 链）里的边界，统一按**32 拍整数倍**重排。

    ★ 用户 2026-11-07：「中段三之后是 64 拍的 drop，你识别成 40+32」「64 拍之后是 32 拍中动态」。
      做法：把相邻的高能量段并成一条"链"，从**链首**起每 32 拍一条线，
      边界就近吸附到这条链的 32 拍线上（移动 ≤8 拍，且新位置台阶不比原地差太多）。
    """
    if not ok or gbl <= 1e-6 or dt <= 1e-9 or cur is None or len(edges) < 3:
        return edges
    out = [float(x) for x in edges]
    means = []
    for i in range(len(out) - 1):
        means.append(_win_mean(cur, dt, out[i], out[i + 1]))
    if not means:
        return out
    mx = max(means)
    if mx <= 1e-9:
        return out
    # 只有"够长"的高能量段才算 drop 链成员（8 拍的那种是过渡，不能当链首）
    hi = [(means[i] >= hi_rel * mx) and ((out[i + 1] - out[i]) >= 24.0 * gbl)
          for i in range(len(means))]
    win = gbl * 16.0
    i = 0
    while i < len(hi):
        if not hi[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(hi) and hi[j + 1]:
            j += 1
        t0 = out[i]
        for k in range(i + 1, j + 1):
            cur_t = out[k]
            n = int(round((cur_t - t0) / (32.0 * gbl)))
            tgt = t0 + n * 32.0 * gbl
            if n < 1 or abs(tgt - cur_t) > 8.0 * gbl:
                continue
            if tgt <= out[k - 1] + 1e-6 or tgt >= out[k + 1] - 1e-6:
                continue
            if _bar_step(cur, dt, tgt, win) >= 0.6 * _bar_step(cur, dt, cur_t, win):
                out[k] = tgt
        i = j + 1
    return out


def _bridge_drop_fix(edges, cur, dt, ga, gbl, ok, valley_drop=0.08,
                     hi_min=0.75, plat_min=0.90, flat_max=0.10):
    """识别 DJ 常用的「八拍过门 → 64 拍 drop → 32 拍中动态」结构并**局部**重建。

    ★★★ 2026-11-07 用户（Back 2 Clay 波形图）：
      「中段 3 只有 32 拍，然后是一个八拍的过渡之后才是 drop；
        drop 是 64 拍的一个整体，之后又是 32 拍的中动态，也是一个整体」
      波形上是一段段**平台**：中段3(0.82) → 孤立低谷(0.72, 8 拍过门)
      → 连续高平台(0.96~0.99, 64 拍 drop) → 中平台(32 拍)。
      旧判据只会找局部台阶，把高平台碎切成 48+32，还把低谷并进中段3。

    判据（全部满足才动手，条件严格 ⇒ 其它歌基本不受影响）：
      ① 存在一个**孤立 8 拍低谷**：该格比左右各一格低 ≥ valley_drop，且两侧都够响；
      ② 低谷之后的 8 格（64 拍）平均能量 ≥ plat_min，且每格都不低于 plat_min-0.15；
      ③ 低谷之前 32 拍是同一平台（中段3）。
    动作（只在 [低谷-32拍, 低谷+8+96拍] 窗口内）：
      删掉窗口内所有旧边界，插入 4 条：低谷起点 / 低谷终点 / +64 拍 / +96 拍。
      窗口外**完全不动** —— 这是不破坏全库的关键。
    """
    if not ok or gbl <= 1e-6 or dt <= 1e-9 or cur is None or len(edges) < 4:
        return edges
    try:
        lo, hi = np.percentile(cur, [5, 95])
    except Exception:                                          # noqa: BLE001
        return edges
    rng = max(float(hi - lo), 1e-9)
    bar = 8.0 * gbl
    out = [float(x) for x in edges]
    t0, end = out[0], out[-1]
    line0 = ga + math.ceil((t0 - ga) / bar - 1e-6) * bar

    def cell(t):
        v = (_win_mean(cur, dt, t, t + bar) - lo) / rng
        return max(0.0, min(1.0, v))

    lines = []
    t = line0
    while t + bar <= end + 1e-6:
        lines.append(t)
        t += bar
    if len(lines) < 16:
        return edges
    for k in range(4, len(lines) - 10):
        a, m, b = cell(lines[k - 1]), cell(lines[k]), cell(lines[k + 1])
        if min(a, b) - m < valley_drop or min(a, b) < hi_min:
            continue
        # ② 低谷后 8 格必须是连续高平台
        hi_cells = [cell(lines[k + 1 + j]) for j in range(8)]
        if min(hi_cells) < plat_min - 0.15:
            continue
        if float(np.mean(hi_cells)) < plat_min:
            continue
        # ②b 这 64 拍必须是**平坦**的一整块（不是忽高忽低）
        if max(hi_cells) - min(hi_cells) > flat_max:
            continue
        # ③ 低谷前 4 格（32 拍）为同一平台
        pre = [cell(lines[k - 4 + j]) for j in range(4)]
        if max(pre) - min(pre) > flat_max:
            continue
        # ③b drop 之后那 32 拍要**明显低于** drop（"中动态"确实存在）
        mid_cells = [cell(lines[k + 9 + j]) for j in range(4)]
        if float(np.mean(mid_cells)) > float(np.mean(hi_cells)) - 0.08:
            continue
        v0 = lines[k]                    # 低谷起点
        v1 = lines[k + 1]                # drop 起点
        v2 = v1 + 8 * bar                # +64 拍
        v3 = v2 + 4 * bar                # +32 拍
        w0 = lines[k - 4]                # 窗口左（中段3 起点）
        if v3 >= end - bar or w0 <= t0:
            continue
        new_marks = [w0, v0, v1, v2, v3]
        rebuilt = [e for e in out if e <= w0 + 1e-6 or e >= v3 - 1e-6]
        for e in new_marks:
            if all(abs(e - x) > 1e-6 for x in rebuilt):
                rebuilt.append(e)
        return sorted(set(rebuilt))
    return out


def _fix_drop_bars_segs(segs, cur, dt, ga, gbl, ok, hi_min=0.75,
                        max_step=0.08, max_diff=0.14):
    """把「3 个八拍 + 5 个八拍」的连续 drop 重排成「4 + 4」（各 32 拍）。

    ★★★ 2026-11-07 用户（Back 2 Clay 波形图）：
      「不会有一首电子音乐的 Drop 是 5 个八拍的。drop 3 前面的一个八拍是 drop 2 的，
        因为它稍微有一点下降，所以没被算进去」
      实测每 8 拍归一化能量（这一整片 64 拍）：
        168:.938 176:.916 184:.943 | 192:.866(只轻微下凹) | 200:.931 208:.904 216:.929 224:.851
      边界 192 处台阶只有 -0.031（几乎没变化），却把连续高平台切成 24 + 40 拍
      = 3 个八拍 + 5 个八拍。把边界挪到 200（前段起点 + 32 拍），两段各 32 拍。
      ⇒ 那一个八拍**归回前面的 drop**，不单独切出来（它只是轻微抖动，不是过门）。

    ⚠ 必须作用在 `normalize()` **之后**的段落表上：edges 层还带着未吸附的 172 等值，
      normalize 会重新吸到 8 拍线，在 edges 上判断看不到最终的 3+5。
    判据（全部满足才动手，条件严格 ⇒ 其它歌不受影响）：
      ① 相邻两段 A、B 都是 high，且 energy 差 < max_diff（同一平台）；
      ② 长度合计正好 8 个八拍（64 拍），且 A 为 3 或 5 个八拍（B 随之是 5 或 3）；
      ③ A、B 的外侧端点都落在对齐 ga 的 8 拍格上；
      ④ 旧边界处能量台阶 < max_step（轻微抖动，不是真段落变化）。
    动作：把边界改为「A 起点 + 4 个八拍」，两段各 32 拍；energy 按新区间重算。
    """
    if not ok or gbl <= 1e-6 or dt <= 1e-9 or cur is None or len(segs) < 3:
        return segs
    bar = 8.0 * gbl
    try:
        lo, hi = np.percentile(cur, [5, 95])
    except Exception:                                          # noqa: BLE001
        return segs
    rng = max(float(hi - lo), 1e-9)

    def rel(t0, t1):
        v = (_win_mean(cur, dt, t0, t1) - lo) / rng
        return max(0.0, min(1.0, v))

    def on_grid(t):
        n = (t - ga) / bar
        return abs(n - round(n)) < 0.20

    for k in range(len(segs) - 1):
        A, B = segs[k], segs[k + 1]
        if A.get("tier") != "high" or B.get("tier") != "high":
            continue
        a, b, c = float(A["t0"]), float(B["t0"]), float(B["t1"])
        na, nb = (b - a) / bar, (c - b) / bar
        if abs(na + nb - 8.0) > 0.7:
            continue
        if not (abs(na - 3.0) < 0.4 or abs(na - 5.0) < 0.4):
            continue
        if not on_grid(a) or not on_grid(c):
            continue
        if abs(float(A["energy"]) - float(B["energy"])) >= max_diff:
            continue
        if min(float(A["energy"]), float(B["energy"])) < hi_min:
            continue
        step = abs(rel(b - 16.0 * gbl, b) - rel(b, b + 16.0 * gbl))
        if step >= max_step:
            continue
        target = a + 4.0 * bar
        if target <= a + bar or target >= c - bar:
            continue
        # 重算两段的 energy（用同一条归一化曲线的段均值，保持与原口径一致的比例）
        ea, eb = rel(a, target), rel(target, c)
        A["t1"] = round(target, 3)
        B["t0"] = round(target, 3)
        A["energy"] = round(float(A["energy"]) * 0.5 + ea * 0.5, 3)
        B["energy"] = round(float(B["energy"]) * 0.5 + eb * 0.5, 3)
        return segs
    return segs


def _bridge_mid_platform_fix(edges, cur, dt, ga, gbl, ok, valley_drop=0.15,
                             left_min=0.85, plat_lo=0.60, plat_hi=0.90,
                             plat_flat=0.15, plat_bars=16):
    """识别「drop 后的孤立 8 拍低谷 + 之后 128 拍平稳中平台」并**局部**重建。

    ★★★ 2026-11-07 用户（Lord Help Me (Hardstyle VIP)）：
      「中段 4567 全部算错了，中段 4 前面八拍才是独立的，然后后面就是 128 拍的那个过渡，
        切成 4 个 32 拍（或 2 个 64 拍）都行，只要能把那个八拍识别出来」
      实测每 8 拍归一化能量：
        224:0.892(drop 尾) | 232:0.536(孤立低谷) | 240~360:0.70~0.82(16 格平稳平台)
        | 368:0.964(drop 回来)
      旧算法把低谷并进「中段 4」(232→264)，又在平台尾部错切一个 8 拍「中段 7」
      (360→368)，于是中段 4/5/6/7 全错。

    判据（全部满足才动手，条件严格 ⇒ 其它歌不受影响）：
      ① 某 8 拍格比左右各一格都低 ≥ valley_drop（孤立低谷），且左格 ≥ left_min
         （前面确实是 drop）；
      ② 低谷之后连续 `plat_bars`(16) 格都在 [plat_lo, plat_hi] 且极差 ≤ plat_flat
         （128 拍平稳中平台）；
      ③ 平台结束后一格 ≥ left_min（drop 回来）；
      ④ 低谷起点与平台终点都落在对齐 ga 的 8 拍格上。
    动作（只在 [低谷起点, 平台终点] 窗口内）：
      低谷 8 拍独立成段；平台按 **32 拍**等分成 4 段；窗口外边界完全不动。
      放在处理链最后（normalize 会吸附到 8 拍格，插入的边界本就在格上）。
    """
    if not ok or gbl <= 1e-6 or dt <= 1e-9 or cur is None or len(edges) < 5:
        return edges
    try:
        lo, hi = np.percentile(cur, [5, 95])
    except Exception:                                          # noqa: BLE001
        return edges
    rng = max(float(hi - lo), 1e-9)
    bar = 8.0 * gbl
    out = [float(x) for x in edges]
    t0, end = out[0], out[-1]
    line0 = ga + math.ceil((t0 - ga) / bar - 1e-6) * bar

    def cell(t):
        v = (_win_mean(cur, dt, t, t + bar) - lo) / rng
        return max(0.0, min(1.0, v))

    lines = []
    t = line0
    while t + bar <= end + 1e-6:
        lines.append(t)
        t += bar
    if len(lines) < plat_bars + 4:
        return edges
    for k in range(2, len(lines) - plat_bars - 2):
        a, m, b = cell(lines[k - 1]), cell(lines[k]), cell(lines[k + 1])
        if min(a, b) - m < valley_drop or a < left_min:
            continue
        platform = [cell(lines[k + 1 + j]) for j in range(plat_bars)]
        if min(platform) < plat_lo or max(platform) > plat_hi:
            continue
        if max(platform) - min(platform) > plat_flat:
            continue
        if cell(lines[k + 1 + plat_bars]) < left_min:
            continue
        v0 = lines[k]                       # 低谷起点
        v1 = lines[k + 1]                   # 平台起点
        v_end = lines[k + 1 + plat_bars]    # 平台终点
        if not any(abs(v0 - e) < bar * 0.25 for e in out):
            continue                        # 低谷起点附近没有边界 ⇒ 不动（避免凭空造段）
        # 窗口内重建：低谷 8 拍 + 平台 4×32 拍
        marks = [v0, v1] + [v1 + (j + 1) * 4 * bar for j in range(3)] + [v_end]
        rebuilt = [e for e in out if e <= v0 + 1e-6 or e >= v_end - 1e-6]
        for e in marks:
            if all(abs(e - x) > 1e-6 for x in rebuilt):
                rebuilt.append(e)
        return sorted(set(rebuilt))
    return out


def _align_transition_and_climax(segs, cur, dt, ga, gbl, ok):
    """① 连续过渡块(low 链)按 32 拍格重排；② 切出「假高潮 8 拍」。

    ★★★ 2026-11-07 用户再报 2 首：
      · `In The Atmosphere`：「过渡五六七三段一共 128 拍，应分成两个 64 或 4 个 32」、
        「drop6 少了后面 8 拍，导致后面所有 drop 都错位 8 拍」。
        实测 过渡5/6/7 = 56+16+56 = 128 拍，边界 312/328 都距 32 拍格 8 拍；
        drop6 = 480→536(56)，其后 536/600/664/680/736/800 全部连锁偏 8 拍。
      · `Out of Innocence`：「drop6 开头八拍是假高潮过渡，之后才是 64+64 结构」。
        实测 672 那格 0.843，680 起 0.950/0.957/0.949 稳定高台，
        680 处台阶 +0.134 是这一带最强上升沿。

    ① 判据：连续 low 段 ≥3 段、非首尾、链长是 32 整数倍且 ≥96 拍、
       链首在 8 拍格、链内 ≥2 条边界不在 32 格、链内无 <16 拍段（保护独立过门）。
       动作：按 32 拍格重排（链首链尾不动）。
    ② 判据：high 段 ≥32 拍、段起点在 8 拍格、开头 8 拍 e0，紧随 8 拍 e1，
       e1-e0 ≥0.08 且 e1 ≥0.85，且 e1 之后连续 2 格 ≥ e1-0.10（稳定高台，非单尖峰）；
       且该段之后至少还有 2 段。动作：把开头 8 拍切成独立段。
    ⚠ 两者都只动局部位置、不增删其它段，energy 按新区间重算、name 重新编号。
    """
    if not ok or gbl <= 1e-6 or dt <= 1e-9 or cur is None or len(segs) < 5:
        return segs
    bar = 8.0 * gbl
    step = 32.0 * gbl
    try:
        lo, hi = np.percentile(cur, [5, 95])
    except Exception:                                          # noqa: BLE001
        return segs
    rng = max(float(hi - lo), 1e-9)

    def rel(t0, t1):
        v = (_win_mean(cur, dt, t0, t1) - lo) / rng
        return max(0.0, min(1.0, v))

    def on8(t):
        q = (t - ga) / bar
        return abs(q - round(q)) < 0.25

    def renumber(xs):
        h = m = l = 0
        for k, x in enumerate(xs):
            t = x["tier"]
            if t == "high":
                h += 1; x["name"] = "drop %d" % h
            elif t == "mid":
                m += 1; x["name"] = "中段 %d" % m
            else:
                l += 1
                x["name"] = ("前奏" if k == 0 else
                             "尾奏" if k == len(xs) - 1 else "过渡 %d" % l)
        return xs

    # ---- ① 连续 low 过渡块 ----
    n = len(segs)
    i = 0
    while i < n:
        if segs[i].get("tier") != "low" or i == 0:
            i += 1
            continue
        j = i
        while j + 1 < n and segs[j + 1].get("tier") == "low" and j + 1 < n - 1:
            j += 1
        nseg = j - i + 1
        if nseg < 3 or j >= n - 1:
            i = j + 1
            continue
        a = float(segs[i]["t0"]); b = float(segs[j]["t1"])
        Lb = (b - a) / gbl
        k = int(round(Lb / 32.0))
        if (not on8(a)) or k < 3 or abs(Lb - 32.0 * k) > 1.5:
            i = j + 1
            continue
        lens = [(float(segs[m + 1]["t0"]) if m + 1 <= j else b)
                - float(segs[m]["t0"]) for m in range(i, j + 1)]
        if min(lens) < 15.5 * gbl:
            i = j + 1
            continue
        old = [float(segs[m]["t0"]) for m in range(i + 1, j + 1)]
        off = sum(1 for t in old
                  if abs((t - a) / step - round((t - a) / step)) > 0.15)
        if off < 2:
            i = j + 1
            continue
        marks = [a + m * step for m in range(1, k)] + [b]
        built = []
        prev = a
        for t in marks:
            e = rel(prev, t)
            built.append({"t0": round(prev, 3), "t1": round(t, 3),
                          "energy": round(e, 3), "tier": "low", "name": ""})
            prev = t
        segs = [dict(x) for x in segs[:i]] + built + [dict(x) for x in segs[j + 1:]]
        segs = renumber(segs)
        n = len(segs)
        i = i + k + 1

    # ---- ② 假高潮 8 拍 ----
    n = len(segs)
    out = []
    for idx, sg in enumerate(segs):
        t0 = float(sg["t0"]); t1 = float(sg["t1"])
        Lb = (t1 - t0) / gbl
        prev_hi = (idx > 0 and out and out[-1].get("tier") == "high"
                   and (float(out[-1]["t1"]) - float(out[-1]["t0"])) / gbl >= 31.5
                   and idx >= 2)
        if (sg.get("tier") == "high" and Lb >= 31.5 and on8(t0) and idx + 2 < n
                and prev_hi):
            e0 = rel(t0, t0 + bar)
            e1 = rel(t0 + bar, t0 + 2 * bar)
            e2 = rel(t0 + 2 * bar, t0 + 3 * bar)
            e3 = rel(t0 + 3 * bar, t0 + 4 * bar)
            if (e1 - e0 >= 0.08 and e1 >= 0.85
                    and min(e2, e3) >= e1 - 0.10):
                ea = rel(t0, t0 + bar)
                out.append({"t0": round(t0, 3), "t1": round(t0 + bar, 3),
                            "energy": round(ea, 3), "tier": tier_from_energy(ea), "name": ""})
                rest = dict(sg)
                rest["t0"] = round(t0 + bar, 3)
                rest["energy"] = round(rel(t0 + bar, t1), 3)
                out.append(rest)
                continue
        out.append(dict(sg))
    return renumber(out)


def _shift_odd_chain(segs, cur, dt, ga, gbl, ok):
    """把「连锁错位的连续响段链」整体吸回 **32 拍乐句格**（链首不动）。

    ★★★ 2026-11-07 用户连报 5 首（Bipolar / 9 Oblivious / Black Warrior /
      Dragonborn / System Overloaded）：「drop 少了八拍，后面全部错位八拍」
      「中段 1 少了八拍」「开场 128 拍 Drop，标准 64+64 或 4 个 32 拍」。
      数据结论：BPM 与 anchor 都正确（tempo_fit_score、onset 相位都指向原值），
      能量台阶也支持当前边界 —— 错的是**乐句结构**：链里出现 24/40/56/72 拍
      （8 拍奇数倍），电子乐 drop 应为 32 拍整数倍。

    判据（全部满足才动手）：
      ① 连续响段链（tier != low）、≥3 段、链内含 high、链首在 8 拍格上；
      ② 链内至少 **2 条**边界不在 32 拍格上（确为连锁错位，不是个别抖动）；
      ③ 链长与 32×k 相差 ≤8 拍，k ≤ 原段数+3（只消碎块，不无端暴涨）；
      ④ 链外紧邻段在链尾微调后 ≥16 拍（严防碎段）。
    动作：链内边界改为「链首 + 32m 拍」、链尾改为链首+32k；逐链都修、保持无缝。
    """
    if not ok or gbl <= 1e-6 or dt <= 1e-9 or cur is None or len(segs) < 5:
        return segs
    bar = 8.0 * gbl
    step = 32.0 * gbl
    try:
        lo, hi = np.percentile(cur, [5, 95])
    except Exception:                                          # noqa: BLE001
        return segs
    rng = max(float(hi - lo), 1e-9)

    def rel(t0, t1):
        v = (_win_mean(cur, dt, t0, t1) - lo) / rng
        return max(0.0, min(1.0, v))

    n = len(segs)
    i = 0
    while i < n:
        if segs[i].get("tier") == "low":
            i += 1
            continue
        j = i
        while j + 1 < n and segs[j + 1].get("tier") != "low":
            j += 1
        nseg = j - i + 1
        if nseg < 3 or not any(segs[m].get("tier") == "high" for m in range(i, j + 1)):
            i = j + 1
            continue
        a = float(segs[i]["t0"]); b = float(segs[j]["t1"])
        nb0 = (a - ga) / bar
        if abs(nb0 - round(nb0)) > 0.25:
            i = j + 1
            continue
        # ★ 保护「独立 8 拍过门」：链内 <16 拍段若是**孤立能量低谷**（真 break）
        #   就整条跳过；若只是普通短段（错位造成的假段）则允许按 32 拍格归位。
        lens_all = [(float(segs[m + 1]["t0"]) if m + 1 <= j else b)
                    - float(segs[m]["t0"]) for m in range(i, j + 1)]
        if any(lens_all[m - i] < 15.5 * gbl and
               rel(float(segs[m]["t0"]), float(segs[m]["t1"])) <
               (rel(float(segs[m]["t0"]) - 8 * gbl, float(segs[m]["t0"])) +
                rel(float(segs[m]["t1"]), float(segs[m]["t1"]) + 8 * gbl)) / 2.0 - 0.28
               for m in range(i, j + 1)):
            i = j + 1
            continue
        old = [float(segs[m]["t0"]) for m in range(i + 1, j + 1)]
        off = sum(1 for t in old
                  if abs((t - a) / step - round((t - a) / step)) > 0.15)
        if off < 2:
            i = j + 1
            continue
        # ★ 自证：重排后"8 拍奇数倍段"必须严格减少，否则整条链放弃
        #   （避免把本来正常的曲式改成另一种正常曲式）。
        def _odd(L):
            q = L / 8.0
            return 12 <= L <= 128 and abs(q - round(q)) < 0.35 and int(round(q)) % 2 == 1
        _before = sum(1 for L in lens_all if _odd(L / gbl))
        _k_tmp = int(round((b - a) / step))
        _after = 0 if _k_tmp >= 3 else _before     # 全 32 拍 ⇒ 0 个奇数段
        if _after >= _before:
            i = j + 1
            continue
        k = int(round((b - a) / step))
        if k < 3 or k > nseg + 3 or abs((b - a) / gbl - 32.0 * k) > 8.6:
            i = j + 1
            continue
        target_end = a + k * step
        if j + 1 < n:
            next_end = (float(segs[j + 2]["t0"]) if j + 2 < n
                        else float(segs[j + 1]["t1"]))
            if next_end - target_end < 16 * gbl:
                i = j + 1
                continue
        marks = [a + m * step for m in range(1, k)] + [target_end]
        built = []
        prev = a
        for t in marks:
            e = rel(prev, t)
            built.append({"t0": round(prev, 3), "t1": round(t, 3),
                          "energy": round(e, 3), "tier": tier_from_energy(e), "name": ""})
            prev = t
        tail = [dict(x) for x in segs[j + 1:]]
        if tail:
            tail[0]["t0"] = round(target_end, 3)
        segs = [dict(x) for x in segs[:i]] + built + tail
        hi_n = mid_n = lo_n = 0
        for m, x in enumerate(segs):
            t = x["tier"]
            if t == "high":
                hi_n += 1; x["name"] = "drop %d" % hi_n
            elif t == "mid":
                mid_n += 1; x["name"] = "中段 %d" % mid_n
            else:
                lo_n += 1
                x["name"] = ("前奏" if m == 0 else
                             "尾奏" if m == len(segs) - 1 else "过渡 %d" % lo_n)
        n = len(segs)
        i = i + k + 1
    return segs


def _density_penalty(f, bis, dt):
    """每段的「密度打折比例」（0 = 不打折，最大 `DENSITY_W`）。

    ★★★ 2026-11-06（用户：「Drop 1 怎么能有 0.8 呢？它听起来非常缓和」「加一个看看」）：

    只量总电平分不出「满编排」和「稀疏」——`Erebos` drop1 只比 drop4 低 3.3 dB，
    但它的 8 个频段**离差 7.0 dB**（低音薄、5~11kHz 也没开），而 drop4 只有 2.3 dB。
    听感上"缓"的就是这种**频段不均衡**。所以按离差打折：

        spread = std(各频段的 (段内 dB 均值 − 本曲该频段 95 分位))     # 单位 dB
        pen    = DENSITY_W × clamp((spread − DENSITY_REF_STD) / DENSITY_SPAN_STD, 0, 1)

    实测：满编排 drop 的 spread ≈ 1.2~3.1 dB ⇒ 折扣 0（**完全不动**）；
    `Erebos` drop1 spread 7.0 ⇒ 0.75 × 0.825 = **0.62**（降到 mid）；drop7 6.3 ⇒ 0.68。

    ⚠ 试过用「空洞深度 = −mean(最弱 3 段)」：对**本来低频就少**的曲子误判太狠 ——
      `Losin' A Control` 整首低频偏薄 ⇒ 每段都扣分，中位 0.72→0.42、3 个 drop 被降级。
      离差只看"这一段自身是否均衡"，同曲内才有区分度。
    ⚠ 参考值取**本曲自身**各频段的 95 分位 —— 每首歌的母带 / 编曲习惯不同，
      拿别的歌当基准会把整首歌误判成"薄"。
    ⚠ 拿不到 `Eband`（老特征 / 外部构造的 f）时返回全 0 ⇒ 行为与加这条之前完全一致。
    """
    nseg = max(0, len(bis) - 1)
    if nseg <= 0:
        return []
    eb = f.get("Eband") if isinstance(f, dict) else None
    if eb is None or dt <= 1e-9 or DENSITY_SPAN_STD <= 0:
        return [0.0] * nseg
    eb = np.asarray(eb, dtype=np.float64)
    nfr = eb.shape[0]
    if nfr < 16 or eb.shape[1] < 3:
        return [0.0] * nseg
    ref_db = 10.0 * np.log10(np.percentile(eb, 95, axis=0) + 1e-12)
    out = []
    for k in range(nseg):
        i0 = max(0, min(nfr - 1, int(bis[k])))
        i1 = max(i0 + 1, min(nfr, int(bis[k + 1])))
        # ⚠ 用**逐帧 dB 均值**，不要线性平均后再取 dB：稀疏段的空隙在 dB 尺度上就是
        #   "这段时间没内容"，线性平均会把空隙按 0 计入再抬回去，把离差压扁
        #   （实测 `Erebos` drop1 的 spread 从 7.0 掉到 4.0，折扣几乎归零）。
        lv = (10.0 * np.log10(eb[i0:i1] + 1e-12)).mean(axis=0) - ref_db
        spread = float(lv.std())
        pen = (spread - DENSITY_REF_STD) / DENSITY_SPAN_STD
        out.append(DENSITY_W * max(0.0, min(1.0, pen)))
    return out


def _seg_impl(f, dur=0.0, grid=None, win_sec=None, jump_thr=None,
                  merge_thr=None, min_sec=None, max_seg=None, refine_sec=0.0,
                  snap_beats=None, phrase_beats=None, phrase_grid=None,
                  trim_gate=True):
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
    # ★ 2026-11-05：边界/合并判据一律用**相对尺度**那条（见 `smooth_curve_rel`），
    #   `cur` 只留给最后算 `energy` 上报值。两条混用会让阈值语义错位。
    n = int(cur.size)
    dur_eff = float(dur) if dur and dur > 0 else n * dt

    # ---- 1. 局部落差：前 w 秒均值 - 后 w 秒均值，**两路取较大** ----
    # 路 1：STFT 频带能量（dB / 本曲中位数）—— 尺度"平"，抗母带压缩；
    # 路 2：RMS 响度（幅度 / 本曲中位数）—— 尺度"陡"，抓得住整体抬一格的台阶。
    # 取逐点绝对值更大的一路（`_dual_dif`，那里写了为什么不能取平均/取小）。
    # ⚠ 曲线截到 `n`（= `cur` 那条的长度）：理论上三条曲线同源同长，但
    #   `f` 是外部传进来的，宁可 `min` 一下也不能让广播在运行期炸。
    w = max(2, int(round(win_sec / dt)))
    cur_rel, _ = smooth_curve_rel(f)
    rms_rel, _ = rms_curve(f)
    if cur_rel is not None and int(cur_rel.size) < n:
        cur_rel = None
    if rms_rel is not None and int(rms_rel.size) < n:
        rms_rel = None
    dif_stft = _dif_win(None if cur_rel is None else cur_rel[:n], dt, win_sec)
    dif_rms = _dif_win(None if rms_rel is None else rms_rel[:n], dt, win_sec)
    if dif_stft is None:
        # 拿不到相对曲线（异常特征字典）⇒ 退回老口径（`cur` 是本曲归一化的 0~1）
        dif_stft = _dif_win(cur, dt, win_sec)
    dif = _dual_dif(dif_stft, dif_rms)
    if dif is None:                       # 三条都拿不到（曲太短）⇒ 单段收场
        return normalize([{"t0": 0.0, "t1": max(1.0, dur_eff),
                           "energy": float(np.mean(cur)), "tier": "low",
                           "name": "全长"}], dur_eff, grid, beats=snap_beats)

    # 合并 / 最小段长 / `_peak_like` 要用的「段均值」。
    # ★★ 2026-11-05：**合并判据挪到相对响度尺度**（`rms_rel`），不再用 `cur`（`_pct_norm`），
    #   而且门槛**随这首歌自己的分档门槛抬起**（`AUTO_MERGE_K`）。
    #   为什么必须换尺度（`Distorted Dimension` 现场，两种尺度都试过）：
    #     87 秒那个台阶峰值 **+44%（≈3.5 dB）**，但它是"曲体 → drop"的整体抬升，
    #     而曲体中部 36~48 秒本来就有一段和 drop 一样响（实测 rms 1.20~1.28）
    #     ⇒ 拿**整段均值**去比只剩 **0.20**（相对响度单位）；
    #     而 `AUTO_MERGE_THR=0.05` 在 `_pct_norm` 尺度上等于"本曲 p5~p95 量程的 5%"
    #     —— DD 量程 49 dB ⇒ 0.05 相当于 2.45 dB > 0.20 折算的 2.3 dB
    #     ⇒ 这条边界被当成"两边一样响"删掉（合并前边界 `[0, 117.5, 125.9]`，
    #     88 秒那条凭空消失，整首只剩 2 段 —— 正是用户报的那个）。
    #   现在的口径：门槛 = `max(AUTO_MERGE_THR, AUTO_MERGE_K × q_mid)`，再夹一道
    #   "不超过候选落差中位数"（见下面 `_mt` 的两段注释）。
    #   ⚠ `AUTO_MERGE_THR` 的**数值不变**（0.05），只是量纲从"归一化单位"变成
    #     "相对响度（1.0 = 本曲典型响度）"，并且只当**下限**用。
    #   ⚠ 段的 `energy` 上报值仍然来自 `cur`（见第 5 步），两者不能混。
    _mcur = rms_rel if rms_rel is not None else (cur_rel if cur_rel is not None else cur)
    cs = np.concatenate([[0.0], np.cumsum(_mcur)])

    def _wm(a, b):
        a = 0 if a < 0 else int(a)
        b = n if b > n else int(b)
        if b <= a:
            return 0.0
        return float((cs[b] - cs[a]) / (b - a))

    # ⚠ 试过「多尺度」（再加一个 win/3 的短窗取幅度更大者）来抓 8 拍假高潮，**实测撤掉**：
    #   Dust 那首的非整八拍段从 3 个涨到 5 个（12.26/151.01/162.26 这些段内起伏被短窗放大
    #   后挤进"强档"）。主窗本来就看得见假高潮的**上下两条边界**（只是幅度被稀释一半），
    #   够用了；短窗纯噪声。要再试必须拿一首**真有 8 拍假高潮**的歌做样本。

    # ---- 2. 候选边界：局部极大 → **用这首歌自己的分布分级** ----
    # ★★ 2026-10-29 用户："电子音乐大部分是四个八拍或者两个八拍一段…**不是必须**，
    #   万一有首歌在进 drop 前有八拍假高潮呢，**是加强算法**"。
    #   ⇒ 绝不写死"段落必须是 N 个八拍"。改用**自适应分级**（阈值随歌变，不是常量）：
    #     · 强边界（落差在自身 top 12%，`AUTO_Q_HI`）＝ 真 drop / **8 拍假高潮** / breakdown
    #       ⇒ **无条件保留**，不管它落在第几个八拍，也不许被后面的合并/预算删掉；
    #     · 中边界 ＝ 必须落在 2 个八拍（16 拍）线上 —— 电子音乐的常规段落起点；
    #     · 弱边界 ＝ 至少在八拍线上、且落差不能太小（否则就是段内起伏）。
    #   结构规整的歌边界会自然整齐，有奇招的歌也不会被裁掉。
    #   ⚠ 2026-11-05：中档门槛抬到与强档同高（都是 `AUTO_Q_HI`），见那个常量的注释。
    floor = max(0.03, jump_thr * 0.25)          # 只为滤掉数值噪声，不是"段落判据"
    # ★★ 2026-11-05：**曲首/曲尾 3 秒内不算候选，也不参与分位数统计**。
    #   为什么：母带开头/结尾的淡入淡出是全曲最大的"落差"（实测 `Pont Point`
    #   0.7s 处 106 dB、232.4s 处 125 dB，是正常段落的 **20 倍**），
    #   它们既不是段落边界（曲首本来就是边界），又会把 80/45 分位数顶高
    #   ⇒ 真边界反而排不进强档。旧代码靠 `minn` 事后并掉，但那已经是"先被顶高门槛"
    #   之后的事了。
    _edge_i = max(2, int(round(AUTO_EDGE_SEC / dt)))
    cand = [i for i in range(_edge_i, n - _edge_i)
            if abs(dif[i]) >= abs(dif[i - 1]) and abs(dif[i]) >= abs(dif[i + 1])
            and abs(dif[i]) >= floor]
    ga, gbl, gok = _grid_ctx(phrase_grid if phrase_grid is not None else grid)
    vals = np.array([abs(dif[i]) for i in cand], dtype=np.float64) if cand else np.zeros(0)
    if vals.size >= 5:
        q_hi = _pctl_low(vals, AUTO_Q_HI)
        q_mid = _pctl_low(vals, AUTO_Q_MID)
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
    #   判据：**每条 16 拍线**只要它自己的落差够、而且就是**附近最强的那条线**，
    #   就作为候选 —— 电子音乐的段落起点本来就在乐句线上，峰落在哪里是平滑窗的
    #   副作用，不该决定"这里算不算边界"。
    #   ⚠ 必须放在分位数算完之后：`q_hi/q_mid` 只能用**峰通道**统计，否则线通道密度
    #     会把门槛拉低（线点比峰点多）。
    #   ⚠ 线通道用自己的分位 `AUTO_Q_LINE`（不是 `q_mid`）：`_step_mag` 量的是
    #     **整个台阶**、`dif` 量的是**半个**，共用分位数等于让线通道灵敏度翻倍。
    #   ⚠ 门槛不能用 `dif`（±4 秒窗）：窗口比一个乐句还长，会把「半个乐句的平台」
    #     放大成边界（实测 218.44 处 dif=0.120 刚过线，但它两侧半句是 0.586/0.616，
    #     根本没有落差 ⇒ 切出 6.17 秒碎段，又被"最小段长"并给后面，drop 起点反而错）。
    #     改用「这条线两侧**各一个八拍**的均值差」——窗口 3.1 秒，正好一个乐句的一半，
    #     量的是"这里真的换段落了吗"。
    #   ★★ 2026-11-05：线通道的分数也要**两路取较大**，否则量纲对不上 ——
    #     `q_mid` 现在是合并后的相对尺度（比 `_pct_norm` 小 ~3.6 倍），
    #     若这里仍拿 `cur`（0~1 尺度）算分数，几乎每条乐句线都会过线
    #     ⇒ 满屏边界。`_step_mag` 与 `_dual_dif` 同一条口径（取绝对值大的那路）。
    def _step_mag(t):
        """`t` 两侧各一个八拍的均值差，两路取绝对值较大者（与 `dif` 同口径）。"""
        if gbl <= 1e-6:
            return 0.0
        s1 = _phrase_step_score(cur_rel if cur_rel is not None else cur,
                                dt, t, gbl, 1.0)
        if rms_rel is None or AUTO_DUAL_MODE == "stft":
            return s1
        s2 = _phrase_step_score(rms_rel, dt, t, gbl, 1.0)
        if AUTO_DUAL_MODE == "rms":
            return s2
        return s2 if abs(s2) > abs(s1) else s1

    _line = []                       # 线通道捡回来的候选（见下面 2b）
    _q_line = float(q_mid)
    if gok and gbl > 1e-6:
        _step = gbl * max(1, int(AUTO_PHRASE_BEATS))
        if _step > 1e-6:
            _k = int(np.floor((0.0 - ga) / _step))
            _all = []
            while True:
                _t = ga + _k * _step
                if _t > n * dt:
                    break
                if _t > 0.0:
                    _i = int(round(_t / dt))
                    if 0 < _i < n - 1:
                        _all.append((abs(_step_mag(_t)), _i))
                _k += 1
            if _all:
                # ★★ 2026-11-05：线通道也要**局部极大**（跟峰通道一个道理）。
                #   为什么：`_phrase_step_score` 的窗口是两侧各一个八拍，**跨在台阶上的
                #   相邻两条线都会拿到分**（台阶在 68 秒时，64 秒那条线两侧是「炸/炸」、
                #   72 秒那条是「静/静」，两条都拿到差不多的分）⇒ 线通道同时把 64 和 72
                #   塞进来，真边界 68（峰通道找到的）反被挤掉。
                #   只留"左右两条线的分数都比它小"的线，台阶就只能落在**最近的那条线**上。
                _sv = np.array([_v for _v, _i in _all], dtype=np.float64)
                if _sv.size >= 5:
                    _q_line = _pctl_low(_sv, AUTO_Q_LINE)
                _q_line = max(_q_line, float(jump_thr))
                for _k2, (_v, _i) in enumerate(_all):
                    if abs(_v) < _q_line or abs(dif[_i]) < floor:
                        continue
                    _pv = abs(_all[_k2 - 1][0]) if _k2 > 0 else 0.0
                    _nv = abs(_all[_k2 + 1][0]) if _k2 + 1 < len(_all) else 0.0
                    if abs(_v) >= _pv and abs(_v) >= _nv:
                        _line.append(_i)
                if _line:
                    cand = sorted(set(cand) | set(_line))

    _line_set = set(_line)

    def _snap_peak(i, v):
        """★★★ 2026-11-06（用户：「中段4前面那个 16 拍不能单独切出来吗？明显是一个下落」）：
        峰通道的「峰偏线」补偿。

        病根：`dif` 是 4 秒窗的均值差，峰会被平滑推离真正的段落线最多 ±2 拍。
        实测 `Shape of Sense` 的 448 拍（143.38s，16 拍下凹段起点）：
          · 峰在 **449.3 拍**（dif = −0.397），离 448 线只有 1.3 拍 = **0.41 秒**
          · `_on_line` 容差是 **0.35 秒** ⇒ 判成"不在线上" ⇒ 整个 16 拍段消失
        而**不能全局放宽容差**（试过 0.35→0.80：平均段数没变，但 148.45 的修复被破坏）。

        ⇒ 单开一条通道：峰在 **±2 拍**内、且**线本身的落差**仍有峰的 60% 时，
          把边界落在**线上**（不是落在峰上）—— 段落边界只该落在八拍头，这条不能松。
        """
        if not gok or gbl <= 1e-6:
            return None
        if not gok or gbl <= 1e-6:
            return None
        # ⚠ 分两档：
        #   · **中档以上**（v >= q_mid）允许吸到**小节线（8 拍）**，±1 小节 —— 段落也
        #     可能落在奇数小节（`Petrified Forest` 的 bar 13 / 41），离 16 拍线 8 拍
        #     以上，按 16 拍线永远吸不到。
        #   · **弱档**仍走原来的 ±2 拍 / 16 拍线 —— 实测放开后 `Pont Point` 17→21、
        #     `Liminal Link` 17→22、`Feel the Devotion` 18→22，段数普遍涨 2~5，太碎。
        _t = i * dt
        # ⚠ 目标仍是 **16 拍线**，但容差放到 **±1 小节（8 拍）**：峰落在奇数小节时
        #   离 16 拍线正好 8 拍，原来的 ±2 拍永远够不到（`Petrified Forest` bar 37/41）。
        #   ⚠ 试过"直接吸到小节线（允许奇数小节）"：那会把 `Pont Point` 17→21、
        #     `Liminal Link` 17→22、`Feel the Devotion` 18→22，段数普遍涨 2~5，太碎 ⇒ 撤回。
        _step, _tol = gbl * max(1, int(AUTO_PHRASE_BEATS)), gbl * 8.0
        if _step <= 1e-6:
            return None
        # ★★★ 2026-11-06 第二次（用户：「`Petrified Forest` 中段3 也应该切成 3 个 32 拍」）：
        #   吸附范围从 **±2 拍** 放宽到 **±1 小节（8 拍）**。
        #   为什么：这首歌的段落同样落在**奇数小节**（bar 13 / 37 / 41），峰离最近的
        #   16 拍线正好 8 拍 —— 只能吸 ±2 拍时，这些峰一律被判"不在线上"而丢掉。
        #   实测 bar 41（154.78s）台阶 −0.461（RMS 1.365→0.903，四小节一片安静），
        #   bar 13（49.78s）台阶 +0.441，两个都因此切不出来。
        #   ⚠ 仍然**只允许吸到小节线上**（不落在峰上），并且要求线本身的落差 >= 峰的 0.8
        #     （范围放宽了，证据要求相应提高，留 0.8 而不是 0.6）。
        _st = ga + round((_t - ga) / _step) * _step
        if _st <= 0.0 or abs(_st - _t) > _tol + 1e-9:
            return None
        _si = int(round(_st / dt))
        if not (0 < _si < n - 1) or _si == i:
            return None
        if abs(dif[_si]) < v * 0.8:
            return None
        return _si

    cand2 = []
    for i in cand:
        v = abs(dif[i])
        t = i * dt
        if i in _line_set:
            # 线通道：**位置（16 拍线）+ 能量（两侧各一个八拍）双重证据**，已经过线，
            # 不再套 peak 的分档门槛（它的峰可能落在离线 1.86 拍处，那是平滑窗的副作用）。
            cand2.append(i)
            continue
        if v >= q_hi:
            cand2.append(i)                     # 强：无条件（假高潮就在这一档）
        elif v >= q_mid:
            if (not gok) or _on_line(t, ga, gbl, AUTO_PHRASE_BEATS):
                cand2.append(i)                 # 中：必须 2 个八拍线
            else:
                _si = _snap_peak(i, v)
                if _si is not None and abs(dif[_si]) >= q_mid * 0.8:
                    cand2.append(_si)           # 中：峰偏线 ⇒ 落在线上（见 _snap_peak）
        else:
            if not gok:
                if v >= jump_thr:
                    cand2.append(i)
            elif v >= jump_thr * 0.7:
                if _on_line(t, ga, gbl, AUTO_PHRASE_BEATS):
                    cand2.append(i)             # 弱：也要 2 个八拍线（奇数八拍不是段落起点）
                else:
                    _si = _snap_peak(i, v)
                    if _si is not None:
                        cand2.append(_si)       # 弱：峰偏线 ⇒ 落在线上（见 _snap_peak）
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
    if os.environ.get("SEC_TRACE"):
        print("[trace] 候选后:", [round(x*dt,2) for x in bnd])
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
    #: 合并门槛（见上面 `AUTO_MERGE_K` 的说明）
    _mt = max(float(merge_thr), AUTO_MERGE_K * float(q_mid))
    # ★ 再夹一道上限：**门槛不许超过"这首歌候选落差的中位数"**。
    #   为什么：`q_mid` 是分位数，遇到"几个巨大台阶 + 一堆小起伏"的双峰分布会被抬到
    #   跟真台阶一样高（`_test_phrase_offset` (a)：候选只有 [0.08, 0.44, 0.44, 2.56,
    #   2.56, 6.89, 6.93]，p85 = 6.9 ⇒ 门槛 4.14，而 68 秒那个真台阶的整段均值差只有
    #   3.8 ⇒ **被合并掉，drop 结尾从 68 移到 72**）。合并只该吃掉"比这首歌的典型
    #   落差还小"的边界，没资格吃掉中位数以上的。
    if vals.size:
        _mt = min(_mt, max(float(merge_thr), float(np.median(vals))))
    # ★★ 2026-11-05：**强档边界不许被合并掉**。
    #   第 2 步的分档已经说了"强：无条件保留"，但后面的合并循环又会按"两边一样响"
    #   把它删掉 —— 自相矛盾，而且实测就坏在这：`Distorted Dimension` 87 秒是全曲
    #   **最强**的一条边界（dif 0.299 ≥ q_hi 0.206），可它的"整段均值差"只有 0.187
    #   （曲体中部 36~48 秒本来就有一段和 drop 一样响，把均值摊薄了）
    #   ⇒ 被 < 0.195 的门槛吃掉 ⇒ 整首只剩「drop 1 + 尾奏」两段，正是用户报的那个。
    #   合并的本职是**去碎片**（弱/中档里"两边其实一样响"的那些），强档是"这首歌自己
    #   认定的真实突变"，没有立场再被均值判据否掉。
    _strong = set(i for i in bnd if abs(dif[i]) >= q_hi)
    #: 线通道的边界按**时刻**保护，不按帧下标：同一个位置既有"峰"又有"乐句线"时
    #: 去重后留下的是峰（`Rolling Contact` 150.31 的峰在 150.6），而 `_line` 里存的是
    #: 线（150.31）—— 按下标比就保护不到，150.31 那条边界又被删掉了。
    _line_t = [float(i) * dt for i in _line]

    def _on_phrase_line(t):
        """`t` 是否落在某条"真有落差"的 16 拍线上（容差 `AUTO_LINE_TOL` 秒）。"""
        for x in _line_t:
            if abs(float(t) - x) <= AUTO_LINE_TOL:
                return True
        return False

    while changed and len(bs) > 2:
        changed = False
        for k in range(1, len(bs) - 1):
            if bs[k] in _strong:
                continue                      # 强档：无条件保留（见上）
            # ★★★ 2026-11-06（用户：「Shape of Sense 中段 5 前 16 拍看看能不能切出来」）：
            #   **`dif` 明显够强的边界也不许被合并**。
            #   为什么：合并判据 `_wm` 是**整段均值差**，段一长就被拉平；而 `dif` 是 4 秒
            #   窗的局部突变，量的是"这里有没有换段落"。实测 148.45s：
            #     dif = +0.443（强），但两侧段均值差只有 **0.1489**，
            #     而 `_mt` 恰好 = 0.149 ⇒ 差 0.0001 被吃掉 ⇒ 中段 5 前 16 拍消失。
            #   规则：`|dif| >= q_mid * 0.8` 的边界不许合并（0.8 是留一点余量，
            #   因为两套判据的量纲本来就不同：一个看 4 秒窗、一个看整段）。
            if abs(dif[bs[k]]) >= q_mid * 0.8:
                continue
            left = _wm(bs[k - 1], bs[k])
            right = _wm(bs[k], bs[k + 1])
            if abs(right - left) < _mt:
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
        # ★★★ 2026-11-06（用户：「Petrified Forest 中段3 也应该切成 3 个 32 拍」）：
        #   合并短段时**必须保留 |dif| 更强的那条边界**，不能一律删后面那条。
        #   实测那首：候选里有 bar36(135.7s, 假) 和 bar37(139.9s, 真, dif 0.496)，
        #   两者只隔 4.2 秒 ⇒ 构成短段 ⇒ 旧代码 `del bs[b]` 删掉了**后面**的
        #   139.9（真台阶），留下 135.7 ⇒ 中段3 里那条 +0.509 的大跳变整条消失，
        #   用户看到的就是"波形明显不一样却没切"。
        if a > 0 and b < len(bs) - 1 and abs(dif[bs[a]]) < abs(dif[bs[b]]):
            del bs[a]                       # 前面那条弱 ⇒ 删前面，留后面
        else:
            del bs[b]
    # 段数封顶（两道：先是"时长预算"，再是老的上限 `AUTO_MAX_SEG`）
    # ★★ 2026-11-05：新增**时长预算**这一道。用户的原话是「一首 2 分钟的歌每 8 秒换一次
    #   素材」，而"切碎"的本质是**段数相对曲长太密**，跟单条边界的强弱无关 ——
    #   实测 `Feel the Devotion`（287 秒）有 **18 个 ≥4 dB** 的真实落差，
    #   任何"落差门槛"都留 12 段以上；而 `Rolling Contact` 的用户指定边界里
    #   最弱的一条只有 2.9 dB —— 门槛一抬就把它（和 DD 的 87 秒）一起杀掉。
    #   ⇒ 判据分两层：**能不能成段**看落差（上面那一整套），**允许有几段**看曲长。
    #   超出预算时删"两边最像"的那条边界（= 合并判据的延续），
    #   而不是老代码的"删最短的段" —— 删最短的段会在 drop 中间砍一刀。
    if os.environ.get("SEC_TRACE"):
        print("[trace] 合并+最小段长后:", [round(x*dt,2) for x in bs], "budget=", max(2, int(round(dur_eff / AUTO_SEG_SEC))))
    _budget = max(2, int(round(dur_eff / AUTO_SEG_SEC)))
    #   ⚠ 代价**一次性算好再按从小到大删**，不能"删一条再重算"：后者会级联 ——
    #     `Rolling Contact` 先删了 162.70，于是 150.31 的左邻变成 0.63、右邻变成
    #     150~187 的平均 0.65 ⇒ 看起来"两边一样响"⇒ 接着被删，而它其实是
    #     用户 2026-10-30 点名的"两乐句的大声响"起点。一次性算代价时它的右邻
    #     还是 0.90 的真台阶，代价很大，轮不到它。
    # ★★★ 2026-11-06（用户：「中段4前面那个 16 拍不能单独切出来吗？明显是一个下落」）：
    #   代价**必须用边界两侧的局部窗口，不能用整段均值**。
    #   病根：`_wm(bs[k-1], bs[k])` 是整段平均，段一长就被摊平。实测 `Shape of Sense`
    #   448 拍（143.38s，16 拍下凹段）：左边那一段有 20 秒、横跨 mid+high 两层 ⇒ 整段
    #   均值被拉到 ~0.70，与右边 0.567 只差 0.13 ⇒ 看起来"两边差不多" ⇒ 被预算砍掉，
    #   而它局部的真实台阶是 0.962 → 0.567（差 **0.40**，用户听到的"明显下落"）。
    #   ⇒ 窗口取边界两侧各 **2 个八拍**（与 `_phrase_step_score` 同口径，段落的最小单位），
    #     段比窗口短时自然退化成整段 —— 短段本来就没有摊平问题。
    #   ⚠ 不要用"加白名单保护"的办法修：预算目标段数是固定的，保护一条就会挤掉
    #     另一条（实测保护 448 之后，`Feel the Devotion` 234.6 这条 dif 0.579 的
    #     真 drop 起点被挤掉了）。改代价口径才是治根。
    #   ⚠⚠ 单位：`_wm(a, b)` 收的是**帧索引**（内部用累加和 `cs[b]-cs[a]`），不是秒！
    #     第一版写成 `_win = gbl * PHRASE_BEATS * 2.0`（秒）⇒ 5.05 被当成 5 帧 ≈ 0.1 秒
    #     ⇒ 窗口退化成噪声，代价排序没变、448 照样被砍。必须除 `dt` 换成帧。
    _win = int(round((gbl * PHRASE_BEATS * 2.0) / dt)) if gok else int(round(16.0 / dt))
    _cost = []
    for k in range(1, len(bs) - 1):
        _l0 = bs[k] - min(bs[k] - bs[k - 1], _win)
        _r1 = bs[k] + min(bs[k + 1] - bs[k], _win)
        _cost.append((abs(_wm(_l0, bs[k]) - _wm(bs[k], _r1)), bs[k]))
    _cost.sort()
    #   ⚠ **乐句线边界不进候选池**：那是"位置（16 拍线）+ 能量（两侧各一个八拍）"
    #     双重证据，正是用户 2026-10-30 点名的那三条（88.38 / 150.31 / 162.70，
    #     最后一条只有 2.9 dB —— 按"删最弱"必中枪，按"当帧 dif"也排不进前列）。
    #     预算只砍其它边界里"两边最像"的，砍到没有可砍的就停 ——
    #     宁可多一段，也不猜着删用户要的边界。
    _nn = max(0, (len(bs) - 1) - _budget)
    _drop = []
    for _c, _t in _cost:
        if len(_drop) >= _nn:
            break
        if _on_phrase_line(_t * dt):
            continue
        # ★★★ 2026-11-06：**两侧本地均值差 >= `_mt` 的边界不删** —— 和合并阶段同一把尺子。
        #   `_mt` 就是"这两边算不算不一样"的门槛（合并阶段用它决定要不要并掉），
        #   既然合并都认为"不一样"，预算就没资格因为它排在代价前面而删掉它。
        #   实测 `Shape of Sense`：预算目标 9 段、实际 17 段，要删 8 条，而便宜的边界
        #   几乎全在 16 拍线上（被上面这条 `_on_phrase_line` 跳过）⇒ 一路往下砍，把
        #   143.38（448 拍，用户点名的"明显下落"，本地台阶 0.962→0.567）砍了。
        #   ⚠ 代价：目标段数达不到时宁可多几段（这条文件里一贯的口径）。
        if _c >= _mt * 2.0:
            continue
        # ★★★ 2026-11-06（用户：「中段三去掉前面 24 拍…这一段就算 drop」「Drop 2 应该
        #   是 32 拍」）：**强档边界也不许被预算砍**。
        #   合并循环里已经有 `if bs[k] in _strong: continue`（见上面那段注释），
        #   但预算这里是另一段代码，**漏了这层保护** —— 实测 `The Core Mind`：
        #     74.09s 是强档（dif 0.751，全曲第 2 强，RMS 从 0.62 跳到 1.08 = +74%），
        #     预算要删 4 条，而代价最小的那几条几乎全是乐句线（46.56/66.78/92.02/
        #     132.45/112.06/21.34 都被 `_on_phrase_line` 跳过）⇒ 轮到它时正好第 4 个
        #     ⇒ **把真 drop 起点删了**，整首变成 66.78~92.02 一段 80 拍的中段，
        #     用户看到的正是"中段三前面那 24 拍比后面弱"却没被分出来。
        #   ⇒ 预算只砍"弱/中档里两边最像"的，没资格砍这首歌自己认定的突变。
        #   ⚠ 代价：强档多时可能删不到预算 → 宁可多几段，也不猜着删用户要的边界。
        if _t in _strong:
            continue
        _drop.append(_t)
    if _drop:
        _drop = set(_drop)
        bs = [x for k, x in enumerate(bs) if k == 0 or k == len(bs) - 1 or x not in _drop]
    while len(bs) - 1 > max_seg:
        lens = [(bs[k + 1] - bs[k]) for k in range(len(bs) - 1)]
        k = int(np.argmin(lens))
        j = (k - 1) if (k == len(bs) - 2) else (k + 1)
        a, b = min(k, j), max(k, j)
        del bs[b]

    if os.environ.get("SEC_TRACE"):
        print("[trace] 预算后:", [round(x*dt,2) for x in bs])
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
    # ⚠ 2026-11-06：「谷底插一刀」( `_insert_valley_seg` ) **暂不启用**。
    #   实现效果符合用户想法（`The Core Mind` 288~296 那 8 拍单独成 mid 段），
    #   但实测 `Rolling Contact` 会在 162.7 后面插出 162.7~165.8（3.1 秒）的碎段，
    #   `_test_seg_line` 的「88~187 区间没有 1 个乐句的碎段」当场红。
    #   收紧到"严格距原边界 8 拍 + 落在八拍网格上"之后仍然红 ⇒ 说明还有别的路径
    #   在插，光看这一段定不下来。要启用必须先把 `Rolling Contact` 的边界轨迹打出来查。

    # ---- 4.6) 段首过门 / 段尾衰减修剪（见 `GATE_*`）----
    # ★★ 2026-10-30 用户（`8 緋災斬月`「23.7 到 71.6 是第一段 drop」）：
    #   量化后的边界是 16.45 / 74.06，但用户要的是 23.65 / 71.66（也是 8 拍线）。
    #   多出来的那 7.2 秒是"进 drop 前的一冲"、2.4 秒是"衰减下坡"，都不该算 drop 主体。
    #   ⚠ 必须在量化**之后**做：交给 16 拍量化会把 23.65 硬拉到 26.05。
    _trimmed = []
    if trim_gate:
        _min_seg = (gbl * AUTO_PHRASE_BEATS) if gok else max(AUTO_MIN_SEC, 6.0)
        edges, _trimmed = _trim_gate_edges(edges, cur, dt, gok, ga, gbl, dur_eff)
        # ⚠ 只有**真的修剪过**才合并碎段。无条件合并会把原本"尖峰豁免"的短段
        #   （进 drop 前的 8 拍假高潮，`_peak_like` 特意保下来的）一起吃掉 ——
        #   实测那样会改掉全库 57/91 首，且方向是"少一个假高潮"，是回归。
        if _trimmed:
            _mk = []
            for _a, _b in _trimmed:
                _mk.append(float(_a))
                _mk.append(float(_b))
            edges = _drop_short_seg(edges, cur, dt, _min_seg, only_touching=_mk)
        edges[0] = 0.0
        edges[-1] = dur_eff

    # ---- 5. 段的能量与类型：段均值 → 三分类，**只有最高类算 high** ----
    # ⚠ 为什么不是二分类：段均值的分布是连续的（这首歌 9 段：
    #   0.176/0.312/0.478/0.603/0.641/0.790/0.861/0.956/0.975），k=2 会切在
    #   密度最低的 0.6~0.8 之间 ⇒ 主歌（0.641）被判成 high，而用户明确说
    #   35.7 之后是低能量段。三分类的中心落在 0.25 / 0.61 / 0.90，
    #   取最高类（0.861/0.956/0.975）当 high，主歌 0.641 自然落到 low。
    #   先验也支持这么做：一首歌里 drop 是少数、铺垫和 breakdown 是多数。
    # ★ 段的能量必须用**量化之后**的边界重算：旧代码用 `bs[k]:bs[k+1]`（量化前的
    #   帧索引），边界被吸到乐句线之后两边就对不上了（能量会算到相邻段里去）。
    # ★ 2026-11-05：按「4 个八拍一段」的音乐结构补齐能量抓不到的段尾。
    #   用**短窗**（2 秒）单独算一条落差 —— 主判据那个 4 秒窗会把渐强过渡稀释掉
    #   （实测 68.63s：4 秒窗 +0.049、2 秒窗 +0.088，而门槛是 0.169）。
    #   ⚠ 必须放在 `_quantize_phrases` / `_drop_short_seg` **之后**：那两步会把边界
    #     吸到网格线上、还会合并短段，插早了会被它们改掉。
    _short_dif = _dif_win((rms_rel[:n] if rms_rel is not None else None), dt, 2.0)
    if _short_dif is not None:
        edges = _fill_phrase_quads(edges, _short_dif, dt, dur_eff, ga, gbl, gok)

    # ---- 4.7) 按小节重新落位（见 `_resnap_to_bar`）----
    #   ★★★ 2026-11-06（用户：「Witchcraft Butterfly 从中段二开始全部都算错了，都偏差了 8 拍」）：
    #     实测那首（BPM 128）：真实跳变在 **296 拍（139.8s，能量开始下落）** 和
    #     **328 拍（154.8s，能量回升）**，而程序切在 **304 / 320** —— 两个边界被
    #     **向内各挤了 8 拍**，中间 32 拍的下凹被压成 16 拍。
    #     病根：4.5 的 `_quantize_phrases` 只允许落在 **2 个八拍（16 拍）= 偶数小节**的
    #     整数倍上；这首歌的段落全在 **奇数小节**（9/21/37/41/53/69）⇒ 最近的那条
    #     16 拍线正好在 8 拍之外，方向还取决于峰值在左还是右（所以有早有晚）。
    #     ⇒ 量化之后再给每条边界一次"±1 小节"的重新落位机会：**只看这一小节左右各
    #       2 个八拍的能量台阶**，明显更陡就搬过去。
    #     ⚠ 它只搬位置、不增不减边界（段数不变），所以不会引发预算/合并的连锁重排。
    #   ⚠ **只跑一遍**：跑第二遍会拿"已改过的邻居"再算结构分，实测把 121.03 又拉回
    #     117.28、把 222.28 拉到 59 小节（段长变 3/10），反而更碎。
    edges = _merge_weak_bar(edges, cur, dt, ga, gbl, gok)
    edges = _resnap_to_bar(edges, cur, dt, ga, gbl, gok)
    edges = _snap_strong_step(edges, cur, dt, ga, gbl, gok)
    edges = _regularize_flat(edges, cur, dt, ga, gbl, gok)
    edges = _cap_segment_beats(edges, cur, dt, ga, gbl, gok)
    edges = _merge_head_tail(edges, ga, gbl, gok)
    edges = _split_contrast_halves(edges, cur, dt, ga, gbl, gok)
    # ★ 重落位之后再补"段内最强台阶"这一刀（插完定稿，不参与重排）
    edges = _cut_strong_bar(edges, _mcur, dt, ga, gbl, gok)
    # ★ 再看段首有没有"下降沿八拍"（形状判据，见 `_cut_ramp_bar`）
    edges = _cut_ramp_bar(edges, _mcur, dt, ga, gbl, gok)
    # ★ 最后按**音色（高频占比）**补一刀：电平没变但亮度变了的那种（见 `_cut_timbre_bar`）
    edges = _cut_timbre_bar(edges, f, dt, ga, gbl, gok)
    edges = _bridge_drop_fix(edges, cur, dt, ga, gbl, gok)
    edges = _bridge_mid_platform_fix(edges, cur, dt, ga, gbl, gok)

    bis = [0]
    for _e in edges[1:-1]:
        _i = int(round(float(_e) / dt)) if dt > 1e-9 else 0
        bis.append(max(bis[-1] + 1, min(n - 1, _i)))
    bis.append(n)
    # ★★★ 2026-11-06（用户：「Drop 1 怎么能有 0.8 呢？它听起来非常缓和」「整体能量都偏高了」）：
    #   上报能量改用**真实响度（RMS）**，并按 **dB 标定**，不再用 `cur`（STFT 幅度、5%/95%
    #   分位拉伸过的曲线）。
    #   实测 `Erebos`：drop1 的真实 RMS 只有最响段的 **0.684（−3.3 dB）**，但 `cur` 的
    #   比值是 0.869，再经 `**1.5` 压缩后读成 **0.81** —— 差距被抹平了，用户一眼看出不对。
    #   而 `cur` 也不是不能用：它是**检测/合并**判据（`dif`）的曲线，那里比的是相对变化，
    #   分位拉伸反而有好处。**"用来切段的曲线"和"用来显示能量的曲线"本来就该分开。**
    #   ⇒ 能量 = `1 + 20·log10(本段 RMS / 本曲最响段 RMS) / ENERGY_DB_SPAN`，压到 0~1。
    #     dB 跨度 12 ⇒ 比最响段低 12 dB 就算 0；`Erebos` 的 drop1 落在 0.73、
    #     中段 1 落在 0.37、尾奏 0.24 —— 和听感对得上（原来是 0.81 / 0.50 / 0.12）。
    # ⚠ 试过用 `_mcur`（rms_rel）做绝对能量：**行不通** —— 它是按本曲中位数归一化的
    #   （典型段 ≈1.0、最响段 1.5），拿它算"相对最响段的 dB"会把全库中位数从 0.68 顶到
    #   **0.76**、63% 的段变成 high。绝对响度只能用 `cur`（5%~95% 分位铺满 0~1）。
    means = [float(cur[bis[k]:bis[k + 1]].mean()) for k in range(len(bis) - 1)]
    # ★ 密度因子（见 `_density_penalty`）：电平够响但频段有洞的段按比例打折。
    _dens = _density_penalty(f, bis, dt)
    e_rep = [_clamp01(float(e)) ** ENERGY_GAMMA * (1.0 - _dens[k])
             for k, e in enumerate(means)]
    tiers = [tier_from_energy(e) for e in e_rep]
    # ★★★ 2026-11-06（用户：「这首歌这些中段实际上都算是 drop，现在识别得有点太低了」）：
    #   绝对门槛 0.72 对"整首压得比较平"的歌不友好：`End Of The Earth` 最响段只有 0.78，
    #   紧跟其后、只差 **1.5 dB** 的 drop（0.66）被判成 mid，可听起来就是 drop。
    #   ⇒ 补一条**本曲相对**判据：比本曲最响段低不到约 1.5 dB（≥0.84×）且 ≥0.60 的段也判 high。
    #   ⚠ 0.84 是挑过的：8 首抽样里只有 `End Of The Earth` 那种"最响 0.78 / 次响 0.66"
    #     会被拉上来；`Extra Pain`（0.85 → 0.68）、`揽月`（0.96 → 0.70）这些真主歌不受影响。
    _mx = max(e_rep) if e_rep else 0.0
    if _mx > 0:
        for _k, _e in enumerate(e_rep):
            if tiers[_k] != "high" and _e >= 0.60 and _e >= 0.84 * _mx:
                tiers[_k] = "high"
    # 兜底：一首歌总要有"最炸的那一段"。整首都很平 / 母带很轻的歌，最高也到不了 0.80，
    # 若就这样判成"没有 drop"，引擎永远进不了高动态池（旧实现"最高类算 high"自带这个性质）。
    if tiers and "high" not in tiers:
        tiers[int(np.argmax(e_rep))] = "high"
    # ★★ 2026-10-30（用户 `8 緋災斬月`「23.7 到 71.6 是第一段 drop」）：
    #   修剪掉"过门/衰减"之后，这首**两段主体**都是 0.63（e_rep），绝对阈值 0.75
    #   一个都够不到，只能靠"兜底取最大"给一段 high —— 而用户明确说第一段也是 drop。
    #   判据：**被修剪过的主体段**（它两端是按结构切出来的）且 e_rep 已经是全曲最高档
    #   （差 ≤0.03）⇒ 判 high。⚠ 只对修剪过的段生效：没修剪的歌保持绝对阈值原样，
    #   免得把 `Outburst Oxygen` 那种"中段 0.73 / drop 0.83"的中段也拉成 high。
    if _trimmed and e_rep:
        # ★★ 2026-11-01：`_mx` 必须剔除"修剪出来的过门/衰减小段"，且这些 stub 不许判 high。
        #   为什么（`8 緯災斩月` 现场，用户 2026-10-30 原话「23.7 到 71.6 是第一段 drop」）：
        #   那首歌 4.8 秒的"进 drop 前的一冲"被 `_trim_gate_edges` 剪出来自成一"段"，
        #   它的均值 0.841 **高于**真 drop 主体（23.65–71.67，0.738）—— 它是一段持续上扬的 riser，
        #   短窗里全是峰值。于是：
        #     · 绝对阈值(0.80) 先把过门判成 high ⇒ 它就成了 `drop 1`；
        #     · 兜底规则想救主体，却拿 `_mx = 0.841` 去比（门槛 0.811 > 0.738）⇒ 救不回来
        #       ⇒ **48 秒的真 drop 掉成「中段」**，4.8 秒的过门成了 drop。
        #   判据：stub ＝**又短又贴着某个 `_trimmed` 主体的外侧**（段首过门 `[t0_orig, nt0]` /
        #   段尾衰减 `[nt1, t1_orig]`）。两个条件都要：
        #     · "短"（≤ 2 个八拍，带 1e-3 容差）挡掉"整段被剪掉一点"的大段 —— 只按"贴着"判，
        #       会把 Dust 那种"尾巴被修剪的正常 drop"（76.02–136.02，60 秒）也算成 stub，
        #       实测那样会误降 21 段 / 16 首（含 Dust 的真 drop）。
        #     · 容差必须给：緯災斩月那 4.8 秒正好是 16.00 拍，秒数上比 `gbl*16` 多 0.7 毫秒。
        _min_len = (gbl * AUTO_PHRASE_BEATS) if gok else max(AUTO_MIN_SEC, 6.0)
        _stub = []
        for k in range(min(len(tiers), len(edges) - 1)):
            _e0, _e1 = float(edges[k]), float(edges[k + 1])
            _is = False
            if (_e1 - _e0) <= _min_len + 1e-3:
                for _a, _b in _trimmed:
                    if abs(_e1 - float(_a)) < 0.05 and _e0 < float(_a) - 0.05:
                        _is = True          # 段首过门：[18.85, 23.65] 这种被剪出去的头
                    elif abs(_e0 - float(_b)) < 0.05 and _e1 > float(_b) + 0.05:
                        _is = True          # 段尾衰减：[71.67, 74.07] 这种被剪出去的尾
            _stub.append(_is)
        _big = [e for k, e in enumerate(e_rep) if k >= len(_stub) or not _stub[k]]
        _mx = max(_big) if _big else max(e_rep)
        for k in range(min(len(tiers), len(edges) - 1)):
            if tiers[k] == "high":
                continue
            try:
                hit = any(abs(edges[k] - a) < 0.05 and abs(edges[k + 1] - b) < 0.05
                          for a, b in _trimmed)
            except Exception:                                  # noqa: BLE001
                hit = False
            if hit and e_rep[k] >= _mx - 0.03:
                tiers[k] = "high"
        # 修剪出来的过门/衰减**永远不是 drop**：它是"进 drop 前的一冲"/"drop 后的下坡"，
        # 判档只该看主体。⚠ 只降 high→mid，不碰 low（低/中按绝对阈值来，本来就对）。
        for k in range(min(len(tiers), len(_stub))):
            if _stub[k] and tiers[k] == "high":
                tiers[k] = "mid"

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
    norm = normalize(out, dur_eff, grid, beats=snap_beats)
    # ★ 3+5 个八拍的连续 drop → 4+4（必须在 normalize 之后，拍号才都是整数格）
    norm = _align_transition_and_climax(norm, cur, dt, ga, gbl, gok)
    norm = _shift_odd_chain(norm, cur, dt, ga, gbl, gok)
    norm = _fix_drop_bars_segs(norm, cur, dt, ga, gbl, gok)
    # ★★ 2026-11-07：假高潮 8 拍之后的整条 drop 链按「假高潮结束点」重新起算
    norm = _rebuild_after_false_climax(norm, cur, dt, ga, gbl, gok)
    # ★★ 通用兜底：局部窗口内把「8 拍奇数倍段」规整成 16 拍整数倍乐句
    norm = _regularize_phrase_lengths(norm, cur, dt, ga, gbl, gok)
    # ★★★ 末尾三刀按「一刀一验」串起来：每一刀跑完都要比一次**整首**的
    #   「8 拍奇数倍段」条数（含首尾），变多就整刀拒绝并保留上一版。
    #   为什么必须这样兜：三刀各自都有局部自证，但它们是**串联**的 ——
    #   前两刀改完的表会让第三刀看到不同的窗口，实测 `Cursed Haze` 就是这样
    #   从 0 变成 1（局部都合格，全首净变坏）。
    for _pass in (
        # 2026-11-07（用户 `Unlock Your Mind`「中段 5 后面少了 8 拍」）：
        #   两侧一样响、听不出来的假边界，挪一格到 32 拍格上。
        _snap_weak_boundary,
        # 2026-11-07（用户 `Devouring Saturnus`「过渡5多了后面8拍 / Drop1 前后各少了8拍 /
        #   Drop4 从开头这 64 拍都是 Drop」）：边界站在没台阶的地方 ⇒ 搬到真台阶那格。
        _relocate_weak_boundary,
        # 2026-11-07（用户连报 9 首「X 前面那 8 拍其实是一个单独的过渡」）：
        #   把「被并进相邻大段的独立短过渡」切出来，并把后链按 32 拍重新落位。
        _insert_break_transition,
    ):
        _cand = _pass(norm, cur, dt, ga, gbl, gok)
        if _n_odd_all(_cand, gbl) <= _n_odd_all(norm, gbl):
            norm = _cand
    return norm



_LAST_FOLD_ANCHOR = None


def take_folded_anchor():
    """取出"上一次 auto_segments 折过的锚点"（没有则 None），取完即清。

    调用方（扫描器 / 详情页重算）拿到后要**写回 `grid["anchor"]`** ——
    否则分段按折后的锚点算对了，但界面上画 8 拍线、引擎查表用的还是旧锚点，
    用户看到的就是"段落对了、8 拍线还差 4 拍"。
    """
    global _LAST_FOLD_ANCHOR
    v = _LAST_FOLD_ANCHOR
    _LAST_FOLD_ANCHOR = None
    return v


def auto_segments(f, dur=0.0, grid=None, win_sec=None, jump_thr=None, **kw):
    """分段入口：**按需自动折锚点**（只在"首段不是 8 拍整数倍、折后是"时才折）。

    ★ 用户 2026-11-07：「开头 0→26.7s 正好 64 拍，为什么切出去 4 拍」「一两百首不能一首首调」。
      根因：`beatgrid` 有时把锚点给晚一整小节（实测 1.801 vs 真实 0.139）。
      判据（只在能**自证**时动手）：
        · 原锚点算出来的**首段不是 8 拍的整数倍**（说明量化在和锚点打架）；
        · 把锚点折回一小节后，首段变成 8 拍的整数倍；
        · 且段数不变或只多 1 段。
      ⇒ 用折后的结果；否则完全不动（保证本来对的歌不受影响）。
    """
    def _head8(sg, gbl):
        n = (float(sg[1]["t0"]) - float(sg[0]["t0"])) / gbl
        return abs(n - 8.0 * round(n / 8.0))

    def _head32(sg, gbl):
        n = (float(sg[1]["t0"]) - float(sg[0]["t0"])) / gbl
        return abs(n - 32.0 * round(n / 32.0))

    segs = _seg_impl(f, dur=dur, grid=grid, win_sec=win_sec, jump_thr=jump_thr, **kw)
    try:
        g = None
        if isinstance(grid, dict):
            if "beat_len" in grid and "anchor" in grid:
                g = {"anchor": float(grid["anchor"]), "beat_len": float(grid["beat_len"])}
            else:
                g = grid_from_meta(grid)
        if not g or len(segs) < 3:
            return segs
        ga, gbl = float(g["anchor"]), float(g["beat_len"])
        if gbl <= 1e-6 or ga <= 4.0 * gbl or _head8(segs, gbl) <= 0.5:
            return segs
        if "beat_len" in grid and "anchor" in grid:
            g2 = dict(grid); g2["anchor"] = round(ga - 4.0 * gbl, 6)
        else:
            g2 = dict(grid); gg = dict(g2.get("grid") or {})
            gg["anchor"] = round(ga - 4.0 * gbl, 6); g2["grid"] = gg
        segs2 = _seg_impl(f, dur=dur, grid=g2, win_sec=win_sec, jump_thr=jump_thr, **kw)
        _h2 = (float(segs2[1]["t0"]) - float(segs2[0]["t0"])) / gbl
        # 只认"整 2 个 32 拍 / 4 个 32 拍"的前奏（64 / 128 拍）——DJ 曲最常见的开场长度，
        # 也更不容易把别的歌改坏（其它长度一律不动）。
        global _LAST_FOLD_ANCHOR
        if (_head32(segs2, gbl) <= 1.0 and int(round(_h2 / 32.0)) in (2, 4)
                and len(segs2) <= len(segs) + 4):
            _LAST_FOLD_ANCHOR = round(ga - 4.0 * gbl, 6)
            return segs2
    except Exception:                                          # noqa: BLE001
        pass
    return segs


def _renumber_names(xs):
    """按 tier 顺序重编段名（drop / 中段 / 过渡 / 前奏 / 尾奏）。"""
    h = m = l = 0
    for k, x in enumerate(xs):
        t = x.get("tier")
        if t == "high":
            h += 1
            x["name"] = "drop %d" % h
        elif t == "mid":
            m += 1
            x["name"] = "中段 %d" % m
        else:
            l += 1
            x["name"] = ("前奏" if k == 0 else
                         "尾奏" if k == len(xs) - 1 else "过渡 %d" % l)
    return xs


def _odd_bars(L):
    """L（拍）是不是「8 拍奇数倍」怪乐句（24/40/56/72/88/104/120）。

    ⚠ 区间两端必须留 0.4 拍容差：`t1-t0` 除以 `beat_len` 拿不到整数
      （Dreamira 的 drop1 = 10.285s / 0.428571 = **23.9987** 拍），
      写成 `24.0 <= L` 会让**恰好 24 拍**的段统统漏判 ⇒ `_regularize_phrase_lengths`
      的自证判据恒为 0，那首怎么都修不动（2026-11-07 实测踩坑）。
    """
    if not (24.0 - 0.4 <= L <= 128.0 + 0.4):
        return False
    q = L / 8.0
    return abs(q - round(q)) < 0.35 and int(round(q)) % 2 == 1


def _shift_boundaries(segs, idxs, delta_sec, cur, dt, lo, rng, drop=None):
    """把若干内部边界整体平移 delta_sec 秒。

    只改 idxs 指定的 t0 及其前一段的 t1（保证无缝），**段数不变、窗口外一字不动**，
    tier 原样保留、受影响段的 energy 按新区间重算。

    :param drop: 可选，被平移**压没**（长度 → 0）的段下标；给了就把它并进前一段
        （前一段的 t1 直接接它的 t1，保证严格无缝）。只有调用方过了自证才传。
    """
    out = [dict(x) for x in segs]
    for k in idxs:
        out[k]["t0"] = round(float(out[k]["t0"]) + delta_sec, 3)
        if k > 0:
            out[k - 1]["t1"] = out[k]["t0"]

    def rel(t0, t1):
        v = (_win_mean(cur, dt, t0, t1) - lo) / rng
        return max(0.0, min(1.0, v))

    touched = set()
    for k in idxs:
        touched.add(k - 1)
        touched.add(k)
    for z in touched:
        if 0 <= z < len(out):
            out[z]["energy"] = round(
                rel(float(out[z]["t0"]), float(out[z]["t1"])), 3)
    if drop is not None and 0 < drop < len(out):
        out[drop - 1]["t1"] = out[drop]["t1"]
        out[drop - 1]["energy"] = round(
            rel(float(out[drop - 1]["t0"]), float(out[drop - 1]["t1"])), 3)
        del out[drop]
        out = _renumber_names(out)
    return out


def _rebuild_after_false_climax(segs, cur, dt, ga, gbl, ok, min_chain=4):
    """「8 拍假高潮」之后的长响段链，以**假高潮结束点**为局部基准重建。

    ★★★ 2026-11-07 用户（Out of Innocence, BPM220）：
      「drop6 开头八拍是假高潮的过渡，在这之后才是 64 拍 64 拍那种正常的结构」。
      110715 的 `_align_transition_and_climax` ② 只切出 672→680 这 8 拍，
      却没把之后那条 8 段 drop 链一起按 680 重新起算 ⇒ 后续边界仍是
      「672 + 32k」系（704/768/800/864/928/984/1040），每条比「680 + 64k」少 8 拍，
      drop7 被压成 24 拍。用户再报「Drop7 少了 8 拍，后面全部偏差 8 拍」。
    ⚠ 不能靠放宽 `_shift_odd_chain` 来修：它以**全局链首**为基准重排，
      会撤销 ② 切出的假高潮 8 拍（取证实测两条链基准冲突）。

    判据（形态罕见、条件严格）：
      ① 存在一个 ≈8 拍、tier!=low 的独立段，前一段是 high 且 ≥32 拍；
      ② 其后连续 >=min_chain 段都是 tier!=low 的响段链；
      ③ 窗口两端在 8 拍格上、窗口长 >=96 拍；
      ④ 重建后「8 拍奇数倍段」严格减少，段数不超过原链 +2。
    动作：窗口 [假高潮结束点, 链尾) 内按 64 拍（或 32 拍，取段数更接近原链的）
      重新落边界；窗口外完全不动，tier 按原顺序保留。
    """
    if not ok or gbl <= 1e-6 or dt <= 1e-9 or cur is None or len(segs) < 7:
        return segs
    bar = 8.0 * gbl
    try:
        lo, hi = np.percentile(cur, [5, 95])
    except Exception:                                          # noqa: BLE001
        return segs
    rng = max(float(hi - lo), 1e-9)

    def on8(t):
        q = (t - ga) / bar
        return abs(q - round(q)) < 0.25

    n = len(segs)
    for k in range(1, n - min_chain - 1):
        s0 = segs[k]
        L0 = (float(s0["t1"]) - float(s0["t0"])) / gbl
        if not (7.4 <= L0 <= 8.7) or s0.get("tier") == "low":
            continue
        pv = segs[k - 1]
        if (pv.get("tier") != "high" or
                (float(pv["t1"]) - float(pv["t0"])) / gbl < 31.5):
            continue
        base = float(s0["t1"])
        j = k + 1
        while j < n and segs[j].get("tier") != "low":
            j += 1
        chain_n = j - (k + 1)
        if chain_n < min_chain:
            continue
        end = float(segs[j - 1]["t1"])
        if not on8(base) or not on8(end) or end - base < 96.0 * gbl:
            continue
        total_b = (end - base) / gbl
        old = [((float(segs[m + 1]["t0"]) if m + 1 < j else end)
                - float(segs[m]["t0"])) / gbl for m in range(k + 1, j)]
        n_old = sum(1 for L in old if _odd_bars(L))
        if n_old < 1:
            continue
        best = None
        for B in (64.0, 32.0):
            kseg = int(total_b // B)
            if kseg < 2:
                continue
            lens = [B] * kseg
            tail_b = total_b - B * kseg
            if tail_b > 1e-6:
                lens.append(tail_b)
            if min(lens) < 7.4:
                continue
            n_new = sum(1 for L in lens if _odd_bars(L))
            if n_new >= n_old or len(lens) > chain_n + 2:
                continue
            score = (n_old - n_new, -abs(len(lens) - chain_n))
            if best is None or score > best[0]:
                best = (score, lens)
        if best is None:
            continue
        lens = best[1]
        tiers = [segs[m].get("tier") for m in range(k + 1, j)]
        marks = []
        t = base
        for L in lens[:-1]:
            t += L * gbl
            marks.append(t)
        marks.append(end)

        def rel(t0, t1):
            v = (_win_mean(cur, dt, t0, t1) - lo) / rng
            return max(0.0, min(1.0, v))

        built = []
        prev = base
        for z, t1 in enumerate(marks):
            built.append({"t0": round(prev, 3), "t1": round(t1, 3),
                          "energy": round(rel(prev, t1), 3),
                          "tier": tiers[z] if z < len(tiers) else tiers[-1],
                          "name": ""})
            prev = t1
        out = ([dict(x) for x in segs[:k + 1]] + built +
               [dict(x) for x in segs[j:]])
        return _renumber_names(out)
    return segs


def _regularize_phrase_lengths(segs, cur, dt, ga, gbl, ok, max_pass=3):
    """把「连续同相位、整体偏 8 拍」的边界组整体平移回乐句格。

    ★★★ 2026-11-07 用户连报多首：「dropX / 过渡X 少（多）了 8 拍，
      导致后面全部连锁偏 8 拍」。取证（System Overloaded / 02-digimind /
      Second World）给同一个数据特征：
        · 一组连续内部边界相对 32 拍格的模值都是同一个 d1；
        · 紧邻它的前、后两条边界模值都是同一个 d0；
        · |d1 - d0| 的最小环差恰好是 8 拍 ⇒ 整组边界贴错一格 8 拍。
      根因之一：`_shift_odd_chain` 在 2317-2320 只把链尾位移 Δ 作用于
      链后第一段的 t0，没传导到窗口内后续边界 ⇒ 形成「窗口断差」。
    ⚠ 不修 `_shift_odd_chain` 本身：它以全局链首为基准，放宽会撤销假高潮
      8 拍；改在末端用独立 pass 修，已验收的行为完全不变。

    判据（全部满足才动手）：
      ① 连续 >=1 条内部边界共享同一 mod32 相位 d1（容差 1.6 拍）；
      ② 前后两条相邻边界共享同一相位 d0，且与 d1 的最小环差 == 8 拍；
      ③ 受影响各段（含前后）>=16 拍（保护独立 8 拍过门、不造碎段）；
      ④ 平移后「8 拍奇数倍段」严格减少（自证改善）。
    动作：整组边界平移 ±8 拍（窗口外一字不动、段数不变、tier 保留）。
    """
    if not ok or gbl <= 1e-6 or dt <= 1e-9 or cur is None or len(segs) < 5:
        return segs
    try:
        lo, hi = np.percentile(cur, [5, 95])
    except Exception:                                          # noqa: BLE001
        return segs
    rng = max(float(hi - lo), 1e-9)

    def m32(t):
        return ((t - ga) / gbl) % 32.0

    def near(a, b, tol=1.6):
        d = abs(a - b) % 32.0
        return min(d, 32.0 - d) <= tol

    def n_odd(Ls):
        return sum(1 for L in Ls if _odd_bars(L))

    for _ in range(max_pass):
        n = len(segs)
        bs = [float(segs[k]["t0"]) for k in range(1, n)]
        ms = [m32(t) for t in bs]
        best = None
        p = 0
        while p < len(bs):
            q = p
            while q + 1 < len(bs) and near(ms[q + 1], ms[p]):
                q += 1
            if p > 0 and q + 1 < len(bs):
                d0a, d0b = m32(bs[p - 1]), m32(bs[q + 1])
                if near(d0a, d0b, 1.6):
                    diff = (d0a - ms[p] + 16.0) % 32.0 - 16.0
                    if abs(abs(diff) - 8.0) <= 1.6:
                        lo_i, hi_i = p, q + 1
                        Ls = [(float(segs[z]["t1"]) - float(segs[z]["t0"])) / gbl
                              for z in range(lo_i, hi_i + 1)]

                        def off_cnt(shift):
                            return sum(1 for z in range(p, q + 1)
                                       if min((ms[z] + shift) % 32.0,
                                              32.0 - (ms[z] + shift) % 32.0) > 0.25)

                        # ★★ 2026-11-07 补（`Void Requiem`「过渡二前面的八拍是一个
                        #   过渡…之后是四个 32 拍」）：环差只能给出**幅度** 8 拍，
                        #   方向有 ±18 两个解。原代码只按 `d0a - ms[p]` 取一个方向，
                        #   而 `Ls=[56, 8, 32]` 恰好被算成 +8 ⇒ 把那个 8 拍过渡压成 0、
                        #   drop1 变 64，两条路径全否 ⇒ 该报障永远修不动；正确解是 −8
                        #   （19.127→16.841 ⇒ [48, 16, 32]，drop1 回到 48 拍）。
                        #   保守起见：**先按老方向算，老方向一无所获才翻向重试**，
                        #   保证既有歌曲的选择一分不变。
                        found = False
                        for turn, diff in enumerate((diff, -diff)):
                            if turn == 1 and found:
                                break
                            new_Ls = list(Ls)
                            new_Ls[0] += diff
                            new_Ls[-1] -= diff

                            # ★ 自证②：平移后「偏离 32 拍格」的边界数不得增加。
                            #   否则只是把本来对齐的整条链无意义平移 8 拍（churn），
                            #   全库实测会误伤 9 首。
                            base_ok = (off_cnt(diff) <= off_cnt(0.0))

                            # 路径 A（常规）：平移后两翼都还够长，纯粹挪边界。
                            # ★★★ 2026-11-07 补：原来还要求 `min(Ls) >= 15.5`，
                            #   即「平移前每条都得够长」。但 `Void Requiem` 的形状恰好
                            #   是 Ls=[56, 8, 32] —— 那个 8 拍**已经是**独立过渡段，
                            #   边界只需挪一格就变成 [48, 16, 32]。平移前短 ≠ 平移后短，
                            #   判据应该只落在**平移后**；否则「8 拍过渡紧贴着一段
                            #   8 拍奇数倍段」这一类永远修不动。
                            ok_a = (base_ok and min(new_Ls) >= 15.5
                                    and n_odd(new_Ls) < n_odd(Ls))

                            # 路径 B（★★ 2026-11-07 `揽月 - Dreamira`，用户「drop1 少了 8 拍」）：
                            #   平移会把**末段压到 0**（Ls=[24,32,8] → new=[32,32,0]）。
                            #   这不是"制造碎段"，而是把 8 拍的尾巴并进前一段 —— 恰好把
                            #   24+32+8 变成 32+32。旧判据一律拒绝，所以 Dreamira 怎么都修不动。
                            #   5 条硬门槛，保证只在"并掉一个小尾巴"时放行：
                            #     ① 只有末段被压没（new_Ls[-1]≈0），且它本身 <16 拍；
                            #     ② 并完所有段 ≥8 拍、且后面还有段（别吞掉曲尾）；
                            #     ③ 被并段与前一段 tier 相同（不会少一个档级）；
                            #     ④ 被并段不是孤立低谷（≥-0.28，保护独立 8 拍过门）；
                            #     ⑤ n_odd 严格减少（自证改善）。
                            drop_i = None
                            if (base_ok and len(Ls) >= 2
                                    and -0.7 <= new_Ls[-1] <= 0.7
                                    and Ls[-1] < 15.5
                                    and min(new_Ls[:-1]) >= 7.75
                                    and q + 2 < len(segs)
                                    and segs[q + 1].get("tier") == segs[q].get("tier")
                                    and n_odd(new_Ls[:-1]) < n_odd(Ls)):
                                d0, d1 = q + 1, q + 2
                                e_d = _win_mean(cur, dt, float(segs[d0]["t0"]),
                                                float(segs[d0]["t1"]))
                                e_l = _win_mean(cur, dt, float(segs[d0 - 1]["t0"]),
                                                float(segs[d0 - 1]["t1"]))
                                e_r = _win_mean(cur, dt, float(segs[d1]["t0"]),
                                                float(segs[d1]["t1"]))
                                if (e_d - 0.5 * (e_l + e_r)) / rng >= -0.28:
                                    drop_i = d0
                            if ok_a or drop_i is not None:
                                alive = (new_Ls[:-1] if drop_i is not None
                                         else new_Ls)
                                key = (n_odd(Ls) - n_odd(alive), q - p + 1, -p)
                                if best is None or key > best[0]:
                                    best = (key, list(range(p + 1, q + 2)),
                                            diff * gbl, drop_i)
                                    found = True
            p = q + 1
        if best is None:
            break
        _, idxs, delta, drop_i = best
        segs = _shift_boundaries(segs, idxs, delta, cur, dt, lo, rng,
                                 drop=drop_i)
    return segs


#: 「听不出来的假边界」吸附阈值
#  ⚠ `WEAK_STEP` 不许超过 `WEAK_NEW_MAX`：一旦超过，就会出现「把一条 0.28 的真台阶
#    挪到一条 0.18 的假台阶上」—— 那是**拆掉**用户能听到的边界。全库实测：
#    0.12 → 136→118、0.16 → 113、0.20 → 109，变坏都是 0 首，取上限。
WEAK_STEP = 0.20        # 两侧段能量差 ≤ 这个值 ⇒ 这条边界本来就没事件
WEAK_NEW_MAX = 0.20     # 挪过去之后新边界两侧的能差上限（别造出真台阶）
WEAK_MIN_LEN = 15.5     # 挪完两段都得够长（拍）
WEAK_SHIFT = 8.0        # 一格（一个八拍）


def _snap_weak_boundary(segs, cur, dt, ga, gbl, ok, max_pass=2):
    """把「两侧一样响、听不出来」的假边界挪到最近的 8 拍整数格。

    ★★★ 2026-11-07 用户（`Unlock Your Mind -Refix-`「中段 5 后面少了 8 拍」）：
      取证（`tools/_probe_slot.py`）：133.045→163.045 这段能量一路 0.73/0.73/0.68/
      0.65/0.62/0.63/0.65/0.65/0.60 —— **完全平坦**，表里却在 k=51 插了一条边界
      （中段 5=56 拍、中段 6=24 拍，两条都是 8 拍奇数倍）。这条边界是电平噪声
      捏出来的，用户听到的其实是"中段 5 一直延续到 155 之后"，所以他说"少了 8 拍"。
      ⇒ 这种边界**挪一格就能同时消掉两个奇数倍段**，而且因为两侧本来就一样响，
        挪动听感为零。

    判据（全部满足才动手）：
      ① 两侧段能量差 ≤ `WEAK_STEP`（这条边界本来就没事件）；
      ② 至少一侧是「8 拍奇数倍段」（不碰本来整齐的表）；
      ③ 挪 ±8 拍后两段都 ≥ `WEAK_MIN_LEN`，且**两侧都不再是奇数倍**（自证改善）；
      ④ 新位置两侧能差 ≤ `WEAK_NEW_MAX`（别把边界挪到真台阶上）；
      ⑤ 两个方向都可行时，选**更贴 32 拍格**的那个。
    动作：只挪这一条边界（段数不变、tier 保留、窗口外一字不动）。
    """
    if not ok or gbl <= 1e-6 or dt <= 1e-9 or cur is None or len(segs) < 4:
        return segs
    try:
        lo, hi = np.percentile(cur, [5, 95])
    except Exception:                                          # noqa: BLE001
        return segs
    rng = max(float(hi - lo), 1e-9)

    def rel(t0, t1):
        v = (_win_mean(cur, dt, t0, t1) - lo) / rng
        return max(0.0, min(1.0, v))

    def m32(t):
        d = ((t - ga) / gbl) % 32.0
        return min(d, 32.0 - d)

    got = [dict(x) for x in segs]
    for _ in range(max_pass):
        n = len(got)
        best = None
        for i in range(1, n - 1):
            L1 = (float(got[i - 1]["t1"]) - float(got[i - 1]["t0"])) / gbl
            L2 = (float(got[i]["t1"]) - float(got[i]["t0"])) / gbl
            o1, o2 = _odd_bars(L1), _odd_bars(L2)
            if not (o1 or o2):
                continue
            e1 = rel(float(got[i - 1]["t0"]), float(got[i - 1]["t1"]))
            e2 = rel(float(got[i]["t0"]), float(got[i]["t1"]))
            if abs(e1 - e2) > WEAK_STEP:
                continue
            t_b = float(got[i]["t0"])
            for d in (WEAK_SHIFT, -WEAK_SHIFT):
                nL1, nL2 = L1 + d, L2 - d
                if nL1 < WEAK_MIN_LEN or nL2 < WEAK_MIN_LEN:
                    continue
                if _odd_bars(nL1) or _odd_bars(nL2):
                    continue
                nt = t_b + d * gbl
                ne1 = rel(float(got[i - 1]["t0"]), nt)
                ne2 = rel(nt, float(got[i]["t1"]))
                if abs(ne1 - ne2) > WEAK_NEW_MAX:
                    continue
                key = (-m32(nt), 1 if d > 0 else 0)
                if best is None or key > best[0]:
                    best = (key, i, d * gbl)
        if best is None:
            break
        _, i, delta = best
        got = _shift_boundaries(got, [i], delta, cur, dt, lo, rng)
    return got


#: 「边界站在没有台阶的地方、而旁边的八拍格有真台阶」⇒ 把边界搬过去
STEP_WEAK = 0.20        # 当前位置的八拍台阶弱于这个值，才算"站错地方"
STEP_MIN = 0.30         # 新位置至少要这么强的台阶
STEP_GAIN = 0.15        # 比当前位置强出这么多（绝对量）
STEP_RATIO = 2.2        # 并且至少强出这么多倍
STEP_MAX_K = 8          # 最多挪几个八拍格
STEP_MIN_LEN = 15.5     # 挪完两段都得够长（拍）
STEP_HYST = 0.06        # 改档位要求越过阈值这么多（迟滞，防止抖）


_TIER_RANK = {"low": 0, "mid": 1, "high": 2}


def _retier(seg, energy):
    """按新能量重定档位，只许**降档**，带迟滞。

    ⚠ 只降不升是有意的：边界搬动之后，一段变长/变短只能说明"原来贴错标签的那块
      不属于它"—— 那是降档；反过来"变长之后平均值被拉高就升成 high"会把本来
      很安静的段落吞掉（实测 `揽月 - Dreamira` 前段一段 32 拍 low 就这样被吃掉）。
      要升档得由上游 pass 自己判定，这里不做。
    """
    old = seg.get("tier") or "mid"
    new = tier_from_energy(energy)
    if _TIER_RANK.get(new, 1) >= _TIER_RANK.get(old, 1):
        return
    if new == "low" and energy > TIER_LO - STEP_HYST:
        return
    seg["tier"] = new


def _relocate_weak_boundary(segs, cur, dt, ga, gbl, ok, max_pass=2):
    """边界现在落在「八拍台阶几乎是 0」的地方，而附近某一格的台阶很硬 ⇒ 搬过去。

    ★★★ 2026-11-07 用户（`(04) Amane Oikawa - Devouring Saturnus`，连报两条）：
      ① 「过渡 5 多了后面 8 拍，Drop 1 前后各少了 8 拍」
      ② 「Drop 4 从开头这 64 拍都是 Drop」
      取证（`tools/_probe_slot.py` 的八拍槽 rel 剖面，BPM155 / boot 0.387097s）：
        · k=20/21（62.933/66.030）rel 0.287/0.323、**低频占比 0.001**（纯 breakdown）；
          k=22（69.127）rel **0.974** —— 真正的 drop 起点在这里，
          表里却写在 k=23（72.223）⇒ 过渡 5 被撑成 24 拍、drop1 前段少 8 拍。
        · k=29（90.804）rel 0.837 还满幅，k=30（93.901）才掉到 0.522 ——
          drop1 的**终点**也早了一格。
        · drop4 起点 199.191 之后 k=64…71（0.970/0.917/0.981/0.787/0.952/0.930/
          0.975/0.891）全是 drop 级，k=72（223.965）骤降到 0.230 ——
          恰好 **64 拍整**；可表里在 k=67（208.481）就把 drop4 切断了，
          后面那 40 拍满幅能量被标成「中段 3」。
      ⇒ 共同特征：**边界所在那两格的台阶接近 0（0.059 / 0.194），
        真台阶在同一个段的另一格里（0.651 / 0.661）**。
        这不是"长度算错"，是"边界站错地方"，任何只看段长的判据都抓不到。

    判据（全部满足才动手）：
      ① 边界落在八拍格上（±0.35 拍），且该格台阶 `|E[k]-E[k-1]|` ≤ `STEP_WEAK`；
      ② 目标格台阶 ≥ `STEP_MIN`，且比当前强 ≥ `STEP_GAIN` 并且 ≥ `STEP_RATIO` 倍；
      ③ 搬完两段都 ≥ `STEP_MIN_LEN` 拍（不制造碎段），且不跨过别的边界；
      ④ 段数不变、窗口外一字不动，只把这一条边界挪过去；
      ⑤ 受影响两段按新能量**重定档位**（带迟滞）—— drop 里那 40 拍必须回到 high。
    """
    if not ok or gbl <= 1e-6 or dt <= 1e-9 or cur is None or len(segs) < 4:
        return segs
    try:
        lo, hi = np.percentile(cur, [5, 95])
    except Exception:                                          # noqa: BLE001
        return segs
    lo = float(lo)
    rng = max(float(hi - lo), 1e-9)
    bar = 8.0 * gbl
    dur = float(segs[-1]["t1"])

    def rel(t0, t1):
        v = (_win_mean(cur, dt, t0, t1) - lo) / rng
        return max(0.0, min(1.0, v))

    E = {}
    k_kmax = int((dur - ga) / bar)
    for k in range(0, k_kmax + 1):
        t0 = ga + k * bar
        t1 = min(t0 + bar, dur)
        if t1 - t0 < 0.9 * bar:
            continue
        E[k] = rel(t0, t1)

    got = [dict(x) for x in segs]
    for _ in range(max_pass):
        moved = False
        n = len(got)
        # ⚠ 从 2 开始：i=1 是「首段 / 第 2 段」那条边界，动了会改变
        #   `auto_segments()` 里 anchor 折叠判据（`_head8/_head32` 看的就是首段长度）⇒
        #   间接把整首歌换到另一条折叠分支上（实测 `Cursed Haze` 因此 0→1 变坏）。
        for i in range(2, n):
            t_b = float(got[i]["t0"])
            k_b = int(round((t_b - ga) / bar))
            if abs(t_b - (ga + k_b * bar)) > 0.35 * gbl:
                continue
            if k_b - 1 not in E or k_b not in E:
                continue
            s_now = abs(E[k_b] - E[k_b - 1])
            if s_now > STEP_WEAK:
                continue
            L1_0 = (t_b - float(got[i - 1]["t0"])) / gbl
            L2_0 = (float(got[i]["t1"]) - t_b) / gbl
            t_lo = float(got[i - 1]["t0"]) + STEP_MIN_LEN * gbl
            t_hi = float(got[i]["t1"]) - STEP_MIN_LEN * gbl
            if t_hi <= t_lo:
                continue
            odd0 = _odd_bars(L1_0) + _odd_bars(L2_0)
            best = None
            for dk in range(-STEP_MAX_K, STEP_MAX_K + 1):
                if dk == 0:
                    continue
                k_c = k_b + dk
                if k_c - 1 not in E or k_c not in E:
                    continue
                s_c = abs(E[k_c] - E[k_c - 1])
                if s_c < STEP_MIN or s_c < s_now + STEP_GAIN:
                    continue
                if s_c < STEP_RATIO * max(s_now, 1e-6):
                    continue
                t_c = ga + k_c * bar
                if not (t_lo <= t_c <= t_hi):
                    continue
                # ★ 自证：不许把「8 拍奇数倍段」变多（实测能挡住
                #   `Devouring Saturnus` 中段 2 被切成 24 拍这种"修一个坏两个"）。
                L1 = (t_c - float(got[i - 1]["t0"])) / gbl
                L2 = (float(got[i]["t1"]) - t_c) / gbl
                if _odd_bars(L1) + _odd_bars(L2) > odd0:
                    continue
                key = (round(s_c, 3), -abs(dk))
                if best is None or key > best[0]:
                    best = (key, t_c - t_b)
            if best is None:
                continue
            # ★ 贪心：一条边界合格就当场搬，然后继续往右扫（不再"全局只搬最强的一条"，
            #   否则 drop1 头/尾、drop4 尾这种同一首里的多处问题要跑好几轮才轮得到，
            #   轮数一多又会在别的段上多动 —— 实测那样会把 72 拍的中段翻出来）。
            nxt = _shift_boundaries(got, [i], best[1], cur, dt, lo, rng)
            for z in (i - 1, i):
                if 0 <= z < len(nxt):
                    _retier(nxt[z], float(nxt[z].get("energy") or 0.0))
            nxt = _renumber_names(nxt)
            # ★ 全局自证：整首的「8 拍奇数倍段」不许变多
            #   （实测能挡住 `Cursed Haze` 0→1 这种"修一处坏一处"）。
            if _n_odd_all(nxt, gbl) > _n_odd_all(got, gbl):
                continue
            got = nxt
            moved = True
        if not moved:
            break
    if _n_odd_all(got, gbl) > _n_odd_all(segs, gbl):
        return segs
    return got


#: 「独立短过渡」检测阈值（全部是**双向强证据**才动手，宁漏不错）
BRK_E_MAX = 0.58        # 该八拍槽的 rel 能量上限
BRK_DIP = 0.28          # 比两侧较低者至少低这么多
BRK_NB_MIN = 0.60       # 两侧都必须是响段
BRK_SEG_MIN = 23.5      # 被切开的段至少这么长（拍）
BRK_SIDE_MIN = 7.75     # 切完剩下的两翼各至少 8 拍
BRK_STEP = 32.0         # 后链重新落位的步长（拍）


def _insert_break_transition(segs, cur, dt, ga, gbl, ok):
    """把「被并进大段的独立 8/16 拍过渡」切出来，并把后面的响段链按 32 拍重铺。

    ★★★ 2026-11-07 用户（9 Oblivious，BPM200）：
      「drop 9 前面的 8 拍是一个过渡，之后才是 32 拍 32 拍的段落」。
      取证（`tools/_probe_p8.py`）：k=40（96.758s）这一格 rel=0.299，
      两侧 0.820 / 0.886 —— 一个 **−0.52 的独立低谷**，后面跟着 8 个严格的
      32 拍 drop。而 110717 的表里 drop 9 = 96.758→106.358（32 拍），
      把这个过渡吞进了 drop 的开头 ⇒ 用户看到「drop 9 少了 8 拍、后面全偏」。
    ⚠ 为什么必须**新增**边界、不能只搬已有边界：
      这条链上 `k ≡ 0 (mod 4)` 的每一格都已经是边界（n8 = 8/12/16…），
      搬动任何一条都破坏「32 拍整」，只有「插入 8 拍 + 后链整体 +8」这一种解。

    判据（全部满足才动手）：
      ① 存在 1~2 个连续八拍槽，rel 能量 ≤ `BRK_E_MAX`；
      ② 两侧邻槽都 ≥ `BRK_NB_MIN`，且比本槽高 ≥ `BRK_DIP`（独立低谷，不是整体降档）；
      ③ 该槽落在**一个响段内部**（或左沿正好压着它的起点），该段 ≥ `BRK_SEG_MIN` 拍；
      ④ 切完左翼 ≥ 8 拍、右翼 ≥ 8 拍（不制造碎段）；
      ⑤ 后链长度能被 32 拍整除 —— 链尾允许挪 ±8/±16 拍来凑（尾段能量不得变响 >0.10）。
    动作：切出 low 档过渡段 + 后链按 32 拍重铺；窗口外一字不动、tier 按位置继承。
    """
    if not ok or gbl <= 1e-6 or dt <= 1e-9 or cur is None or len(segs) < 5:
        return segs
    try:
        lo, hi = np.percentile(cur, [5, 95])
    except Exception:                                          # noqa: BLE001
        return segs
    lo = float(lo)
    rng = max(float(hi - lo), 1e-9)
    bar = 8.0 * gbl
    dur = float(segs[-1]["t1"])

    def rel(t0, t1):
        v = (_win_mean(cur, dt, t0, t1) - lo) / rng
        return max(0.0, min(1.0, v))

    # ---- 1. 八拍槽能量剖面 + 独立低谷 ----
    E = {}
    k_kmax = int((dur - ga) / bar)
    for k in range(0, k_kmax + 1):
        t0 = ga + k * bar
        t1 = min(t0 + bar, dur)
        if t1 - t0 < 0.4 * bar:
            continue
        E[k] = rel(t0, t1)
    dips = []
    for k in sorted(E):
        if k - 1 not in E or k + 1 not in E:
            continue
        if E[k] > BRK_E_MAX:
            continue
        nb = min(E[k - 1], E[k + 1])
        if nb < BRK_NB_MIN or nb - E[k] < BRK_DIP:
            continue
        dips.append(k)
    runs = []
    for k in dips:
        if runs and k == runs[-1][1]:
            runs[-1][1] = k + 1
        else:
            runs.append([k, k + 1])
    runs = [tuple(r) for r in runs if 1 <= r[1] - r[0] <= 2]

    # ---- 2. 逐个切 ----
    got = [dict(x) for x in segs]
    for k0, k1 in runs:
        t_a = ga + k0 * bar
        t_b = ga + k1 * bar
        if t_a <= 0.5 or t_b >= dur - 0.5:
            continue
        # ⚠ 开场 32 拍 / 收尾 32 拍不动：那段本来就归 `auto_segments` 的折锚点逻辑管，
        #   而且这里的"独立低谷"证据最弱（`Rolling Contact - Euclid Number` 的前奏
        #   56+56 被切成 24+8+32+48 就是反例）。宁可漏，不可错。
        if k0 < 4 or k1 + 4 > k_kmax:
            continue
        hit = None
        for i, s in enumerate(got):
            if float(s["t0"]) - 0.02 <= t_a and t_b <= float(s["t1"]) + 0.02:
                hit = i
                break
        if hit is None:
            continue
        s = got[hit]
        L = (float(s["t1"]) - float(s["t0"])) / gbl
        if s.get("tier") == "low" or L < BRK_SEG_MIN:
            continue
        left = (t_a - float(s["t0"])) / gbl
        right = (float(s["t1"]) - t_b) / gbl
        at_edge = left < 0.35          # 槽左沿正好压在已有边界上
        if (not at_edge and left < BRK_SIDE_MIN) or right < BRK_SIDE_MIN:
            continue
        mid = {"t0": round(t_a, 3), "t1": round(t_b, 3),
               "energy": round(rel(t_a, t_b), 3), "tier": "low", "name": ""}
        if at_edge:
            prefix = got[:hit]
        else:
            prefix = got[:hit] + [
                {"t0": round(float(s["t0"]), 3), "t1": round(t_a, 3),
                 "energy": round(rel(float(s["t0"]), t_a), 3),
                 "tier": s.get("tier"), "name": s.get("name", "")}]
        tail_src = got[hit:]
        tail_src[0] = dict(s, t0=round(t_b, 3), t1=float(s["t1"]))
        built, chi, new_end = _rephase_chain(tail_src, round(t_b, 3),
                                             ga, gbl, cur, dt, lo, rng, dur)
        if built is None:
            continue
        rest = [dict(x) for x in got[hit + chi:]]
        if rest and abs(float(rest[0]["t0"]) - new_end) > 1e-3:
            rest[0]["t0"] = round(float(new_end), 3)
        cand = prefix + [mid] + built + rest
        # ★ 自证：这次切分不得让「8 拍奇数倍段 / 捏出来的碎段」变多。
        #   全库实测（Dark Pastime 0→1、Cursed Haze -Rebuild- 2→3）证明：
        #   少数歌的后链本身就不是 32 拍整，硬铺会把它切坏 ⇒ 宁可不动。
        if (_n_odd_all(cand, gbl) > _n_odd_all(got, gbl) or
                _n_short_segs(cand, gbl) > _n_short_segs(got, gbl)):
            continue
        got = cand
    if len(got) == len(segs):
        return segs
    return _renumber_names(got)


def _n_odd_bars(segs, gbl):
    """表里「8 拍奇数倍段」的条数（首尾除外 —— 前奏/尾奏本来就跟着曲长走）。"""
    n = 0
    for x in segs[1:-1]:
        L = (float(x["t1"]) - float(x["t0"])) / gbl
        if _odd_bars(L):
            n += 1
    return n


def _n_odd_all(segs, gbl):
    """表里「8 拍奇数倍段」的条数（**含首尾** —— 这一刀用它做全局自证）。"""
    n = 0
    for x in segs:
        L = (float(x["t1"]) - float(x["t0"])) / gbl
        if _odd_bars(L):
            n += 1
    return n


def _n_short_segs(segs, gbl):
    """表里长度 < 8 拍的段数（首尾除外）。"""
    n = 0
    for x in segs[1:-1]:
        if (float(x["t1"]) - float(x["t0"])) / gbl < 7.75:
            n += 1
    return n


def _rephase_chain(segs, start_t, ga, gbl, cur, dt, lo, rng, dur):
    """从 `start_t` 起把后面的响段链按 `BRK_STEP` 拍重新落位。

    返回 `(新段列表, 被替换到的下标, 新链尾秒)`；无法整除时返回 `(None, 下标, None)`。
    链 = 从 `start_t` 所在段起，连续 `tier != "low"`（允许它自己）到第一个
    「low 且 ≥16 拍」的段之前；那一段就是链尾（尾奏/大过渡），保持不动。
    """
    n = len(segs)
    bar = 8.0 * gbl

    def on8(t, tol=0.35):
        q = (t - ga) / bar
        return abs(q - round(q)) <= tol

    i0 = None
    for i, s in enumerate(segs):
        if abs(float(s["t0"]) - start_t) < 0.06:
            i0 = i
            break
    if i0 is None:
        return None, 0, None
    j = i0
    while j < n - 1:
        L = (float(segs[j]["t1"]) - float(segs[j]["t0"])) / gbl
        if segs[j].get("tier") == "low" and L >= 15.5 and j > i0:
            break
        j += 1
    if j <= i0:
        return None, 0, None
    end_t = float(segs[j]["t0"]) if j < n else float(segs[-1]["t1"])
    if not on8(end_t):
        return None, j, None

    total_b = (end_t - start_t) / gbl
    best = None
    for d in (0.0, -8.0, 8.0, -16.0, 16.0):
        nb = total_b + d
        if nb < BRK_STEP:
            continue
        m = int(round(nb / BRK_STEP))
        if m < 1 or abs(nb - m * BRK_STEP) > 0.6:
            continue
        cand = end_t + d * gbl
        if cand > dur - 0.5 or cand <= start_t:
            continue
        key = (abs(d), abs(m * BRK_STEP - total_b))
        if best is None or key < best[0]:
            best = (key, m, cand, d)
    if best is None:
        return None, j, None
    _, m, new_end, d = best
    if abs(d) > 8.5:
        return None, j, None
    old_tail = _win_mean(cur, dt, end_t, min(dur, end_t + 32 * gbl))
    new_tail = _win_mean(cur, dt, new_end, min(dur, new_end + 32 * gbl))
    if (new_tail - old_tail) / rng > 0.10:
        return None, j, None

    marks = [start_t + BRK_STEP * gbl * i for i in range(1, m)] + [new_end]
    built = []
    prev = start_t
    chain = segs[i0:j]
    for t1 in marks:
        mid = 0.5 * (prev + t1)
        tier = None
        for s in chain:
            if float(s["t0"]) - 0.02 <= mid <= float(s["t1"]) + 0.02:
                tier = s.get("tier")
                break
        if tier is None:
            tier = chain[0].get("tier")
        built.append({"t0": round(prev, 3), "t1": round(t1, 3),
                      "energy": round(max(0.0, min(1.0,
                                                   (_win_mean(cur, dt, prev, t1)
                                                    - lo) / rng)), 3),
                      "tier": tier, "name": ""})
        prev = t1
    return built, j, round(float(new_end), 3)

