# -*- coding: utf-8 -*-
"""结构分段（8 拍块级 DP）——2026-11-16。

背景
----
用户反馈：现有「全局 novelty 找峰」对 8/16 拍过渡、64 拍接 64 拍的歌切不准；
The Gravelord 的目标结构是 56 build + 16 过渡 + 64 drop + 16 过渡 + 64 + 16 + 64
+ 64 buildup + 8 过渡 + 64 drop。

做法
----
1) 把整首曲按 8 拍一块切成块（块 0 从 anchor 起算），每块算 6 个特征：
   cur（谱能量）、rms、Elow、Emid、Ehigh、flux 的块均值。
2) 特征做 z-score，用 DP 做「分段常量」拟合：代价 = 段内 SSE + λ × 段数，
   段长只允许 8/16/32/48/56/64 拍（1/2/4/6/7/8 块）——这正是 EDM 的常见句长。
3) 后处理（把 DP 的过度切分收回来，规则都是「结构性」的）：
   a. 两个相邻 8 拍段、都低于 drop 判定、合并后 16 拍 ⇒ 合并（8 拍低段并成 16 拍过渡）；
   b. 相邻两段同类、合并后是合法长度且边界变化分 < 阈值 ⇒ 合并（避免 32+32 被切碎）；
   c. 某段长 72 拍且紧邻 drop ⇒ 重切成 64 + 8（用户要的「8 拍过渡」）；
   d. 任何段 > 64 拍 ⇒ 按最接近的合法长度切（64 + 余数）。
4) 输出边界秒数，交给 `sections.segments_from_edges()` 算能量/档位（能量口径不变）。

设计约束：只产边界，不产档位；档位仍由旧能量算法决定（用户 2026-11-16 要求）。
"""
from __future__ import annotations

import os

import numpy as np

BLOCK_BEATS = 8.0
# 允许的段长（块数）：8/16/32/48/56/64 拍
_ALLOWED = (1, 2, 4, 6, 7, 8)
_LAM = float(os.environ.get("ERLIB_STRUCT_LAM", "3.2"))
_MERGE_SAME = float(os.environ.get("ERLIB_STRUCT_MERGE", "0.22"))
#: 段落语法软先验：32/64 拍（4/8 块）不加分，8/16 过渡少量加分，
#: 最近一次 structure_edges_hybrid 选中的模式（仅诊断用；生产用 EdgeList.mode）。
LAST_MODE = "general"


def _block_means(arr, ga, bl, dt, n):
    out = np.empty(n, dtype=np.float64)
    for k in range(n):
        i0 = max(0, int((ga + k * BLOCK_BEATS * bl) / dt))
        i1 = min(len(arr) - 1, int((ga + (k + 1) * BLOCK_BEATS * bl) / dt))
        out[k] = float(arr[i0:i1].mean()) if i1 > i0 else 0.0
    return out


def block_features(f, dur, ga, bl):
    """返回 dict：n、cur/rms/Elow/Emid/Ehigh/flux 的块均值（长度 n）。"""
    import sections as S

    dt = float(f["dt"])
    n = int(max(1, (float(dur) - float(ga)) // (BLOCK_BEATS * float(bl))))
    cur, _ = S.smooth_curve(f)
    rms, _ = S.rms_curve(f)
    feats = {
        "cur": _block_means(cur, ga, bl, dt, n),
        "rms": _block_means(rms, ga, bl, dt, n),
        "Elow": _block_means(f["Elow"], ga, bl, dt, n),
        "Emid": _block_means(f["Emid"], ga, bl, dt, n),
        "Ehigh": _block_means(f["Ehigh"], ga, bl, dt, n),
        "flux": _block_means(f["flux"], ga, bl, dt, n),
    }
    feats["n"] = n
    return feats


def _dp_cuts(X, lam=_LAM, allowed=_ALLOWED):
    """DP：最小化段内 SSE + lam×段数，返回块下标切点（含 0 与 n）。"""
    n = len(X)
    if n <= 1:
        return [0, n]
    X = np.asarray(X, dtype=np.float64)
    cs = np.cumsum(np.vstack([np.zeros((1, X.shape[1])), X]), axis=0)
    sq = np.cumsum(np.r_[0.0, (X * X).sum(axis=1)])
    INF = 1e18
    best = [INF] * (n + 1)
    back = [0] * (n + 1)
    best[0] = 0.0
    for j in range(1, n + 1):
        for L in allowed:
            i = j - L
            if i < 0:
                continue
            m = cs[j] - cs[i]
            sse = sq[j] - sq[i] - float((m * m).sum()) / L
            cost = best[i] + sse + lam
            if cost < best[j]:
                best[j] = cost
                back[j] = i
    cuts = [n]
    j = n
    while j > 0:
        j = back[j]
        cuts.append(j)
    cuts.reverse()
    return cuts


def _class_of(feats, k, med_lo, med_hi):
    c = feats["cur"][k]
    lo = feats["Elow"][k] / (med_lo or 1.0)
    hi = feats["Ehigh"][k] / (med_hi or 1.0)
    if lo >= 1.55 and c >= 0.55:
        return "drop"
    if c <= 0.45 or lo <= 0.40:
        return "low"
    return "mid"


def _change(feats, k):
    """块边界 k（即块 k-1 与 k 之间）的多特征变化分。"""
    if k <= 0 or k >= feats["n"]:
        return 0.0
    def d(key, log=True):
        a = feats[key][k - 1] + 1e-9
        b = feats[key][k] + 1e-9
        return abs(np.log10(b / a)) if log else abs(b - a)
    return max(d("Elow"), d("Ehigh"), d("flux"), d("cur", log=False) * 2.0)


def cuts_to_edges(cuts, ga, bl):
    return [max(0.0, float(ga) + int(c) * BLOCK_BEATS * float(bl)) for c in cuts]


def structure_edges(f, dur, ga, bl):
    """核心入口：返回 8 拍对齐的边界秒数列表（含 0，不含曲尾）。"""
    feats = block_features(f, dur, ga, bl)
    n = feats["n"]
    if n < 4:
        return []
    cur = feats["cur"]
    live = cur > max(0.15, float(np.median(cur)) * 0.3)
    med_lo = float(np.median(feats["Elow"][live])) if live.any() else 1.0
    med_hi = float(np.median(feats["Ehigh"][live])) if live.any() else 1.0

    X = np.vstack([
        cur,
        np.log10(feats["Elow"] + 1.0),
        np.log10(feats["Emid"] + 1.0),
        np.log10(feats["Ehigh"] + 1.0),
        np.log10(feats["flux"] + 1.0),
        feats["rms"],
    ]).T
    X = (X - X.mean(0)) / (X.std(0) + 1e-9)
    cuts = _dp_cuts(X)

    def cls(i, j):
        seg = slice(int(i), int(j))
        c = float(cur[seg].mean())
        lo = float(feats["Elow"][seg].mean()) / (med_lo or 1.0)
        if lo >= 1.55 and c >= 0.55:
            return "drop"
        if c <= 0.45 or lo <= 0.40:
            return "low"
        return "mid"

    changed = True
    _dbg = os.environ.get("ERLIB_STRUCT_DEBUG") == "1"
    if _dbg:
        print("[struct] dp cuts:", cuts)
    while changed:
        changed = False
        # a. 两个相邻 8 拍段、都低于 drop、合并 16 拍 ⇒ 合并
        for idx in range(1, len(cuts) - 1):
            i0, i1, i2 = cuts[idx - 1], cuts[idx], cuts[idx + 1]
            if (i1 - i0) == 1 and (i2 - i1) == 1:
                c0, c1 = cls(i0, i1), cls(i1, i2)
                if c0 != "drop" and c1 != "drop":
                    del cuts[idx]
                    changed = True
                    break
        if changed:
            continue
        # b. 相邻同类、合并后合法且边界变化小 ⇒ 合并
        for idx in range(1, len(cuts) - 1):
            i0, i1, i2 = cuts[idx - 1], cuts[idx], cuts[idx + 1]
            if cls(i0, i1) != cls(i1, i2):
                continue
            if (i2 - i0) not in _ALLOWED:
                continue
            if _change(feats, i1) < _MERGE_SAME:
                del cuts[idx]
                changed = True
                break
        if changed:
            continue
        # b2. 两个相邻非 drop 段、合并成 32/64 拍、且边界变化小 ⇒ 合并
        for idx in range(1, len(cuts) - 1):
            i0, i1, i2 = cuts[idx - 1], cuts[idx], cuts[idx + 1]
            if "drop" in (cls(i0, i1), cls(i1, i2)):
                continue
            if (i2 - i0) not in (4, 6, 7):
                continue
            if _change(feats, i1) < 0.26:
                del cuts[idx]
                changed = True
                break
        if changed:
            continue
        # b5. merge two non-drop segments into a 48-beat phrase before the first drop
        for idx in range(1, len(cuts) - 1):
            i0, i1, i2 = cuts[idx - 1], cuts[idx], cuts[idx + 1]
            if (i2 - i0) != 6:
                continue
            if "drop" in (cls(i0, i1), cls(i1, i2)):
                continue
            _fd = None
            for _k in range(1, len(cuts)):
                if cls(cuts[_k - 1], cuts[_k]) == "drop":
                    _fd = cuts[_k - 1]
                    break
            if _fd is not None and i2 > _fd:
                continue
            del cuts[idx]
            changed = True
            break
        if changed:
            continue
        # b3. 两个相邻非 drop 段合并恰好 64 拍、且边界变化不算大 ⇒ 合并
        #     （breakdown 内部常被高频起伏切成 32+32，用户按 64 拍看）
        for idx in range(1, len(cuts) - 1):
            i0, i1, i2 = cuts[idx - 1], cuts[idx], cuts[idx + 1]
            if (i2 - i0) != 8:
                continue
            if "drop" in (cls(i0, i1), cls(i1, i2)):
                continue
            c0 = float(cur[int(i0):int(i1)].mean())
            c1 = float(cur[int(i1):int(i2)].mean())
            if c0 >= 0.35 and c1 >= 0.35 and _change(feats, i1) < 0.75:
                del cuts[idx]
                changed = True
                break
        if changed:
            continue
        # b4. 56 + 16 紧邻 drop ⇒ 64 + 8（用户要的「64 后面跟 8 拍过渡」）
        for idx in range(0, len(cuts) - 2):
            i0, i1, i2 = cuts[idx], cuts[idx + 1], cuts[idx + 2]
            if (i1 - i0) == 7 and (i2 - i1) == 2 and "drop" not in (cls(i0, i1), cls(i1, i2)):
                nxt = cuts[idx + 3] if idx + 3 < len(cuts) else None
                if nxt is not None and cls(i2, nxt) == "drop" and cls(i1, i2) == "mid":
                    cuts[idx + 1] = i2 - 1
                    changed = True
                    break
        if changed:
            continue
        # c. 72 拍段紧邻 drop ⇒ 64 + 8
        for idx in range(0, len(cuts) - 1):
            i0, i1 = cuts[idx], cuts[idx + 1]
            if (i1 - i0) == 9 and idx + 1 < len(cuts) - 1 and cls(i1, cuts[idx + 2]) == "drop":
                cuts.insert(idx + 1, i0 + 8)
                changed = True
                break
        if changed:
            continue
        # d. >64 拍 ⇒ 64 + 余数（余数 <8 并入前半）
        for idx in range(0, len(cuts) - 1):
            i0, i1 = cuts[idx], cuts[idx + 1]
            if (i1 - i0) > 8:
                cut = i0 + 8
                if (i1 - cut) < 1:
                    cut = i1 - 1
                cuts.insert(idx + 1, cut)
                changed = True
                break
    # e. 末尾静音段合并成一条尾奏
    while len(cuts) > 2:
        k = cuts[-2]
        # 合并后尾段不能超过 8 块（64 拍），否则不并
        if (cuts[-1] - k) > 8:
            break
        if feats["cur"][k] < 0.12 and feats["cur"][min(k + 1, n - 1)] < 0.12:
            cuts.pop(-2)
        else:
            break
    if _dbg:
        print("[struct] final cuts:", cuts, " n=", n,
              " blocks=", [cuts[k + 1] - cuts[k] for k in range(len(cuts) - 1)])
    # g. 56 拍段之后的 16 拍低能过渡：两半响度/能量差异明显时切成 8+8
    #    （2026-11-16 用户要求：build up 的尾巴两段声音不一样，倾向切成两段）
    for _idx in range(0, len(cuts) - 2):
        _i0, _i1, _i2 = cuts[_idx], cuts[_idx + 1], cuts[_idx + 2]
        if (_i1 - _i0) != 7 or (_i2 - _i1) != 2:
            continue
        if cls(_i1, _i2) != "low":
            continue
        _dc = abs(float(feats["cur"][_i1]) - float(feats["cur"][_i1 + 1]))
        _dr = abs(float(feats["rms"][_i1]) - float(feats["rms"][_i1 + 1])) / max(
            1e-6, float(feats["rms"][_i1 + 1]))
        if _dc >= 0.05 or _dr >= 0.10:
            cuts.insert(_idx + 2, _i1 + 1)
            break

    # g2. 段数上限：过碎时反复合并「最短且变化最小的非 drop 相邻对」
    _limit = 26
    _guard2 = 0
    while len(cuts) - 1 > _limit and _guard2 < 200:
        _guard2 += 1
        _best = None
        for _idx in range(1, len(cuts) - 1):
            _i0, _i1, _i2 = cuts[_idx - 1], cuts[_idx], cuts[_idx + 1]
            if "drop" in (cls(_i0, _i1), cls(_i1, _i2)):
                continue
            _m = _i2 - _i0
            if _m > 8:
                continue
            _ch = _change(feats, _i1)
            _key = (_m, _ch)
            if _best is None or _key < _best[0]:
                _best = (_key, _idx)
        if _best is None:
            break
        del cuts[_best[1]]

    # f. 硬上限：合并规则可能把段又拼回 >64 拍，这里做最后一次硬切（保证 <=8 块）
    _guard = 0
    while _guard < 64:
        _guard += 1
        _hit = False
        for _idx in range(0, len(cuts) - 1):
            _i0, _i1 = cuts[_idx], cuts[_idx + 1]
            if (_i1 - _i0) > 8:
                cuts.insert(_idx + 1, _i0 + 8)
                _hit = True
                break
        if not _hit:
            break

    cuts = _phrase_after_ramp(cuts, feats)
    cuts = _align_class_change(cuts, feats, med_lo)
    return cuts_to_edges(cuts, ga, bl)


def _class_block(feats, k, med_lo):
    if k < 0 or k >= int(feats["n"]):
        return "low"
    c = float(feats["cur"][k])
    lo = float(feats["Elow"][k]) / max(float(med_lo), 1e-9)
    if lo >= 1.55 and c >= 0.55:
        return "drop"
    if c <= 0.45 or lo <= 0.40:
        return "low"
    return "mid"


def _align_class_change(cuts, feats, med_lo):
    """Move a boundary that sits inside a homogeneous class run to the real
    class transition one 8-beat block away. Audio-feature based; no song names
    or fixed segment lengths.
    """
    if len(cuts) < 3:
        return cuts
    out = list(cuts)
    for _ in range(4):
        changed = False
        for i in range(1, len(out) - 1):
            c = int(out[i])
            prev_c = int(out[i - 1])
            next_c = int(out[i + 1])
            if c - 1 <= prev_c or c + 1 >= next_c:
                continue
            left = _class_block(feats, c - 1, med_lo)
            cur = _class_block(feats, c, med_lo)
            right = _class_block(feats, c + 1, med_lo)
            # boundary is inside a same-class run; the actual change is at c+1
            if (left == cur and cur != right
                    and c + 1 - prev_c <= 8):
                # 只允许移到仍然合法的 8/16/32/64 拍边界，避免把 16/64out[i] = c + 1
                changed = True
                break
        if not changed:
            break
    return sorted(set(out))


def _phrase_after_ramp(cuts, feats):
    """After a detected 8-beat build/ramp, phrase the following high chain
    in 32-beat units, stopping at an existing structural boundary.

    This is driven by block features (cur/rms), not by a per-song template:
      · ramp block: energy rises from the previous block and continues up
        into a clear drop at the next block;
      · only on a 32-beat (4-block) phrase line;
      · the chain end must already be a DP boundary, and the chain must be
        4..16 blocks long. No fixed 64/8/32/32/32/32 layout.
    """
    n = int(feats["n"])
    cur = feats["cur"]
    rms = feats["rms"]
    if n < 8:
        return cuts
    for k in range(4, n - 2, 4):
        # block k is the build; k+1 is the drop
        if not (cur[k - 1] < cur[k] < cur[k + 1]):
            continue
        if cur[k] - cur[k - 1] < 0.12 or cur[k + 1] - cur[k] < 0.20:
            continue
        if rms[k] - rms[k - 1] < 0.06 or rms[k + 1] - rms[k] < 0.14:
            continue
        if cur[k + 1] < 0.65:
            continue
        ends = [c for c in cuts
                if c > k + 1 and (c - (k + 1)) % 4 == 0
                and 4 <= c - (k + 1) <= 16]
        if not ends:
            continue
        end = max(ends)
        keep = [c for c in cuts if c <= k or c >= end]
        keep = sorted(set(keep + [k, k + 1, end]))
        keep += [k + 1 + 4 * j
                 for j in range(1, (end - (k + 1)) // 4)]
        return sorted(set(keep))
    return cuts


def strict_32_64_edges(f, dur, ga, bl):
    """32/64-beat-only phrase segmentation (strict candidate).

    Uses the same 8-beat block features and DP as the normal structure pass,
    but allows only 4-block (32-beat) or 8-block (64-beat) sections. The tail
    shorter than 32 beats is kept as the final short section.
    """
    feats = block_features(f, dur, ga, bl)
    n = int(feats["n"])
    n4 = (n // 4) * 4
    if n4 < 8:
        return []
    X = np.vstack([
        feats["cur"],
        np.log10(feats["Elow"] + 1.0),
        np.log10(feats["Emid"] + 1.0),
        np.log10(feats["Ehigh"] + 1.0),
        np.log10(feats["flux"] + 1.0),
        feats["rms"],
    ]).T
    X = (X - X.mean(0)) / (X.std(0) + 1e-9)
    lam = float(os.environ.get("ERLIB_STRICT_LAM", "1.2"))
    cuts = _dp_cuts(X[:n4], lam=lam, allowed=(4, 8))
    edges = [float(x) for x in cuts_to_edges(cuts, ga, bl)]
    tail = float(ga) + n4 * BLOCK_BEATS * float(bl)
    edges = [round(x, 3) for x in edges if 0.0 <= x < dur - 1e-6]
    if not edges:
        edges = [round(float(ga), 3)]
    if tail < dur - 0.5:
        edges.append(round(tail, 3))
    edges.append(round(float(dur), 3))
    return sorted(set(edges))


class EdgeList(list):
    """边界列表，带生产方式标记（general / strict），供 sections 跳过形状规则。"""
    mode = "general"


def _edge_list(edges, mode):
    out = EdgeList(edges)
    out.mode = mode
    return out


#: 严格候选只有在代价 ≤ 60% 通用候选时才启用；同时要求它没有漏掉
#: 通用结果命中的强变化（≥0.9）。Digimind 这类有独立 8 拍过渡的歌
#: 不会被严格网格吃掉；System Overloaded 这种旧分段明显错位的会切过来。
STRICT_GATE_RATIO = float(os.environ.get("ERLIB_STRICT_GATE", "0.60"))
STRICT_GATE_LAM = float(os.environ.get("ERLIB_STRICT_GATE_LAM", "1.2"))
STRICT_MISS_CHANGE = float(os.environ.get("ERLIB_STRICT_MISS", "0.90"))


def _feature_matrix(feats):
    X = np.vstack([
        feats["cur"],
        np.log10(feats["Elow"] + 1.0),
        np.log10(feats["Emid"] + 1.0),
        np.log10(feats["Ehigh"] + 1.0),
        np.log10(feats["flux"] + 1.0),
        feats["rms"],
    ]).T
    return (X - X.mean(0)) / (X.std(0) + 1e-9)


def _cuts_from_segments(segs, ga, bl, n):
    cuts = {0, n}
    for s in segs[1:]:
        b = (float(s["t0"]) - ga) / (BLOCK_BEATS * bl)
        if abs(b - round(b)) > 0.18:
            return None
        cuts.add(int(round(b)))
    return sorted(cuts)


def _cuts_from_edges(edges, ga, bl, n):
    cuts = {0, n}
    for e in edges:
        b = (float(e) - ga) / (BLOCK_BEATS * bl)
        if b < -0.18 or b > n + 0.05:
            continue
        if abs(b - round(b)) > 0.18:
            b = float(n)
        cuts.add(int(round(min(float(n), max(0.0, b)))))
    return sorted(cuts)


def _cut_cost(X, cuts, lam):
    if len(cuts) < 2:
        return 0.0
    cs = np.cumsum(np.vstack([np.zeros((1, X.shape[1])), X]), axis=0)
    sq = np.cumsum(np.r_[0.0, (X * X).sum(axis=1)])
    tot = 0.0
    for a, b in zip(cuts[:-1], cuts[1:]):
        if b <= a:
            continue
        m = cs[b] - cs[a]
        tot += sq[b] - sq[a] - float((m * m).sum()) / (b - a)
    return float(tot) + lam * (len(cuts) - 1)


def _strong_misses(feats, cuts, thr=STRICT_MISS_CHANGE):
    hit = set(cuts)
    n = int(feats["n"])
    return sum(1 for k in range(1, n)
               if _change(feats, k) >= thr and k not in hit)


def _strict_candidate_wins(f, dur, grid, ga, bl, general_edges, strict_edges):
    """音频证据是否支持把通用分段换成严格 32/64 主体。"""
    if not general_edges or len(general_edges) < 3:
        return False
    if not strict_edges or len(strict_edges) < 3:
        return False
    try:
        import sections as S
        gsegs = S.segments_from_edges(f, general_edges, dur=dur,
                                      grid=grid, post=False)
    except Exception:                                          # noqa: BLE001
        return False
    feats = block_features(f, dur, ga, bl)
    n = int(feats["n"])
    if n < 4:
        return False
    X = _feature_matrix(feats)
    gcuts = _cuts_from_segments(gsegs, ga, bl, n)
    scuts = _cuts_from_edges(strict_edges, ga, bl, n)
    if not gcuts or not scuts or len(gcuts) < 2 or len(scuts) < 2:
        return False
    cg = _cut_cost(X, gcuts, STRICT_GATE_LAM)
    cs = _cut_cost(X, scuts, STRICT_GATE_LAM)
    if os.environ.get("ERLIB_STRICT_GATE_DEBUG") == "1":
        print("[strict-gate] cg=%.2f cs=%.2f ratio=%.3f miss_g=%d miss_s=%d"
              % (cg, cs, cs / max(cg, 1e-9),
                 _strong_misses(feats, gcuts), _strong_misses(feats, scuts)))
        print("[strict-gate] gcuts=%s" % (gcuts,))
        print("[strict-gate] scuts=%s" % (scuts,))
    if cs > STRICT_GATE_RATIO * cg:
        return False
    if _strong_misses(feats, scuts) > _strong_misses(feats, gcuts):
        return False
    return True


def structure_edges_hybrid(f, dur, grid, base_segs):
    """结构 DP 为主；边界太少（DP 失败）时回退旧能量分段边界。

    同时生成严格 32/64 主体候选；只有当音频代价 ≤ 通用结果的 60%、
    且没有漏掉通用结果命中的强变化时，才切到严格候选。
    """
    ga = float(grid["anchor"])
    bl = float(grid["beat_len"])
    global LAST_MODE
    try:
        se = structure_edges(f, dur, ga, bl)
    except Exception:                                        # noqa: BLE001
        se = []
    try:
        ss = strict_32_64_edges(f, dur, ga, bl)
        if _strict_candidate_wins(f, dur, grid, ga, bl, se, ss):
            LAST_MODE = "strict"
            return _edge_list(ss, "strict")
    except Exception:                                        # noqa: BLE001
        pass
    LAST_MODE = "general"
    if len(se) >= 3:
        return _edge_list(se, "general")
    out = {round(float(s["t0"]), 3) for s in (base_segs or [])}
    out.add(round(float(dur), 3))
    return _edge_list(sorted(out), "general")
