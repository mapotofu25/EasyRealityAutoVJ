# -*- coding: utf-8 -*-
"""★ ERCUE 自包含指纹核心（零 Qt，纯 numpy/stdlib）。

**为什么要它**：用户明确"ercue 是给 DJ 用的"，DJ 机器上**可能没装 AutoVJ** ⇒ ERCUE 必须
**自己**能算指纹，不能依赖 AutoVJ 的指纹库。

**算法来源**：逐字移植 AutoVJ `src/fp.py`（Dejavu 算法，参数与 VJVision `fp/fingerprint.h`
一致）。⚠ **哈希算法一字不改**（`fp.py` 顶部硬警告：改了就等于换指纹格式，AutoVJ 侧
1421 首用户库得整库重扫）。本文件下方 `_spectrogram/_find_peaks/_hashes/fingerprint_audio`
与 `fp.py` **逐字节等价**，所有常量原样保留以便两端指纹可比对。

**不做的事**（本任务范围外，见报告）：这里只提供"**把一个音频信号算成指纹签名**"的
**纯计算**能力；AutoVJ 那套 `FingerprintDB`（SQLite 倒排索引 + 现场查询匹配）本次**未移植**
—— 本任务的认歌是"**同一份文件/同一段音频在本机重算签名 → 按签名认歌**"，不需要倒排库。
若将来要"播着歌实时识别是哪首"，再按同一套算法在 ERCUE 自己的位置建库
（`%LOCALAPPDATA%\\EasyRealityCue\\cache\\fp\\fingerprints.db`，**绝不与 AutoVJ 共库**）。

**零 Qt 纪律**：本模块**不得** import PySide6 / shiboken（`tools/selftest.py` 会 AST 扫描
`model/` 下的 Qt 绑定，命中即失败）。
"""
from __future__ import annotations

import hashlib

import numpy as np

# ==========================================================================
# 指纹参数（★ 原样保留自 AutoVJ src/fp.py —— 不要改）
# ==========================================================================
SAMPLE_RATE = 44100
FFT_WINDOW = 4096
HOP_SIZE = 2048
FAN_VALUE = 8            # 每峰配后 fan-1=7 个峰
AMP_MIN = 12.0           # dB
HASH_LEN = 20            # SHA1 前 20 hex = 80 bit
MAX_HASH_TIME_DELTA = 200
DT_QUANT = 4             # 时间差量化（抗 keylock ±8% 变速）
FREQ_QUANT = 4           # 频率量化
PEAK_NEIGHBORHOOD = 20   # 41×41 邻域严格局部极大值（VJVision peaks.cpp 同款）
FP_SCHEMA_VERSION = 2    # 峰值检测算法版本（v1=规则分块；v2=邻域极大值，抗变速）

# 签名前缀：ERCUE 侧"指纹 id"的格式版本。`2` 对应 `FP_SCHEMA_VERSION=2`。
# ⚠ 前缀里的版本号一旦变化，等于换签名格式 ⇒ 旧码表的 fp_id 不再命中（会退到 md5/meta 级）。
SIG_PREFIX = "fp2-"
SIG_HEX = 16             # 签名取 sha1 前 16 hex = 64 bit（碰撞概率对"认歌"足够低）

# md5 分块读的块大小（1 MiB）：大文件（如 165s FLAC ≈ 40MB）也不占内存、不卡线程。
MD5_CHUNK = 1 << 20


def _spectrogram(sig):
    """mono float32(44.1k) → log-power 谱 (dB)，shape [freq_bins, frames]。**与 fp.py 逐字一致。**"""
    if len(sig) < FFT_WINDOW:
        return np.zeros((FFT_WINDOW // 2 + 1, 0), dtype=np.float32)
    win = np.hanning(FFT_WINDOW).astype(np.float32)
    nframes = 1 + (len(sig) - FFT_WINDOW) // HOP_SIZE
    idx = np.arange(nframes)[:, None] * HOP_SIZE + np.arange(FFT_WINDOW)[None, :]
    frames = sig[idx] * win                      # [nframes, FFT]
    spec = np.abs(np.fft.rfft(frames, FFT_WINDOW, axis=1)).T   # [bins, frames]
    return 10.0 * np.log10(spec ** 2 + 1e-10)


def _find_peaks(spec):
    """2D 峰值：41×41 邻域严格局部极大值（VJVision peaks.cpp 同款，抗变速）。

    **与 fp.py 逐字一致**（先时间轴、后频率轴的滑动最大值 → 候选 → 排除平顶并列）。
    """
    from numpy.lib.stride_tricks import sliding_window_view
    F, T = spec.shape
    N = PEAK_NEIGHBORHOOD
    W = 2 * N + 1
    if F <= 2 * N or T <= 2 * N:
        return []

    # 1) 时间轴滑动最大（每频率行）
    pad_t = np.full((F, N), -np.inf, dtype=np.float32)
    a = np.concatenate([pad_t, spec, pad_t], axis=1)          # (F, T+2N)
    tMax = sliding_window_view(a, W, axis=1).max(axis=2)      # (F, T)

    # 2) 频率轴滑动最大（每时间列）
    pad_f = np.full((N, T), -np.inf, dtype=np.float32)
    b = np.concatenate([pad_f, tMax, pad_f], axis=0)          # (F+2N, T)
    sqMax = sliding_window_view(b, W, axis=0).max(axis=2)     # (F, T)

    # 3) 候选：内部点，值 == 邻域最大 且 > 阈值
    inner = spec[N:F - N, N:T - N]
    cand = np.where((inner == sqMax[N:F - N, N:T - N]) & (inner > AMP_MIN))
    peaks = []
    ap = peaks.append
    for r, c in zip(*cand):
        f, t = int(r) + N, int(c) + N
        val = spec[f, t]
        # 邻域里 >= val 的个数减去中心自己（它恒等于 val）> 0 ⇒ 存在并列 ⇒ 不是严格极大
        nb = spec[f - N:f + N + 1, t - N:t + N + 1]
        if int(np.count_nonzero(nb >= val)) > 1:
            continue
        ap((f, t))
    peaks.sort(key=lambda p: (p[1], p[0]))
    return peaks


def _hashes(peaks):
    """峰对 → `[(hash, offset)]`。⚠⚠ **与 fp.py 逐字一致，不要改哈希算法。**"""
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
    """`sig`：mono float32 → `[(hash, offset_frame), ...]`（offset 单位 = hop 帧）。

    **与 AutoVJ `fp.py:fingerprint_audio` 逐字节等价**（含非 44.1k 的线性重采样）。
    """
    if sr != SAMPLE_RATE:
        x = np.linspace(0, len(sig) - 1, max(1, int(len(sig) * SAMPLE_RATE / sr)))
        sig = np.interp(x, np.arange(len(sig)), sig).astype(np.float32)
    spec = _spectrogram(sig)
    return _hashes(_find_peaks(spec))


def signature_from_hashes(hashes) -> str:
    """指纹哈希序列 → **稳定的签名串**（`fp2-<16 hex>`）。

    ★ 为什么用"哈希序列的 sha1"而不是"取前 N 个哈希"：
      · 前 N 个只覆盖歌曲开头（前奏/静音差异大，且不同歌开头可能相同）；
      · 全序列 sha1 覆盖整首 ⇒ 同**一段音频**（同文件重算）必然得到**同一签名**，稳定可复现。
    ★ 代价：**只有"同一段音频"才得同一签名**（重新编码/剪辑会变）—— 这正是它与
      `file_md5`（同文件）、`meta`（同标签）、`title+artist`（同名）互补的地方：
      md5 抗改名/换目录，签名抗改名/换目录**且**抗换容器（只要解码出的 PCM 一致）。
    """
    if not hashes:
        return ""
    h = hashlib.sha1()
    for hh, off in hashes:
        h.update(hh.encode("ascii", "replace"))
        h.update(b":")
        h.update(b"%d|" % int(off))
    return SIG_PREFIX + h.hexdigest()[:SIG_HEX]


def fingerprint_signature(sig, sr) -> str:
    """一步到位：音频信号 → 指纹签名（`fp2-<16 hex>`）；无峰值返回 ""。"""
    try:
        return signature_from_hashes(fingerprint_audio(sig, sr))
    except Exception:                                  # noqa: BLE001
        return ""


def fingerprint_hashes(sig, sr) -> list:
    """音频信号 → **完整哈希序列** `[(hash_hex, offset_frame), ...]`（失败返回 `[]`）。

    ★ 为什么需要它（用户原话「理论来讲 era 导入就应该要识别到」）：
      码表里只写 `fp2-<16 hex>` 这个**摘要**，演出端拿着 16 个字符**认不出音频** ——
      它必须手里有那首歌、自己重算一遍哈希序列才能比对。所以导出时要把**整条序列**
      打进包（`fp/<fp_id>.bin`，格式见 `model/fp_pack.py`），演出端灌进自己的倒排库
      就能在**没有音频文件**的情况下认出这首歌。
    ★ 与 `fingerprint_audio()` **完全同一套算法**（本函数只是加了一层"绝不抛"）——
      两端哈希算法一旦分叉，写进包里的指纹在演出端一条都匹配不上。
    """
    try:
        return fingerprint_audio(sig, sr)
    except Exception:                                  # noqa: BLE001
        return []


def hashes_and_signature(sig, sr) -> tuple:
    """**一次** STFT → `(hashes, signature)`；两者都来自同一份 `fingerprint_audio` 结果。

    ★ 为什么要合并：`signature_from_hashes(fingerprint_audio(...))` 与"再取一次 hashes"
      若各调一次 `fingerprint_audio`，就是**两遍全曲 STFT**（实测 2.67 s/首 × 2）。
      身份扫描本来就要整曲解码算指纹 ⇒ 这里只算一遍，签名与指纹包（`fp_pack`）共用。
    """
    hashes = fingerprint_hashes(sig, sr)
    return hashes, signature_from_hashes(hashes)


def file_md5(path, chunk: int = MD5_CHUNK) -> str:
    """文件内容的 **md5**（`hexdigest`）；**分块读**（大文件不占内存、不阻塞调用方）。

    读不到 / 环境禁用 md5（FIPS）⇒ 返回 `""`（调用方按"没有 md5"降级，绝不抛）。
    """
    try:
        try:
            h = hashlib.md5(usedforsecurity=False)      # Py3.9+ 允许非安全用途
        except TypeError:
            h = hashlib.md5()
    except Exception:                                  # noqa: BLE001
        return ""
    try:
        with open(path, "rb") as f:
            while True:
                b = f.read(chunk)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()
    except Exception:                                  # noqa: BLE001
        return ""


def selftest() -> bool:
    """零 Qt 自检：md5 分块读正确、签名确定性、与 AutoVJ 算法常量一致。"""
    import os
    import tempfile
    # 1) md5 与一次性读一致（证明"分块"没算错）
    fd, p = tempfile.mkstemp(prefix="ercue_md5_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(os.urandom(3 * MD5_CHUNK + 12345))
        one = hashlib.md5(open(p, "rb").read()).hexdigest()
        assert file_md5(p) == one, "分块 md5 与一次性 md5 不一致"
        assert file_md5(p, chunk=1024) == one, "小块 md5 与一次性 md5 不一致"
    finally:
        try:
            os.remove(p)
        except OSError:
            pass
    assert file_md5("__no_such_file__") == "", "坏路径应返回空 md5（绝不抛）"

    # 2) 指纹签名：同信号 ⇒ 同签名；不同信号 ⇒ 不同签名（确定性）
    rng = np.random.default_rng(20261015)
    sig = (rng.standard_normal(SAMPLE_RATE * 3) * 0.2).astype(np.float32)
    sig[::7919] += 1.5                                   # 造一些峰
    a = fingerprint_signature(sig, SAMPLE_RATE)
    b = fingerprint_signature(sig.copy(), SAMPLE_RATE)
    assert a.startswith(SIG_PREFIX) and a == b, ("签名不确定", a, b)
    sig2 = sig.copy()
    sig2[len(sig2) // 2] += 5.0                          # 改一个样本
    c = fingerprint_signature(sig2, SAMPLE_RATE)
    assert c and c != a, "改一个样本后签名应变化（否则认歌会撞）"
    assert fingerprint_signature(np.zeros(10, dtype=np.float32), SAMPLE_RATE) == "", \
        "太短的信号应返回空签名"

    # 2b) `hashes_and_signature` 与「先 fingerprint_audio 再签名」**逐条一致**
    #     （★ 一次 STFT 同时供签名与指纹包用；绝不能变成两套结果）
    hs, sg = hashes_and_signature(sig, SAMPLE_RATE)
    assert hs == fingerprint_audio(sig, SAMPLE_RATE), "hashes_and_signature 的哈希序列不一致"
    assert sg == a and len(hs) > 0, ("hashes_and_signature 的签名不一致", sg, a)
    assert fingerprint_hashes(np.zeros(10, dtype=np.float32), SAMPLE_RATE) == [], \
        "太短的信号应返回空哈希序列（绝不抛）"

    # 3) 算法常量（与 AutoVJ 一致，防"手滑改参数"）
    assert (SAMPLE_RATE, FFT_WINDOW, HOP_SIZE, FAN_VALUE, AMP_MIN, HASH_LEN,
            DT_QUANT, FREQ_QUANT, PEAK_NEIGHBORHOOD) == \
        (44100, 4096, 2048, 8, 12.0, 20, 4, 4, 20), "指纹参数被改动（必须与 AutoVJ 一致）"
    print("[fp_core] 自检通过 ✓  md5 分块读一致；签名确定性；算法常量与 AutoVJ 一致")
    return True


if __name__ == "__main__":
    selftest()
