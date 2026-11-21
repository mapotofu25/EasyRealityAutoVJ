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

存储：`cfg["music_meta"][path]["grid"]`（**由曲库扫描直接写**；本模块的 `store_grid` / `get_grid` 是等价封装，目前只有 tools 在用）。
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
HOP = 512                  # STFT 步长（22050 下 ≈ 23 ms）
# ★★★ 2026-11-01（用户：「(07) [Amane Oikawa] Distorted Dimension.flac 这首实际是 200bpm」，
#   曲库里存的是 132.7）：**BPM 扫描的时间分辨率不够**。
#   原来是 1024（46ms/帧）⇒ 200 BPM（300ms/拍）只有 **6.5 帧/拍**，而 `grid_conc` 要把
#   相位折叠到 32 个格子（每格 9.4ms）⇒ 6.5 个点摊在 32 格里，大部分格子是空的，
#   集中度被"哪几格恰好落到帧"决定，与真实节拍强度脱钩：
#     实测这首 —— 132.65 得 2.519（= 200×2/3 的伪峰）、真值 200 只有 **1.773**
#     ⇒ 连 `scan_bpm` 的候选前三都进不去，`pick_bpm` 只能挑伪峰。
#   减半到 512（23ms/帧，12.9 帧/拍）后同一首直接扫出 **198.75**（第一名 3.499）。
#   全库 A/B（54 首带 VDJ 参照，`tools/_ab_bpm_hop.py`）：
#     完全一致 50/54 (93%) → **52/54 (96%)**，同族 51/54 → **53/54**，
#     变化的 5 首里 4 首变准（161.5→160、136→170 ×2、143.55→143）、1 首仍错、**零退化**。
#   代价：`features` 耗时 ×2（0.34 → 0.65 s/首）。曲库扫描是后台任务，可接受。
#   ⚠ `SEG_HOP=256` 是**分段检测**自己的高帧率（它的注释早就写着"默认 HOP=1024 时每拍
#     只有 ~7 帧，不够"），与本常量独立，不用改。
HOP_OLD = 1024             # 仅留档：旧值（46ms/帧），排查老曲库结果时对照用
WIN = 2048                 # STFT 窗长（≈ 93 ms）

F_LOW = (30.0, 200.0)      # kick / 低音
F_MID = (200.0, 2000.0)    # 人声 / 主奏
F_HIGH = (2000.0, 9000.0)  # hihat

#: 上报能量的「密度因子」用的 8 个对数频段（Hz）。
#  ★★★ 2026-11-06（用户：「Drop 1 怎么能有 0.8 呢？它听起来非常缓和」）：
#    光看总电平分不出「满编排的 drop」和「稀疏的 drop」—— 两者 RMS 只差 3 dB 很正常。
#    实测 `Erebos` drop1：总电平只比 drop4 低 3.3 dB，但**频段间有 26 dB 的空洞**
#    （40~80Hz 掉到 −25.8 dB、5~11kHz 掉到 −12.9 dB），听感上就是"薄、缓"。
#    所以另算一套 8 频段能量，供 `sections` 求「空洞深度」用（见 `_density_penalty`）。
BAND_EDGES = ((40.0, 80.0), (80.0, 160.0), (160.0, 320.0), (320.0, 640.0),
              (640.0, 1280.0), (1280.0, 2560.0), (2560.0, 5120.0), (5120.0, 11025.0))

BPM_MIN, BPM_MAX = 60.0, 220.0   # 上限 220：Frenchcore/Uptempo 200~220 BPM（2026-10-27 高 BPM 支持）
# ★★ 2026-10-29：上界 190 → **210**。
#   实测（76 首带 VDJ 参照：老曲库 59 首 + `<曲库目录>/新建文件夹` 17 首）：
#     190 ⇒ 老曲库 51/59、新目录 8/17，合计 59/76
#     210 ⇒ 老曲库 51/59、新目录 13/17，合计 64/76   ← 老曲库**一首都没退化**
#   原因：东方同人 / Hardcore / Frenchcore 的主体在 200~210，旧上界把它们整族
#   挡在区间外 ⇒ `pick_bpm` 退化成 top-1（往往是半速 100），
#   实测 10 昏樹海 / 11 緋魔殞罪 / 3 Time Massacre / 9 Oblivious 全是这个病症。
#   ⚠ 再往上扩（220）会开始引入伪峰：200→220 那几档合计掉到 63/76。
#   ⚠ 下界 110 不动：降到 100 会让"200 BPM 的曲子被判成 100"复辟（合计 61/76）。
#   ⚠⚠ 编排端 `EasyRealityCue/src/ercue/model/bpm_core.py` 有同一行，**待同步**
#      （用户 2026-10-29 明确：AutoVJ 全部验完再动 ercue / erlib）。
BPM_PRIOR = (110.0, 210.0)  # DJ 音乐的先验区间：半速歧义时优先取落在这里的候选
PHRASE_MIN, PHRASE_MAX = 0.62, 1.30   # 八拍相位对比度阈值（低于此判定"证据不足"）

# BPM 分析的最长时间窗（秒）。**等于"尽量全曲"**：本机曲库最长 466 秒，都不受影响。
# ★★★ 2026-10-30 用户报「Crescent Horizon 分析成 143.6，我记得是 145」——真凶就是这里。
#   那时默认 180 秒，而这把歌（298 秒）**前 120 秒 ≈145、后 178 秒 ≈143.5**（差 1%）。
#   用 180 秒分析 → 143.55 得分 4.09 > 145 的 3.81 ⇒ 选了错的；
#   用全曲 298 秒 → 145.00 得分 3.93 > 143.55 的 3.82，且 VDJ 标注正是 **144.9983**。
#   原理：fold 的分辨力随长度线性增长 —— 1% 的差异在 180 秒上只累积 4 拍，
#   在 298 秒上累积 7 拍，长窗才分得开。实测另外 5 首（200 / 128 / 142 / 143.55）
#   在 180 秒与全曲下结果**完全一致**，耗时只差 <1 秒（大头是解码与特征，不是扫描）。
#   ⚠ 别为了省时间调回 180：那会把"长曲里 1% 级别的双 BPM 歧义"重新引入。
GRID_MAXSEC = 600.0

DEFAULT_VDJ = ""           # 不硬编码路径，统一由 vdj_db_path() 自动探测

# 网格算法版本：算法 / 参数变更时递增。曲库增量扫描据此判断「已有记录的网格是否过期」，
# 只对过期 / 缺失的网格重算（见 ui_main 扫描），而不用整库全量重扫。
#   1 = 初版（隐式，未写入 grid dict）
#   2 = 2026-09-27：_assemble 输出显式带 grid_ver；配合增量补网格
GRID_VER = 8          # 8 = 2026-11-18 八拍相位/结构分段同步；存量网格需重算
                      # 4 = 2026-10-30：BPM 分析长度 180 秒 → `GRID_MAXSEC`(600)。
                      #     长曲里 1% 级别的双 BPM 歧义（Crescent Horizon 298 秒：
                      #     前 120 秒 ≈145、后 178 秒 ≈143.5）在 180 秒窗上分不开 ⇒
                      #     旧版本把它存成 143.55，与 VDJ 的 144.9983 不符。
                      # 6 = 2026-10-30：**BPM 被候选级决胜改判后，anchor 不再沿用 Beat This 的**
                      #     （`_bpm_changed_by_fit`）。用户报「偏了四拍」：Addicted 沿用 bt anchor
                      #     得 1.801/phrase=4，纯相位估计是 0.3595/phrase=0 —— **差 3.47 拍**。
                      #     原因：bt 的 anchor 是按它自己那套节奏假设给的，BPM 一换就不再是小节头。
                      # 5 = 2026-10-30：新增**候选级 `tempo_fit` 决胜**（`disambiguate_by_fit`）。
                      #     `grid_conc` 会偏袒伪峰：Addicted 2 Love (Digi-Tizio Mix) 的
                      #     143.55 得 4.31、真值 144.40 只有 2.38 ⇒ `pick_bpm` 挑到 152；
                      #     而 tempo_fit 给 144.40=0.786 > 143.55=0.676 > 152=0.612。
                      #     ⚠ 必须升版本，否则存量歌永远拿不到新结果（`_need_grid` 靠它判断）。
                      #     代价：下次「扫描分析」会把 grid_ver<5 的歌放进 `todo_grid`
                      #     （**只算网格**，跳过指纹/联网/本地 AI）。
                      # 7 = 2026-11-01：**STFT 步长 1024 → 512**（见 `HOP` 的长注释）。
                      #     200 BPM 每拍从 6.5 帧变 12.9 帧 ⇒ 高档 hardcore 的 2/3 伪峰
                      #     （200→132.65）消失；全库 54 首带参照：93% → 96%，零退化。
                      #     ⚠ 必须升版本，否则存量歌永远拿不到新 BPM（`_need_grid` 靠它判断）。
                      #     代价：下次「扫描分析」把 grid_ver<7 的歌放进 `todo_grid`
                      #     （**只算网格**，~1.5s/首，不重跑指纹）。
                      # ⚠ 每次改网格算法都要 +1：ui_main 的 `_need_grid` 靠它判断"该重算"。


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
    # ★★ 2026-10-29 真机抓到（<曲库目录>\新建文件夹\11 緋魔殞罪 466 秒 FLAC 直接崩）：
    #   `idx` 原来 `.astype(np.float32)`，而 float32 在 2^24(16777216) 以上最小间隔是 **2**，
    #   最后一个采样点 22367999 被舍入成 **22368000** ⇒ `i0` 越界 1 个 ⇒ IndexError。
    #   触发条件：48kHz 下长度 > ≈350 秒（= 16777216/48000），短曲永远测不出来。
    #   ⇒ 索引用 **float64** 算，并且显式 clip（linspace 的端点浮点误差也不该靠运气）。
    _n = int(len(mono))
    idx = np.linspace(0.0, float(_n - 1), n_out)          # float64
    i0 = np.clip(np.floor(idx), 0, _n - 1).astype(np.int64)
    i1 = np.minimum(i0 + 1, _n - 1)
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
    # ★ 8 频段能量（只给 `sections` 算密度因子用；见 `BAND_EDGES` 的注释）。
    #   ⚠ 这里必须**逐段累加**，不能一次性 `np.stack([(S[:, m]**2).mean(1) ...])` ——
    #     那会给每个频段各开一份 (nf × bins) 的临时数组，长曲子上峰值内存翻几倍。
    eb = np.empty((nf, len(BAND_EDGES)), np.float32)
    for _k, (_a, _b) in enumerate(BAND_EDGES):
        _m = (freqs >= _a) & (freqs < _b)
        if _m.sum() < 2:                      # 频段太窄（极短窗）⇒ 取最近的一个 bin
            _m = np.zeros(freqs.size, dtype=bool)
            _m[np.argmin(np.abs(freqs - (_a + _b) * 0.5))] = True
        eb[:, _k] = (S[:, _m] ** 2).mean(axis=1)
    out["Eband"] = eb
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


TEMPO_FIT_MARGIN = 0.12      # 跨段命中率领先多少个百分点才改判（0.12 = 12 点）
TEMPO_FIT_MIN_SEG = 2        # 至少这么多段可比才敢改判（短曲退回原行为）

# 「候选级」tempo_fit 决胜（`disambiguate_by_fit`）—— 门槛比上面那道**松**：
#   跨族仲裁（Beat This vs 扫描）是两个**不相干**的节奏假设，要证据很足才敢翻（0.12）；
#   而这里是在同一首曲子的**近邻候选**里挑，`grid_conc` 本身就会偏袒伪峰，
#   0.12 会把正确答案挡在外面（实测 Addicted 2 Love 的真值只领先 0.11）。
# ★★ 2026-11-01（HOP 1024→512 之后重标定）：门槛 0.08 → **0.04**，候选数 12 → **24**。
#   为什么必须一起改：HOP 减半让"真值族"的 `grid_conc` 排名上来了，但 `scan_bpm` 的
#   top-3 仍是老样子（细扫只围绕粗扫前三名），所以 `pick_bpm` 照样给伪峰；
#   能救回来的只有这里——而实测 `Addicted 2 Love (radio_edit)`：
#     `grid_conc` 第一名 **144.80(2.482)**（=真值族）、pick 给 152.00，
#     但 fit(144.80) 只比 fit(152) 高 **0.063** ⇒ 被 0.08 挡住 ⇒ 存成 152（错 5.1%）。
#   全库扫描（54 首带 VDJ 参照，`tools/_sweep_bpm_margin.py`）：
#     margin 0.08 / topn 12  ⇒ 完全一致 52/54 (96%)、同族 53/54 (98%)
#     margin 0.04 / topn 24  ⇒ 完全一致 **53/54 (98%)**、同族 **54/54 (100%)**  ← 采用
#   同时把 `Bottom -outro-`（topn=12 时 136、错）救到 170（VDJ 169.01）。
#   ⚠ topn 24 的代价：多 12 次 `tempo_fit_score`（≈+0.5s/首），只有扫库时付。
TEMPO_FIT_ALT_MARGIN = 0.04  # 候选级改判门槛（fit 领先这么多才换）
TEMPO_FIT_ALT_STEP = 0.2     # 枚举步长（BPM）
TEMPO_FIT_ALT_TOPN = 24      # 参与 tempo_fit 打分的候选数（按 grid_conc 取前 N）
TEMPO_FIT_ALT_FLOOR = 0.5    # 候选的 grid_conc 至少是区间最高值的这个比例（过滤弱峰）
TEMPO_FIT_ALT_SAME = 0.02    # 与当前值相对差小于此 ⇒ 视作同一个峰，不参与改判


def disambiguate_by_fit(t, env, bpm, step=None, topn=None, margin=None):
    """在 `BPM_PRIOR` 区间里重新枚举候选，用 `tempo_fit_score` 挑更可信的那个。

    ★★★ 2026-10-30 用户第三次报 BPM 偏差（`Addicted 2 Love (Digi-Tizio Mix)`）：
      界面显示 **152.0**，用户说 **144.4**（VDJ 三首分别是 144.4 / 144.4 / 144.8，用户对）。
      实测这首曲子的 `grid_conc`（相位直方图峰均比）排名是：
        143.55 → 4.31、129.20 → 3.29 …… **真值 144.40 只有 2.38**，连 `scan_bpm` 的
        top-3 都进不去（那三名是 102/1.997、76/1.955、152/1.955）⇒ `pick_bpm` 只能在
        先验区间里挑到 152。
      但 `tempo_fit_score`（前 1/3 段拟合相位、测**所有段**的命中率）给：
        144.40 → **0.786**（74%/76%/87%）
        143.55 → 0.676（59%/70%/74%）
        152.00 → 0.612（62%/56%/66%）
      ⇒ `grid_conc` 对"周期略错"不敏感（它只看整首折叠后的峰有多尖），
        累积漂移只有跨段才暴露 —— 这与 `tempo_fit_score` docstring 里记录的
        `Power Generator`（147 vs 141.77）是同一个病根。

    做法：区间内按 `TEMPO_FIT_ALT_STEP` 枚举 → 取 `grid_conc` 前 `topn` →
    对每个候选算 `tempo_fit_score`，**领先 `margin` 才改判**。

    ⚠ 与当前值「同族」（含 2^n：半速/双速）的候选**不参与**改判 —— 倍速是 `pick_bpm`
      先验区间 + `octave_fix` 的职责，这里再插一手会把 200 改成 100。

    实测 8 首（145 / 200 / 128 / 142 / 143.55 / 175 全部不变，只有 152 → 144.40），
    成本 ≈0.7 秒/首（枚举 0.19s + 12 次 tempo_fit 0.5s）。返回**新 BPM**；
    没有更好的候选 ⇒ 返回 `None`（调用方保持原值）。
    """
    step = TEMPO_FIT_ALT_STEP if step is None else float(step)
    topn = TEMPO_FIT_ALT_TOPN if topn is None else int(topn)
    margin = TEMPO_FIT_ALT_MARGIN if margin is None else float(margin)
    try:
        cur = float(bpm)
    except (TypeError, ValueError):
        return None
    if cur <= 0 or step <= 0 or topn <= 0:
        return None
    try:
        if len(t) < 32 or len(env) != len(t):
            return None
        e = env.astype(np.float64)
        lo, hi = float(BPM_PRIOR[0]), float(BPM_PRIOR[1])
        bs = np.arange(lo, hi + 1e-9, step)
        if bs.size < 4:
            return None
        sc = np.array([grid_conc(t, e, 60.0 / b, 32)[0] for b in bs])
        if not np.isfinite(sc).any() or float(sc.max()) <= 0:
            return None
        order = np.argsort(-sc)[:topn]
        gmax = float(sc[order[0]])
        if gmax <= 0:
            return None
        best_fit = tempo_fit_score(t, e, cur)[0]
        best_b = None
        for i in order:
            b = float(bs[i])
            if abs(b - cur) <= cur * TEMPO_FIT_ALT_SAME:
                continue                       # 就是当前这个峰本身
            # 同族（含 2^n）不参与：倍速由 pick_bpm/octave_fix 负责
            p2 = _octave_norm(b, cur)
            if abs(p2 - cur) <= cur * TEMPO_FIT_ALT_SAME:
                continue
            if float(sc[i]) < TEMPO_FIT_ALT_FLOOR * gmax:
                continue                       # 弱峰不参与
            f2, _a2, r2 = tempo_fit_score(t, e, b)
            if len(r2) < TEMPO_FIT_MIN_SEG:
                continue
            if f2 - best_fit >= margin:
                best_fit, best_b = f2, b
        return best_b
    except Exception:                                            # noqa: BLE001
        return None


def tempo_fit_score(t, env, bpm, tol=0.05, nseg=3, min_beats=32):
    """跨段一致度（0~1）：用**前 1/nseg 段拟合相位**，再测**所有段**每拍 ±tol 的命中率。

    为什么需要它（真实案例 2026-10-28 `Power Generator`）：
      扫描给 147.00、Beat This! 给 141.77，差 3.6% —— 既不是「一致」也不是「2×」。
      `grid_conc` 集中度只差 12%（4.74 vs 4.22，判不出来），但 200 秒后相位累积漂了
      **18 拍**（后半段完全反相）。跨段命中率差 28 个百分点（82%/84%/67% vs 51%/54%/45%）。
      ⇒ 「集中度」看整首折叠，对「周期略错」不敏感；「跨段」才会暴露累积漂移。

    ⚠ 必须用**同一个 anchor**去测所有段。每段各自重新拟合相位会把慢漂移抹平，
      那样两个候选都能对齐，判据失效。

    返回 `(score, anchor, rates)`；样本太少 / 参数非法 ⇒ `(0.0, 0.0, [])`。
    """
    try:
        bl = 60.0 / float(bpm)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0, 0.0, []
    n = int(len(t))
    if bl <= 0.0 or n < 16 or len(env) != n:
        return 0.0, 0.0, []
    dt = float(t[1] - t[0])
    if dt <= 0.0:
        return 0.0, 0.0, []
    dur = float(t[-1])
    if dur < bl * min_beats * 2:            # 太短：连两段都凑不出，交给调用方保守处理
        return 0.0, 0.0, []
    e = env.astype(np.float64)
    seg = dur / float(nseg)

    def _hit_of_phase(phase):
        """给定相位，返回该相位下全曲每拍命中率（自适应阈值，同 ② 的算法）。"""
        tot, good = 0, 0
        for i in range(nseg):
            a = i * seg
            b = dur if i + 1 >= nseg else (i + 1) * seg
            n0 = int(np.ceil((a - phase) / bl))
            n1 = int(np.floor((b - phase) / bl))
            if n1 - n0 < 8:
                continue
            mseg = (t >= a) & (t < b)
            thr = (max(0.12, float(np.percentile(e[mseg], 70)))
                   if int(mseg.sum()) >= 8 else 0.25)
            beats = phase + np.arange(n0, n1 + 1) * bl
            i0 = np.maximum(0, np.round((beats - tol) / dt).astype(np.int64))
            i1 = np.minimum(n - 1, np.round((beats + tol) / dt).astype(np.int64))
            for s0, s1 in zip(i0, i1):
                if s1 > s0:
                    tot += 1
                    if float(e[s0:s1 + 1].max()) > thr:
                        good += 1
        return (good / float(tot)) if tot > 0 else 0.0

    # ① 定相位：**按命中率直接搜**，不用相位直方图。
    #   ⚠ 2026-10-28（INTERNET YAMERO 实测）：`grid_conc` 用 96 桶，而 onset 帧率只有
    #   46ms/帧（185 BPM 一个周期才 7 帧）⇒ 桶比帧细 13 倍，argmax 落在稀疏噪声上。
    #   实测把 185（正确答案）拟合成相位 -0.140，而真值是 -0.006（差 0.41 拍），
    #   于是 185 的跨段命中率只有 11%，比错误答案 182.5（27%）还低 —— 判据彻底失效。
    #   既然最终判据就是"每拍命中率"，相位也应该**按同一指标**搜。
    best_ph, best_sc = 0.0, -1.0
    for k in range(24):                       # 粗搜 1/24 拍
        ph = k * bl / 24.0
        sc = _hit_of_phase(ph)
        if sc > best_sc:
            best_ph, best_sc = ph, sc
    for k in range(-2, 3):                    # 细搜 ±1/12 拍
        ph = best_ph + k * bl / 96.0
        sc = _hit_of_phase(ph)
        if sc > best_sc:
            best_ph, best_sc = ph, sc
    ph = best_ph
    if ph > bl * 0.5:
        ph -= bl

    # ② 各段命中率（每拍 ±tol 内 onset 最大值，超过**该段 onset 的 70 分位**记命中）
    #   ⚠ 阈值必须按段自适应：固定 0.25 会让 onset 整体偏弱的歌（INTERNET YAMERO 实测
    #   峰值只有 0.2~0.3）整首判成 3%~5%，连正确答案都认不出来 ⇒ 判据彻底失效。
    rates = []
    for i in range(nseg):
        a = i * seg
        b = dur if i + 1 >= nseg else (i + 1) * seg
        n0 = int(np.ceil((a - ph) / bl))
        n1 = int(np.floor((b - ph) / bl))
        if n1 - n0 < 8:
            continue
        mseg = (t >= a) & (t < b)
        thr = max(0.12, float(np.percentile(e[mseg], 70))) if int(mseg.sum()) >= 8 else 0.25
        beats = ph + np.arange(n0, n1 + 1) * bl
        i0 = np.maximum(0, np.round((beats - tol) / dt).astype(np.int64))
        i1 = np.minimum(n - 1, np.round((beats + tol) / dt).astype(np.int64))
        vals = []
        for s0, s1 in zip(i0, i1):
            if s1 > s0:
                vals.append(float(e[s0:s1 + 1].max()))
        if not vals:
            continue
        v = np.asarray(vals)
        rates.append(float((v > thr).mean()))
    if not rates:
        return 0.0, ph, []
    return float(np.mean(rates)), float(ph), rates


# ---- 分段 BPM（多 BPM 段的歌，如变速曲 / 拼接曲）----
SEG_ENABLE = True
SEG_WIN = 20.0            # 分段判定窗长（秒）
SEG_STEP = 5.0            # 滑窗步长
SEG_MIN_DUR = 30.0        # 一段至少 30 秒（20s 会放过「过渡/加花」假段）
SEG_BAD_HIT = 0.55        # 全曲 BPM 下命中率低于此 ⇒ 该窗可疑
SEG_GOOD_HIT = 0.70       # 换成局部 BPM 后至少这么高才算「这段是另一个 BPM」
SEG_GAIN = 0.15           # 且要比原来高出这么多（百分点）
SEG_BPM_TOL = 0.05        # 差 <5% 视为同一段（噪声/过渡段的量级）
SEG_LOW_ENERGY = 0.25     # 窗能量低于全局中位的这个比例 ⇒ 没鼓（前奏/间奏），不参与
SEG_HOP = 256             # 分段检测专用高帧率（默认 HOP=1024 时每拍只有 ~7 帧，不够）


def _seg_hit_rate(t, e, bpm, anchor, t0, t1, tol=0.05, rel=70.0, floor=0.12):
    """每拍 ±tol 内 onset 最大值，超过「该段 onset 的 `rel` 分位」记命中。

    ⚠ 阈值必须**按段自适应**：固定 0.25 会把轻节奏段整段判死
      （实测 `Dust` 的 128 BPM 段 onset 峰值只有 0.20~0.25，明明对齐了却记 0 命中）。
    """
    try:
        bl = 60.0 / float(bpm)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0
    dt = float(t[1] - t[0]) if len(t) > 1 else 0.0
    if bl <= 0 or dt <= 0:
        return 0.0
    n0 = int(math.ceil((t0 - anchor) / bl))
    n1 = int(math.floor((t1 - anchor) / bl))
    if n1 - n0 < 4:
        return 0.0
    beats = anchor + np.arange(n0, n1 + 1) * bl
    i0 = np.maximum(0, np.round((beats - tol) / dt).astype(np.int64))
    i1 = np.minimum(len(e) - 1, np.round((beats + tol) / dt).astype(np.int64))
    vals = [float(e[a:b + 1].max()) for a, b in zip(i0, i1) if b > a]
    if not vals:
        return 0.0
    m = (t >= t0) & (t < t1)
    thr = floor
    if int(m.sum()) >= 8:
        thr = max(floor, float(np.percentile(e[m], rel)))
    return float((np.asarray(vals) > thr).mean())


def _seg_local_anchor(tw, ew, bpm, nbins=128):
    """该 BPM 下最集中的相位（秒，归一到 [-bl/2, bl/2)，全局时间轴坐标）。"""
    if len(tw) < 8 or bpm <= 0:
        return 0.0
    bl = 60.0 / bpm
    _sc, h = grid_conc(tw, ew, bl, nbins)
    ph = (int(np.argmax(h)) + 0.5) / nbins * bl
    if ph > bl * 0.5:
        ph -= bl
    return float(ph)


def _seg_local_bpm(t, e, t0, t1, lo=BPM_MIN, hi=BPM_MAX):
    """局部区间 BPM（粗 1.0 → 细 0.05，与 `scan_bpm` 同一套两级做法）。"""
    m = (t >= t0) & (t < t1)
    if int(m.sum()) < 30:
        return 0.0
    tw, ew = t[m], e[m]
    best_b, best_s = 0.0, 0.0
    for b in np.arange(lo, hi + 1e-9, 1.0):
        s = grid_conc(tw, ew, 60.0 / b, 64)[0]
        if s > best_s:
            best_b, best_s = float(b), float(s)
    if best_b <= 0:
        return 0.0
    for b in np.arange(max(lo, best_b - 1.0), min(hi, best_b + 1.0) + 1e-9, 0.05):
        s = grid_conc(tw, ew, 60.0 / b, 64)[0]
        if s > best_s:
            best_b, best_s = float(b), float(s)
    return best_b


def detect_segments(mono, sr, f, env, bpm0, anchor0, enable=None):
    """**多 BPM 段检测**（用户 2026-10-28 问「一首歌有多个 BPM 段怎么办」）。

    返回 `[{"t0","t1","bpm","beat_len","anchor"}, …]`；判定为恒速 ⇒ 返回 `[]`
    （调用方就保持「全曲单 BPM」，行为与以前完全一致）。

    ## 为什么这么设计
    - **恒速歌零额外成本**：先用低帧率逐窗测「全曲 BPM 下的命中率」，全过关就直接返回 `[]`。
      只有过关失败的（可疑变速）才重算 `SEG_HOP` 高帧率 —— 46ms/帧 时 175 BPM 每拍只有
      7 帧，相位直方图稀疏到会随机翻边，152/175 分辨不出来（实测）。
    - **判据三条缺一不可**：① 该窗在全曲 BPM 下命中率塌了；② 该窗有鼓（不是前奏）；
      ③ 换局部 BPM 后命中率明显更高。否则宁可判恒速 —— 误分段会让原本对的歌全乱。

    ⚠ 边界一定「前段 t1 == 后段 t0」：`segment_at()` 是按 t0 取段的，
      重叠会让引擎在重叠区取错段。
    """
    if enable is None:
        enable = SEG_ENABLE
    if not enable:
        return []
    try:
        bpm0 = float(bpm0)
    except (TypeError, ValueError):
        return []
    if bpm0 <= 0:
        return []
    return _detect_segments_impl(mono, sr, f, env, bpm0, float(anchor0 or 0.0))


def _detect_segments_impl(mono, sr, f, env, bpm0, an0):
    dt = f["dt"]
    t = (np.arange(env.shape[0]) * dt).astype(np.float64)
    e = env.astype(np.float64)
    dur = float(t[-1])
    if dur < SEG_WIN * 2:
        return []

    # ① 预检：恒速歌在这里就结束
    wins = []
    for w0 in np.arange(0.0, max(1.0, dur - SEG_WIN), SEG_STEP):
        w1 = min(dur, w0 + SEG_WIN)
        m = (t >= w0) & (t < w1)
        if int(m.sum()) < 8:
            continue
        wins.append([float(w0), float(w1), float(e[m].mean())])
    if not wins:
        return []
    ens = np.array([w[2] for w in wins])
    med = float(np.median(ens)) or 1e-9
    bad = 0
    for w0, w1, en in wins:
        if en < SEG_LOW_ENERGY * med:
            continue
        if _seg_hit_rate(t, e, bpm0, an0, w0, w1) < SEG_BAD_HIT:
            bad += 1
    if bad == 0:
        return []

    # ② 可疑 ⇒ 换高帧率重算（相位分辨率才够）
    try:
        f2 = features(mono, sr, hop=SEG_HOP)
        env2 = onset_env(f2, "mix")
    except Exception:                                        # noqa: BLE001
        return []
    dt = f2["dt"]
    t = (np.arange(env2.shape[0]) * dt).astype(np.float64)
    e = env2.astype(np.float64)
    dur = float(t[-1])
    peaks = scan_bpm(f2, env2)
    bpm0, _ = pick_bpm(peaks)
    if bpm0 <= 0:
        return []
    _sc, h = grid_conc(t, e, 60.0 / bpm0, 128)
    an0 = (int(np.argmax(h)) + 0.5) / 128 * (60.0 / bpm0)
    if an0 > 30.0 / bpm0:
        an0 -= 60.0 / bpm0

    wins = []
    for w0 in np.arange(0.0, max(1.0, dur - SEG_WIN), SEG_STEP):
        w1 = min(dur, w0 + SEG_WIN)
        m = (t >= w0) & (t < w1)
        if int(m.sum()) < 20:
            continue
        wins.append([float(w0), float(w1), float(e[m].mean()), 0.0, 0.0])
    if not wins:
        return []
    ens = np.array([w[2] for w in wins])
    med = float(np.median(ens)) or 1e-9

    # ③ 逐窗判定
    for w in wins:
        w0, w1, en = w[0], w[1], w[2]
        hr = _seg_hit_rate(t, e, bpm0, an0, w0, w1)
        w[3], w[4] = bpm0, hr
        if en < SEG_LOW_ENERGY * med or hr >= SEG_BAD_HIT:
            continue
        lb = _seg_local_bpm(t, e, w0, w1)
        if lb <= 0:
            continue
        lb = min([lb, lb / 2.0, lb * 2.0], key=lambda x: abs(x - bpm0))
        m = (t >= w0) & (t < w1)
        if int(m.sum()) < 20:
            continue
        ph = _seg_local_anchor(t[m], e[m], lb)
        lh = _seg_hit_rate(t, e, lb, ph, w0, w1)
        if abs(lb / bpm0 - 1.0) > SEG_BPM_TOL and lh >= SEG_GOOD_HIT \
                and lh - hr >= SEG_GAIN:
            w[3], w[4] = lb, lh

    # ④ run-length（边界相接，绝不重叠）
    segs = []
    for w in wins:
        b = w[3]
        if segs and abs(b / segs[-1]["bpm"] - 1.0) <= SEG_BPM_TOL:
            segs[-1]["t1"] = w[1]
        else:
            if segs:
                segs[-1]["t1"] = w[0]
            segs.append({"t0": w[0], "t1": w[1], "bpm": b})
    # 段内用整段重新估 BPM（逐窗值噪声大）
    for s in segs:
        lb = _seg_local_bpm(t, e, s["t0"], s["t1"])
        if lb > 0:
            s["bpm"] = lb
    # 相邻同 BPM 合并
    i = 1
    while i < len(segs):
        if abs(segs[i]["bpm"] / segs[i - 1]["bpm"] - 1.0) <= SEG_BPM_TOL:
            segs[i - 1]["t1"] = segs[i]["t1"]
            segs.pop(i)
        else:
            i += 1
    # 短段并入更接近的邻居
    changed = True
    while changed and len(segs) > 1:
        changed = False
        for i, s in enumerate(segs):
            if s["t1"] - s["t0"] >= SEG_MIN_DUR:
                continue
            tgt = i - 1 if i > 0 else None
            if i + 1 < len(segs):
                if tgt is None or abs(segs[i + 1]["bpm"] / s["bpm"] - 1.0) < \
                        abs(segs[tgt]["bpm"] / s["bpm"] - 1.0):
                    tgt = i + 1
            if tgt is None:
                continue
            if tgt < i:
                segs[tgt]["t1"] = s["t1"]
            else:
                segs[tgt]["t0"] = s["t0"]
            segs.pop(i)
            changed = True
            break

    if len(segs) < 2 or min(s["t1"] - s["t0"] for s in segs) < SEG_MIN_DUR:
        return []                     # 判定为恒速 ⇒ 与旧行为一致

    # ⑤ 补齐 beat_len / anchor（anchor = 段内落在真实拍上的位置）
    out = []
    for s in segs:
        bl = 60.0 / s["bpm"]
        m = (t >= s["t0"]) & (t < s["t1"])
        try:
            ph = _seg_local_anchor(t[m], e[m], s["bpm"]) if int(m.sum()) >= 8 else 0.0
        except Exception:                                    # noqa: BLE001
            ph = 0.0
        n = int(math.ceil((s["t0"] - ph) / bl))
        out.append({"t0": round(float(s["t0"]), 3),
                    "bpm": round(float(s["bpm"]), 3),
                    "beat_len": round(float(bl), 6),
                    "anchor": round(float(ph + n * bl), 4)})
    return out


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
    """从候选里挑「真实 BPM」：优先落在 DJ 先验区间 `BPM_PRIOR`（现 [110,210]），
    且分数足够接近最高分。

    为什么必须这么做：把 onset 按周期折叠时，**半速值（BPM/2）往往得分更高**
    （kick 每 2 拍响一次时，周期翻倍会让所有 kick 落进同一相位）。
    实测 5 首里有 2 首的第一名是 64（真值 128）。"""
    if not peaks:
        return 0.0, 0.0
    best_s = peaks[0][1]
    # ⚠ 边界必须带容差：实测有一首歌的首选峰真值是 110.0，但 `fine_bpms` 的浮点累加
    #   给出 109.99999999999994 ⇒ `110 <= 109.9999…` 为假 ⇒ 被先验排除 ⇒ 掉到 164.9（双速）。
    #   这类误差只在"峰恰好落在先验边界上"时出现，加 1e-6 容差就干净。
    #   ⚠⚠ 编排端 `EasyRealityCue/src/ercue/model/bpm_core.py` 里有同一行，**必须同步改**，
    #      否则两端对同一首歌会给出不同 BPM（码表按自己的 BPM 换算秒，会整体不对拍）。
    #   不升 `GRID_VER`：改动只影响"恰好卡在边界 1e-6 内"的候选，结果差 ≤0.05 BPM（细扫步长），
    #   已有网格不受影响，不值得让用户全库重算。
    inrange = [(b, s) for b, s in peaks
               if BPM_PRIOR[0] - 1e-6 <= b <= BPM_PRIOR[1] + 1e-6]
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


#: 全 8 槽搜索时，"胜出槽"要比 0/4 里较好的那个强多少倍才采纳（见 `phrase_phase`）
PHRASE_ALL_MIN = 1.30
#: 0/4 候选附近 ±1 拍的最小得分倍数（只允许局部一拍，不做全 8 槽乱跳）
PHRASE_LOCAL_MIN = 1.50


def phrase_phase(f, anchor, period, maxsec=None, nbeat=8, slots=None):
    """在给定锚点（**视作干净的第 1 拍**）下，定「哪一个八拍头才是乐句头」。

    ★ `anchor` 必须传**原始锚点**（不要先折叠到 [0, period)）——
      折叠会丢掉「锚点就是小节头」这个信息，使答案整体平移若干拍
      （2026-09-27 实测：折叠后 Outburst 的八拍头落到 VDJ 第 3 拍上）。
      这里让拍号 k=0 恰好对应 anchor，拍网向两侧铺满全曲。

    :param slots: 可选，限定候选拍偏移，如 `(0, 4)`。**当锚点是小节头时
        （Beat This 的 downbeat）只应传 `(0, 4)`** —— 八拍头必然落在小节线上，
        相对小节头的偏移只可能是 0 或 4；放开到全 8 槽会让弱证据把网格推 1~3 拍
        （2026-11-12 实测 89 首生产库里有 7 首存了 `phrase ∈ {1,2,3,5,7}`，
        数学上不可能，八拍线因此整体偏 1~3 拍）。传 None 保持旧的 8 槽行为。
        锚点不是小节头（纯相位估计）时不要传，否则会错误地排除合法相位。

    返回 (phrase, contrast, scores)：
      phrase   ∈ slots（默认 {0, 4} 为典型结果，见上面 slots 说明）
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
    # ★★★ 2026-11-12：**限定候选槽**（锚点是小节头时只允许 0/4）。
    #   来源：审计 89 首生产库发现 7 首存了不可能的 phrase（{1,2,3,5,7}），
    #   都是下面的"全 8 槽 argmax + 1.30 倍门槛"在弱证据下过线造成的；
    #   限定到 {0,4} 后这 7 首回到小节线上，而其余 82 首结论一首不变
    #   （因为它们的 argmax 本来就落在 0/4，或非 0/4 槽没过门槛被回退）。
    if slots:
        _cand = [int(s) % nbeat for s in slots]
        phrase = int(max(_cand, key=lambda p: float(sc[p])))
        # ★ 2026-11-18：BT 的 bar anchor 可能整体早/晚一拍。先在 0/4 里选，
        #   再只检查相邻 ±1 拍；相邻槽得分必须 ≥ 1.5× 当前槽才移动。
        #   这是局部相位细化，不是全 8 槽搜索，也不是按歌曲特判。
        _base_phrase = phrase
        _base_sc = float(sc[_base_phrase])
        for _p in ((_base_phrase - 1) % nbeat, (_base_phrase + 1) % nbeat):
            if (float(sc[_p]) >= _base_sc * PHRASE_LOCAL_MIN
                    and float(sc[_p]) > float(sc[phrase])):
                phrase = int(_p)
        _cur = float(sc[phrase])
        _oth = sorted((float(sc[p]) for p in _cand if p != phrase), reverse=True)
        _second = _oth[0] if _oth else 0.0
        contrast = (_cur / _second) if _second > 1e-9 else 99.0
        return phrase, contrast, [float(x) for x in sc]
    # ★★★ 2026-11-06（用户：「这个位置也错了一拍 —— 你这个八拍切分它偏了一拍」）：
    #   **原来只在 {0, 4} 里二选一**，等于假设"底层拍的锚点必然是小节头或半小节头"。
    #   实测 `Erebos`：八个槽的得分是
    #     0: 0.146  1: 0.104  2: 0.065  3: 0.114  4: 0.149  5: 0.135  6: 0.000  **7: 0.287**
    #   真正的八拍头在**槽 7**（是槽 0 的 1.97 倍、槽 4 的 1.93 倍），而旧代码只能
    #   在 0/4 里挑（contrast 1.02 = 抛硬币），结果整首小节线偏了 3 拍，
    #   用户听到的就是"八拍切分偏了一拍"（相对最近的强拍）。
    #   ⇒ 放开到**全 8 槽搜索**，但要求"胜出槽明显强于 0/4 里较好的那个"才移
    #     （`PHRASE_ALL_MIN` = 1.30 倍），否则保持原来的 0/4 二选一，
    #     避免在证据不足时把本来正确的网格推走。
    _best = int(np.argmax(sc))
    _base_best = 0 if s0 >= s4 else 4
    if _best != _base_best and _base_best not in (0, 4):
        _base_best = 0
    if _best not in (0, 4):
        _ref = max(s0, s4)
        if float(sc[_best]) < _ref * PHRASE_ALL_MIN:
            _best = _base_best
    phrase = int(_best)
    _cur = float(sc[phrase])
    _other = sorted((float(x) for x in sc), reverse=True)
    _second = _other[1] if len(_other) > 1 else 0.0
    contrast = (_cur / _second) if _second > 1e-9 else 99.0
    return phrase, contrast, [float(x) for x in sc]


# ==========================================================================
# 4. 整曲离线分析
# ==========================================================================
# --------------------------------------------------------------------------
# 倍速消歧 / 多窗投票（2026-10-28）
# --------------------------------------------------------------------------
# 背景：拿 VDJ 的网格当标准答案实测 50 首，纯自算是 **BPM 46/50、小节相位 41/50、
#   乐句相位 36/50**。BPM 那 4 个错里有 2 个是 **2× 错**（99.95→199.73、87.5→175.12）——
#   不是算不出，是「半速/倍速」这对孪生解里挑错了。
# 判据：真拍上每一拍都有 kick；被估成 2× 时，**奇数拍槽会明显空**
#   （实测两类样本的 odd/even 比：真值 ≈0.8~1.0，2× 错 ≈0.2~0.4）。
OCT_BALANCE_MIN = 0.55   # odd/even 低于此 ⇒ 认为当前 bpm 是 2×，取一半
# ★★ 2026-10-30（用户 `9 無邪気弾 ~ Out of Innocence`「这首是 220」）：
#   `pick_bpm` 的 `BPM_PRIOR` 上限是 210 ⇒ 真实的 220 被先验排除，只能拿到半速 110；
#   而 `octave_fix` 只会**往下纠**（奇数拍空才减半），纠不了往上。
#   ⇒ 补一条「半速检测」：把 2× 当候选，同时看**候选峰值**和**拍槽比**。
OCT_SLOW_RATIO = 0.95    # 2× 候选的 scan 得分要达到 1× 的这个比例（打平或更高）
OCT_SLOW_BALANCE = 0.80  # 2× 网格上低频拍槽的 odd/even 下限（两拍都有 kick）
OCT_SLOW_HI = 260.0      # 翻倍后的上限（再高已不是 DJ 曲的常规范围）
# ★★ 2026-10-30（用户「我扫了三遍还是 172.3」）：**整数吸附**。
#   生产路径先把整曲重采样到 22050 再算特征，`scan_bpm` 的细扫步长有限
#   ⇒ 真实的 172.0 会被折成 172.3。0.3 BPM 摊到 200 秒里累积 ≈1 拍偏差，
#   用户听到的就是"后半段卡点漂了"。而跨段命中率能明确分出高下：
#   实测 `9 無邪気弾` 172.3 fit=0.620 / 172.0 fit=0.674。
INT_SNAP_TOL = 0.40      # 小数部分离整数多近才值得试（0.40 = 差不到半拍/分钟）
INT_SNAP_GAIN = 0.02     # 整数版命中率要高出这么多才采纳（防"本来就不是整数"）
OCT_LO, OCT_HI = 60.0, 220.0
VOTE_WINDOWS = 3         # 多窗投票的窗口数
VOTE_WIN_SEC = 60.0      # 每窗秒数


def beat_slot_energy(f, env, bpm, anchor, half_win=0.12):
    """按「拍槽」采样 onset 包络 → (偶数拍均值, 奇数拍均值)。

    每拍取 ±`half_win`×拍长 内的**峰值**（不用瞬时值：onset 有抖动，
    单点采样会随机漏掉）。两组的比值就是"是不是每拍都有 kick"的判据。
    """
    try:
        dt = float(f["dt"])
        n_env = int(env.shape[0])
        if bpm <= 0 or n_env <= 8 or dt <= 0:
            return 0.0, 0.0
        bl = 60.0 / float(bpm)
        dur = n_env * dt
        cnt = int((dur - anchor) / bl)
        if cnt < 8:
            return 0.0, 0.0
        ts = anchor + np.arange(cnt, dtype=np.float64) * bl
        w = max(1, int(round(half_win * bl / dt)))
        e = np.asarray(env, dtype=np.float64)
        vals = np.empty(cnt, dtype=np.float64)
        for i, t in enumerate(ts):
            c = int(round(t / dt))
            lo = max(0, c - w)
            hi = min(n_env, c + w + 1)
            vals[i] = e[lo:hi].max() if hi > lo else 0.0
        even, odd = vals[0::2], vals[1::2]
        m = min(even.size, odd.size)
        if m < 4:
            return 0.0, 0.0
        return float(even[:m].mean()), float(odd[:m].mean())
    except Exception:                                            # noqa: BLE001
        return 0.0, 0.0


def octave_fix(f, env, bpm, anchor):
    """若当前 bpm 是「2× 高估」，返回 bpm/2；否则原样返回。

    ⚠ 只能纠「太快」，纠不了「太慢」：估成半速时每一拍槽都有 kick（奇偶一样满），
      从能量上看不出任何异常 —— 那是纯音乐歧义，交给用户/VDJ 决定。
    """
    try:
        bpm = float(bpm)
        if bpm <= 0:
            return bpm
        half = bpm / 2.0
        if half < OCT_LO:
            return bpm
        even, odd = beat_slot_energy(f, env, bpm, anchor)
        if even <= 1e-9:
            return bpm
        balance = odd / even
        if balance < OCT_BALANCE_MIN:
            return half
        return bpm
    except Exception:                                            # noqa: BLE001
        return bpm


def octave_fix_slow(f, env_low, bpm, anchor, peaks):
    """若当前 `bpm` 是「半速」（真实 = 2×），返回 `bpm*2`；否则原样返回。

    ★ 2026-10-30 用户 `9 無邪気弾 ~ Out of Innocence`：「这首是 220」，软件给 110。
      链路是：`scan_bpm` 折出 220(3.03) / 110(3.03) 打平 → `pick_bpm` 的
      `BPM_PRIOR=[110,210]` 把 220 排除（上限）→ 取 110 → Beat This 也给 110 当提示
      → `octave_fix` 只会往下纠（half=55 < OCT_LO，直接返回）⇒ 永远卡在 110。

    三重判据（缺一不可，宁可漏判也不能误翻）：
      ① 候选峰值里**真的有** 2× 那一档得分（是 `scan_bpm` 折叠出来的，不是硬算的）；
      ② 它 ≥ 1× 得分的 `OCT_SLOW_RATIO`（半速歌里 2× 往往**反而更高**）；
      ③ 用**低频** onset 在 2× 网格上采拍槽，odd/even ≥ `OCT_SLOW_BALANCE`。

    ⚠ ③ 不能用 mix / 单靠它：hi-hat 常铺在 8 分音符上，`Raccoon Memes`(128) 的
      2× 拍槽比是 0.775、`Mr.Mamizou`(129.2) 是 1.339 —— 都比这首真半速的 0.914 还高。
      所以「候选峰值打平」才是主判据，拍槽比只是把"碰巧有 2× 候选"的假阳性按住。
    """
    try:
        bpm = float(bpm)
        if bpm <= 0:
            return bpm
        dbl = bpm * 2.0
        if dbl > OCT_SLOW_HI:
            return bpm
        # ① 候选里找 2×（容差 2%：`fine_bpms` 的浮点累加会差一点点）
        s1 = s2 = 0.0
        for b, sc in (peaks or []):
            try:
                b, sc = float(b), float(sc)
            except (TypeError, ValueError):
                continue
            if abs(b - bpm) <= 0.02 * bpm and sc > s1:
                s1 = sc
            if abs(b - dbl) <= 0.02 * dbl and sc > s2:
                s2 = sc
        if s2 <= 0.0 or s1 <= 0.0:
            return bpm                      # 候选里没有 2× ⇒ 不猜
        if s2 < s1 * OCT_SLOW_RATIO:
            return bpm                      # 2× 明显更弱 ⇒ 当前就是对的
        # ③ 拍槽比：2× 网格上是不是每拍都有低频击打
        even, odd = beat_slot_energy(f, env_low, dbl, anchor)
        if even <= 1e-9:
            return bpm
        if (odd / even) < OCT_SLOW_BALANCE:
            return bpm
        return dbl
    except Exception:                                            # noqa: BLE001
        return bpm


def snap_bpm_to_int(t, env, bpm):
    """候选 BPM 的小数部分很接近整数时，试一次整数版本，用跨段命中率裁决。

    返回 `(bpm, gain)`；`gain > 0` 表示换成了整数版（并给出命中率提升）。

    ★ 2026-10-30 用户「我扫了三遍还是 172.3」：生产路径重采样到 22050 之后，
      `scan_bpm` 只能折出 172.3（细扫步长 + 折叠分辨率），而真实的 172.0 明显更贴合：
      `tempo_fit_score` 172.3=0.620 / 172.0=0.674。差 0.3 BPM 在 200 秒里累积 1 拍，
      现场表现就是"前面对得上、后面越来越偏"。

    ⚠ 只在**小数部分离整数 ≤ INT_SNAP_TOL** 时才试（电子乐 BPM 基本是整数，
      但像 129.2 那种"半拍不是半拍"的也要放过 ⇒ 超了就不试，不做无边界的搜索）。
    ⚠ 成本：只在触发时多算 2 次 `tempo_fit_score`（约 0.1s）。
    """
    try:
        b = float(bpm)
        if b <= 0:
            return bpm, 0.0
        r = float(round(b))
        if r <= 0 or abs(b - r) > INT_SNAP_TOL or abs(b - r) < 1e-6:
            return bpm, 0.0
        fit_o, _p1, _r1 = tempo_fit_score(t, env, b)
        fit_r, _p2, _r2 = tempo_fit_score(t, env, r)
        if fit_r >= fit_o + INT_SNAP_GAIN:
            return r, float(fit_r - fit_o)
        return bpm, 0.0
    except Exception:                                            # noqa: BLE001
        return bpm, 0.0


def _octave_norm(b, ref):
    """把 b 折算到「与 ref 同一八度」的值（2 的整数次幂缩放）。"""
    try:
        b, ref = float(b), float(ref)
        if b <= 0 or ref <= 0:
            return b
        r = b / ref
        while r > 1.5:
            r /= 2.0
        while r < 0.75:
            r *= 2.0
        return ref * r
    except Exception:                                            # noqa: BLE001
        return b


def analyze_bpm_only(mono, sr):
    """只估 BPM（features + scan + pick），**不跑 Beat This、不算相位**。

    多窗投票只需要 BPM；这样每窗约 0.6s，比整套分析便宜 3 倍。
    """
    try:
        f = features(mono, sr)
        env = onset_env(f, "mix")
        peaks = scan_bpm(f, env)
        bpm, _ = pick_bpm(peaks)
        return float(bpm or 0.0)
    except Exception:                                            # noqa: BLE001
        return 0.0


def analyze_vote(mono, sr):
    """整曲主分析 + **额外窗口只投 BPM**。

    ★ 2026-10-28 第二版（第一版踩坑记录）：第一版对 3 个窗口各跑一次完整分析、
      再把 BPM 与 anchor 一起投票 —— 实测**相位被打崩**（小节 82%→28%）。
      原因是短窗的小节相位本就比整段差（Beat This 只看 60s），投票反而降级。
      ⇒ 正确做法：**相位/乐句始终用整段主分析**（那条路有 Beat This 兜底），
        只有 BPM 这一个维度用多窗投票（BPM 是整曲恒定的，短窗一样准）。
    """
    n = len(mono)
    dur = n / float(sr)
    a_full = analyze_signal(mono, sr)
    if not a_full or a_full.get("bpm", 0) <= 0:
        return a_full
    win = int(min(VOTE_WIN_SEC, max(20.0, dur / 3.0)) * sr)
    if n <= win * 1.3:                       # 太短：投票没意义
        return a_full

    votes = [float(a_full["bpm"])]
    for f_frac in (0.45, 0.80):
        st = int(max(0, min(n - win, f_frac * max(0, n - win))))
        b = analyze_bpm_only(mono[st:st + win], sr)
        if b > 0:
            votes.append(b)
    ref = votes[0]
    cand = [_octave_norm(b, ref) for b in votes]
    cand.sort()
    bpm_vote = cand[len(cand) // 2]

    if abs(bpm_vote - ref) <= 0.5:
        a_full["vote"] = {"bpms": [round(v, 2) for v in votes], "pick": round(bpm_vote, 2),
                          "changed": False}
        return a_full

    # 投票结果与主分析不同 ⇒ 用新 BPM 在**整段**上重算相位（保留 Beat This 的小节头不现实，
    # 因为它给的 anchor 是按它自己的 BPM；这里退回纯相位估计，并把它标成低置信）
    period = 60.0 / bpm_vote
    f = features(mono, sr)
    env = onset_env(f, "mix")
    base, conc = phase_of(f, env, period)
    phrase, contrast, _ = phrase_phase(f, base, period)
    a_full = dict(a_full)
    a_full["bpm"] = round(bpm_vote, 4)
    a_full["bpm_auto"] = round(bpm_vote, 3)
    a_full["anchor"] = round(base + phrase * period, 4)
    a_full["base_anchor"] = round(base, 4)
    a_full["phrase"] = int(phrase)
    a_full["contrast"] = round(float(contrast), 3)
    a_full["phase_conc"] = round(float(conc), 3)
    a_full["bt"] = False
    a_full["vote"] = {"bpms": [round(v, 2) for v in votes], "pick": round(bpm_vote, 2),
                      "changed": True}
    return a_full


def analyze_track(path, maxsec=180.0, anchor_hint=None, bpm_hint=None, do_scan=True,
                  vote=None):
    """读文件后分析（见 `analyze_signal`）。

    `vote=True` 时走多窗投票（`analyze_vote`）；默认：**没有外部提示 + 需要自算**时开启。
    """
    mono, sr = load_mono(path, maxsec=maxsec)
    # ⚠ 2026-10-28 实测：**默认关闭**。多窗投票在这 50 首上整体更差
    #   （BPM 48→42、小节相位 43→36、乐句 37→32）：短窗的小节相位本来就不如整段，
    #   投票把好的也拉低了。真要开，显式传 vote=True（代码保留备用）。
    if vote is None:
        vote = False
    if vote:
        return analyze_vote(mono, sr)
    return analyze_signal(mono, sr, anchor_hint=anchor_hint, bpm_hint=bpm_hint,
                          do_scan=do_scan)


def analyze_signal(mono, sr, anchor_hint=None, bpm_hint=None, do_scan=True,
                   out_feats=None):
    """从**已解码**的单声道信号分析 → {bpm, anchor, phrase, contrast, peaks, …}。

    - `bpm_hint` / `anchor_hint` 给定时（例如来自 VDJ），用它们做基准；
    - `do_scan=False` 时**跳过 BPM 扫描**（单首最贵的一步）—— 曲库扫描时用它提速。
    - 返回的 `anchor` **统一表示「某个八拍头」**（= 上游锚点 + phrase*beat_len）。
    """
    dur = len(mono) / float(sr)
    f = features(mono, sr)
    env = onset_env(f, "mix")
    # ★ 2026-10-30（扫描提速）：把这份 STFT 结果**交出去**，让曲库扫描的能量分段
    #   直接复用 —— 它本来要 `resample + features` 再算一遍**完全相同**的东西
    #   （实测 297 秒的歌白花 0.45s/首：resample 0.20 + features 0.25）。
    #   ⚠ 只读不写：本函数与 `detect_segments` 都只读 `f`，调用方拿到后同样只读。
    if isinstance(out_feats, dict):
        out_feats["f"] = f
        out_feats["dur"] = dur

    # ★ Beat This!（本地神经网络）补位：**只在没有 VDJ 提示时**用。
    #   它解决的是"哪一拍是小节头"（我们自算的 `phase_of` 实测 21 首全错、偏 1~2 拍），
    #   而"两个小节里哪一个是乐句头"那 1 bit 仍交给下面的 `phrase_phase` 判。
    #   实测（2026-09-28，21 首）：小节相位 21/21 判对；窗口 60 秒 → 约 1.9 秒/首。
    #   不可用时（模型缺失 / onnxruntime 异常）静默退回原路径，绝不影响扫描。
    bt_used = False
    if anchor_hint is None and bpm_hint is None:
        try:
            import phrase_bt
            r = phrase_bt.analyze(mono, sr, maxsec=phrase_bt.WIN_DEFAULT)
            if r and r.get("bpm", 0) > 0:
                bpm_hint = float(r["bpm"])
                anchor_hint = float(r["anchor"])
                bt_used = True
        except Exception:                                        # noqa: BLE001
            bt_used = False

    # ★★ 2026-10-28（BPM 46/50 → 目标更高）：**扫描与 Beat This 交叉校验**。
    #   实测（VDJ 当标准答案）：`scan_bpm` 的 top-1 大多是对的
    #     （Extra Pain 99.40 / DeepSleep 175.00 / Cursed Haze 170.00 / Floating Globe 140.00），
    #   反倒是 **Beat This 给的 BPM 会把它们改坏**（99.4→199.7、175→179.9）。
    #   所以：BPM 以**扫描**为主；Beat This 只负责它真正擅长的「哪一拍是小节头」。
    #   ⚠ 但仍保留"扫描不可信时听 Beat This"的退路（两者折叠分数差得多时）。
    if bt_used:
        peaks = scan_bpm(f, env)          # 额外做一次扫描（约 0.4s），用于交叉校验
        bpm_auto, _ = pick_bpm(peaks)
    elif (do_scan and not bt_used) or not bpm_hint:
        peaks = scan_bpm(f, env)
        bpm_auto, _ = pick_bpm(peaks)
    else:
        peaks, bpm_auto = [], (float(bpm_hint) if bpm_hint else 0.0)

    bpm = float(bpm_hint) if bpm_hint else bpm_auto
    if bpm <= 0:
        bpm = bpm_auto
    # ---- BPM 归属：**同族听扫描、跨族交给命中率仲裁** ----
    #   ① 同族（两者只差 2^n，含完全相等）⇒ 用扫描的
    #      `pick_bpm` 已经用「先验区间 [110,210] + 0.7×最高分」把半速歧义定完了，
    #      Beat This 再翻倍/砍半只会把它改坏。实测（`tools/_eval_bpm_pick.py`）：
    #        · 11 緋魔殞罪 bt=100.02 / 扫描 200.00 ⇒ 旧逻辑保留 bt ⇒ 半速错
    #        · 2 Mare Merum bt=74.02 / 扫描 148.00 ⇒ 同上
    #        · 9 Oblivious bt=98.67 / 扫描 199.95 ⇒ 同上
    #      这一族错误在新目录里占 5 首，是"200 BPM 全成 100"的主因。
    #   ② 跨族（连 2^n 都凑不上）⇒ 两者是**不同的节奏假设**，用
    #      `tempo_fit_score`（前 1/3 段拟合相位、测所有段每拍命中率）仲裁：
    #      哪边让"整首每一拍都落在鼓点上"，就用哪边。
    #      基线（老曲库 59 首）里 bt 对而扫描错的曲子靠这一条（`Euphoria In Mind`
    #      扫描 99.4 落在 3:2 伪峰上、bt 150 才是对的）。
    #   ⚠ 必须"bt 领先 ≥ TEMPO_FIT_MARGIN 才采纳 bt"：默认用扫描，
    #     否则会把 `Power Generator`（扫描 147 领先 bt 141.77 达 17.5 点）改回错的。
    _bt_bpm = float(bpm_hint) if (bt_used and bpm_hint) else 0.0
    if bpm_auto > 0:
        bpm = bpm_auto
    if _bt_bpm > 0 and bpm_auto > 0:
        _same = abs(_bt_bpm - bpm_auto) <= 0.03 * bpm_auto
        _p2 = _octave_norm(_bt_bpm, bpm_auto)
        _same_oct = abs(_p2 - bpm_auto) <= 0.03 * bpm_auto
        if not (_same or _same_oct):
            try:
                _t = (np.arange(env.shape[0]) * f["dt"]).astype(np.float64)
                _e = env.astype(np.float64)
                _fa, _pa, _ra = tempo_fit_score(_t, _e, bpm_auto)
                _fb, _pb, _rb = tempo_fit_score(_t, _e, _bt_bpm)
            except Exception:                                    # noqa: BLE001
                _fa, _fb, _ra, _rb = 0.0, 0.0, [], []
            if (len(_ra) >= TEMPO_FIT_MIN_SEG and len(_rb) >= TEMPO_FIT_MIN_SEG
                    and (_fb - _fa) >= TEMPO_FIT_MARGIN):
                bpm = _bt_bpm

    # ★★★ 2026-10-30：**候选级 tempo_fit 决胜**（见 `disambiguate_by_fit` 的完整说明）。
    #   为什么必须在 `bpm` 定下来之后、算 anchor 之前做：
    #     `grid_conc` 只看"整首折叠后峰有多尖"，对"周期略错"不敏感 ——
    #     `Addicted 2 Love (Digi-Tizio Mix)` 的伪峰 143.55 得 4.31、真值 144.40 只有 2.38，
    #     于是 `pick_bpm` 在区间内挑到 152（1.955），实测 VDJ 三首都是 144.4/144.4/144.8。
    #   只改 `bpm`、不动 anchor 的算法（下面 `phase_of` 会按新 period 重算相位）。
    _bpm_changed_by_fit = False
    if bpm > 0 and env is not None and len(env) > 32:
        try:
            _t_alt = (np.arange(env.shape[0]) * f["dt"]).astype(np.float64)
            _alt = disambiguate_by_fit(_t_alt, env, bpm)
            if _alt and abs(_alt - bpm) > 0.05:
                bpm = float(_alt)
                _bpm_changed_by_fit = True
        except Exception:                                        # noqa: BLE001
            pass
    period = 60.0 / bpm if bpm > 0 else 0.5

    # ★★★ 2026-10-30（用户紧接着报「偏了四拍」）：**BPM 被上面改判后，
    #   Beat This 的小节头 anchor 不能再信** —— 它是按 Beat This 自己那套节奏假设
    #   给的相位，BPM 一换，那个"小节第 1 拍"就不再是小节头。
    #   实测 Addicted 2 Love (Digi-Tizio Mix)：沿用 bt anchor ⇒ phrase=4、
    #   anchor=1.8010；而按 144.4 纯相位估计（`phase_of`+`phrase_phase`）得
    #   phrase=0、anchor=0.3595 —— **差 3.47 拍，正是用户听到的"偏四拍"**。
    #   ⇒ 改判过 BPM 就退回纯相位估计（`phrase_phase` 的 contrast=1.264 足够高，
    #     说明八拍头证据充分，不需要 bt 兜底）。
    # ★ 2026-11-12（真实 ERLIB 库 `Liminal Link`）：`disambiguate_by_fit` 把扫描的
    #   假 BPM 152 改判成 190 —— **正好落回 Beat This 自己那一族**（189.94），说明
    #   节奏假设其实没换，BT 的小节头仍然有效。不能按"连假设都换了"处理：否则退回
    #   纯相位估计，八拍头偏 3 拍（base 0.0740 + 3*0.3158 = 1.0214，而 BT downbeat
    #   实测在 1.08）—— 正是用户报的「八拍线偏 1 拍/整体偏几拍」。
    _bt_family_ok = bool(bt_used and anchor_hint is not None and _bt_bpm > 0
                         and abs(bpm - _bt_bpm) <= 0.03 * _bt_bpm)
    if anchor_hint is not None and (not _bpm_changed_by_fit or _bt_family_ok):
        # ★ 不要折叠到 [0, period)：锚点本身就是「小节第 1 拍」，
        #   折叠会丢掉这个信息，使八拍头整体平移（实测踩过）。
        base = float(anchor_hint)
        conc = 0.0
    elif (bt_used and _bt_bpm > 0 and abs(bpm - _bt_bpm) > 0.5
          and not _bpm_changed_by_fit):
        # BPM 被扫描改掉、但 Beat This 的**拍点位置仍然可用**（它是拍位置，不随倍速变）
        # ⇒ 以它为先验挑相位，而不是完全丢掉这份信息。
        # ⚠ 但 `_bpm_changed_by_fit` 除外：那是"连节奏假设都换掉了"，bt 的相位也跟着失效
        #   （实测偏 3.47 拍），必须走下面的纯相位估计。
        base, conc = phase_of(f, env, period)
        try:
            _ba = float(anchor_hint) if anchor_hint is not None else 0.0
            if _ba > 0:
                _best, _bd = base, 1e9
                for _i in range(4):          # 4 个候选小节头，取离 bt 拍点最近的那个
                    _c = base + _i * period
                    _d = abs((_c - _ba) % (4.0 * period))
                    _d = min(_d, 4.0 * period - _d)
                    if _d < _bd:
                        _best, _bd = _c, _d
                base = _best
        except Exception:                                        # noqa: BLE001
            pass
    else:
        base, conc = phase_of(f, env, period)

    # ★ 2026-11-12：锚点来源决定候选槽。Beat This 的 downbeat 是**小节头**，
    #   八拍头只可能落在小节线上 ⇒ 只允许 {0,4}；纯相位估计得到的 base 只是
    #   普通拍格（可能是小节第 2/3/4 拍），必须保留全 8 槽，否则会排除合法相位。
    #   `_bpm_changed_by_fit` 时 BT 的相位假设已失效（见 2026-10-30 注释），也不算小节头。
    _slot04 = (0, 4) if (bt_used and anchor_hint is not None
                         and (not _bpm_changed_by_fit or _bt_family_ok)) else None

    phrase, contrast, _ = phrase_phase(f, base, period, slots=_slot04)
    anchor = base + phrase * period

    # ★ 2026-10-28：**倍速消歧**。只在"没有外部 BPM"时做（VDJ 给了 bpm_hint 就不动）。
    #   ⚠ Beat This（bt_used）也要纠：它给的 BPM 同样会落在 2× 上
    #     （实测 Extra Pain 99.95→199.73、System Overloaded 87.5→175.12）；
    #     但它给的**小节头 anchor 仍然可信**（那是一拍位置，不随倍速变）⇒
    #     减半后**沿用它的 anchor** 重算乐句相位，不要退回纯相位估计。
    if bpm_hint is None:
        bpm2 = octave_fix(f, env, bpm, anchor)
        if abs(float(bpm2) - float(bpm)) > 0.5:
            bpm = float(bpm2)
            bpm_auto = bpm
            period = 60.0 / bpm
            if bt_used:
                base = anchor          # 保留 Beat This 的拍点位置
                conc = 0.0
            else:
                base, conc = phase_of(f, env, period)
            phrase, contrast, _ = phrase_phase(f, base, period, slots=_slot04)
            anchor = base + phrase * period

    # ★★ 2026-10-30（用户 `9 無邪気弾`「这首是 220」）：**半速检测（往上纠）**。
    #   `pick_bpm` 的先验上限 210 会把真实的 220 排除 ⇒ 只能拿到 110；
    #   Beat This 再给 110 当提示 ⇒ 锁死。`octave_fix` 只往下纠，这里补往上。
    #   ⚠ 拍点位置沿用原 anchor（2× 只是把每拍再对半分，不会挪动已有的拍头），
    #     但**小节相位必须重算** —— period 变了，"第几拍是小节头"跟着变。
    try:
        bpm3 = octave_fix_slow(f, onset_env(f, "low"), bpm, anchor, peaks)
        if abs(float(bpm3) - float(bpm)) > 0.5:
            bpm = float(bpm3)
            bpm_auto = bpm
            period = 60.0 / bpm
            base = anchor
            phrase, contrast, _ = phrase_phase(f, base, period, slots=_slot04)
            anchor = base + phrase * period
    except Exception:                                            # noqa: BLE001
        pass

    # ★★ 2026-10-30（用户「我扫了三遍还是 172.3」）：**整数吸附**。
    #   172.3 是"重采样 + 细扫步长"造成的折叠误差，不是真实 BPM；
    #   用跨段命中率比较 172.0，明显更好就换掉（并重算小节相位）。
    try:
        _t = (np.arange(env.shape[0]) * f["dt"]).astype(np.float64)
        bpm4, _gain = snap_bpm_to_int(_t, env.astype(np.float64), bpm)
        if abs(float(bpm4) - float(bpm)) > 1e-6:
            bpm = float(bpm4)
            bpm_auto = bpm
            period = 60.0 / bpm
            base = anchor
            phrase, contrast, _ = phrase_phase(f, base, period, slots=_slot04)
            anchor = base + phrase * period
    except Exception:                                            # noqa: BLE001
        pass

    # ★★ 2026-11-13：±1 拍相位校正（只在小节头可信、证据够强时启用；见 ACC_PHASE_FIX）
    if ACC_PHASE_FIX and _slot04 is not None:
        try:
            _ap, _ar = _accent_phase_offset(f, anchor, period, dur)
            if _ap in (1, 7) and _ar >= ACC_PHASE_RATIO_MIN:
                _d = 1 if _ap == 1 else -1
                anchor = float(anchor) + _d * period
                if anchor < 0.0:
                    anchor += 8.0 * period
                phrase = int(round((anchor - base) / period)) % 8
        except Exception:                                    # noqa: BLE001
            pass

    # ★★ 2026-10-28：多 BPM 段检测（用户问「一首歌有多个 BPM 段怎么办」）。
    #   恒速歌在 `detect_segments` 的预检里就返回 [] ⇒ 零额外成本、结果与以前完全一致；
    #   只有「全曲 BPM 逐窗对不上」时才会重算高帧率并真的分段。
    segs = []
    try:
        segs = detect_segments(mono, sr, f, env, bpm, anchor)
    except Exception:                                        # noqa: BLE001
        segs = []

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
        "bt": bt_used,          # 是否用了 Beat This 的小节头（_assemble 据此定 src）
        "segments": segs,       # 多 BPM 段（空 = 恒速，行为与以前一致）
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
      实测遇到过：`<VDJ目录>` 里是 48 首的旧库，而真正在用的是另一盘上的
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
def _assemble(mono, sr, e, dur, do_scan=None, allow_local=True, out_feats=None):
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
        # ⚠ 2026-10-28：库扫描**不用**多窗投票（实测更差，见 analyze_track 注释）。
        a = analyze_signal(mono, sr, anchor_hint=anchor_hint, bpm_hint=bpm_hint,
                           do_scan=do_scan, out_feats=out_feats)
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
    elif a.get("bt"):
        # Beat This 的小节头实测 21/21 对（偏置 ≤0.25 拍）⇒ 给足置信度，
        # 让 ui_main 的 `conf>=0.6` 二次保险能过。⚠ 它只保证"小节头"，
        # 那 1 bit（0/4）仍来自 phrase_phase（对比度低时由用户手动翻转）。
        conf = max(conf, 0.85)
    return {
        "bpm": bpm, "beat_len": round(bl, 6), "anchor": a["anchor"],
        "phrase": a["phrase"], "contrast": a["contrast"],
        "src": ("vdj" if e else ("bt" if a.get("bt") else "local")),
        "conf": round(conf, 3),
        "dur": dur or a.get("dur_analyzed", 0.0),
        "vdj_bpm": (round(e["bpm"], 3) if e else 0.0),
        "vdj_delta": a.get("bpm_delta", 0.0),
        "remix": list(e["remix"]) if e else [],
        "peaks": a["peaks"],
        "segments": list(a.get("segments") or []),   # 多 BPM 段（空 = 恒速）
        "grid_ver": GRID_VER,
    }


def compute_grid(path, vdj=None, maxsec=GRID_MAXSEC, allow_local=True, do_scan=None,
                 use_vdj=True):
    """算一首歌的网格（自己读文件）。返回 dict（失败返回 None）。

    `anchor` 统一表示「某个八拍头」：**八拍头 = anchor + 8n * beat_len**。
    `do_scan=None`（默认）时自动决定：**有 VDJ 数据就跳过 BPM 扫描**（省掉单首最贵的一步），
    没有才自算 —— 曲库扫描（上千首）靠这个把成本压下来。

    `maxsec` 默认 `GRID_MAXSEC`（= 尽量全曲，见该常量的注释）：180 秒的分辨力
    不足以分开长曲里 1% 级别的双 BPM（Crescent Horizon 就被这个坑成 143.55）。
    `use_vdj=False`：完全不读 VDJ 的 BPM（2026-10-30 用户要求，见 `grid_from_signal`）。
    """
    if not path or not os.path.isfile(path):
        return None
    dur = _file_duration(path)
    e = vdj_lookup(vdj, path, dur) if use_vdj else None
    mono, sr = load_mono(path, maxsec=maxsec)
    return _assemble(mono, sr, e, dur, do_scan=do_scan, allow_local=allow_local)


# --------------------------------------------------------------------------
# ★★ 2026-11-13：±1 拍相位校正（用户 7 例里「八拍线整体提前/晚了一拍」）
#   Trauma 2.1240→听感应 +1 拍、Cothurnus 0.0505→应 -1 拍；Beat This 的 downbeat
#   与听感差 1 拍时，低音重音/通量会一致落在相邻拍上（全库审计里这两首的比值
#   1.19 是最强一档）。只在「小节头可信 + 证据 ≥1.15 倍 + 恰好 ±1 拍」时动。
ACC_PHASE_FIX = True
ACC_PHASE_RATIO_MIN = 1.15


def _accent_phase_offset(f, anchor, period, dur=0.0):
    """用「低音重音 + 通量」在 8 槽上投票，返回 `(最佳偏移 0..7, 相对 0 槽倍数)`。"""
    try:
        nf = int(f["nf"])
        dt = float(f["dt"])
        if nf < 64 or dur <= 0 or period <= 1e-6:
            return 0, 0.0
        n = int((float(dur) - float(anchor)) / period)
        if n < 32:
            return 0, 0.0
        t = np.arange(nf) * dt
        beats = float(anchor) + np.arange(n) * period
        i0 = np.minimum(np.searchsorted(t, beats), nf - 1)
        i1 = np.minimum(np.searchsorted(t, beats + 0.9 * period), nf)
        elow = np.zeros(n, dtype=np.float64)
        flux = np.zeros(n, dtype=np.float64)
        for i in range(n):
            a, b = int(i0[i]), int(max(i1[i], i0[i] + 1))
            elow[i] = float(np.max(f["Elow"][a:b]))
            flux[i] = float(np.max(f["flux"][a:b]))
        idx8 = np.arange(n) % 8
        ml = float(np.median(elow)) + 1e-9
        mf = float(np.median(flux)) + 1e-9
        acc = np.zeros(8, dtype=np.float64)
        for p in range(8):
            q = idx8 == p
            if not q.any():
                return 0, 0.0
            acc[p] = (elow[q] / ml).mean() + 0.6 * (flux[q] / mf).mean()
        best = int(np.argmax(acc))
        return best, float(acc[best] / (acc[0] + 1e-9))
    except Exception:                                        # noqa: BLE001
        return 0, 0.0


def grid_from_signal(mono, sr, vdj=None, path="", maxsec=GRID_MAXSEC, dur=None,
                     allow_local=True, do_scan=None, use_vdj=True, out_feats=None):
    """从**已解码**的单声道整曲信号算网格。

    ★ 曲库扫描时用它：扫描已经为指纹解码过一次，这里直接复用，省掉第二次读盘
      （HDD 上这很值钱）。只取前 `maxsec` 秒做分析。

    :param use_vdj: ★★ 2026-10-30 用户要求「**去掉 vdj 源，完全依赖本地扫描确定 bpm**」。
        `False` ⇒ **完全不读 VDJ 的 BPM**：`e=None` ⇒ `_assemble` 走 `do_scan=True`
        （强制全 BPM 扫描）、`bpm_hint=None`、`src` 标 `local`/`bt`。
        为什么：VDJ 给的 BPM 经常是半速/倍速（实测 175→87.5、200→99.95；
        `Sociable State` 的 145 也疑似 5:4 层级），当先验会把本地结果一起带偏；
        更糟的是用户手改 BPM 只写进 `grid_manual.bpm_manual`，`grid.beat_len` 仍是
        旧值 ⇒ 分段按错的拍长算 ⇒ 用户看到的"**偏差三拍**"。
    """
    if dur is None:
        dur = len(mono) / float(sr)
    e = None
    if use_vdj and path:
        e = vdj_lookup(vdj, path, dur)
    n = min(len(mono), int(maxsec * sr))
    sig = np.ascontiguousarray(mono[:n], dtype=np.float32)
    sig, sr2 = resample(sig, sr, SR_TARGET)
    g = _assemble(sig, sr2, e, dur, do_scan=do_scan, allow_local=allow_local,
                  out_feats=out_feats)
    # 2026-11-18（用户要求）：去掉相位校准表，八拍相位只由算法决定。
    return g


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
# 6.5 手工标注（grid_manual）与「有效网格」合并
# ==========================================================================
def effective_grid(grid, manual, meta=None):
    """合并算法 `grid` + 手工 `manual` → 引擎/详情页统一消费的「有效网格」。

    规则（与架构设计 §3.2/§8-Q2 一致）：
      · `manual.segments` 非空 → **分段模式**，`bpm_manual`/`anchors` 忽略；
        顶层 `bpm/beat_len/anchor` 取第 0 段（兼容只读单值的旧消费方）；
      · 无分段且 `manual.bpm_manual>0` → 覆盖整曲 `bpm/beat_len/src="manual"`；
      · 无分段且 `manual.anchors` 非空 → `anchors` 生效，`anchor=anchors[0]`；
      · 二者可共存（单段模式：手动 BPM + 多个八拍头）。

    返回**新 dict**（绝不原地改 `grid`——grid 是算法产物，重扫要能重建）。

    :param meta: 可选，`cfg["music_meta"][path]` 那一层 dict。给了就从
        `meta["grid_flip"]`（用户手工的「半句相位翻转」开关）换算成 `flip` 拍
        写进返回值。

    ⚠⚠ 2026-11-07（用户 `Northad - Frosty Dance`「这首歌的 8 拍的线偏移了 4 拍」）：
      `grid_flip` 一直只有**引擎**在读（`ui_main.py` 注入 item 时换算成 `flip=4.0`），
      详情页波形走的是本函数 ⇒ 拿不到 `flip` ⇒ 同一首歌「引擎的八拍头」和
      「波形上的八拍线」差 4 拍（0.413793×4 = 1.655 s）。现在统一从这一处下发。
    """
    if not isinstance(grid, dict):
        grid = {}
    g = dict(grid)
    m = manual if isinstance(manual, dict) else {}

    def _f(v, default=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def _with_flip(out):
        src = meta if isinstance(meta, dict) else {}
        try:
            on = bool(src.get("grid_flip"))
        except Exception:                                        # noqa: BLE001
            on = False
        out["flip"] = 4.0 if on else 0.0
        return out

    segs = m.get("segments") or []
    if segs:
        g["segments"] = [dict(s) for s in segs if isinstance(s, dict)]
        if g["segments"]:
            s0 = g["segments"][0]
            bpm0 = _f(s0.get("bpm")) or _f(g.get("bpm"))
            bl0 = _f(s0.get("beat_len")) or (60.0 / bpm0 if bpm0 > 0 else 0.0)
            g["bpm"] = bpm0
            g["beat_len"] = round(bl0, 6)
            g["anchor"] = _f(s0.get("anchor"))
            g["src"] = "manual"
        return _with_flip(g)

    bpm_m = _f(m.get("bpm_manual"))
    if bpm_m > 0:
        g["bpm"] = bpm_m
        g["beat_len"] = round(60.0 / bpm_m, 6)
        g["src"] = "manual"

    anchors = m.get("anchors") or []
    if anchors:
        g["anchors"] = sorted({_f(a) for a in anchors if _f(a) >= 0.0})
        if g["anchors"]:
            g["anchor"] = g["anchors"][0]
    return _with_flip(g)


def segment_at(segments, offset, grid=None):
    """返回 `offset` 所在段（`t0` 升序、首尾相接）。

    · `segments` 非空 → 取 `t0 <= offset` 的最后一段；
    · `segments` 为空 → 单段（由 `grid` 的 bpm/beat_len/anchor 构造；
      没给 `grid` 时返回全零单段）。
    """
    if segments:
        seg = None
        for s in segments:
            if not isinstance(s, dict):
                continue
            try:
                t0 = float(s.get("t0") or 0.0)
            except (TypeError, ValueError):
                t0 = 0.0
            if t0 <= offset:
                seg = s
            else:
                break
        if seg is not None:
            return dict(seg)
    if isinstance(grid, dict):
        bl = grid.get("beat_len") or 0.0
        bpm = grid.get("bpm") or 0.0
        if not bpm and bl > 0:
            bpm = 60.0 / bl
        if not bl and bpm > 0:
            bl = 60.0 / bpm
        return {"t0": 0.0, "bpm": float(bpm), "beat_len": float(bl),
                "anchor": float(grid.get("anchor") or 0.0)}
    return {"t0": 0.0, "bpm": 0.0, "beat_len": 0.0, "anchor": 0.0}


def anchor_for(grid, offset):
    """当前 offset 应采用哪个八拍头锚点。

    · 单锚（无 `anchors`）→ `grid.anchor`；
    · 多锚（`anchors` 升序）→ offset 前最近一个 `anchor_i`。
    """
    if not isinstance(grid, dict):
        return 0.0
    anchors = grid.get("anchors") or []
    if anchors:
        best = None
        for a in anchors:
            try:
                av = float(a)
            except (TypeError, ValueError):
                continue
            if av <= offset:
                best = av
            else:
                break
        if best is not None:
            return best
        return float(anchors[0])
    return float(grid.get("anchor") or 0.0)


def grid_anchor_after_v2(grid, offset_sec, phrase_len=8):
    """返回**下一个八拍头**的绝对秒数（分段/多锚感知；无分段无多锚时逐值等同旧 `grid_anchor_after`）。

    与 `grid_anchor_after` 的区别：分段时用「offset 所在段」的 beat_len/anchor，
    多锚时用 `anchor_for`（offset 前最近一个八拍头）而不是整曲单 anchor。
    """
    if not isinstance(grid, dict):
        return None
    if grid.get("segments"):
        seg = segment_at(grid["segments"], offset_sec, grid)
        bl = seg.get("beat_len") or 0.0
        if bl <= 0:
            return None
        a = seg.get("anchor") or 0.0
        step = phrase_len * bl
        return a + (math.floor((offset_sec - a) / step) + 1) * step
    bl = grid.get("beat_len") or 0.0
    if bl <= 0:
        return None
    a = anchor_for(grid, offset_sec)
    step = phrase_len * bl
    return a + (math.floor((offset_sec - a) / step) + 1) * step


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

    def reanchor(self, beat_len, anchor, off, r, ref_t):
        """跨段/换锚时**硬重锚**：坐标整体换系，不经「有限增益校正 / 跳变判据」。

        与 `update` 的差别：段边界是**离线已知**的（manual.segments / anchors），
        跨段时 BPM/锚点突变是预期行为、不是识别抖动 ⇒ 无需防抖，直接一步到位。
        `sid` 不变（同一首歌只是换了坐标系）。
        """
        if beat_len is None or beat_len <= 0:
            return
        r = 1.0 if (r is None or not (0.5 <= r <= 2.0)) else float(r)
        with self._lock:
            ref_pos = (off - anchor) / beat_len
            self.bl = float(beat_len)
            self.r = r
            self.t0 = ref_t
            self.pos0 = ref_pos
            self.ref_t = ref_t
            self.last_pos = None
            self._err_run = 0
            self._err_sign = 0

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
