# -*- coding: utf-8 -*-
"""AutoVJ 离线节拍分析工具。

用途
----
1) 离线估计 BPM（多候选 + 对齐置信度）—— 用于**验证**实时引擎的 BPM 是否准确；
2) 在给定时间区间内生成拍位网格（相位自动搜索，可容忍 ±1s 的标注误差）；
3) 识别「八拍乐句」边界（8 拍一组的第 1 拍）。

环境限制：项目 venv 只有 numpy + soundfile（无 librosa / scipy），因此全部用
numpy 手写实现（STFT / 谱通量 / 相位折叠打分）。

命令行
------
    python tools/beat_analyze.py "<file.flac>" [--maxsec 120] [--user-bpm 128] \
                                 [--seg 0 30 64] [--seg 60 91 64]
时间写法：`分.秒`（`1.31` = 1 分 31 秒）或纯秒数（`91` / `91.5`）。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import soundfile as sf

HOP = 512          # STFT 步长（44.1k 下 ≈ 11.6 ms）
WIN = 2048         # STFT 窗长（≈ 46 ms）

# 频带边界
F_LOW = (30.0, 200.0)      # kick / 低音
F_MID = (200.0, 2000.0)    # 人声 / 主奏
F_HIGH = (2000.0, 9000.0)  # hihat / 齿音


# --------------------------------------------------------------------------
# 时间解析
# --------------------------------------------------------------------------
def parse_time(s):
    """支持 `分.秒`（1.31 = 1分31秒）、`分.百分秒`（1.31 若是两位则当秒）、纯秒。

    规则：含 `.` 时，小数点前 = 分钟；小数点后两位 = 秒（00~59）；
    三位 = 毫秒。纯数字 = 秒。"""
    s = str(s).strip()
    if "." in s:
        a, b = s.split(".", 1)
        a = int(a or 0)
        if len(b) == 3:
            return a * 60 + int(b) / 1000.0
        if len(b) <= 2:
            return a * 60 + int(b or 0)
        return a * 60 + float("0." + b) * 100  # 兜底：当小数秒
    return float(s)


def fmt_t(sec):
    """秒 → `分:秒.毫秒`"""
    m = int(sec // 60)
    r = sec - m * 60
    return "%d:%06.3f" % (m, r)


# --------------------------------------------------------------------------
# 加载 / 特征
# --------------------------------------------------------------------------
def load_mono(path, maxsec=240.0):
    info = sf.info(path)
    sr = int(info.samplerate)
    frames = -1 if maxsec is None else int(maxsec * sr)
    data, sr = sf.read(path, dtype="float32", always_2d=True, frames=frames)
    mono = data.mean(axis=1).astype(np.float32)
    del data
    return mono, sr, float(info.duration)


def features(mono, sr, hop=HOP, win=WIN):
    """分块 STFT → 频带能量 / 谱通量 / 低频谱质心。"""
    w = np.hanning(win).astype(np.float32)
    n = len(mono)
    if n < win:
        mono = np.concatenate([mono, np.zeros(win - n, np.float32)])
        n = len(mono)
    nf = 1 + (n - win) // hop
    sw = np.lib.stride_tricks.sliding_window_view(mono, win)
    fr = np.ascontiguousarray(sw[::hop][:nf])
    S = np.abs(np.fft.rfft(fr * w, axis=1)).astype(np.float32)
    del fr
    freqs = np.fft.rfftfreq(win, 1.0 / sr).astype(np.float32)
    dt = hop / float(sr)

    m_low = (freqs >= F_LOW[0]) & (freqs < F_LOW[1])
    m_mid = (freqs >= F_MID[0]) & (freqs < F_MID[1])
    m_high = (freqs >= F_HIGH[0]) & (freqs < F_HIGH[1])

    Elow = (S[:, m_low] ** 2).sum(1)
    Emid = (S[:, m_mid] ** 2).sum(1)
    Ehigh = (S[:, m_high] ** 2).sum(1)

    # 谱通量（对数压缩，正向差分半波整流）
    L = np.log1p(S * 8.0)
    d = np.diff(L, axis=0, prepend=L[:1, :])
    dn = np.maximum(d, 0.0)
    flux = dn.sum(1)
    flux_low = dn[:, m_low].sum(1)
    del L, d, dn

    fl = freqs[m_low]
    sl = S[:, m_low]
    c_low = (sl * fl).sum(1) / (sl.sum(1) + 1e-9)   # 低频谱质心（Hz）

    return dict(dt=dt, nf=nf, sr=sr, freqs=freqs,
                S=S, Elow=Elow, Emid=Emid, Ehigh=Ehigh,
                flux=flux, flux_low=flux_low, c_low=c_low,
                m_low=m_low, m_mid=m_mid, m_high=m_high)


def onset_env(f, mode="mix"):
    """节拍对齐用的 onset 包络。默认 mix = 全频 + 加强低频（kick）。"""
    if mode == "low":
        v = f["flux_low"].astype(np.float64)
    elif mode == "full":
        v = f["flux"].astype(np.float64)
    else:
        fl = f["flux_low"].astype(np.float64)
        ft = f["flux"].astype(np.float64)
        fl = fl / (np.median(fl) + 1e-12)
        ft = ft / (np.median(ft) + 1e-12)
        v = ft + 1.5 * fl
    v = np.maximum(v - np.median(v), 0.0)
    m = v.max()
    return v / (m if m > 0 else 1.0)


# --------------------------------------------------------------------------
# BPM：相位折叠集中度打分
# --------------------------------------------------------------------------
def grid_conc(t, env, period, nbins=32):
    """把 onset 包络按 `period` 折叠成相位直方图，返回 (集中度, 直方图)。

    集中度 = max/mean（完美对齐 ≈ nbins，完全乱 ≈ 1）。"""
    ph = (t / period) % 1.0
    b = np.minimum((ph * nbins).astype(np.int32), nbins - 1)
    h = np.bincount(b, weights=env, minlength=nbins)
    s = h.sum()
    if s <= 1e-12:
        return 0.0, h
    h = h / s
    return float(h.max() / (h.mean() + 1e-12)), h


def scan_bpm(f, env, lo=60.0, hi=200.0, step=0.05, nbins=32, maxsec=None):
    dt = f["dt"]
    n = env.shape[0] if maxsec is None else min(env.shape[0], int(maxsec / dt))
    t = (np.arange(n) * dt).astype(np.float64)
    e = env[:n].astype(np.float64)
    bpms = np.arange(lo, hi + 1e-9, step)
    sc = np.empty(bpms.shape[0], dtype=np.float32)
    for i, b in enumerate(bpms):
        sc[i] = grid_conc(t, e, 60.0 / float(b), nbins)[0]
    return bpms, sc


def local_peaks(x, k=10, min_sep=3.0):
    """返回 (值, 位置值) 的降序局部峰列表，峰间至少隔 min_sep。"""
    order = np.argsort(-x)
    out = []
    for i in order:
        pos = float(i)
        if all(abs(pos - o[1]) >= min_sep for o in out):
            out.append((float(x[i]), pos))
        if len(out) >= k:
            break
    return out


def phase_of(f, env, period, nbins=128, maxsec=None):
    """搜索最优拍相位（秒，落在 [0, period)）。"""
    dt = f["dt"]
    n = env.shape[0] if maxsec is None else min(env.shape[0], int(maxsec / dt))
    t = (np.arange(n) * dt).astype(np.float64)
    e = env[:n].astype(np.float64)
    conc, h = grid_conc(t, e, period, nbins)
    if h.sum() <= 1e-12:
        return 0.0, 0.0
    i = int(np.argmax(h))
    y0, y1, y2 = h[(i - 1) % nbins], h[i], h[(i + 1) % nbins]
    den = y0 - 2 * y1 + y2
    off = 0.0 if abs(den) < 1e-12 else 0.5 * (y0 - y2) / den
    off = max(-0.5, min(0.5, off))
    frac = ((i + off) % nbins) / float(nbins)
    return float(frac * period), conc


def beat_grid(t0, t1, period, phase):
    """生成 [t0-1, t1+1] 内的拍位（秒）。"""
    k0 = int(np.ceil((t0 - 1.0 - phase) / period))
    k1 = int(np.floor((t1 + 1.0 - phase) / period))
    ks = np.arange(k0, k1 + 1)
    return phase + ks * period


# --------------------------------------------------------------------------
# 每拍特征 → 八拍乐句相位
# --------------------------------------------------------------------------
def per_beat_feats(f, beats, span_ratio=0.95):
    """按拍窗口取每拍的特征（峰值/均值）。"""
    dt = f["dt"]
    t = np.arange(f["nf"]) * dt
    period = float(beats[1] - beats[0]) if len(beats) > 1 else 0.5
    keys = ["Elow", "Emid", "Ehigh", "flux", "flux_low", "c_low"]
    out = {k: np.zeros(len(beats), dtype=np.float32) for k in keys}
    for i, bt in enumerate(beats):
        i0 = int(np.searchsorted(t, bt))
        i1 = int(np.searchsorted(t, bt + span_ratio * period))
        if i1 <= i0:
            i1 = min(i0 + 1, f["nf"])
        if i0 >= f["nf"]:
            continue
        for k in keys:
            seg = f[k][i0:i1]
            out[k][i] = float(np.mean(seg)) if k == "c_low" else float(np.max(seg))
    return out


def _nz(a):
    m = float(np.median(a))
    return a / (m + 1e-12)


def phrase_scores(bf, ks=None, nbeat=8, w=None):
    """八拍组相位打分（长度 nbeat）。

    `ks` = 每个采样拍对应的**全局拍号**数组。**必须传**：否则分组用「数组下标」，
    而数组起点（beat_grid 为了取完整窗口会向前多给 ~2 拍）与全局拍号差一个常数，
    会把答案整体平移（2026-09-26 实测：正是「八拍开头晚 2 拍」的原因）。

    分组规则：拍号 k ≡ p (mod nbeat) 的所有拍归入相位 p。
    第 1 拍通常：低频更重 + 谱通量更大 + 低音换音（低频谱质心跳变）。
    """
    if w is None:
        w = {"low": 1.0, "flux": 0.6, "change": 0.9, "high": 0.25}
    n = len(bf["Elow"])
    el = _nz(bf["Elow"].astype(np.float64))
    eh = _nz(bf["Ehigh"].astype(np.float64))
    fl = _nz(bf["flux"].astype(np.float64))
    cl = bf["c_low"].astype(np.float64)
    dcl = _nz(np.abs(np.diff(cl, prepend=cl[:1])))

    if ks is None:
        ks = np.arange(n)
    ks = np.asarray(ks)

    s = np.zeros(nbeat, dtype=np.float64)
    for p in range(nbeat):
        idx = np.flatnonzero((ks % nbeat) == p)
        if idx.size == 0:
            continue
        s[p] = (w["low"] * el[idx].mean()
                + w["flux"] * fl[idx].mean()
                + w["change"] * dcl[idx].mean()
                + w["high"] * eh[idx].mean())
    s = s - s.min()
    tot = s.sum()
    return s / (tot if tot > 0 else 1.0)


def segment_report(path, seg=None, user_bpm=0.0, maxsec=240.0,
                   bpm_range=(60.0, 200.0), verbose=True):
    """对单个文件做完整分析，返回结果 dict。"""
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    mono, sr, dur_full = load_mono(path, maxsec=maxsec)
    f = features(mono, sr)
    del mono
    env = onset_env(f, "mix")

    res = {"path": path, "sr": sr, "dur_full": dur_full,
           "dur_analyzed": f["nf"] * f["dt"], "user_bpm": float(user_bpm),
           "segs": []}

    bpms, sc = scan_bpm(f, env, lo=bpm_range[0], hi=bpm_range[1])
    peaks = local_peaks(sc, k=8, min_sep=3.0)
    res["bpm_peaks"] = [(float(bpms[int(p)]), float(v)) for v, p in peaks]

    if verbose:
        print("  文件: %s" % os.path.basename(path))
        print("  采样率 %d Hz，总长 %s，本次分析 %s"
              % (sr, fmt_t(dur_full), fmt_t(res["dur_analyzed"])))
        print("  --- BPM 候选（相位折叠集中度，越大越可信）---")
        for b, v in res["bpm_peaks"]:
            print("      %7.2f BPM   得分 %.2f" % (b, v))

    # 对每个标注区间做分析
    if seg:
        for (t0, t1, nbeat) in seg:
            item = {"t0": t0, "t1": t1, "beats_claim": nbeat}
            dur = t1 - t0
            item["bpm_from_seg"] = (nbeat / dur * 60.0) if dur > 0 else 0.0
            # 用「区间推导 BPM」附近的候选做网格（±6% 内取 scan 最高分）
            target = item["bpm_from_seg"] or user_bpm or 128.0
            band = (bpms >= target * 0.90) & (bpms <= target * 1.10)
            if band.any():
                i_loc = int(np.argmax(sc[band]))
                bpm_sel = float(bpms[np.flatnonzero(band)[i_loc]])
            else:
                bpm_sel = float(target)
            item["bpm_sel"] = bpm_sel
            period = 60.0 / bpm_sel
            ph, conc = phase_of(f, env, period, maxsec=t1 + 5.0)
            item["phase"] = ph
            item["phase_conc"] = conc
            grid = beat_grid(t0 - 0.6, t1 + 0.6, period, ph)
            inside = grid[(grid >= t0 - 0.6) & (grid <= t1 + 0.6)]
            item["n_beats_grid"] = int(((grid >= t0) & (grid <= t1)).sum())
            item["grid_edge"] = [float(inside[0]) if inside.size else None,
                                 float(inside[-1]) if inside.size else None]
            # 端点贴合度（拍数）
            ne = np.round((t0 - ph) / period)
            item["t0_off_beats"] = float((t0 - (ph + ne * period)) / period)
            ne2 = np.round((t1 - ph) / period)
            item["t1_off_beats"] = float((t1 - (ph + ne2 * period)) / period)
            # 逐拍 onset 对齐质量
            ti = (np.arange(f["nf"]) * f["dt"])
            idx = np.searchsorted(ti, grid)
            idx = idx[(idx >= 0) & (idx < f["nf"])]
            if idx.size:
                item["beat_align"] = float(env[idx].mean() / (env.mean() + 1e-12))
            # 四踩检测：拍位 vs 半拍位的低频 onset 强度比
            # ≈1 → 每拍都有 kick（四踩）；明显 >1 → kick 只在部分拍上（此时真实
            # 「beat」可能是本 BPM 的 1/2，BPM 存在半速歧义）。
            ti = np.arange(f["nf"]) * f["dt"]
            half = grid + period * 0.5
            ih = np.searchsorted(ti, half)
            ih = ih[(ih >= 0) & (ih < f["nf"])]
            ib = np.searchsorted(ti, grid)
            ib = ib[(ib >= 0) & (ib < f["nf"])]
            env_low = onset_env(f, "low")
            if ib.size and ih.size:
                item["beat_vs_half"] = float(env_low[ib].mean() / (env_low[ih].mean() + 1e-12))
            else:
                item["beat_vs_half"] = 0.0

            # 八拍乐句 —— ★ 统一用「全局拍号 k」坐标系：拍位 t = ph + k*period。
            # ka/kb 向前后各多给一拍，保证 per_beat_feats 的窗口完整。
            # （旧实现把「数组下标 mod 8」当成「全局拍号 mod 8」，因 beat_grid 会
            #   向前多给 2 拍，导致八拍开头整体晚 2 拍 —— 2026-09-26 用户反馈证实。）
            ka = int(np.floor((t0 - ph) / period))
            kb = int(np.ceil((t1 - ph) / period))
            ks = np.arange(ka, kb + 1)
            beats = ph + ks * period
            ok = beats >= 0.0
            ks, beats = ks[ok], beats[ok]
            bf = per_beat_feats(f, beats)
            item["nbeats_for_phrase"] = int(len(ks))
            ps = phrase_scores(bf, ks)
            item["phrase_scores"] = [float(x) for x in ps]
            best = int(np.argmax(ps))
            item["phrase_best"] = best
            item["phrase_starts"] = [float(ph + k * period)
                                     for k in ks if (k % 8) == best]
            item["n_beats_grid"] = int(((beats >= t0) & (beats <= t1)).sum())
            res["segs"].append(item)

            if verbose:
                print()
                print("  --- 区间 %s ~ %s（标注 %d 拍）---"
                      % (fmt_t(t0), fmt_t(t1), nbeat))
                print("    区间时长 %.2fs → 推导 BPM %.2f（标注 %s）"
                      % (dur, item["bpm_from_seg"],
                         ("%.0f" % user_bpm) if user_bpm else "—"))
                print("    网格 BPM %.2f（相位 %.3fs，集中度 %.2f）"
                      % (bpm_sel, ph, conc))
                print("    区间内检测到 %d 拍（标注 %d，差 %+d）"
                      % (item["n_beats_grid"], nbeat, item["n_beats_grid"] - nbeat))
                print("    端点贴合：起点偏 %+.2f 拍，终点偏 %+.2f 拍"
                      % (item["t0_off_beats"], item["t1_off_beats"]))
                print("    拍上 onset 对齐度 %.2f（>1.5 为好）"
                      % item.get("beat_align", 0.0))
                bh = item.get("beat_vs_half", 0.0)
                hint = "四踩（每拍都有 kick）" if bh < 1.35 else \
                       ("kick 只在部分拍上 → BPM 可能有 1/2 半速歧义" if bh > 1.8 else "中间态")
                print("    拍位/半拍位 低频强度比 %.2f  → %s" % (bh, hint))
                print("    八拍相位打分: %s  → 第 1 拍相位 = %d"
                      % (" ".join("%.3f" % x for x in ps), best))
                print("    八拍边界（该段内每 8 拍的第 1 拍）:")
                for j, ts in enumerate(item["phrase_starts"]):
                    if ts < t0 - period or ts > t1 + period:
                        continue
                    print("        #%02d  %s" % (j + 1, fmt_t(ts)))
    return res


# --------------------------------------------------------------------------
def main():
    import argparse
    ap = argparse.ArgumentParser(description="离线节拍/BPM/八拍分析")
    ap.add_argument("file")
    ap.add_argument("--seg", nargs=3, action="append", metavar=("T0", "T1", "N"),
                    help="标注区间：起点 终点 拍数（时间写 分.秒 或秒）")
    ap.add_argument("--user-bpm", type=float, default=0.0)
    ap.add_argument("--maxsec", type=float, default=240.0)
    ap.add_argument("--lo", type=float, default=60.0)
    ap.add_argument("--hi", type=float, default=200.0)
    a = ap.parse_args()

    seg = [(parse_time(x[0]), parse_time(x[1]), int(x[2])) for x in (a.seg or [])]
    print("=" * 74)
    segment_report(a.file, seg=seg or None, user_bpm=a.user_bpm,
                   maxsec=a.maxsec, bpm_range=(a.lo, a.hi))
    print("=" * 74)


if __name__ == "__main__":
    main()
