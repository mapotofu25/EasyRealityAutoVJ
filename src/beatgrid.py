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
import threading
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

# 网格算法版本：算法 / 参数变更时递增。曲库增量扫描据此判断「已有记录的网格是否过期」，
# 只对过期 / 缺失的网格重算（见 ui_main 扫描），而不用整库全量重扫。
#   1 = 初版（隐式，未写入 grid dict）
#   2 = 2026-09-27：_assemble 输出显式带 grid_ver；配合增量补网格
GRID_VER = 2


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
                "vdj_bpm": e["bpm"], "vdj_delta": 0.0, "peaks": [],
                "grid_ver": GRID_VER}
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
                    "err": str(ex)[:120], "grid_ver": GRID_VER}
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
        "grid_ver": GRID_VER,
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


# ==========================================================================
# 7. 演出用的「网格时钟」——把离线网格变成一条连续、单调、防抖的拍位
# ==========================================================================
class GridClock:
    """把「离线八拍网格」变成一条**连续、单调、抗抖**的实时拍位时钟。

    为什么需要它（而不是每帧直接拿识别结果算拍位）：
      指纹识别每 0.5s 才出一次结果，且结果带 ±几十 ms 抖动。若每帧直接用它推算，
      拍位会随识别抖动一跳一跳 —— 素材切换点跟着抖，演出就是"乱切"。
      正确做法（照引擎现有 PLL 拍钟思路）：识别结果只做**有限增益相位校正**，
      两次识别之间由局部时钟**自由匀速推进**。

    坐标定义（与 `grid_anchor_after` 完全一致）：
        beat_pos = (offset_orig - anchor) / beat_len
      ⇒ 八拍头（原曲 anchor + 8n·beat_len 秒）落在 **beat_pos = 8n**（8 的整数倍）。
      于是"对齐到八拍头"在网格坐标里就是"对齐到 8 的整数倍"，消费方只记这一条。

    速度换算（关键）：原曲 1 拍 = beat_len 秒；播放快 r 倍 ⇒ 挂钟 1 秒前进 r/beat_len 拍。
      反向换算（节拍→秒）必须**除以 r**：1 拍 = beat_len/r 秒。

    update() 的输入：
        sid      当前确认的歌 id
        beat_len 该歌网格的拍长（秒）
        anchor   该歌网格的八拍头锚点（秒）
        off      识别窗口末端在**原曲时间轴**上的秒数（已含 +4.5·r 修正，见 audio_engine）
        r        输入音乐播放速率（EMEA 估计，未知时 1.0）
        ref_t    该识别结果对应的挂钟时刻（time.perf_counter()）
    """

    def __init__(self, phrase=8.0, gain=0.20, max_corr=0.25, big_gap=2.0,
                 seek_beats=3.5, jump_err=0.75, jump_run=2, jump_max=32.0,
                 hint_hold_wins=3):
        self.phrase = float(phrase)        # 乐句长度（拍）：八拍
        self.gain = float(gain)            # 相位校正有限增益（每 0.5s 一次，小步防抖）
        self.max_corr = float(max_corr)    # 单次相位校正上限（拍）：一次抖动绝不跳拍
        self.big_gap = float(big_gap)      # 识别空档超过这么久 → 硬重锚（坐标系可能已漂移）
        # ★ 相位误差「判为真跳转」的阈值（拍），与「单调保护」（last_pos 钳位）**解耦**：
        #   旧实现只用单一阈值 `abs(err) >= seek_beats`（=4.0）。问题：
        #     · 恰好 4 拍的 Loop 被浮点判成 < 4.0 ⇒ 落进微调分支，而微调上限仅
        #       gain·max_corr = 0.05 拍/次（≈0.1 拍/秒）⇒ 几乎拽不动，时钟既不后退也不跟随，
        #       连打 20s 实测最大滞后 +7.8 拍；
        #     · −1 / −2 拍 Beat Jump（err≈−1/−2，远小于 4）更是永远进不了重锚，留下持续偏差。
        #   现在改为「单次巨跳 OR 连续同向超小阈值」两条独立判据：
        #     · |err| ≥ seek_beats（≥半个乐句）        → 单次即判真跳转（保留原大跳保护）；
        #     · 连续 jump_run 次 err 同向且 |err| ≥ jump_err → 判真跳转（覆盖 −1/−2 拍小回跳）。
        #   jump_err=0.75 的依据：识别 offset 的量化抖动上限 ≈ ±0.4 拍
        #   （DT_QUANT=4 帧 ≈ 186ms @128BPM），远低于 0.75；而 −1 拍 Beat Jump 的 err ≈ −1.0
        #   明显高于它 ⇒ 干净切分。要求「连续 2 次」是为了进一步压掉偶发单次大抖动：
        #   一次坏窗只让 err 超阈 1 次，run=1 不触发（只做有界微调）；真跳转是持续偏移，
        #   第 2 个窗仍同向超阈 ⇒ run=2 触发硬重锚。
        #   seek_beats=3.5（略小于半个乐句）：4 拍 Loop 的 err 在浮点 + 之前微调残留下会在
        #   4.0 上下浮动（实测有 −3.86），取 3.5 保证「恰好 4 拍的回跳」也能**单次**命中、
        #   不必等第 2 个窗（把识别窗残余从 ~3.9 拍压到 ~0）。量化抖动 max≈0.37 远低于它。
        self.seek_beats = float(seek_beats)
        self.jump_err = float(jump_err)    # 「连续同向」判据的阈值（拍）
        self.jump_run = int(jump_run)      # 连续同向超阈次数达到它 ⇒ 判真跳转
        # ★ 单次巨跳（强窗）允许的**最大幅值**（拍）：超过它就不再「单帧立即跟随」，改走
        #   「连续同向确认」（jump_run 次）后才硬重锚。
        #   为什么（阻塞项 C：强重复段落的高票错窗）：同一段音频在歌里出现多次时，查询窗会
        #   与"另一处出现"哈希对上，票数甚至高于正确位置且票数≥强门槛；旧的 `big=strong and
        #   |err|≥seek_beats` 会让这种**孤立**的强错窗**单帧**把整条时钟拽到错坐标（实测
        #   瞬时偏移 148~738 拍）。DJ 软件的 Beat Jump 档位最大 **32 拍**，所以：
        #     · |err| ≤ 32 拍（1/2/4/8/16/32 拍 Loop 与 Beat Jump）→ 仍**单帧立即跟随**（不退化）；
        #     · |err| > 32 拍 = 远距离 seek 或错匹配 → 必须**连续 2 个可信窗一致**才硬重锚
        #       （多等一个识别窗 ≈0.5s，对真 seek 可接受；把孤立强错窗挡在门外）。
        self.jump_max = float(jump_max)
        # ★ R3（闭环自证偏差，已锁定后的边界情况）：>jump_max（>32 拍）的**硬重锚**后，
        #   暂停向外发布"连续性提示(hint)"若干识别窗，让 fp.match 在**开环**（全局众数）下
        #   重新验证坐标：若位置保持不变（开环仍报新坐标）说明重锚正确，恢复发布 hint；
        #   若开环把位置拉回旧坐标，则时钟会在后续窗按连续同向判据再重锚回来。
        #   为什么只针对 >jump_max：≤32 拍的重锚是「1/2/4/8/16/32 拍 Loop/Beat Jump」的
        #   正常跟随（可信、无害），不该因此丢掉连续性提示。取 3：>jump_max 的重锚需
        #   **连续 jump_run(=2)** 个同向强窗确认，再留 3 个开环窗复核（≈1.5s）足够让
        #   正确的全局众数重新出现。**有界**（仅 3 窗），不改变常规路径。
        self.hint_hold_wins = int(hint_hold_wins)
        # ★ 自带内部锁（必须是 RLock：beats_to_phrase 内部会复用 _pos_locked 逻辑）：
        #   本时钟会被「引擎线程（每帧 update/取拍位）」与「GUI HUD 线程（只读取拍位）」
        #   并发访问。**绝不能**为此去拿 engine._tick_lock —— 那是引擎每帧整帧渲染期间
        #   持有的总锁，HUD 去等它会被慢帧拖住（违反「GUI 不能因引擎卡而卡」）。
        #   这把锁只保护下面几行**纯数学**的状态读写；锁内不做任何可能阻塞的操作
        #   （不 sleep、不 IO、不等待事件）。
        self._lock = threading.RLock()
        self.reset()

    def reset(self):
        """清空时钟（换歌库 / 重扫 / 识别丢失时调用）。"""
        with self._lock:
            self.sid = None
            self.t0 = 0.0        # 时钟基准挂钟时刻
            self.pos0 = 0.0      # 时钟在 t0 时刻的拍位
            self.bl = 1.0        # 当前 beat_len（秒/拍）
            self.r = 1.0         # 当前输入速率
            self.ref_t = 0.0     # 上次用于校正的识别参考时刻
            self.last_pos = None # 单调保护：上次返回的拍位
            self._err_run = 0    # 真跳转判据：连续同向超阈次数
            self._err_sign = 0   # 该连续段的方向（+1 / -1 / 0）
            self._hint_hold = 0  # R3：>jump_max 重锚后仍要暂停发布 hint 的剩余窗数

    def _speed(self):
        """每挂钟秒前进多少拍（= r / beat_len）。只在持锁时调用。"""
        if self.bl <= 0:
            return 0.0
        return self.r / self.bl

    def update(self, sid, beat_len, anchor, off, r, ref_t, strong=True):
        """喂一次识别参考，更新内部时钟（不返回拍位；拍位由 pos(now) 取）。

        `strong`：该识别窗是否为**强匹配**（票数 ≥ 强门槛，默认 True 兼容直接调用）。
          强窗才允许「单次小幅巨跳」（|err| 在 [seek_beats, jump_max]）立即硬重锚；弱窗
          （票数介于普通门槛与强门槛之间）即便 err 很大也不单次重锚 —— 一次坏匹配（重复段落
          歧义）若不设防会直接把坐标拽到错位置（阻塞项 B）。**任何窗**只要 |err| > jump_max
          （>32 拍：远距离 seek 或错匹配）都**不**单帧重锚，须靠**连续同向**（jump_run 次）
          确认后才重锚（阻塞项 C）。"""
        if beat_len is None or beat_len <= 0:
            return
        r = 1.0 if (r is None or not (0.5 <= r <= 2.0)) else float(r)
        with self._lock:
            ref_pos = (off - anchor) / beat_len
            # 换歌 / 首次：硬锚定到参考点（坐标整体换系，不做平滑）
            if self.sid != sid:
                self.sid = sid
                self.bl = float(beat_len)
                self.r = r
                self.t0 = ref_t
                self.pos0 = ref_pos
                self.ref_t = ref_t
                self.last_pos = None
                self._err_run = 0
                self._err_sign = 0
                self._hint_hold = 0
                return
            if ref_t > self.ref_t + 1e-6:
                # R3：每个新识别窗消耗一次"暂停发布 hint"额度（额度在下面的重锚里补充）
                if self._hint_hold > 0:
                    self._hint_hold -= 1
                gap = ref_t - self.ref_t
                pred = self.pos0 + (ref_t - self.t0) * self._speed()
                err = ref_pos - pred
                # ★ 真跳转判据（与单调保护解耦，见 __init__ 说明）：
                #   连续同向超 jump_err 计数；单次巨跳（仅强窗）或计数达标 ⇒ 硬重锚。
                sign = 1 if err > 0.0 else (-1 if err < 0.0 else 0)
                if sign != 0 and abs(err) >= self.jump_err:
                    if sign == self._err_sign:
                        self._err_run += 1
                    else:
                        self._err_sign = sign
                        self._err_run = 1
                else:
                    self._err_run = 0
                    self._err_sign = 0
                big = (strong and self.seek_beats <= abs(err) <= self.jump_max)
                run_hit = (self._err_run >= self.jump_run)
                if (gap > self.big_gap or big or run_hit):
                    # 坐标系真的跳了：识别中断后重出现（gap 过大）、强窗**小幅**巨跳（|err|
                    # 落在 [seek_beats, jump_max]：1/2/4/8/16/32 拍 Loop/Beat Jump，单帧跟随），
                    # 或误差连续同向超阈（含 >jump_max 的远距离 seek：需连续 jump_run 个窗确认）
                    # —— 硬重锚到参考点，清空单调保护（last_pos=None）以允许 pos 后退到新坐标。
                    # ★ >jump_max（32 拍）的跳变**不再**走单帧 big：孤立强错窗只会有界微调，
                    #   连续 jump_run 个同向窗一致才重锚（阻塞项 C：强重复段落高票错窗）。
                    self.t0 = ref_t
                    self.pos0 = ref_pos
                    self.last_pos = None
                    self._err_run = 0
                    self._err_sign = 0
                    # ★ R3：>jump_max（>32 拍）的硬重锚且**由连续同向强错窗确认**（run_hit，
                    #   不是识别中断 gap 重锚）—— 正常路径只有「远距离 seek」与「连续同向强错窗
                    #   把坐标拽走」两种；后者会让后续 hint 继续指向错坐标（闭环自证）。这里
                    #   **有界**暂停发布 hint（hint_hold_wins 个窗），让 fp.match 在开环下复核：
                    #   正确坐标会在 1~2 个窗内重新出现并被采纳。仅在 run_hit 时触发（gap 重锚
                    #   多为识别中断后的正常复位，不应丢连续性提示）。
                    if run_hit and abs(err) > self.jump_max:
                        self._hint_hold = self.hint_hold_wins
                else:
                    # 有限增益相位校正：只把预测值朝参考值靠一小步（防抖、不跳拍）
                    e = max(-self.max_corr, min(self.max_corr, err))
                    self.t0 = ref_t
                    self.pos0 = pred + self.gain * e
                self.ref_t = ref_t
                self.bl = float(beat_len)
                self.r = r
            else:
                # 同一参考期内（识别还没出新结果）：速率做 EMA 平滑采纳，避免速度突变
                self.bl = float(beat_len)
                self.r = self.r * 0.6 + r * 0.4

    def update_rate(self, beat_len, r):
        """低票数窗的**轻量**更新：只刷新速率（r / beat_len），**绝不触碰相位**。

        为什么需要（阻塞项 A）：低票数时引擎会跳过 `update`（offset 不可信，不能做相位
        校正），但 `RateEstimator` 仍在收敛新的 r；若时钟的 r / beat_len 一直冻结在旧值，
        自由跑期间时钟会按**错误速度**推进，等恢复可信窗时误差已积累到数拍 ⇒ 单帧猛跳
        （实测 60s@(1.05/1.00) 跳变 6.65 拍）。这里只让速度跟上估计值，相位
        （pos0 / t0 / last_pos）一律不动 ⇒ 自由跑速度正确、恢复时无需大跳。
        """
        if beat_len is None or beat_len <= 0:
            return
        r = 1.0 if (r is None or not (0.5 <= r <= 2.0)) else float(r)
        with self._lock:
            if self.sid is None:
                return          # 还没锁定坐标系：速率无意义，不动
            self.bl = float(beat_len)
            self.r = r

    def _pos_locked(self, now):
        """持锁状态下取拍位（单调不减）。"""
        p = self.pos0 + (now - self.t0) * self._speed()
        if self.last_pos is not None and p < self.last_pos:
            p = self.last_pos
        self.last_pos = p
        return p

    def pos(self, now):
        """当前拍位（网格坐标）。单调不减：一次识别抖动绝不后退。线程安全。"""
        with self._lock:
            return self._pos_locked(now)

    def beats_to_phrase(self, now):
        """距下一个八拍头还有几拍。线程安全。

        ★ 恰好踩在八拍头（拍位是 phrase 的整数倍）时返回 **0.0**，而不是 phrase。
          旧实现 `(floor(p/phrase)+1)*phrase - p` 在整点会返回 8.0（"还要等整整一个乐句"），
          与题意不符（此刻就在八拍头上，剩余 0 拍）。"""
        with self._lock:
            p = self._pos_locked(now)
            r = p % self.phrase
            if r < 1e-9:
                return 0.0
            return self.phrase - r

    def ref_time(self):
        """最近一次用于校正的识别参考时刻（线程安全读取；缺失返回 0）。"""
        with self._lock:
            return self.ref_t

    def hint_hold(self):
        """R3：还剩几个识别窗要暂停发布 hint（线程安全读取）。>0 时调用方不应发布 hint。"""
        with self._lock:
            return self._hint_hold
