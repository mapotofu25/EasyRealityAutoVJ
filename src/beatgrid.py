# -*- coding: utf-8 -*-
"""节拍网格（Beat Grid）：BPM / 锚点 / 八拍乐句相位。

★ 设计前提（用户 2026-09-27 明确的硬约束：**一切为实时演出服务**）：
   凡是需要「整首歌的信息」才能判准的东西（BPM、相位、尤其是 **8 拍乐句相位**），
   **只在建库（扫描曲库）时离线算一次并入库**；演出中只做**查表**（O(1)），
   绝不实时去猜结构。实时特征检测只作为「曲库外音乐」的兜底。

数据来源（按优先级）：
  1) **VirtualDJ 数据库**（`database.xml`，路径自动探测，见 `vdj_db_path()`；也支持
     用环境变量 `AUTO_VJ_VDJ_DB` 显式指定）
     —— BPM + 锚点 + 段落标记，可信度最高，且成本为零（只读 XML）
     注意：VDJ 的 `Scan@Bpm` 是**每拍秒数**（真实 BPM = 60/它），`Phase` 才是锚点。
  2) **本地离线分析**（纯 numpy，无 librosa/scipy 依赖）—— 自算 BPM + 锚点 + 8 拍相位

统一约定（`compute_grid` 的输出）：
    beat_len = 60 / bpm
    **八拍头（乐句第 1 拍）的位置 = anchor + 8n * beat_len**   （n = 0, ±1, …）
    也就是说 `anchor` 统一表示「某个八拍头」的秒数 —— 不论它来自 VDJ 还是自算，
    消费方（引擎）只需这一条公式。
    `phrase` 记录它相对上游锚点的偏移（0=首个八拍头就在锚点处；4=要往后推 4 拍），
    仅用于诊断。

存储：`cfg["music_meta"][path]["grid"]`（见 `store_grid`）。
"""
from __future__ import annotations

import math
import os
import re
import xml.etree.ElementTree as ET
from collections import defaultdict

import numpy as np

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
SR_TARGET = 22050          # 分析用采样率（节拍信息都在 20Hz~10kHz，够了；比 44.1k 省一半）
HOP = 1024                 # STFT 步长（22050 下 ≈ 46 ms）
WIN = 2048                 # STFT 窗长（≈ 93 ms）

F_LOW = (30.0, 200.0)      # kick / 低音
F_MID = (200.0, 2000.0)    # 人声 / 主奏
F_HIGH = (2000.0, 9000.0)  # hihat

BPM_MIN, BPM_MAX = 60.0, 200.0
BPM_PRIOR = (110.0, 190.0)  # DJ 音乐的先验区间：半速歧义时优先取落在这里的候选
PHRASE_MIN, PHRASE_MAX = 0.62, 1.30   # 八拍相位对比度阈值（低于此判定"证据不足"）

DEFAULT_VDJ = ""           # 不硬编码路径，统一由 vdj_db_path() 自动探测


# ==========================================================================
# 1. 特征提取
# ==========================================================================
def resample(mono, sr0, sr_target=SR_TARGET):
    """线性插值重采样。节拍检测不需要高保真（特征都在 20Hz~10kHz）。"""
    if not sr_target or sr0 == sr_target:
        return mono, sr0
    n_out = int(len(mono) * sr_target / sr0)
    if n_out <= 1:
        return mono, sr0
    idx = np.linspace(0.0, len(mono) - 1.0, n_out).astype(np.float32)
    i0 = np.floor(idx).astype(np.int32)
    i1 = np.minimum(i0 + 1, len(mono) - 1)
    frac = (idx - i0).astype(np.float32)
    out = mono[i0] * (1.0 - frac) + mono[i1] * frac
    return np.ascontiguousarray(out, dtype=np.float32), sr_target


def load_mono(path, maxsec=240.0, sr_target=SR_TARGET):
    """读取为单声道 float32，并重采样到 `sr_target`。返回 (mono, sr)。"""
    import soundfile as sf
    info = sf.info(path)
    sr0 = int(info.samplerate)
    frames = -1 if maxsec is None else int(maxsec * sr0)
    data, _ = sf.read(path, dtype="float32", always_2d=True, frames=frames)
    mono = data.mean(axis=1)
    del data
    return resample(np.ascontiguousarray(mono, dtype=np.float32), sr0, sr_target)


def features(mono, sr, hop=HOP, win=WIN):
    """分块 STFT → 频带能量 / 对数谱通量 / 低频谱质心。"""
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

    out = {
        "dt": dt, "nf": nf, "sr": sr, "freqs": freqs,
        "Elow": (S[:, m_low] ** 2).sum(1),
        "Emid": (S[:, m_mid] ** 2).sum(1),
        "Ehigh": (S[:, m_high] ** 2).sum(1),
    }
    L = np.log1p(S * 8.0)
    d = np.diff(L, axis=0, prepend=L[:1, :])
    dn = np.maximum(d, 0.0)
    out["flux"] = dn.sum(1)
    out["flux_low"] = dn[:, m_low].sum(1)
    fl = freqs[m_low]
    sl = S[:, m_low]
    out["c_low"] = (sl * fl).sum(1) / (sl.sum(1) + 1e-9)
    del L, d, dn, S
    return out


def onset_env(f, mode="mix"):
    """节拍对齐用的 onset 包络（归一化到 0~1）。"""
    if mode == "low":
        v = f["flux_low"].astype(np.float64)
    elif mode == "full":
        v = f["flux"].astype(np.float64)
    else:
        fl = f["flux_low"].astype(np.float64)
        ft = f["flux"].astype(np.float64)
        v = ft / (np.median(ft) + 1e-12) + 1.5 * fl / (np.median(fl) + 1e-12)
    v = np.maximum(v - np.median(v), 0.0)
    m = v.max()
    return v / (m if m > 0 else 1.0)


# ==========================================================================
# 2. BPM 与拍相位
# ==========================================================================
def grid_conc(t, env, period, nbins=32):
    """把 onset 按 `period` 折叠成相位直方图 → (集中度, 直方图)。集中度 = max/mean。"""
    ph = (t / period) % 1.0
    b = np.minimum((ph * nbins).astype(np.int32), nbins - 1)
    h = np.bincount(b, weights=env, minlength=nbins)
    s = h.sum()
    if s <= 1e-12:
        return 0.0, h
    h = h / s
    return float(h.max() / (h.mean() + 1e-12)), h


def scan_bpm(f, env, lo=BPM_MIN, hi=BPM_MAX, coarse=1.0, fine=0.05,
             topk=3, nbins=32):
    """粗扫 + 细扫两级，返回 [(bpm, score), …]（降序）。

    两级扫描是因为：曲库扫描要处理上千首，2800 个候选逐个算太慢。"""
    dt = f["dt"]
    t = (np.arange(env.shape[0]) * dt).astype(np.float64)
    e = env.astype(np.float64)

    coarse_bpms = np.arange(lo, hi + 1e-9, coarse)
    coarse_scores = np.array([grid_conc(t, e, 60.0 / b, nbins)[0]
                              for b in coarse_bpms])
    order = np.argsort(-coarse_scores)[:max(1, topk)]
    peaks = []
    for i in order:
        c = float(coarse_bpms[i])
        fine_bpms = np.arange(max(lo, c - coarse), min(hi, c + coarse) + 1e-9, fine)
        fine_scores = [grid_conc(t, e, 60.0 / b, nbins)[0] for b in fine_bpms]
        j = int(np.argmax(fine_scores))
        peaks.append((float(fine_bpms[j]), float(fine_scores[j])))
    peaks.sort(key=lambda x: -x[1])
    # 去重（相邻 1 BPM 内只留最高分）
    out = []
    for b, s in peaks:
        if all(abs(b - o[0]) > 1.0 for o in out):
            out.append((b, s))
    return out


def pick_bpm(peaks):
    """从候选里挑「真实 BPM」：优先落在 DJ 先验区间 [110,190]，且分数足够接近最高分。

    为什么必须这么做：把 onset 按周期折叠时，**半速值（BPM/2）往往得分更高**
    （kick 每 2 拍响一次时，周期翻倍会让所有 kick 落进同一相位）。
    实测 5 首里有 2 首的第一名是 64（真值 128）。"""
    if not peaks:
        return 0.0, 0.0
    best_s = peaks[0][1]
    inrange = [(b, s) for b, s in peaks if BPM_PRIOR[0] <= b <= BPM_PRIOR[1]]
    if inrange:
        for b, s in inrange:
            if s >= 0.70 * best_s:
                return b, s
        return inrange[0]
    return peaks[0]


def phase_of(f, env, period, nbins=128):
    """搜索最优拍相位（秒，落在 [0, period)）。"""
    dt = f["dt"]
    t = (np.arange(env.shape[0]) * dt).astype(np.float64)
    _, h = grid_conc(t, env.astype(np.float64), period, nbins)
    if h.sum() <= 1e-12:
        return 0.0, 0.0
    i = int(np.argmax(h))
    y0, y1, y2 = h[(i - 1) % nbins], h[i], h[(i + 1) % nbins]
    den = y0 - 2 * y1 + y2
    off = 0.0 if abs(den) < 1e-12 else 0.5 * (y0 - y2) / den
    off = max(-0.5, min(0.5, off))
    return float(((i + off) % nbins) / nbins * period), float(h.max() / (h.mean() + 1e-12))


# ==========================================================================
# 3. 每拍特征 → 八拍相位
# ==========================================================================
def per_beat_feats(f, beats, span_ratio=0.95):
    """按拍窗口取每拍的特征（峰值 / 低频谱质心均值）。"""
    dt = f["dt"]
    t = np.arange(f["nf"]) * dt
    period = float(beats[1] - beats[0]) if len(beats) > 1 else 0.5
    keys = ("Elow", "Emid", "Ehigh", "flux", "flux_low", "c_low")
    out = {k: np.zeros(len(beats), dtype=np.float32) for k in keys}
    for i, bt in enumerate(beats):
        i0 = int(np.searchsorted(t, bt))
        i1 = int(np.searchsorted(t, bt + span_ratio * period))
        if i0 >= f["nf"] or i1 <= i0:
            continue
        i1 = min(i1, f["nf"])
        for k in keys:
            seg = f[k][i0:i1]
            out[k][i] = float(np.mean(seg)) if k == "c_low" else float(np.max(seg))
    return out


def _nz(a):
    m = float(np.median(a))
    return a / (m + 1e-12)


def phrase_scores(bf, ks, nbeat=8, w=None):
    """八拍相位打分（长度 nbeat）。

    `ks` = 每个采样拍对应的**全局拍号**（必须传 —— 否则分组用数组下标，
    与输出时间戳的坐标系差一个常数，答案会整体平移；2026-09-26 踩过）。
    第 1 拍的判据：低频更重 + 谱通量更大 + 低音换音（低频谱质心跳变）+ 高频镲。"""
    if w is None:
        w = {"low": 1.0, "flux": 0.6, "change": 0.9, "high": 0.25}
    el = _nz(bf["Elow"].astype(np.float64))
    eh = _nz(bf["Ehigh"].astype(np.float64))
    fl = _nz(bf["flux"].astype(np.float64))
    cl = bf["c_low"].astype(np.float64)
    dcl = _nz(np.abs(np.diff(cl, prepend=cl[:1])))
    ks = np.asarray(ks)
    s = np.zeros(nbeat, dtype=np.float64)
    for p in range(nbeat):
        idx = np.flatnonzero((ks % nbeat) == p)
        if idx.size == 0:
            continue
        s[p] = (w["low"] * el[idx].mean() + w["flux"] * fl[idx].mean()
                + w["change"] * dcl[idx].mean() + w["high"] * eh[idx].mean())
    s = s - s.min()
    tot = s.sum()
    return s / (tot if tot > 0 else 1.0)


def phrase_phase(f, anchor, period, maxsec=None, nbeat=8):
    """在给定锚点（**视作干净的第 1 拍**）下，定「哪一个八拍头才是乐句头」。

    ★ `anchor` 必须传**原始锚点**（不要先折叠到 [0, period)）——
      折叠会丢掉「锚点就是小节头」这个信息，使答案整体平移若干拍
      （2026-09-27 实测：折叠后 Outburst 的八拍头落到 VDJ 第 3 拍上）。
      这里让拍号 k=0 恰好对应 anchor，拍网向两侧铺满全曲。

    返回 (phrase, contrast, scores)：
      phrase   ∈ {0, 4}（八拍头相对锚点的拍偏移）
      contrast = 首选/次选的得分比（≥PHRASE_MAX 视为证据足够）
    """
    dt = f["dt"]
    n = f["nf"] if maxsec is None else min(f["nf"], int(maxsec / dt))
    t_end = n * dt
    ks = np.arange(-int(math.floor(anchor / period)),
                   int(math.ceil((t_end - anchor) / period)) + 1)
    beats = anchor + ks * period
    ok = (beats >= 0.0) & (beats <= t_end)
    ks, beats = ks[ok], beats[ok]
    if len(ks) < 16:
        return 0, 0.0, [0.0] * nbeat
    bf = per_beat_feats(f, beats)
    sc = phrase_scores(bf, ks % nbeat, nbeat=nbeat)
    s0, s4 = float(sc[0]), float(sc[4])
    phrase = 0 if s0 >= s4 else 4
    hi, lo = max(s0, s4), min(s0, s4)
    contrast = (hi / lo) if lo > 1e-9 else 99.0
    return phrase, contrast, [float(x) for x in sc]


# ==========================================================================
# 4. 整曲离线分析
# ==========================================================================
def analyze_track(path, maxsec=180.0, anchor_hint=None, bpm_hint=None, do_scan=True):
    """读文件后分析（见 `analyze_signal`）。"""
    mono, sr = load_mono(path, maxsec=maxsec)
    return analyze_signal(mono, sr, anchor_hint=anchor_hint, bpm_hint=bpm_hint,
                          do_scan=do_scan)


def analyze_signal(mono, sr, anchor_hint=None, bpm_hint=None, do_scan=True):
    """从**已解码**的单声道信号分析 → {bpm, anchor, phrase, contrast, peaks, …}。

    - `bpm_hint` / `anchor_hint` 给定时（例如来自 VDJ），用它们做基准；
    - `do_scan=False` 时**跳过 BPM 扫描**（单首最贵的一步）—— 曲库扫描时用它提速。
    - 返回的 `anchor` **统一表示「某个八拍头」**（= 上游锚点 + phrase*beat_len）。
    """
    dur = len(mono) / float(sr)
    f = features(mono, sr)
    env = onset_env(f, "mix")

    if do_scan or not bpm_hint:
        peaks = scan_bpm(f, env)
        bpm_auto, _ = pick_bpm(peaks)
    else:
        peaks, bpm_auto = [], 0.0

    bpm = float(bpm_hint) if bpm_hint else bpm_auto
    if bpm <= 0:
        bpm = bpm_auto
    period = 60.0 / bpm if bpm > 0 else 0.5

    if anchor_hint is not None:
        # ★ 不要折叠到 [0, period)：锚点本身就是「小节第 1 拍」，
        #   折叠会丢掉这个信息，使八拍头整体平移（实测踩过）。
        base = float(anchor_hint)
        conc = 0.0
    else:
        base, conc = phase_of(f, env, period)

    phrase, contrast, _ = phrase_phase(f, base, period)
    anchor = base + phrase * period

    return {
        "bpm": round(float(bpm), 4),
        "bpm_auto": round(float(bpm_auto), 3),
        "bpm_delta": round(float((bpm_auto - bpm) if bpm_auto > 0 else 0.0), 3),
        "anchor": round(float(anchor), 4),
        "base_anchor": round(float(base), 4),
        "phrase": int(phrase),
        "contrast": round(float(contrast), 3),
        "phase_conc": round(float(conc), 3),
        "peaks": [(round(b, 2), round(s, 3)) for b, s in peaks[:4]],
        "dur_analyzed": round(dur, 2),
    }


# ==========================================================================
# 5. VirtualDJ 数据库
# ==========================================================================
def vdj_db_path():
    """自动探测 VirtualDJ 的 `database.xml`（**不硬编码任何本机路径**）。

    探测顺序：
      1) 环境变量 `AUTO_VJ_VDJ_DB`（显式指定，最优先）
      2) `%LOCALAPPDATA%\\VirtualDJ\\` 与用户目录下的 `VirtualDJ\\`
      3) 各盘符下的 `VirtualDJ\\database.xml`（VDJ 允许把曲库放在任意磁盘）

    ⚠ 一台机器上可能同时存在**多个 VDJ 库**（例如旧库被弃用、新库放在别的盘）。
      实测遇到过：`P:\\VirtualDJ\\` 里是 48 首的旧库，而真正在用的是另一盘上的
      1421 首主库。**只按盘符顺序取第一个会取错** ⇒ 多个候选时取**体积最大**的
      （主库通常最大）；要精确指定就用 `AUTO_VJ_VDJ_DB`。
    """
    env = os.environ.get("AUTO_VJ_VDJ_DB")
    if env and os.path.isfile(env):
        return env
    la = os.environ.get("LOCALAPPDATA", "")
    home = os.path.expanduser("~")
    cands = []
    if DEFAULT_VDJ:
        cands.append(DEFAULT_VDJ)
    cands += [
        os.path.join(la, "VirtualDJ", "database.xml") if la else "",
        os.path.join(home, "Documents", "VirtualDJ", "database.xml"),
        os.path.join(home, "VirtualDJ", "database.xml"),
    ]
    for d in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        cands.append("%s:\\VirtualDJ\\database.xml" % d)
        cands.append("%s:\\VirtualDJ\\Library\\database.xml" % d)

    found = []
    seen = set()
    for p in cands:
        if not p:
            continue
        k = os.path.normcase(os.path.normpath(p))
        if k in seen:
            continue
        seen.add(k)
        try:
            if os.path.isfile(p):
                found.append(p)
        except OSError:
            continue
    if not found:
        return ""
    if len(found) == 1:
        return found[0]
    try:
        found.sort(key=lambda x: os.path.getsize(x), reverse=True)
    except OSError:
        pass
    return found[0]


def vdj_all_db_paths():
    """列出本机所有可用的 VDJ 库（诊断用；按体积降序）。"""
    env = os.environ.get("AUTO_VJ_VDJ_DB")
    out = []
    if env and os.path.isfile(env):
        out.append(env)
    la = os.environ.get("LOCALAPPDATA", "")
    home = os.path.expanduser("~")
    cands = []
    cands += [
        os.path.join(la, "VirtualDJ", "database.xml") if la else "",
        os.path.join(home, "Documents", "VirtualDJ", "database.xml"),
        os.path.join(home, "VirtualDJ", "database.xml"),
    ]
    for d in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        cands.append("%s:\\VirtualDJ\\database.xml" % d)
    seen = set()
    for p in cands:
        if not p:
            continue
        k = os.path.normcase(os.path.normpath(p))
        if k in seen:
            continue
        seen.add(k)
        try:
            if os.path.isfile(p) and p not in out:
                out.append(p)
        except OSError:
            continue
    try:
        out.sort(key=lambda x: os.path.getsize(x), reverse=True)
    except OSError:
        pass
    return out


def _norm_title(name):
    """把文件名规范化成可跨目录比对的「曲名」。

    必需的原因：VDJ 库里记的是原始专辑文件名（形如 `(06) [Artist] Title (Remix).flac`），
    而用户自己的库或演出素材里可能叫 `Artist - Title (Remix).flac` ——
    basename 完全不同，只按 basename 匹配会漏掉一半（实测 21 首只命中 10 首）。
    """
    s = os.path.splitext(os.path.basename(name))[0]
    s = s.replace("（", "(").replace("）", ")").replace("［", "[").replace("］", "]")
    s = re.sub(r"^\(\d+\)\s*", "", s)              # 开头 "(06) "
    s = re.sub(r"^\[[^\]]*\]\s*", "", s)           # 开头 "[Artist] "
    s = re.sub(r"^rolling contact\s*-\s*", "", s, flags=re.I)
    s = re.sub(r"\([^)]*\)", " ", s)               # 去括号内容（Remix/年份/版本）
    s = re.sub(r"[\[\]]", " ", s)
    s = s.replace("_", " ").replace("-", " ")
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return " ".join(s.split())


class VdjIndex:
    """VirtualDJ 数据库索引，三级匹配：文件名 → 规范化曲名 → 时长。"""

    def __init__(self):
        self.by_base = {}
        self.by_title = {}
        self.by_dur = defaultdict(list)
        self.n = 0
        self.analyzed = 0

    def __len__(self):
        return self.n

    def __bool__(self):
        return self.n > 0

    def add(self, entry):
        self.n += 1
        if entry.get("anchor") is None or not entry.get("beat_len"):
            return
        self.analyzed += 1
        base = os.path.basename(entry["path"]).lower()
        self.by_base.setdefault(base, entry)
        t = _norm_title(entry["path"])
        if t:
            self.by_title.setdefault(t, entry)
        if entry.get("dur"):
            self.by_dur[int(round(entry["dur"]))].append(entry)

    def lookup(self, file_path, dur=None):
        b = os.path.basename(file_path).lower()
        if b in self.by_base:
            return self.by_base[b]
        t = _norm_title(file_path)
        if not t:
            return None
        if t in self.by_title:
            return self.by_title[t]
        # 时长 + 曲名近似（时长几乎唯一，用来兜住文件名差异大的情况）
        if dur:
            for d in (int(round(dur)), int(round(dur)) - 1, int(round(dur)) + 1):
                for cand in self.by_dur.get(d, ()):
                    ct = _norm_title(cand["path"])
                    if ct and (ct == t or ct.startswith(t[:12]) or t.startswith(ct[:12])
                               or (len(t) > 6 and t in ct) or (len(ct) > 6 and ct in t)):
                        return cand
        return None


def load_vdj(path=None):
    """读 VDJ 库 → VdjIndex。

    ⚠ `Scan@Bpm` 是**每拍秒数**，真实 BPM = 60 / 它；`Phase` 才是锚点（秒）。
    未分析过的条目（没有 anchor）会被索引但不会被匹配到。
    """
    idx = VdjIndex()
    path = path or vdj_db_path()
    if not path or not os.path.isfile(path):
        return idx
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return idx
    for s in root.iter("Song"):
        fp = s.get("FilePath")
        if not fp:
            continue
        scan = s.find("Scan")
        infos = s.find("Infos")
        e = {"path": fp, "bpm": 0.0, "beat_len": 0.0, "anchor": None,
             "key": "", "dur": 0.0, "remix": [], "cues": []}
        if infos is not None and infos.get("SongLength"):
            try:
                e["dur"] = float(infos.get("SongLength"))
            except (TypeError, ValueError):
                pass
        if scan is not None and scan.get("Bpm"):
            try:
                bl = float(scan.get("Bpm"))
            except (TypeError, ValueError):
                bl = 0.0
            if bl > 0:
                e["beat_len"] = bl
                e["bpm"] = 60.0 / bl
            ph = scan.get("Phase")
            if ph is not None:
                try:
                    v = float(ph)
                    if v >= 0:
                        e["anchor"] = v
                except (TypeError, ValueError):
                    pass
            e["key"] = scan.get("Key") or ""
        for poi in s.findall("Poi"):
            ty = poi.get("Type")
            try:
                pos = float(poi.get("Pos"))
            except (TypeError, ValueError):
                continue
            if ty == "remix":
                e["remix"].append((poi.get("Name") or "", pos))
            elif ty == "cue":
                e["cues"].append(pos)
        idx.add(e)
    return idx


def vdj_lookup(vdj, file_path, dur=None):
    """在 VDJ 索引里找**已分析**的记录（三级匹配）。"""
    if vdj is None:
        return None
    if isinstance(vdj, VdjIndex):
        return vdj.lookup(file_path, dur)
    # 兼容：传入普通 dict（basename → entry）
    try:
        e = vdj.get(os.path.basename(file_path).lower())
    except Exception:
        return None
    if e and e.get("anchor") is not None and e.get("beat_len"):
        return e
    return None


def _file_duration(path):
    """文件时长（秒），读头部即可，很快。"""
    try:
        import soundfile as sf
        return float(sf.info(path).duration)
    except Exception:
        return None


# ==========================================================================
# 6. 统一入口
# ==========================================================================
def _assemble(mono, sr, e, dur, do_scan=None, allow_local=True):
    """把已重采样好的 mono 组装成 grid dict（compute_grid / grid_from_signal 共用）。"""
    if not isinstance(mono, np.ndarray) or mono.size < sr:      # 太短，放弃
        return None
    if e and not allow_local:
        return {"bpm": e["bpm"], "beat_len": e["beat_len"], "anchor": e["anchor"],
                "phrase": 0, "contrast": 0.0, "src": "vdj", "conf": 0.9,
                "dur": e["dur"] or (dur or 0.0), "remix": list(e["remix"]),
                "vdj_bpm": e["bpm"], "vdj_delta": 0.0, "peaks": []}
    if do_scan is None:
        do_scan = (e is None)
    bpm_hint = e["bpm"] if e else None
    anchor_hint = e["anchor"] if e else None
    try:
        a = analyze_signal(mono, sr, anchor_hint=anchor_hint, bpm_hint=bpm_hint,
                           do_scan=do_scan)
    except Exception as ex:            # noqa: BLE001
        if e:                          # 分析失败但有 VDJ 数据 → 至少给网格
            return {"bpm": e["bpm"], "beat_len": e["beat_len"], "anchor": e["anchor"],
                    "phrase": 0, "contrast": 0.0, "src": "vdj", "conf": 0.5,
                    "dur": e["dur"] or (dur or 0.0), "remix": list(e["remix"]),
                    "vdj_bpm": e["bpm"], "vdj_delta": 0.0, "peaks": [],
                    "err": str(ex)[:120]}
        return None

    bpm = a["bpm"]
    bl = 60.0 / bpm if bpm > 0 else 0.0
    if bl <= 0:
        return None
    # 置信度：有 VDJ 数据就高；再看八拍相位对比度
    conf = 0.55 + min(0.4, max(0.0, (a["contrast"] - PHRASE_MIN) * 2.0))
    if e:
        conf = min(1.0, conf + 0.25)
    return {
        "bpm": bpm, "beat_len": round(bl, 6), "anchor": a["anchor"],
        "phrase": a["phrase"], "contrast": a["contrast"],
        "src": "vdj" if e else "local", "conf": round(conf, 3),
        "dur": dur or a.get("dur_analyzed", 0.0),
        "vdj_bpm": (round(e["bpm"], 3) if e else 0.0),
        "vdj_delta": a.get("bpm_delta", 0.0),
        "remix": list(e["remix"]) if e else [],
        "peaks": a["peaks"],
    }


def compute_grid(path, vdj=None, maxsec=180.0, allow_local=True, do_scan=None):
    """算一首歌的网格（自己读文件）。返回 dict（失败返回 None）。

    `anchor` 统一表示「某个八拍头」：**八拍头 = anchor + 8n * beat_len**。
    `do_scan=None`（默认）时自动决定：**有 VDJ 数据就跳过 BPM 扫描**（省掉单首最贵的一步），
    没有才自算 —— 曲库扫描（上千首）靠这个把成本压下来。
    """
    if not path or not os.path.isfile(path):
        return None
    dur = _file_duration(path)
    e = vdj_lookup(vdj, path, dur)
    mono, sr = load_mono(path, maxsec=maxsec)
    return _assemble(mono, sr, e, dur, do_scan=do_scan, allow_local=allow_local)


def grid_from_signal(mono, sr, vdj=None, path="", maxsec=180.0, dur=None,
                     allow_local=True, do_scan=None):
    """从**已解码**的单声道整曲信号算网格。

    ★ 曲库扫描时用它：扫描已经为指纹解码过一次，这里直接复用，省掉第二次读盘
      （HDD 上这很值钱）。只取前 `maxsec` 秒做分析。
    """
    if dur is None:
        dur = len(mono) / float(sr)
    e = vdj_lookup(vdj, path, dur) if path else None
    n = min(len(mono), int(maxsec * sr))
    sig = np.ascontiguousarray(mono[:n], dtype=np.float32)
    sig, sr2 = resample(sig, sr, SR_TARGET)
    return _assemble(sig, sr2, e, dur, do_scan=do_scan, allow_local=allow_local)


def store_grid(cfg, path, grid):
    """把网格写进曲库元数据（`cfg['music_meta'][path]['grid']`）。"""
    if not isinstance(cfg, dict) or not path or not grid:
        return False
    mm = cfg.setdefault("music_meta", {})
    rec = mm.get(path)
    if not isinstance(rec, dict):
        rec = {}
        mm[path] = rec
    rec["grid"] = grid
    return True


def get_grid(cfg, path):
    """从曲库元数据里取网格（演出中查表用）。"""
    try:
        rec = cfg["music_meta"].get(path)
    except Exception:
        return None
    if isinstance(rec, dict):
        g = rec.get("grid")
        if isinstance(g, dict) and g.get("beat_len"):
            return g
    return None


def grid_anchor_after(grid, offset_sec, phrase_len=8):
    """给定「当前播放到 offset 秒」，返回**下一个八拍头**的绝对秒数。

    这是演出中真正要的东西：知道当前在哪 → 推出下一个乐句头还有多久。
    """
    if not grid:
        return None
    bl = grid.get("beat_len") or 0.0
    if bl <= 0:
        return None
    a = grid.get("anchor") or 0.0
    step = phrase_len * bl
    n = math.floor((offset_sec - a) / step) + 1
    return a + n * step


def grid_beat_index(grid, offset_sec, phrase_len=8):
    """当前 offset 处在八拍组的第几拍（0..7）。"""
    if not grid:
        return None
    bl = grid.get("beat_len") or 0.0
    if bl <= 0:
        return None
    a = grid.get("anchor") or 0.0
    k = (offset_sec - a) / bl
    return int(math.floor(k)) % phrase_len
