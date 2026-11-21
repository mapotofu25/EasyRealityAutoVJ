# -*- coding: utf-8 -*-
"""音频指纹（Dejavu 算法，参数与 VJVision fp/fingerprint.h 一致）。

STFT(4096/Hann/hop2048) → log-power 谱 → 2D 峰值(分块最大值, 12dB) →
SHA1(量化 freq1|freq2|dt) 前 20 hex → SQLite 倒排索引 → 对齐投票匹配。

自建库自匹配（建库与查询用同一套峰值检测），无需与 VJVision 二进制指纹互通。
"""
import hashlib
import sqlite3

import numpy as np

SAMPLE_RATE = 44100
FFT_WINDOW = 4096
HOP_SIZE = 2048
FAN_VALUE = 8            # 每峰配后 fan-1=7 个峰
AMP_MIN = 12.0           # dB
HASH_LEN = 20            # SHA1 前 20 hex = 80 bit
MAX_HASH_TIME_DELTA = 200
DT_QUANT = 4             # 时间差量化（抗 keylock ±8% 变速）
FREQ_QUANT = 4           # 频率量化
MIN_CONFIDENCE = 0.05
PEAK_NEIGHBORHOOD = 20   # 41×41 邻域严格局部极大值（VJVision peaks.cpp 同款）
FP_SCHEMA_VERSION = 2    # 峰值检测算法版本（v1=规则分块；v2=邻域极大值，抗变速）

# 「连续性提示」窗口（单位 = hop 帧，与 match 内部的 delta 同一坐标轴）。
# 背景：强重复段落里，同一段音频在歌里出现多次，查询窗与"另一处出现"哈希对得上，
#   票数甚至高于正确位置 ⇒ 纯靠票数取全局众数会挑到错位置（实测偏差 148~738 拍，
#   ≈1490~7450 帧）。引擎若把「当前预测位置」作为 hint 传进来，就能在这堆候选里
#   优先选**靠近预测位置**的那一个，从根上避开重复段落歧义。
# 取值依据（tools/_fix3_d_hint.py 实测，20 首真实逐窗序列、逐窗投票直方图）：
#   · 正确候选距"真值"的偏差：max 10.3 帧（p99 0.6）—— 指纹量化误差；
#   · 生产侧 hint 误差：时钟跟踪 ≲1 拍(≈10.7 帧) + 一窗时滞(≈0.5s≈10.8 帧) ⇒ 合计 ≲21 帧；
#   · 可触发硬重锚的错误候选(|err|≥3.5 拍)距"真值"的下界：min 37.7 帧（p1 50.7）。
#   取 **32 帧**≈1.5s：≥3× 正确候选偏差、覆盖 hint 误差(21 帧)并留余量，且低于错误候选下界
#   (37.7 帧)。端到端实测 W∈[16,32] 全局 max|err| ≤ 3.78 拍；W≥40 升到 7.28；W≥96 反升到
#   14.55（开始放进错误候选）。窗口内无候选 ⇒ 退回全局众数 ⇒ 真实远距离 seek 仍生效。
#
# ⚠ 32 帧是**帧**坐标，换算成拍数随 BPM 变化（1 拍 = beat_len·SR/HOP 帧）：
#   128BPM ⇒ 32 帧 ≈ 2.97 拍；200BPM ⇒ 32 帧 ≈ 4.44 拍（已越过 3.5 拍错误候选下界！）；
#   90BPM ⇒ 32 帧 ≈ 1.87 拍。⇒ 固定帧窗在两端 BPM 都不合理。**受支持的定义改为按拍数**：
#   `HINT_W` 仅作为「调用方未显式给窗口」时的缺省（保持旧调用方与既有行为逐值一致）。
#   引擎按当前歌 grid 的 beat_len 用 `hint_window_frames()` 换算后传 `hint_w`。
HINT_W = 32

#: hint 窗口内候选"够不够格赢"的最低票数比例（相对全局最高票）。
#: ★★★ 2026-10-18 真机定位（用户："码表视频还是慢 跟不上音频"）：
#:   原值 0.10 太松 —— 提示窗里只要有 **3 票**就能压过全局 40 票的正确位置。
#:   而 hint 来自**拍钟预测**，预测一旦滞后，匹配就被锁在旧位置附近、**自我强化**
#:   ⇒ 实测 `off` 连续 **8~10 秒**卡在同一个值不动 ⇒ 码表位置落后 8~14 秒 ⇒ "一直慢"。
#:   提到 0.5：同段重复段落的两处出现实测票数比 ≈60%（35 vs 58）仍能胜出，
#:   而"滞后提示的弱候选"（10 vs 40 = 25%）被拒 ⇒ 退回全局众数，位置能自己纠回来。
HINT_WIN_MIN_RATIO = 0.50

# 「连续性提示」窗口半宽的**受支持定义**（单位 = 拍）：跨 BPM 一致。
# 依据（第三轮 fix：正确候选偏差 ≲0.5 拍；生产侧 hint 误差 ≲1 拍跟踪 + 0.5s 时滞；
#   可触发硬重锚的错误候选下界 ≈3.5 拍）：
#   · 下界：须覆盖 hint 误差。时滞 0.5s 在帧坐标里是常数(≈10.8 帧)，折成拍数 = 0.5/bl，
#     高 BPM（bl 小）时更大；1 拍跟踪 + 0.5/bl 拍 ≈ 2.0 拍(150BPM)~2.7 拍(200BPM)。
#   · 上界：必须 < 3.5 拍（错误候选下界），否则会放进"另一处出现"的高票错候选。
#   实测扫描（tools/_gridfix4_accept.py，真实逐窗直方图 × 跨 BPM）取 **2.5 拍**：
#     既覆盖 hint 误差（含 200BPM 的 2.67 拍仍在校验范围），又低于 3.5 拍错误下界。
HINT_W_BEATS = 2.5

# ★★★ 2026-11-12（用户：码表内容来回跳 / 认歌慢）：**已锁定曲目的本地跟踪**。
#   对应 KVJ 的“锁定后只在 ±60ms/±1% 内局部跟踪”，而不是每个窗口重新全球认歌。
#   `match(lock_sid=...)` 时：
#     · 先只看当前锁定曲的票；只要它的最佳位置 ≥ 6 票、且 ≥ 全球最高票的 25%，
#       就继续返回当前曲（位置仍按 hint 选，防重复段落）。
#     · 低于这两条 ⇒ 退回全球众数，让 charge_engine 有机会真正换歌。
#   这样相似曲/同专辑曲不会在单窗里把 sid 抢走，码表就不会反复重锚。
#   ⚠ 2026-11-18 同步 ERLIB `fp.py` 时这段被漏掉（ERLIB 不跑实时认歌）——
#     AVJ `audio_engine._recognize_loop` 传 `lock_sid=` 会 TypeError，
#     **整个认歌循环直接死掉**（现场：歌一直"认不出来"）。
TRACK_LOCK_MIN_VOTES = 6
TRACK_LOCK_RATIO = 0.25


def hint_window_frames(beat_len, w_beats=None):
    """把「hint 窗口半宽（拍）」换算成 delta 坐标的 hop 帧数。

    delta 的单位 = 44100 下的 hop 帧。1 拍 = beat_len 秒 = beat_len·SR/HOP 帧。
    `w_beats` 缺省用 `HINT_W_BEATS`。`beat_len` 非法时回退缺省帧窗 `HINT_W`。
    """
    try:
        bl = float(beat_len)
    except (TypeError, ValueError):
        bl = 0.0
    wb = HINT_W_BEATS if w_beats is None else float(w_beats)
    if bl <= 0.0 or wb <= 0.0:
        return float(HINT_W)
    return wb * bl * SAMPLE_RATE / HOP_SIZE


# 分块帧数：只影响**峰值内存**，不影响 FFT 数值（逐块 FFT 与整块 FFT 逐位相同）。
# 背景（2026-11-12 用户"重新分析卡 0%"）：原实现一次性构造
#   idx = arange(nframes)[:,None]*HOP + arange(4096)[None,:]   → int64 (nframes,4096)
# 4 分钟歌 nframes≈4852 ⇒ 197 MB；再叠加 frames float32 82 MB / rfft complex 40 MB /
# abs+spec 80 MB / sig 42 MB ⇒ 单首峰值 400~500 MB。本机 commit 余量只剩 0.27 GB 时
# 每首都 MemoryError，界面看起来就"卡 0%"。分块后峰值 ≈ spec(40 MB)+块(4 MB)。
_SPEC_CHUNK_FRAMES = 256

# 峰值检测 numpy 回退路径的分块粒度（元素数，float32 ⇒ 1<<21 = 8 MB/块）。
# 同样只影响峰值内存：每个输出点只在自己的 (2N+1) 窗口上求 max，分块不影响结果。
_PEAK_CHUNK_ELEMS = 1 << 21


def _spectrogram(sig):
    """mono float32(44.1k) → log-power 谱 (dB)，shape [freq_bins, frames]"""
    if len(sig) < FFT_WINDOW:
        return np.zeros((FFT_WINDOW // 2 + 1, 0), dtype=np.float32)
    win = np.hanning(FFT_WINDOW).astype(np.float32)
    nframes = 1 + (len(sig) - FFT_WINDOW) // HOP_SIZE
    nbin = FFT_WINDOW // 2 + 1
    # 滑窗视图不复制数据；每块再乘窗 → 只分配 chunk×FFT 的 float32
    from numpy.lib.stride_tricks import sliding_window_view
    frames_all = sliding_window_view(sig, FFT_WINDOW)[::HOP_SIZE]
    spec = np.empty((nbin, nframes), dtype=np.float32)
    chunk = _SPEC_CHUNK_FRAMES
    for s in range(0, nframes, chunk):
        e = min(nframes, s + chunk)
        block = frames_all[s:e] * win            # [chunk, FFT] float32
        spec[:, s:e] = np.abs(np.fft.rfft(block, FFT_WINDOW, axis=1)).T
        del block
    # 原地平方/加/取对数：省掉 2 份 (bins×frames) 的临时数组（4 min 歌每份 46 MB）
    np.square(spec, out=spec)
    spec += 1e-10
    np.log10(spec, out=spec)
    spec *= 10.0
    return spec


def _find_peaks(spec):
    """2D 峰值：41×41 邻域严格局部极大值（VJVision peaks.cpp 同款，抗变速）。

    先做可分离最大滤波（先时间轴、后频率轴的滑动最大值）得到每个点的邻域最大值，
    候选 = 值等于邻域最大且 > 阈值；再排除「平顶并列」（邻域有同值）保证严格最大。

    ★ 2026-11-08：可分离最大值改用 cv2.dilate（1×41 + 41×1 两次），
      1080p/4.5s 窗实测 9.5ms → 2.2ms，峰值列表与旧 numpy 实现逐项相同
      （tools/_opt_verify_fp.py 断言）。没有 cv2 时退回旧实现。
    """
    F, T = spec.shape
    N = PEAK_NEIGHBORHOOD
    W = 2 * N + 1
    if F <= 2 * N or T <= 2 * N:
        return []
    from numpy.lib.stride_tricks import sliding_window_view
    cand_pts = []                     # 候选 (f, t)，还没做「平顶并列」过滤
    cp = cand_pts.append
    try:
        import cv2
        a = cv2.dilate(spec, np.ones((1, W), np.uint8),
                       borderType=cv2.BORDER_CONSTANT, borderValue=-np.inf)
        mx = cv2.dilate(a, np.ones((W, 1), np.uint8),
                        borderType=cv2.BORDER_CONSTANT, borderValue=-np.inf)
        del a
        inner = spec[N:F - N, N:T - N]
        cand = np.where((inner == mx[N:F - N, N:T - N]) & (inner > AMP_MIN))
        for r, c in zip(*cand):
            cp((int(r) + N, int(c) + N))
        del mx, inner, cand
    except Exception:                                          # noqa: BLE001
        # numpy 回退：可分离滑动最大 + 分块比较（峰值内存从 ~5×spec 降到 ~2×spec）。
        # 每个输出点只在自己的窗口上求 max/比较 ⇒ 分块结果与整体实现逐位相同。
        # 时间轴：按行分块（块内两侧 pad N 列再滑窗）
        tMax = np.empty((F, T), dtype=np.float32)
        row_chunk = max(1, min(F, _PEAK_CHUNK_ELEMS // max(1, T + 2 * N)))
        for s in range(0, F, row_chunk):
            e = min(F, s + row_chunk)
            pad_t = np.full((e - s, N), -np.inf, dtype=np.float32)
            blk = np.concatenate([pad_t, spec[s:e], pad_t], axis=1)
            tMax[s:e] = sliding_window_view(blk, W, axis=1).max(axis=2)
            del blk, pad_t
        # 频率轴：按列分块，块内出最大后**立刻**比较出候选（不保留整张 mx）
        col_chunk = max(1, min(T, _PEAK_CHUNK_ELEMS // max(1, F + 2 * N)))
        for s in range(0, T, col_chunk):
            e = min(T, s + col_chunk)
            cs, ce = max(s, N), min(e, T - N)
            if ce <= cs:
                continue
            pad_f = np.full((N, e - s), -np.inf, dtype=np.float32)
            blk = np.concatenate([pad_f, tMax[:, s:e], pad_f], axis=0)
            mxb = sliding_window_view(blk, W, axis=0).max(axis=2)
            del blk, pad_f
            inner = spec[N:F - N, cs:ce]
            cand = np.where((inner == mxb[N:F - N, cs - s:ce - s]) & (inner > AMP_MIN))
            for r, c in zip(*cand):
                cp((int(r) + N, int(c) + cs))
            del mxb, inner, cand
        del tMax

    # 候选：内部点，值 == 邻域最大 且 > 阈值；再排除平顶并列
    peaks = []
    ap = peaks.append
    for f, t in cand_pts:
        val = spec[f, t]
        # ⚠ 原来这里 `.copy()` 一个 41×41 邻域、把中心改成 -inf 再 any(>= val)；
        #   候选峰动辄上千个 ⇒ 每次识别白拷贝几 MB。改成"切片视图 + 计数"：
        #   邻域里 >= val 的个数减去中心自己（它恒等于 val）> 0 ⇒ 存在并列。
        #   语义与原写法**完全等价**，但零拷贝。
        nb = spec[f - N:f + N + 1, t - N:t + N + 1]
        if int(np.count_nonzero(nb >= val)) > 1:
            continue
        ap((f, t))
    peaks.sort(key=lambda p: (p[1], p[0]))
    return peaks


def _hashes(peaks):
    """峰对 → [(hash, offset)]。

    ⚠⚠ **不要改这里的哈希算法**：换了就等于换指纹格式，用户库里 1421 首得整库重扫
    （`FP_SCHEMA_VERSION` 会检测到并清空 fingerprints 表）。所以下面只做
    「输出逐字节相同」的加速：
      · `f1 // FREQ_QUANT` 提到内层循环外（原来每对峰都重算一次）；
      · `b"%d|%d|%d" % (...)` 直接产 bytes，省掉 str→encode 的一次分配；
      · 常量与 append 提成局部变量（内层循环每窗口要跑 ~1700 次）。
    """
    out = []
    ap = out.append
    n = len(peaks)
    fq, dq, hl = FREQ_QUANT, DT_QUANT, HASH_LEN
    fan, dmax = FAN_VALUE, MAX_HASH_TIME_DELTA
    for i in range(n):
        f1, t1 = peaks[i]
        q1 = f1 // fq
        for j in range(1, fan):
            if i + j >= n:
                break
            f2, t2 = peaks[i + j]
            dt = t2 - t1
            if dt < 0 or dt > dmax:
                continue
            h = hashlib.sha1(b"%d|%d|%d" % (q1, f2 // fq, dt // dq)).hexdigest()[:hl]
            ap((h, t1))
    return out


def fingerprint_audio(sig, sr):
    """sig: mono float32 → [(hash, offset_frame), ...]（offset 单位 = hop 帧）"""
    if sr != SAMPLE_RATE:
        x = np.linspace(0, len(sig) - 1, max(1, int(len(sig) * SAMPLE_RATE / sr)))
        sig = np.interp(x, np.arange(len(sig)), sig).astype(np.float32)
    spec = _spectrogram(sig)
    return _hashes(_find_peaks(spec))


# ==========================================================================
# 「指纹签名」`fp2-<16hex>` —— 与 EasyRealityCue 的 `model/fp_core.py` **逐字节一致**
# ==========================================================================
#: 签名格式版本；`2` 对应 `FP_SCHEMA_VERSION=2`。两端必须同步改。
SIG_PREFIX = "fp2-"
SIG_HEX = 16              # sha1 前 16 hex = 64 bit


def signature_from_hashes(hashes):
    """指纹哈希序列 → **稳定的签名串**（`fp2-<16 hex>`）；空序列 ⇒ `""`。

    ★ 这是码表（.ercue）里 `identity.fp_id` 的**同一个东西**：编排端
      `fp_core.signature_from_hashes()` 逐字节同算法 ⇒ 同一段音频在两端算出**同一串**。
      ⚠「同一段音频」= 解码出的 PCM 必须一致。任何一端换了重采样/声道混合方式，
        签名就变 ⇒ 跨端比对**静默永不命中**（`tools/_fp_sig_probe.py` 就是为此写的探针）。
    """
    if not hashes:
        return ""
    h = hashlib.sha1()
    for hh, off in hashes:
        h.update(hh.encode("ascii", "replace"))
        h.update(b":")
        h.update(b"%d|" % int(off))
    return SIG_PREFIX + h.hexdigest()[:SIG_HEX]


def fingerprint_signature(sig, sr):
    """一步到位：音频信号 → 指纹签名（`fp2-<16 hex>`）；算不出来返回 `""`。"""
    try:
        return signature_from_hashes(fingerprint_audio(sig, sr))
    except Exception:                                            # noqa: BLE001
        return ""


def file_md5(path, chunk=1 << 20):
    """**文件内容**的 md5（`hexdigest`，小写）；读不到返回 `""`。

    ⚠⚠ **不要和 `song_id` 混用**：
      · 本函数 = 文件字节的 md5（32 hex），码表 `identity.file_md5` 就是这个；
      · `song_id = int(md5(路径字符串)[:8], 16)`（8 hex，见 `ui_main` 建库处）
        —— 它是**指纹库主键 / `recognized_song_id`**，跟文件内容无关。
      两者互比永远不相等，且**不会报错**（一个 32 hex、一个整数）。
    """
    try:
        h = hashlib.md5()
        with open(str(path), "rb") as f:
            for blk in iter(lambda: f.read(chunk), b""):
                h.update(blk)
        return h.hexdigest()
    except Exception:                                            # noqa: BLE001
        return ""


# ==========================================================================
# `cfg["music_meta"][path]` 里与「码表认歌」有关的两个字段
# ==========================================================================
#: 指纹签名键（`fp2-<16hex>` ⇒ 码表 `identity.fp_id`，认歌 **① 级**）
META_KEY_FP = "fp_id"
#: 音频文件字节 md5 键（32 hex ⇒ 码表 `identity.file_md5`，认歌 **⓪ 级**）
META_KEY_MD5 = "file_md5"


def meta_fp_fields(rec):
    """`music_meta` 的一条记录 → `(fp_id, file_md5)`；非 dict / 缺键 ⇒ `""`。

    ⚠ 只读**这两个键**。`song_id`（路径哈希）不在这套语义里；`legacy` 的 `md5`
      键只在 `engine._cue_track_query()` 里做旧配置兼容，本函数不回写它。
    """
    if not isinstance(rec, dict):
        return "", ""
    return (str(rec.get(META_KEY_FP) or ""), str(rec.get(META_KEY_MD5) or ""))


def meta_needs_fp(rec, legacy=False):
    """这条 `music_meta` 记录**是否还缺认歌字段**（缺任一个 ⇒ `True`）。

    ★ 为什么是"缺任一个"而不是只看 `fp_id`：⓪ 级（文件字节 md5）与 ① 级（指纹签名）
      是**互补**的两级 —— ① 级抗改名/换容器（同一段音频重编码仍命中），⓪ 级抗
      "指纹算不出来 / 指纹算法版本变了"（同一个文件是确定判据）。少写一个等于少一条命。
    ★ 非 dict / 记录不存在 ⇒ `False`（它不在"已有元数据"之列，由别处的"全量分析"负责）。
    :param legacy: `True` = **旧判据**（只看 `fp_id`）。仅供回归对照与排障：
                   `tools/cue_fp_link_test.py` 用它做"反向复现"，证明新判据真的在起作用。
    """
    if not isinstance(rec, dict):
        return False
    fp_id, fmd5 = meta_fp_fields(rec)
    if legacy:
        return not fp_id
    return not (fp_id and fmd5)


# ==========================================================================
# 变速（倍率）对齐 —— 「票数最多的那条直线」
# ==========================================================================
# ★★★ 2026-10-29：**为什么需要它**
#   本库的 hash 是 `SHA1(f1|f2|Δt)`，Δt 被量化成 4 帧（185ms）的桶。DJ 机台
#   beatmatch 时**保留音高、只改速度** ⇒ 频率部分完全命中，但
#     ① Δt 随倍率缩放 ⇒ 只有 Δt 小的那批 hash 还落在同一个桶里（其余丢掉）；
#     ② 同一个事件在查询窗里的帧坐标 `q` 与在库里的帧坐标 `d` 满足
#        **`d = rate·q + delta`**（rate = 播放倍率、delta = 对齐偏移）。
#   旧实现只统计 `dboff - qoff`（等价于写死 rate=1）⇒ 一条斜线上的票被切成
#   一片片单点，1.02 变速就能把单点票打掉一个数量级。
#   这不是本库独有的现象：audfprint 官方实测（225s 查询、同一段音频重采样）
#     stretch 1.000 → 固定偏移票 340 / 允许线性 warp 票 340
#             1.001 → 127 / 325
#             1.005 → 15  / 131
#             1.010 → 4   / 49
#             1.020 → 2   / 11
#   —— 票没消失，是**沿一条直线摊开了**。所以修法是在匹配端补「找票数最多的
#   那条直线」（Hough/RANSAC 的思路，等于 audfprint 的 `-warpmax` 的推广版：
#   不是"允许小漂移"，而是直接解出最优斜率）。
#   ⚠ 不动指纹库、不动 hash 格式、不改建库流程 ⇒ 老库直接可用。
#
# 假阳性风险评估（决定阈值怎么取）：
#   无关曲的 (q,d) 点是**随机散布**，在 71 个候选斜率里挑最大值，期望峰值
#   ≈ N/跨度 ≈ 4000/200000 = 0.02 票 ⇒ 拟合票要上 80 是不可能的。真正需要
#   防的是「同专辑/同曲风共用采样的另一首歌」—— 那种情况点**真的会成线**，
#   所以身份判据必须保留「对第二名的领先倍数」和 BPM 交叉校验，不能只看绝对票数。
WARP_RATE_LO = 0.80        # 允许的播放倍率下界（-20%；DJ 极端降速也够）
WARP_RATE_HI = 1.25        # 上界（+25%）
WARP_COARSE_STEP = 0.010   # 粗扫步长
WARP_FINE_HALF = 0.012     # 细扫半宽（在粗扫最优点附近）
WARP_FINE_STEP = 0.002     # 细扫步长
WARP_MAX_PTS = 4000        # 单曲参与拟合的点数上限（超出固定种子下采样，结果可复现）
# ★★★ 2026-10-29 真机验证：**40 不能降**。
#   试过降到 12 让它在"单点票只有 11~17"的变速窗上也能出手 —— 结果 1.20× 的
#   dmax 从 0.59 恶化到 **7.19 秒**（9 个点超 0.6、位置窜动 1 次）。
#   根因：十几个点凑一条线太容易，`_span_ok` 只能要求"铺满窗口"，挡不住
#   "铺满窗口的错线"（重复段落的另一处出现同样铺满）。**低信噪比下宁可不用拟合**，
#   位置退回单点票结果（有 `_off_state` 兜着），也不会被错线带跑。
WARP_MIN_PTS = 40          # 少于此数直接放弃（统计上凑不出线）
WARP_TOL_FRAME = 1.0       # 「落在线上」的容差（帧）；与 match 的精确到帧口径一致
# ★★★ 2026-10-29 实测（真实 DJ 直播素材）发现必须加「跨度覆盖」约束：
#   没有它时，拟合会稳定给出 **0.72（= 当时的搜索下界）** —— 因为 `delta = d - a·q`
#   在 a 变小时 q 的权重降低、delta 更接近 d 本身；只要这首歌的命中集中在若干重复
#   段落上，d 的方差本来就小 ⇒ **a 越小票越多**。这是纯数学偏向，与"真倍率"无关。
#   ⇒ 除了收窄量程，还要求内点**在 q 上铺开**：真斜线必须横跨整个窗口，
#     而"a 偏小挤出来的假集中"只会堆在 q 的一端。
WARP_SPAN_MIN = 0.55       # 内点在 q 上的跨度 / 全部点的 q 跨度 的最小比例
WARP_SEG_N = 3             # 把 q 等分成几段做"每段都得有内点"的检查
WARP_SEG_NEED = 2          # 至少几段有内点（不是每段都要：窗内有静音/淡出会让某段空）
# ★ 单测发现：只比"相对跨度"挡不住「票本来就全挤在一小段」的情况（自身跨度=100%）。
#   斜率要可辨识，点集本身得横跨足够长的时间 —— 少于 25 帧（≈1.16s）时，
#   a 变化 0.01 只让 b 变 0.25 帧，任何斜率都能凑出高票 ⇒ 必须直接拒绝。
WARP_MIN_QSPAN = 25.0      # 参与拟合的点在 q 上的最小跨度（帧）


class FingerprintDB:
    """指纹库：add_song 建库，match 现场匹配（返回对齐票数，供音乐电池引擎消费）"""
    def __init__(self, path):
        # check_same_thread=False：指纹库在主线程创建、识别线程读取（扫描建库在后台线程写）
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
        # ★★ 2026-11-11（用户：识别要十几秒）：指纹库 3.5M 行 / ~300MB，
        #   SQLite 默认只有 2MB 页缓存（PRAGMA cache_size=-2000）且 mmap=0，
        #   现场 5~10 路视频同时读盘时，一次 4.5s 窗的随机 hash 查询会被拖到几秒。
        #   ⇒ 放大页缓存 + 映射整个库 + 临时表放内存（只读热路径，不改变数据）。
        try:
            self.conn.execute("PRAGMA cache_size=-65536")      # 64 MB
            self.conn.execute("PRAGMA mmap_size=536870912")    # 512 MB（库 ~300MB）
            self.conn.execute("PRAGMA temp_store=MEMORY")
            self.conn.execute("PRAGMA busy_timeout=5000")
        except Exception:                                      # noqa: BLE001
            pass
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA journal_mode=WAL")   # WAL：扫描写/识别读可并发
        self._pending = 0          # 批量模式下"已写未 commit"的歌曲数（见 commit_batch）
        self.last_song_votes = {}  # ★ 上一次 match 的「每曲总票」：{song_id: votes}
        # ★ 上一次 match 的「命中哈希率」：查询哈希里有多大比例在库里出现过（0~1）。
        #   beatmatch 变速不改变音高 ⇒ 哈希仍命中（→ 高命中率），但位置漂移
        #   ⇒ 单对齐票被摊平。这个指标用来把"同一首歌被变速"和"另一首相似的歌"分开。
        self.last_hit_rate = 0.0
        # ★ 上一次 match 的「每曲最佳单对齐票」：{song_id: votes}
        #   与 last_song_votes（总票）配合：总票会被重复段落抬高，
        #   单对齐最佳票才是"某个位置真的对上了"的证据。
        self.last_song_best = {}
        # ★★ 2026-10-19：**只允许当前曲库里真实存在的 song_id 参与匹配**。
        #   背景：指纹库会残留「已从曲库移除」的历史条目；若不设白名单，放一首
        #   不在曲库的歌，也会被匹配到这些过期 id ⇒ 界面报出一首不存在的歌（假阳性）。
        #   None = 不限制（兼容旧行为 / 单元测试）。
        self.allowed = None
        self._scan_orig_sync = None
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS fingerprints ("
            "hash TEXT NOT NULL, song_id INTEGER NOT NULL, offset INTEGER NOT NULL)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_hash ON fingerprints(hash)")
        # ★ 2026-09-26：`add_song` 里的 `DELETE FROM fingerprints WHERE song_id=?`
        #   原先**没有 song_id 索引** ⇒ 每次都是全表扫描，库越大越慢
        #   （实测：2M 行 0.15s → 24M 行 1.1s，而扫描时**每一首歌**都要付这个成本）。
        #   加上索引后按 song_id 直接定位。`IF NOT EXISTS` 幂等：老库只在第一次补建
        #   （大库补建可能花几秒~几十秒，但发生在**扫描线程**里，不影响界面与演出）。
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_song ON fingerprints(song_id)")
        # 指纹算法版本：参数/峰值检测变了 → 旧指纹不兼容，自动清库强制重扫
        stored = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if stored != FP_SCHEMA_VERSION:
            self.conn.execute("DELETE FROM fingerprints")
            self.conn.execute(f"PRAGMA user_version={FP_SCHEMA_VERSION}")
        self.conn.commit()

    def add_song(self, song_id, sig, sr, commit=True, hashes=None, want_sig=False):
        """把一首歌的指纹入库。返回**入库行数**；`want_sig=True` 时返回 `(行数, 签名)`。

        :param song_id:  指纹库主键（`int(md5(路径字符串)[:8], 16)`，见 `ui_main` 建库处）。
                         ⚠ 这是**路径**哈希，与文件内容无关，**不能**当 `file_md5` 用。
        :param sig:      单声道 float 波形（`sr` 采样率）。
        :param sr:       采样率。
        :param commit:   `False` ⇒ 攒批（配合 `commit_batch()`）。
        :param hashes:   **可选**，已算好的 `fingerprint_audio(sig, sr)` 结果
                         （`[(hash, offset), ...]`）。扫描流程要同时拿"入库行"和
                         "`fp_id` 签名"，若这里再算一次就等于对整首歌做**两遍**
                         全曲 STFT（65 首要几分钟）。传进来就复用，`None` 才算。
        :param want_sig: 是否顺带返回指纹签名 `fp2-<16hex>`（= 码表 `identity.fp_id`）。
                         ⚠ 返回值类型随它变化是**刻意**的：所有旧调用方不传 `want_sig`
                         就拿 `int`，行为与改动前**逐值一致**。
        """
        # 重扫时先删该曲旧指纹，防止库膨胀、匹配变慢（DELETE+INSERT 同一隐式事务）
        # ★ `idx_song` 索引让这条 DELETE 从"全表扫描"变成按索引定位（2026-09-26 实测 0.000s）。
        self.conn.execute("DELETE FROM fingerprints WHERE song_id=?", (song_id,))
        hs = hashes if hashes is not None else fingerprint_audio(sig, sr)
        rows = [(h, song_id, off) for h, off in hs]
        if rows:
            self.conn.executemany(
                "INSERT INTO fingerprints VALUES (?,?,?)", rows)
        if commit:
            self.conn.commit()
            self._pending = 0
        else:
            self._pending += 1          # 攒着，由调用方 commit_batch() 统一落盘
        if want_sig:
            return len(rows), signature_from_hashes(hs)
        return len(rows)                # ← 旧调用方拿到的仍是 int（行为不变）

    def pending(self):
        """批量模式下"已写未 commit"的歌曲数"""
        return self._pending

    def commit_batch(self):
        """把批量模式下挂起的事务落盘（配合 `add_song(commit=False)` 用）。

        ★ 为什么值得：`INSERT` 3~4.5 万行本身贵，但**每首一次 commit 还要多付一次 fsync**；
        在 HDD 上单次 fsync 要等盘片（10~50ms）。攒 5~10 首再落盘能把这部分摊薄。
        """
        if self._pending:
            self.conn.commit()
            self._pending = 0

    def begin_scan(self):
        """扫描开始的批量写入模式（**用完必须调 end_scan() 恢复**）。

        `synchronous=OFF`：扫描期间不做"每次提交都落盘"的强同步 —— 扫描是**可重建**的操作
        （出问题重扫即可），换来明显更快的写入。同时把 cache 调大、临时表放内存。
        """
        try:
            self.conn.commit()                       # 先把之前的事务收干净
            self._scan_orig_sync = self.conn.execute("PRAGMA synchronous").fetchone()[0]
            self.conn.execute("PRAGMA synchronous=OFF")
            self.conn.execute("PRAGMA cache_size=-40000")     # ~40MB page cache
            self.conn.execute("PRAGMA temp_store=MEMORY")
        except Exception:                                                # noqa: BLE001
            pass

    def end_scan(self):
        """结束批量写入模式：落盘 + 把同步级别恢复回原值。"""
        try:
            self.conn.commit()
            self._pending = 0
            if self._scan_orig_sync is not None:
                self.conn.execute("PRAGMA synchronous=%d" % int(self._scan_orig_sync))
                self._scan_orig_sync = None
        except Exception:                                                # noqa: BLE001
            pass

    @staticmethod
    def _pick(votes, hint_delta=None, hint_w=None, hint_sid=None):
        """从 (song_id, delta)->votes 的投票表里挑出结果，返回 ((sid, delta), votes) 或 None。

        · hint_delta 为 None：与旧实现**逐值一致** —— `max(items, key=votes)`（同分取先出现的）。
        · hint_delta 给定：先在 |delta - hint_delta| <= hint_w 的候选里取最高票；该窗口内
          **没有候选时**才退回全局众数（保证真实远距离 seek 仍有结果）。
          ⚠ 这里**只按"窗口内是否命中有候选"选择，不判断 hint 本身是否正确**：
          一旦窗口内存在候选（哪怕是"另一处出现"的同歌候选），就取它 —— 即 hint 能给错时
          会把它带到错位置。这是**有意的**：没有可靠的"hint 对不对"判据（QA 实测"投票比"
          区分不了"hint 正确但弱"与"hint 错误"，两者全局票都更高）。因此**调用方必须**
          只在坐标系可信时给 hint（见 engine._beat_pos_grid：未锁定不发布；重锚后有界暂停）。
        · hint_sid（2026-10-27 加）：hint 所属的 song_id。**换歌保护**——上一窗锁在 A、
          这一窗放的已经是 B 时，hint 指向的是 A 的时间轴，对 B 毫无意义；而密集音乐里
          "hint 窗口内无候选"这个前提根本不成立（任何位置都有票）⇒ hint 会把结果按在
          A（或 B 的某个错误位置）上，电荷条永远看不到"新歌证据"。
          给了 hint_sid 时，窗内胜者**必须是这首歌**才允许 hint 生效，否则退回全局众数。
          None ⇒ 保持旧行为（逐值一致）。
        hint_w 缺省用 `HINT_W`（帧）—— 引擎按该歌 beat_len 换算后传入，跨 BPM 一致。
        delta 单位 = hop 帧；窗内同分同样取先出现者（dict 插入序确定）。
        """
        if not votes:
            return None
        # ★★★ 2026-10-18（真机"一直慢"的最终定位）：**只有"前两名票数接近"时才准用 hint**。
        #   证据：关掉 hint 后，画面相对音频的最大偏离从 **71.5 秒降到 3.45 秒**；
        #   而有 hint 时 `off` 会连续 8~10 秒卡在同一个值（拍钟预测滞后 ⇒ 匹配被锁在旧位置）。
        #   但 hint 本身是有用的（重复段落要靠它挑对那处），所以保留它、只限制使用条件：
        #     · 全局第一名**明显领先**（第二名 < 50% 第一名）⇒ 事实清楚，不需要 hint；
        #     · 前两名接近（≥50%）⇒ 真有歧义，这时才用 hint 在两者间挑。
        #   这样既不会被滞后预测带跑，也不会丢掉"重复段落挑对位置"的能力。
        _rank = sorted(votes.values(), reverse=True)
        _clear = len(_rank) >= 2 and _rank[0] >= 2 * _rank[1]
        if hint_delta is not None and _clear:
            hint_delta = None
        if hint_delta is not None:
            w = HINT_W if hint_w is None else float(hint_w)
            if w < 0.0:
                w = 0.0
            lo = hint_delta - w
            hi = hint_delta + w
            best = None
            for key, cnt in votes.items():
                if lo <= key[1] <= hi and (best is None or cnt > best[1]):
                    best = (key, cnt)
            if best is not None:
                # ★★ 2026-10-17：**窗内候选还必须"够强"才准赢**。
                #   现场实证（101719）：连续性提示窗里落进一个 **1 票**的杂散候选，
                #   它把真实位置（182 票）挤掉 ⇒ `raw` 在 29/34/1 之间跳 ⇒
                #   电荷条的"每拍 +0.5s"判据永远不成立 ⇒ 永不确认。
                #   判据取 `max(3, 全局最高票 × 10%)`：
                #     · 同一段音频在歌里出现两次 ⇒ 两处票数同量级（实测 35 vs 58 = 60%）
                #       ⇒ 仍然会被选中，**原有"防重复段落挑错位置"的能力不受影响**；
                #     · 纯噪声候选只有 1~3 票 ⇒ 被拒 ⇒ 退回全局众数（等于没有 hint）。
                #   ⚠ 早先 QA 结论是"投票比区分不了 hint 正确但弱 / hint 错误"——
                #     那研究的是"两者都是一个量级"的情形；这里卡的是**数量级差异**，
                #     不是同一个问题。阈值取 10% 而不是 50%，正是为了不误伤前者。
                _top = max(votes.values())
                if best[1] >= max(6, int(_top * HINT_WIN_MIN_RATIO)):
                    # ★ 2026-10-27：换歌保护 —— hint 只在「窗内胜者就是 hint 所属那首歌」时生效。
                    #   否则（已经换歌了）退回全局众数，让新歌的正确位置能赢。
                    if hint_sid is None:
                        return best
                    try:
                        _same = int(best[0][0]) == int(hint_sid)
                    except (TypeError, ValueError):
                        _same = (best[0][0] == hint_sid)
                    if _same:
                        return best
        return max(votes.items(), key=lambda kv: kv[1])

    def set_allowed(self, ids):
        """限定可匹配的 song_id 集合（None = 不限制）。返回设置后的规模。"""
        try:
            self.allowed = None if ids is None else set(int(x) for x in ids)
        except Exception:                                        # noqa: BLE001
            self.allowed = None
        return 0 if self.allowed is None else len(self.allowed)

    def match(self, sig, sr, hint_delta=None, hint_w=None, hint_sid=None,
              rate=1.0, hs=None, lock_sid=None):
        """返回 (song_id, aligned_votes, offset_sec) 或 (-1, 0, 0.0)

        hint_delta: 可选。预测的「对齐帧偏移」（单位 = HOP 帧，与内部 delta 同轴）。
          None（默认）⇒ 与改动前**逐值一致**（全局众数）；给定值 ⇒ 优先选预测位置
          附近（±hint_w 帧）的最高票候选，避开强重复段落的歧义错匹配；**窗内无候选**
          时才退回全局众数。
        hint_w: 可选。hint 窗口半宽（帧）。None ⇒ 用缺省 `HINT_W`（保持旧调用方逐值一致）。
          引擎按该歌 grid 的 beat_len 用 `hint_window_frames()` 换算后传入，使窗口
          **按拍数**定义、跨 BPM 一致（固定帧窗在 90~200BPM 间对应的拍数差 2 倍以上）。
        hint_sid: 可选。hint 所属的 song_id（= 上一窗锁定的歌）。给了它 ⇒ hint 只在
          「窗内胜者就是这首歌」时生效，换歌后自动退回全局众数（见 `_pick` 说明）。
          调用点见 engine._beat_pos_grid → audio_engine._recognize_loop。
        rate: 可选。**播放倍率假设**（0.80~1.25）。DJ 机台变速**保留音高、只改速度**时，
          谱峰频率不变 ⇒ 哈希照样命中，但「查询帧 → 参考帧」的映射要乘 rate 才不会
          把票摊到一串相邻 delta 上。默认 1.0 ⇒ 与改动前逐值一致。
          实战里由引擎在「锁不上」时按档位重试（见 audio_engine.FP_RATE_CANDS）。
        hs: 可选。已算好的 `fingerprint_audio(sig, sr)` 结果，供同一段音频按多个 rate
          重试时复用（否则每次都重算谱峰，7 个档位就慢 7 倍）。
        lock_sid: 可选。当前已锁定的曲目 id。给了它 ⇒ **本地跟踪优先**：只要这首
          在当前窗仍有 ≥`TRACK_LOCK_MIN_VOTES` 票、且 ≥全球最高票的
          `TRACK_LOCK_RATIO`，就继续返回这首；否则退回全球众数。
          对应 KVJ 的“锁定后局部跟踪”，避免相似曲单窗抢走 sid。"""
        if hs is None:
            hs = fingerprint_audio(sig, sr)
        # ★ 2026-10-29：留一份引用给 `fit_rates()` 复用（变速对齐要同一份 hash 再查一次
        #   单曲的 (qoff, dboff)）。只是一个 list 引用，零拷贝、零计算。
        self.last_hs = hs
        if not hs:
            self.last_hit_rate = 0.0
            self.last_song_best = {}
            self.last_global_song = -1
            self.last_global_votes = 0
            return -1, 0, 0.0
        qoff_map = {}
        hashes = []
        for h, qoff in hs:
            qoff_map.setdefault(h, []).append(qoff)
            hashes.append(h)
        votes = {}
        # ★ 2026-10-19：同时统计「每个 song_id 的总票（跨所有对齐位置）」。
        #   为什么要它：重复/相似段落会把票摊到多个 delta，单对齐票常只有个位数，
        #   而整首歌的总票仍有几百 —— 只用单对齐票当"证据"会严重低估。
        song_tot = {}
        # ★ 2026-10-25：**命中哈希率**（有多少查询哈希在库里出现过，不看白名单）。
        #   为什么要它：DJ 混音视频里的歌常被 beatmatch 调速（保留音高、只改速度），
        #   谱峰频率不变 ⇒ 哈希照样命中，但**位置整体漂移** ⇒ 单对齐票被打散、
        #   过不了「单点 ≥80」的门槛。命中率高（实测 85~91%）说明"库里就是这段录音"，
        #   这比单点票更可靠地区分「同一首歌被变速」与「另一首相似的歌」。
        seen = set()
        # 批量 IN 查询（分块避开 SQLite 999 变量上限）
        for i in range(0, len(hashes), 500):
            chunk = hashes[i:i + 500]
            ph = ",".join("?" * len(chunk))
            for (h, song_id, dboff) in self.conn.execute(
                    f"SELECT hash, song_id, offset FROM fingerprints "
                    f"WHERE hash IN ({ph})", chunk):
                seen.add(h)
                # 只认当前曲库里的歌（过期指纹会被历史扫描留在库里）
                if self.allowed is not None and song_id not in self.allowed:
                    continue
                for qoff in qoff_map.get(h, ()):
                    # 变速假设：把查询帧换算到参考帧再算对齐差（rate=1 时不变）
                    _q = qoff if rate == 1.0 else int(round(qoff * rate))
                    key = (song_id, dboff - _q)
                    votes[key] = votes.get(key, 0) + 1
                    song_tot[song_id] = song_tot.get(song_id, 0) + 1
        self.last_song_votes = song_tot
        self.last_hit_rate = (len(seen) / float(len(qoff_map))) if qoff_map else 0.0
        # 每曲最佳单对齐票（用于"这首歌在某个位置明显领先"的判定）
        _best = {}
        for (s_, _d), v_ in votes.items():
            if v_ > _best.get(s_, 0):
                _best[s_] = v_
        self.last_song_best = _best
        # ★ 2026-11-12：全球最高票（诊断 + 本地跟踪的释放判据）
        if not votes:
            self.last_global_song, self.last_global_votes = -1, 0
            return -1, 0, 0.0
        _gbest = max(votes.items(), key=lambda kv: kv[1])
        try:
            self.last_global_song = int(_gbest[0][0])
            self.last_global_votes = int(_gbest[1])
        except (TypeError, ValueError, IndexError):
            self.last_global_song, self.last_global_votes = -1, 0
        picked = None
        if lock_sid is not None:
            try:
                _ls = int(lock_sid)
            except (TypeError, ValueError):
                _ls = None
            if _ls is not None:
                _lv = {k: v for k, v in votes.items() if int(k[0]) == _ls}
                if _lv:
                    _lb = max(_lv.values())
                    if (_lb >= TRACK_LOCK_MIN_VOTES
                            and _lb >= TRACK_LOCK_RATIO * max(1, int(_gbest[1]))):
                        picked = self._pick(_lv, hint_delta, hint_w, _ls)
        if picked is None:
            picked = self._pick(votes, hint_delta, hint_w, hint_sid)
        if picked is None:
            return -1, 0, 0.0
        (song_id, delta), cnt = picked
        return song_id, cnt, delta * HOP_SIZE / SAMPLE_RATE

    # ==================================================================
    # 变速对齐：对候选曲拟合「票数最多的直线」 d = rate·q + delta
    # ==================================================================
    @staticmethod
    def _line_votes(b):
        """一维投票：返回 (最多票数, 该桶代表值)。b 为浮点数组（已量化到帧前）。"""
        bi = np.rint(b).astype(np.int64)
        if bi.size == 0:
            return 0, 0
        u, c = np.unique(bi, return_counts=True)
        k = int(c.argmax())
        return int(c[k]), int(u[k])

    @staticmethod
    def _span_ok(qf, df, a, b0):
        """这条斜线的内点是否**真的铺开在整个窗口上**。

        见 `WARP_SPAN_MIN` 的说明：不加这条约束时，拟合会稳定收敛到搜索下界
        （a 越小 ⇒ delta 越接近 d 本身 ⇒ 重复段落的小方差被当成"高票"）。
        真斜线的内点必然横跨整个 q 区间；假集中只会堆在一端。
        """
        b = df - a * qf
        inl = np.abs(b - b0) <= WARP_TOL_FRAME
        k = int(inl.sum())
        if k < WARP_MIN_PTS:
            return False
        span = float(qf.max() - qf.min())
        if span <= 1e-9:
            return False
        qi = qf[inl]
        if float(qi.max() - qi.min()) < WARP_SPAN_MIN * span:
            return False
        edges = np.linspace(qf.min(), qf.max(), WARP_SEG_N + 1)
        seg_hit = 0
        for i in range(WARP_SEG_N):
            lo_, hi_ = edges[i], edges[i + 1]
            m = (qi >= lo_) & ((qi <= hi_) if i == WARP_SEG_N - 1 else (qi < hi_))
            if bool(m.any()):
                seg_hit += 1
        return seg_hit >= WARP_SEG_NEED

    def _fit_one(self, q, d):
        """对一首歌的点集做斜率搜索。返回 `(rate, votes, delta_frames)` 或 None。

        `q` = 查询窗内的帧坐标（0 起），`d` = 库里同一 hash 的帧坐标。
        遍历候选斜率 a，把点投到 `b = d - a·q` 上做直方图；最大桶的票数就是
        「这条斜率下能对上的票」。最后用内点做一次最小二乘精修，把 a 的量化
        误差从 ±0.01 压到 ±0.0005（否则窗口两端的 b 会差半帧）。

        ★ 每个候选斜率都要过 `_span_ok`（内点铺满窗口）—— 见 `WARP_SPAN_MIN`。
        """
        n = int(q.size)
        if n < WARP_MIN_PTS:
            return None
        if n > WARP_MAX_PTS:
            rs = np.random.RandomState(12345)      # 固定种子 ⇒ 同一输入结果可复现
            idx = rs.choice(n, WARP_MAX_PTS, replace=False)
            q, d = q[idx], d[idx]
        qf = q.astype(np.float64)
        df = d.astype(np.float64)
        # 斜率可辨识的前提：点集本身横跨足够长的时间（见 WARP_MIN_QSPAN）
        if float(qf.max() - qf.min()) < WARP_MIN_QSPAN:
            return None
        cand = []
        for a in np.arange(WARP_RATE_LO, WARP_RATE_HI + 1e-9, WARP_COARSE_STEP):
            v, b0 = self._line_votes(df - a * qf)
            if v >= WARP_MIN_PTS:
                cand.append((int(v), float(a), int(b0)))
        if not cand:
            return None
        cand.sort(key=lambda t: -t[0])
        # 票数最多的前几个里，挑第一个「内点铺满窗口」的；都不满足 ⇒ 认不出这条线
        pick = None
        for v, a, b0 in cand[:6]:
            if self._span_ok(qf, df, a, b0):
                pick = (v, a, b0)
                break
        if pick is None:
            return None
        best_v, best_a, best_b0 = pick
        lo = max(WARP_RATE_LO, best_a - WARP_FINE_HALF)
        hi = min(WARP_RATE_HI, best_a + WARP_FINE_HALF)
        for a in np.arange(lo, hi + 1e-9, WARP_FINE_STEP):
            v, b0 = self._line_votes(df - a * qf)
            if v > best_v and self._span_ok(qf, df, a, b0):
                best_v, best_a, best_b0 = int(v), float(a), int(b0)
        # 内点最小二乘精修：只拿「确实落在这条线上」的点重解 a、b
        b = df - best_a * qf
        b0 = float(best_b0)
        inl = np.abs(b - b0) <= WARP_TOL_FRAME
        if int(inl.sum()) >= WARP_MIN_PTS:
            qi, di = qf[inl], df[inl]
            _mq, _md = qi.mean(), di.mean()
            _den = float(((qi - _mq) ** 2).sum())
            if _den > 1e-9:
                a2 = float(((qi - _mq) * (di - _md)).sum() / _den)
                if WARP_RATE_LO <= a2 <= WARP_RATE_HI:
                    d2 = _md - a2 * _mq
                    v2, _ = self._line_votes(df - a2 * qf)
                    if v2 >= best_v and self._span_ok(qf, df, a2, _md - a2 * _mq):
                        return a2, int(v2), float(d2)
        return best_a, int(best_v), float(b0)

    def fit_rates(self, hs, sids):
        """对给定候选曲各自做变速拟合。返回 `{song_id: (rate, votes, delta_frames)}`。

        :param hs:   本窗指纹 `[(hash, qoff_frame), ...]`（= `match` 收的同一份）。
        :param sids: 要尝试的 song_id 序列（调用方给「总票 top-N」，通常 3 个）。
        :return: 只包含「点集足够 + 拟合成功」的曲目；调用方按 votes 排序取第一。

        ⚠ 这里**不做身份判定**，只回答"这首歌按某个倍率能对上多少票"。
          认不认由调用方的门槛（票数下限 + 对第二名的领先 + BPM 校验）决定。
        ⚠ 白名单 `self.allowed` 与 `match` 保持同一口径：过期指纹不参与。
        """
        if not hs or not sids:
            return {}
        want = set()
        for s in sids:
            try:
                want.add(int(s))
            except (TypeError, ValueError):
                pass
        if not want:
            return {}
        qoff_map = {}
        for h, qoff in hs:
            qoff_map.setdefault(h, []).append(qoff)
        if not qoff_map:
            return {}
        hashes = list(qoff_map.keys())
        acc = {}
        for i in range(0, len(hashes), 500):
            chunk = hashes[i:i + 500]
            ph = ",".join("?" * len(chunk))
            for (h, song_id, dboff) in self.conn.execute(
                    f"SELECT hash, song_id, offset FROM fingerprints "
                    f"WHERE hash IN ({ph})", chunk):
                if song_id not in want:
                    continue
                if self.allowed is not None and song_id not in self.allowed:
                    continue
                for qoff in qoff_map.get(h, ()):
                    e = acc.get(song_id)
                    if e is None:
                        e = acc[song_id] = ([], [])
                    e[0].append(int(qoff))
                    e[1].append(int(dboff))
        out = {}
        for song_id, (qs, ds) in acc.items():
            try:
                r = self._fit_one(np.asarray(qs, np.int64), np.asarray(ds, np.int64))
            except Exception:                                    # noqa: BLE001
                r = None
            if r is not None:
                out[int(song_id)] = r
        return out

    def close(self):
        self.conn.close()


def count_query_hits(conn, hashes, chunk=500):
    """一批指纹哈希 → `{song_id: 命中的**查询哈希**个数}`。

    ★★ 必须按"命中的**查询哈希**去重计数"，不能累加库里的行数：
      同一 hash 在库里可能出现多行（同一首歌被扫过两次、合成指纹与本机指纹并存），
      累加行数会算出 **>100%** 的假比例（2026-10-30 实测第一版就是 118.9%）。

    用途（用户 2026-10-30：「我期望的是用指纹对比」）：
      判断"码表里这首歌本机是不是也有"。`identity.fp_id` 是
      `sha1(全部哈希)[:16]` —— **一个哈希不同签名就变**，所以只能判"完全同一个 PCM"；
      而同一首歌的不同压制版本（只换容器/码率）签名完全不同，指纹内容却大面积重合。
      实测（测试151.ercue × 本机 91 首）：同一文件 100.0%、同曲不同混音 11.5%、
      无关曲目 8.7~9.1% ⇒ 用重合率判"同一首"区分度极大。
    """
    out = {}
    try:
        items = [str(h) for h in (hashes or ())]
    except Exception:                                            # noqa: BLE001
        return out
    for i in range(0, len(items), max(1, int(chunk))):
        part = items[i:i + max(1, int(chunk))]
        if not part:
            continue
        ph = ",".join("?" * len(part))
        try:
            rows = conn.execute(
                "SELECT hash, song_id FROM fingerprints WHERE hash IN (%s)" % ph,
                part).fetchall()
        except Exception:                                        # noqa: BLE001
            continue
        for r in rows:
            try:
                h, sid = str(r[0]), int(r[1])
            except (TypeError, ValueError, IndexError):
                continue
            out.setdefault(sid, set()).add(h)
    # 转成计数（set 去重是关键，见 docstring）
    return {sid: len(s) for sid, s in out.items()}


def purge_except(conn, keep_ids, chunk=200):
    """删掉**不属于** `keep_ids` 的指纹行；返回 `(删掉的歌曲数, 删掉的行数)`。

    ★★ 2026-10-30 现场事故的修法。用户两次强调：
       「清除分析数据 不要清码表和曲库分析包的东西」。

    为什么必须按 song_id 删、而不是删库文件：
      `fingerprints.db` 里同时装着**三家**的指纹 ——
        ① **本机曲库**（`song_id = int(md5(路径)[:8], 16)`）→ 清除分析数据该清的就是它；
        ② **码表 `.ercue` 的内嵌曲**（`fp_song_id(fp_id)` 合成 id，见 `cue_fp_pack`）；
        ③ **导入的曲库分析包**（`.eravjlib`，id 记在 `imported_meta.db`）。
      旧实现 `os.remove(fingerprints.db)` ⇒ ②③ 一起没了。实测代价：导入包 50 首
      （本机**没有音频文件**、只能靠指纹识别）全部失联；4 首码表曲同样；更糟的是
      `ui_main._init_recognizer()` 在库文件不存在时**直接 return** ⇒ 连识别器都不建
      （静默失效，一条日志都没有）。

    ★★ 为什么用"重建表"而不是逐行 DELETE（2026-10-30 02:06 第二次事故）：
      第一版写成 `DELETE FROM fingerprints WHERE song_id IN (...)`。功能对，但
      **每删一行要维护 `idx_hash` + `idx_song` 两个索引**。现场真库 146 万行实测
      **8153 行/秒 ⇒ 外推 198 秒**（冷盘那次直接 28 分钟），而且这段跑在 GUI
      主线程 ⇒ 用户看到的画面就是「点清除后软件卡死（未响应）」。
      改成本函数现在的做法：`CREATE 新表 → 只搬白名单的行 → DROP 旧表 → RENAME → 重建索引`
      ⇒ `DROP TABLE` 是 O(页数)、不逐行改索引，同一份真库实测 **0.16 秒（快 1238 倍）**。
      白名单通常很小（码表内嵌曲 + 导入包），要"搬"的行更少 ⇒ 天然更快。
      基准脚本：`tools/_bench_purge.py`（用真实库副本跑，不碰用户数据）。

    ⚠ 事务写法：python sqlite3 默认在每个 DDL 前**自动开事务**，直接 `BEGIN` 会报
      `cannot start a transaction within a transaction` ⇒ 这里先 `commit()` 清掉挂起
      事务、再切到 autocommit（`isolation_level=None`），手动 `BEGIN IMMEDIATE`。
    """
    keep = set()
    for x in (keep_ids or ()):
        try:
            keep.add(int(x))
        except (TypeError, ValueError):
            continue
    try:
        all_ids = [int(r[0]) for r in conn.execute(
            "SELECT DISTINCT song_id FROM fingerprints")]
    except Exception:                                            # noqa: BLE001
        return 0, 0
    victims = [x for x in all_ids if x not in keep]
    if not victims:
        return 0, 0
    try:
        total = int(conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0])
    except Exception:                                            # noqa: BLE001
        total = 0

    old_iso = getattr(conn, "isolation_level", "")
    kept_rows = 0
    try:
        try:
            conn.commit()                     # 清掉任何挂起事务（DDL 前必须）
        except Exception:                                        # noqa: BLE001
            pass
        conn.isolation_level = None           # autocommit：DDL 不再自动开事务
        conn.execute("DROP TABLE IF EXISTS keep_ids")
        conn.execute("CREATE TEMP TABLE keep_ids (song_id INTEGER PRIMARY KEY)")
        if keep:
            conn.executemany("INSERT OR IGNORE INTO keep_ids (song_id) VALUES (?)",
                             [(int(x),) for x in keep])
        conn.execute("BEGIN IMMEDIATE")
        try:
            # schema 与 `FingerprintDB.__init__` **逐字一致**（列顺序也一致：
            # `INSERT` 显式写列名是本项目铁律，但"搬行"这里是 SELECT 全列，顺序必须对）
            conn.execute("CREATE TABLE fingerprints__new ("
                         "hash TEXT NOT NULL, song_id INTEGER NOT NULL, "
                         "offset INTEGER NOT NULL)")
            conn.execute("INSERT INTO fingerprints__new (hash, song_id, offset) "
                         "SELECT hash, song_id, offset FROM fingerprints "
                         "WHERE song_id IN (SELECT song_id FROM keep_ids)")
            try:
                kept_rows = int(conn.execute(
                    "SELECT COUNT(*) FROM fingerprints__new").fetchone()[0])
            except Exception:                                    # noqa: BLE001
                kept_rows = 0
            conn.execute("DROP TABLE fingerprints")
            conn.execute("ALTER TABLE fingerprints__new RENAME TO fingerprints")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_hash ON fingerprints(hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_song ON fingerprints(song_id)")
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:                                    # noqa: BLE001
                pass
            raise
    except Exception:                                            # noqa: BLE001
        # 重建失败 ⇒ 返回 0（调用方会打日志）。**绝不退回逐行 DELETE**：
        # 那条路要几分钟，退回等于把"卡死"重新引进来。
        return 0, 0
    finally:
        try:
            conn.isolation_level = old_iso
        except Exception:                                        # noqa: BLE001
            pass
        try:
            conn.execute("DROP TABLE IF EXISTS keep_ids")
        except Exception:                                        # noqa: BLE001
            pass
        # DROP TABLE 会往 WAL 里写一批页，收尾截断，免得白占几十 MB
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:                                        # noqa: BLE001
            pass
    return len(victims), max(0, total - kept_rows)
