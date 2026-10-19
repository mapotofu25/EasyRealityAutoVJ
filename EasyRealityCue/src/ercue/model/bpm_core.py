# -*- coding: utf-8 -*-
"""★ BPM 自动识别核心（零 Qt，纯 numpy/stdlib）—— **逐字移植自 `AutoVJ/src/beatgrid.py`**。

⚠⚠ **与 `P:\\ERAVJ\\AutoVJ\\src\\beatgrid.py` 逐字一致：改一端必须改另一端。**
  两端的 BPM 必须给出**同一个数**：编排端算出来写进 `.ercue` 的 `identity.bpm` 会决定
  码表里 `at_beats → 秒` 的换算，演出端又按自己的 BPM 把拍换算成秒去走画面；
  只要两边差零点几，预览与现场就会越走越偏。所以这里**不"另写一个更好的"**，
  而是连同常量、算法、语句顺序一起照抄（与 `model/fp_core.py` 移植 `fp.py` 同一套路）。

**移植范围**（编排端只要 BPM；不需要 anchor / 八拍相位，因为码表的 `at_beats` 是
相对曲首的拍数，不吃 anchor）：
    常量            SR_TARGET / HOP / WIN / BPM_MIN / BPM_MAX / BPM_PRIOR / F_LOW / F_MID / F_HIGH
    features()      频带能量 / 对数谱通量 / 低频谱质心
    onset_env()     节拍对齐用的 onset 包络（mode="mix"）
    grid_conc()     按周期折叠成相位直方图 → 集中度
    scan_bpm()      粗扫 + 细扫两级
    pick_bpm()      ★ 半速/双速歧义裁决（没有它有一半的歌会报半速）
**未移植**：`phase_of` / `per_beat_feats` / `phrase_*` / `analyze_signal` / VDJ 数据库 /
Beat This 神经网络（编排端不需要 anchor 与乐句相位，也不引入 onnx 依赖）。

**采样率**：★★ 必须和演出端一样先重采样到 `SR_TARGET`（22050）再分析。
  演出端生产路径是 `analyze_track → load_mono(maxsec) → resample(mono, sr, SR_TARGET) → features`，
  **永远在 22050 上算**。`features()` 里的 `dt = hop / sr` 与 FFT 窗长（2048 采样）都是**随采样率
  变**的：44.1k 上 dt 减半（23ms）、窗长减半（46ms）⇒ onset 包络的时间/频率分辨都不同 ⇒
  同一首歌在同一套算法下会给出**不同的 BPM**。实测（本机曲库 12 首）：
      原生率直接算  7/12 命中 VDJ 真值；
      先降到 22050  8/12 命中（且与演出端函数**逐位相同**）。
  ⇒ 为了"两端同一个数"，`detect_bpm()` 内部**先 `resample()` 到 `SR_TARGET`**，
    与 `load_mono` 的预处理一字不差（这也顺带把耗时降一半）。

**零 Qt 纪律**：本模块**不得** import PySide6 / shiboken（`tools/selftest.py` 会 AST 扫描
`model/` 下的 Qt 绑定，命中即失败）。
"""
from __future__ import annotations

import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import defaultdict

import numpy as np

log = logging.getLogger("ercue.model.bpm_core")

# ==========================================================================
# 常量（★ 与 AutoVJ/src/beatgrid.py 逐值一致 —— 不要改）
# ==========================================================================
SR_TARGET = 22050          # 分析用采样率（节拍信息都在 20Hz~10kHz；比 44.1k 省一半）
HOP = 1024                 # STFT 步长（22050 下 ≈ 46 ms）
WIN = 2048                 # STFT 窗长（≈ 93 ms）

F_LOW = (30.0, 200.0)      # kick / 低音
F_MID = (200.0, 2000.0)    # 人声 / 主奏
F_HIGH = (2000.0, 9000.0)  # hihat

BPM_MIN, BPM_MAX = 60.0, 200.0
BPM_PRIOR = (110.0, 190.0)  # DJ 音乐先验区间：半速歧义时优先取落在这里的候选

# VirtualDJ 库路径不硬编码，统一由 `vdj_db_path()` 自动探测（与 beatgrid 同名常量）。
DEFAULT_VDJ = ""

# 识别失败时的返回值约定（调用方据此判"没算出来"，绝不静默）
NO_BPM = 0.0

__all__ = [
    "SR_TARGET", "HOP", "WIN", "F_LOW", "F_MID", "F_HIGH",
    "BPM_MIN", "BPM_MAX", "BPM_PRIOR", "NO_BPM",
    "resample", "features", "onset_env", "grid_conc", "scan_bpm", "pick_bpm",
    "detect_bpm",
    "vdj_db_path", "vdj_all_db_paths", "load_vdj", "vdj_lookup", "VdjIndex",
    "get_vdj_index", "reset_vdj_cache", "vdj_stats",
    "selftest",
]


# ==========================================================================
# 1. 预处理 + 特征提取（与 beatgrid.py 逐字一致）
# ==========================================================================
def resample(mono, sr0, sr_target=SR_TARGET):
    """线性插值重采样（★ 与 `beatgrid.resample` 逐字一致）。

    节拍检测不需要高保真（特征都在 20Hz~10kHz），但**分析率必须统一**：
    演出端 `load_mono()` 永远先降到 `SR_TARGET`，编排端照做才能得到同一个 BPM。
    """
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


def features(mono, sr, hop=HOP, win=WIN):
    """分块 STFT → 频带能量 / 对数谱通量 / 低频谱质心。

    Args:
        mono: 单声道 float32 波形（任意采样率，不做重采样）。
        sr: 该波形的采样率（用于算 `dt` 与频率轴）。
        hop: STFT 步长（帧）。
        win: STFT 窗长（帧）。

    Returns:
        dict：`dt / nf / sr / freqs / Elow / Emid / Ehigh / flux / flux_low / c_low`。
    """
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
    """节拍对齐用的 onset 包络（归一化到 0~1）。与 beatgrid.py 逐字一致。"""
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
# 2. BPM 扫描与半速裁决（与 beatgrid.py 逐字一致）
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
    """粗扫 + 细扫两级，返回 `[(bpm, score), …]`（降序）。

    两级扫描是因为：曲库扫描要处理上千首，几千个候选逐个算太慢。
    """
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
    实测 5 首里有 2 首的第一名是 64（真值 128）。
    """
    if not peaks:
        return 0.0, 0.0
    best_s = peaks[0][1]
    # ⚠ 边界必须带容差：实测有一首歌的首选峰真值是 110.0，但 `fine_bpms` 的浮点累加
    #   给出 109.99999999999994 ⇒ `110 <= 109.9999…` 为假 ⇒ 被先验排除 ⇒ 掉到 164.9（双速）。
    #   这类误差只在"峰恰好落在先验边界上"时出现，加 1e-6 容差就干净。
    #   ⚠⚠ **与 AutoVJ/src/beatgrid.py 同步（同一行、同一个 1e-6），改一端必须改另一端**：
    #      两端对同一首歌给出不同 BPM 时，码表里按**编排端** bpm 写下的 `at_beats`
    #      会被演出端按它**自己的** beat_len 乘回去 ⇒ 片段位置整体差 `bpm_编排/bpm_演出` 倍。
    #      （注意：用户**留空** BPM 不在此列 —— 那时两端读到的都是 0、都按 1 拍 = 1 秒，
    #        换算一致，位置不会错；只有"同一首歌两端算出两个不同 BPM"才会错位。）
    #   不升 `GRID_VER`：改动只影响"恰好卡在边界 1e-6 内"的候选，结果差 ≤0.05 BPM（细扫步长）。
    inrange = [(b, s) for b, s in peaks
               if BPM_PRIOR[0] - 1e-6 <= b <= BPM_PRIOR[1] + 1e-6]
    if inrange:
        for b, s in inrange:
            if s >= 0.70 * best_s:
                return b, s
        return inrange[0]
    return peaks[0]


# ==========================================================================
# 3. VirtualDJ 数据库索引（★ 与 beatgrid.py 同名函数逐字一致）
#
# **为什么优先读 VDJ**：这台机器上真实 BPM 一直躺在 `W:\\VirtualDJ\\database.xml` 里，
# 演出端生产路径 `_assemble()` 就是「有 VDJ 记录 ⇒ 用 VDJ 的 BPM 且**跳过扫描**」。
# 音频扫描只是 VDJ 没记录时的兜底（本机实测：VDJ vs 演出端曲库 grid.bpm 20 首 19 命中，
# 而纯音频扫描对同一批只有 8/12）⇒ 编排端必须同优先级，BPM 才对得上、还更准更快。
# ⚠ 未移植 `_file_duration`（本仓库 venv 没有 soundfile）：时长由调用方给
#   （`identity.duration_sec` 本来就有），见 `detect_bpm(..., dur=...)`。
# ==========================================================================
def vdj_db_path():
    """自动探测 VirtualDJ 的 `database.xml`（**不硬编码任何本机路径**）。

    探测顺序：
      1) 环境变量 `AUTO_VJ_VDJ_DB`（显式指定，最优先）
      2) `%LOCALAPPDATA%\\VirtualDJ\\` 与用户目录下的 `VirtualDJ\\`
      3) 各盘符下的 `VirtualDJ\\database.xml`（VDJ 允许把曲库放在任意磁盘）

    ⚠ 一台机器上可能同时存在**多个 VDJ 库**（旧库被弃用、新库在别的盘）⇒
      多个候选时取**体积最大**的（主库通常最大）；要精确指定就用 `AUTO_VJ_VDJ_DB`。
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
        except OSError:                                          # 掉线的网络盘 / 无权限
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
    """把文件名规范化成可跨目录比对的「曲名」（与 beatgrid.py 逐字一致）。

    必需的原因：VDJ 库里记的是原始专辑文件名（形如 `(06) [Artist] Title (Remix).flac`），
    而用户自己的库可能叫 `Artist - Title (Remix).flac` —— basename 完全不同，
    只按 basename 匹配会漏掉一半（实测 21 首只命中 10 首）。
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
    """读 VDJ 库 → `VdjIndex`（与 beatgrid.py 逐字一致）。

    ⚠ `Scan@Bpm` 是**每拍秒数**，真实 BPM = 60 / 它；`Phase` 才是锚点（秒）。
    未分析过的条目（没有 anchor）会被索引但不会被匹配到。
    **读失败一律返回空索引**（`W:` 盘掉线 / xml 损坏 / 没装 VDJ ⇒ 调用方自动退回音频扫描）。
    """
    idx = VdjIndex()
    path = path or vdj_db_path()
    if not path or not os.path.isfile(path):
        return idx
    try:
        root = ET.parse(path).getroot()
    except Exception as e:                                       # noqa: BLE001
        log.warning("读 VirtualDJ 库失败（忽略，退回音频扫描）：%s: %s", type(e).__name__, e)
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
    """在 VDJ 索引里找**已分析**的记录（三级匹配）。找不到返回 None。"""
    if vdj is None or not file_path:
        return None
    if isinstance(vdj, VdjIndex):
        return vdj.lookup(file_path, dur)
    # 兼容：传入普通 dict（basename → entry）
    try:
        e = vdj.get(os.path.basename(file_path).lower())
    except Exception:                                            # noqa: BLE001
        return None
    if e and e.get("anchor") is not None and e.get("beat_len"):
        return e
    return None


# ---- 惰性 + 缓存（2.6MB xml 解析不便宜，绝不每首歌读一次；也绝不在 import 时读）----
_VDJ_CACHE = {"key": None, "index": None, "loads": 0, "last_cost": 0.0}


def get_vdj_index(path=None):
    """取 VirtualDJ 索引（**惰性加载 + 进程内缓存**）。返回 `VdjIndex`（失败=空索引，绝不抛）。

    `path` 为空 ⇒ 用 `vdj_db_path()` 自动探测。
    ⚠ 只在 worker 线程里首次调用（本模块不做线程调度，但解析要 1s 级，别放 GUI 线程）。
    """
    key = str(path or "")
    if _VDJ_CACHE["key"] == key and _VDJ_CACHE["index"] is not None:
        return _VDJ_CACHE["index"]                               # 命中缓存 ⇒ 不重复解析
    t0 = time.perf_counter()
    try:
        idx = load_vdj(path or None)
    except Exception as e:                                       # noqa: BLE001
        log.warning("加载 VirtualDJ 索引失败（退回音频扫描）：%s: %s", type(e).__name__, e)
        idx = VdjIndex()
    _VDJ_CACHE["key"] = key
    _VDJ_CACHE["index"] = idx
    _VDJ_CACHE["loads"] = int(_VDJ_CACHE["loads"]) + 1
    _VDJ_CACHE["last_cost"] = time.perf_counter() - t0
    return idx


def reset_vdj_cache():
    """清空索引缓存（测试 / 用户换库后重新加载用）。"""
    _VDJ_CACHE.update({"key": None, "index": None, "loads": 0, "last_cost": 0.0})


def vdj_stats():
    """诊断信息：`{path, entries, analyzed, loads, last_cost}`（不触发加载）。"""
    idx = _VDJ_CACHE["index"]
    return {"path": vdj_db_path(), "entries": (idx.n if idx is not None else 0),
            "analyzed": (idx.analyzed if idx is not None else 0),
            "loads": int(_VDJ_CACHE["loads"]),
            "last_cost": float(_VDJ_CACHE["last_cost"])}


# ==========================================================================
# 4. 对外唯一入口（两级：VDJ 优先 → 音频扫描兜底）
# ==========================================================================
def detect_bpm(mono, sr, maxsec=180.0, vdj=None, path="", dur=None):
    """单声道波形 / VDJ 库 → `(bpm, score, peaks[:4], source)`。

    优先级（**与演出端 `_assemble()` 一致**）：
      1. **VDJ 库里有这首歌的已分析记录** ⇒ 直接用 VDJ 的 BPM，`source="vdj"`，
         **不跑扫描**（省 ~0.7s/首，而且更准 —— VDJ 的值就是演出端在用的值）；
      2. 否则 ⇒ 音频扫描，`source="audio"`（准确率约 8/12~71%，**建议用户核对**）；
      3. 都没有 ⇒ `(0.0, 0.0, [], "")`。

    Args:
        mono: 单声道 float32（原生采样率即可，内部会降到 `SR_TARGET`）。
        sr: 采样率（<=0 或数据太短 ⇒ `source=""`）。
        maxsec: 只取前 maxsec 秒做扫描（None/<=0 = 全曲）。
        vdj: `VdjIndex`（`get_vdj_index()`）或 `None`（= **明确不用 VDJ**，测试用）。
        path: 音频文件路径（VDJ 三级匹配靠它：basename → 规范化曲名 → 时长）。
        dur: 时长（秒）。VDJ 的第三级匹配用它；本仓库不自己读时长，由调用方给。

    Returns:
        `(bpm, score, peaks, source)`
          · `score` 只在 `source=="audio"` 时有意义（`grid_conc` 集中度；vdj 恒 0.0）；
          · `peaks` 是 `[(bpm, score)]` 前 4 个候选（供 UI 展示"备选"，vdj 恒空）。
    """
    # ① VDJ 优先
    if vdj is not None and path:
        try:
            e = vdj_lookup(vdj, path, dur)
        except Exception:                                        # noqa: BLE001
            e = None
        if e:
            try:
                vb = float(e.get("bpm") or 0.0)
            except (TypeError, ValueError):
                vb = 0.0
            if vb > 0:
                return vb, 0.0, [], "vdj"

    # ② 音频扫描（原逻辑不变）
    try:
        x = np.ascontiguousarray(mono, dtype=np.float32)
    except Exception as e:                                        # noqa: BLE001
        log.warning("BPM 识别：波形不可用（%s: %s）", type(e).__name__, e)
        return NO_BPM, 0.0, [], ""
    try:
        sr = int(sr or 0)
    except (TypeError, ValueError):
        sr = 0
    if sr <= 0 or x.ndim != 1 or x.size < WIN:
        return NO_BPM, 0.0, [], ""
    if maxsec is not None and float(maxsec) > 0:
        n = int(float(maxsec) * sr)
        if 0 < n < x.size:
            x = np.ascontiguousarray(x[:n])
    try:
        # ★ 先降到 SR_TARGET（= 演出端 load_mono 的预处理）：两端必须**分析率一致**，
        #   否则同一首歌会因 dt / 窗长不同而给出不同 BPM（见模块 docstring 的实测数据）。
        x, sr = resample(x, sr, SR_TARGET)
        f = features(x, sr)
        env = onset_env(f, "mix")
        peaks = scan_bpm(f, env)
        bpm, score = pick_bpm(peaks)
        if bpm <= 0:
            return NO_BPM, 0.0, [], ""
        return float(bpm), float(score), [(float(b), float(s)) for b, s in peaks[:4]], "audio"
    except Exception as e:                                        # noqa: BLE001
        log.exception("BPM 识别失败（%s）", type(e).__name__)
        return NO_BPM, 0.0, [], ""


# ==========================================================================
# 自检
# ==========================================================================
def selftest() -> bool:
    """零 Qt 自检：常量与 AutoVJ 一致 + 合成 click track 能识别出正确 BPM。"""
    assert (SR_TARGET, HOP, WIN, BPM_MIN, BPM_MAX, BPM_PRIOR) == \
        (22050, 1024, 2048, 60.0, 200.0, (110.0, 190.0)), "常量被改动（必须与 AutoVJ 一致）"
    assert F_LOW == (30.0, 200.0) and F_MID == (200.0, 2000.0) and F_HIGH == (2000.0, 9000.0)

    # 合成 128 BPM 的 click track（44.1k / 60 秒）：每拍一个短促低频敲击
    sr = 44100
    bpm_true = 128.0
    dur = 60.0
    n = int(sr * dur)
    y = np.zeros(n, dtype=np.float32)
    period = 60.0 / bpm_true
    click_len = int(0.03 * sr)
    env = np.exp(-np.linspace(0.0, 8.0, click_len)).astype(np.float32)
    rng = np.random.default_rng(20261018)
    k = 0
    while True:
        pos = int(round(k * period * sr))
        if pos + click_len >= n:
            break
        y[pos:pos + click_len] += env * (0.8 if k % 4 == 0 else 0.5)
        k += 1
    y += (rng.standard_normal(n) * 0.001).astype(np.float32)
    bpm, score, peaks, source = detect_bpm(y, sr, maxsec=60.0)   # vdj=None ⇒ 走音频扫描
    assert source == "audio", ("没给 VDJ 时必须走音频扫描", source)
    assert abs(bpm - bpm_true) <= 0.5, ("合成 click track 应识别为 %.1f，实得 %.2f (%s)"
                                        % (bpm_true, bpm, peaks))
    # VDJ 优先：索引里有记录 ⇒ 直接用它，且不跑扫描
    idx = VdjIndex()
    idx.add({"path": "X:/music/fake_song.flac", "bpm": 174.0, "beat_len": 60.0 / 174.0,
             "anchor": 0.0, "dur": 123.0, "remix": [], "cues": [], "key": ""})
    b2, s2, p2, src2 = detect_bpm(y, sr, maxsec=60.0, vdj=idx, path="X:/music/fake_song.flac")
    assert src2 == "vdj" and abs(b2 - 174.0) < 1e-9 and p2 == [], (b2, src2, p2)
    # 查不到（换一首不存在的）⇒ 退回音频扫描
    b3, _s3, _p3, src3 = detect_bpm(y, sr, maxsec=60.0, vdj=idx, path="X:/music/nope.flac")
    assert src3 == "audio" and abs(b3 - bpm_true) <= 0.5, (b3, src3)
    print("[bpm_core] 自检通过 ✓  常量与 AutoVJ 一致；合成 %.0f BPM ⇒ 识别 %.2f（score=%.3f）；"
          "VDJ 优先 %.2f / 查不到时退回扫描 %.2f" % (bpm_true, bpm, score, b2, b3))
    return True


if __name__ == "__main__":
    selftest()
