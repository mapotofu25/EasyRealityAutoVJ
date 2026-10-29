# -*- coding: utf-8 -*-
"""频带特征库（BandFeat）——给「白化频带相关」校验用的侧车索引。

背景（2026-10-27）：指纹是**票数**口径，对「变速不变调」造成的谱峰时刻抖动
（相位声码器几十 ms）零容忍，票会被摊到相邻 delta 桶里 ⇒ 变速样本认不出。
KDJ/KVJ 的做法是「先提候选，再用**白化频带相关**判真假」，其中白化（逐频带 z 归一化）
是决定性的——不做白化时响的低频段会支配相关系数，D1 的峰值会跑到错倍率上（实测）。

存储：SQLite 一张表 `bandfeat(song_id, hop_ms, frames, vmin, vmax, data BLOB)`，
      data 是 **uint8** 量化（v = vmin + q/255*(vmax-vmin)），16 字节/帧 ≈ 96 KB/分钟。
      本机 65 首 × 约 5 分钟 ≈ 31 MB，可接受。
运行时：只读打开 + 进程内 LRU（识别线程里只碰这个字典，不做重活）。

⚠ 建库是**离线**的（tools/build_bandfeat.py）；识别线程绝不现场解码库文件
  （库在 P:/W: 网络盘，一次几百毫秒会拖死识别循环）。
"""
import os
import sqlite3
import threading

import numpy as np

SR = 8000              # 与 KDJ 一致：8 kHz 足够，且快
HOP = 80               # 10 ms
NFFT = 256
BANDS = 16
_EDGES = np.clip(np.unique(np.round(np.geomspace(60.0, 3800.0, BANDS + 1)).astype(int)),
                 1, NFFT // 2 - 1)
_WIN = np.hanning(NFFT).astype(np.float32)

DB_NAME = "bandfeat.db"


def db_path(local_dir):
    return os.path.join(local_dir, DB_NAME)


def to_mono8k(sig, sr):
    """任意采样率单声道 → 8 kHz（线性插值，够用且无依赖）。"""
    x = np.asarray(sig, dtype=np.float32)
    if x.ndim > 1:
        x = x.mean(axis=1)
    if int(sr) == SR:
        return x
    n = int(len(x) * SR / float(sr))
    if n <= 1:
        return x
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def band_matrix(x8k):
    """(帧数, BANDS) 的 **log10 能量**矩阵。x8k 必须是 8 kHz。"""
    n = 1 + max(0, (len(x8k) - NFFT) // HOP)
    if n <= 0:
        return np.zeros((0, BANDS), dtype=np.float32)
    idx = np.arange(NFFT)[None, :] + HOP * np.arange(n)[:, None]
    spec = np.abs(np.fft.rfft(x8k[idx] * _WIN, axis=1)) ** 2
    out = np.zeros((n, BANDS), dtype=np.float32)
    for b in range(BANDS):
        lo, hi = _EDGES[b], max(_EDGES[b] + 1, _EDGES[b + 1])
        out[:, b] = np.log10(spec[:, lo:hi].mean(axis=1) + 1e-10)
    return out


# ---------------------------------------------------------------- chroma（12 音级）
# ★★★ 2026-10-29：照 KDJ `crates/kdj-analysis/src/alignment/fuzzy.rs:39` 的 chroma。
#   为什么必须加这一列：现有 16 个**对数频带**每带约 0.375 个八度（≈4.5 个半音），
#   折不出 12 个半音级；而 chroma 是**音级能量聚合**，对相位不敏感 ——
#   这正是 landmark 票数在变速下崩掉时仍然稳的那种特征。
#   实测（tools/_test_chroma.py，同一首 20 秒，atempo 保音高变速）：
#     倍速   相似度   最佳 slope 命中
#     1.00   1.000   1.000
#     1.04   0.777   1.040
#     1.08   0.687   1.080
#     1.15   0.754   1.150
#     1.20   0.745   1.200
#   而**异曲最高只有 0.105**（两首同专辑的都不超过 0.11）⇒ 分离度 7 倍以上。
#   KDJ 的阈值 0.32/0.30/0.40 在两边都有充足余量。
CHROMA_NFFT = 2048         # 2048 @ 8kHz = 4 Hz 分辨率（60Hz 处一个半音才 3.6Hz）
CHROMA_HOP = 800           # 100 ms
CHROMA_MIDI = (48, 108)    # C3 ~ C8
_HANN_C = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(CHROMA_NFFT)
                             / (CHROMA_NFFT - 1))).astype(np.float32)


def _note_bins():
    """每个 MIDI 音（48~108）对应的 rfft bin 下标数组。"""
    bins = np.arange(1, CHROMA_NFFT // 2 + 1)
    freq = bins * SR / float(CHROMA_NFFT)
    midi = 69.0 + 12.0 * np.log2(freq / 440.0)
    out = []
    for note in range(CHROMA_MIDI[0], CHROMA_MIDI[1] + 1):
        out.append(np.nonzero(np.abs(midi - note) < 0.5)[0])
    return out


_NOTE_BINS = _note_bins()


def chroma_matrix(x8k, demean=True):
    """→ (帧数, 12)。逐帧 L2 归一化 →（可选）减均值 → 再归一化。

    减均值那一步是 KDJ 原注释点明的关键：`Remove each recording's persistent tonal
    colour, so sharing a key alone contributes little.` —— 不去掉的话，
    两首**同一个调**的歌会有很高的基础相似度。

    ★★★ 2026-10-29 落库自检抓到的坑：**建库时必须 `demean=False`**。
      KDJ 之所以能"减全曲均值"，是因为它两端都是完整录音（整个视频 vs 整首歌）。
      而我们是「12 秒现场查询 vs 整首歌参考」—— 库里存的是整曲均值，
      查询减的是自己 12 秒的均值，**两边基准不同**，相似度会直接崩到 0.006
      （实测）。⇒ 库里存**未减均值**的版本，打分时让参考的那一小段自己减均值，
        两端才是同一把尺子。

    ⚠ 返回值逐帧已 L2 归一化，所以两帧的余弦相似度就是点积，不用再除范数。
    """
    n = 1 + max(0, (len(x8k) - CHROMA_NFFT) // CHROMA_HOP)
    if n <= 0:
        return np.zeros((0, 12), dtype=np.float32)
    out = np.zeros((n, 12), dtype=np.float32)
    # 分块，避免长时间录音一次性构造 (帧数 × 2048) 的大矩阵
    blk = 512
    for a in range(0, n, blk):
        b = min(n, a + blk)
        idx = (np.arange(CHROMA_NFFT)[None, :]
               + CHROMA_HOP * np.arange(a, b)[:, None])
        spec = np.abs(np.fft.rfft(x8k[idx] * _HANN_C, axis=1)) ** 2
        for note, sel in enumerate(_NOTE_BINS):
            if sel.size:
                out[a:b, note % 12] += np.sqrt(spec[:, sel].sum(axis=1))
    nrm = np.sqrt((out ** 2).sum(axis=1, keepdims=True))
    out = np.divide(out, np.maximum(nrm, 1e-8), out=np.zeros_like(out), where=nrm > 1e-8)
    if demean:
        out = out - out.mean(axis=0, keepdims=True)
        nrm = np.sqrt((out ** 2).sum(axis=1, keepdims=True))
        out = np.divide(out, np.maximum(nrm, 1e-8), out=np.zeros_like(out),
                        where=nrm > 1e-8)
    return out


CHROMA_Q = 255.0           # 固定量化：未减均值版 v ∈ [0,1] ⇔ q ∈ [0,255]


# ---------------------------------------------------------------- 建库（离线）
def build_db(local_dir, items, progress=None):
    """items = [(song_id, path)]；返回写入/跳过的统计。"""
    path = db_path(local_dir)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS bandfeat ("
                "song_id INTEGER PRIMARY KEY, sr INTEGER, hop INTEGER, frames INTEGER,"
                "vmin REAL, vmax REAL, dur REAL, data BLOB)")
    # ★ 2026-10-29：chroma 单独一张表 —— 老库没有它也能照常用 16 频带通道，
    #   不破坏向后兼容（`build_db` 会自动把缺的那一半补上）。
    con.execute("CREATE TABLE IF NOT EXISTS chromafeat ("
                "song_id INTEGER PRIMARY KEY, frames INTEGER, dur REAL, data BLOB)")
    con.commit()
    wrote, skipped, failed = 0, 0, []
    for sid, p in items:
        try:
            if not os.path.exists(p):
                failed.append((sid, "找不到文件"))
                continue
            r1 = con.execute("SELECT frames FROM bandfeat WHERE song_id=?", (int(sid),)).fetchone()
            r2 = con.execute("SELECT frames FROM chromafeat WHERE song_id=?", (int(sid),)).fetchone()
            need_band = not (r1 and r1[0])
            need_chroma = not (r2 and r2[0])
            if not need_band and not need_chroma:
                skipped += 1
                continue
            import soundfile as sf
            d, s0 = sf.read(p, dtype="float32", always_2d=True)
            x = to_mono8k(d, s0)
            if need_band:
                m = band_matrix(x)
                if m.shape[0] < 40:
                    failed.append((sid, "太短"))
                    continue
                vmin, vmax = float(m.min()), float(m.max())
                rng = max(1e-6, vmax - vmin)
                q = np.clip((m - vmin) / rng * 255.0, 0, 255).astype(np.uint8)
                con.execute("INSERT OR REPLACE INTO bandfeat "
                            "(song_id,sr,hop,frames,vmin,vmax,dur,data) VALUES (?,?,?,?,?,?,?,?)",
                            (int(sid), SR, HOP, int(m.shape[0]), vmin, vmax,
                             float(len(x)) / SR, sqlite3.Binary(q.tobytes())))
            if need_chroma:
                cm = chroma_matrix(x, demean=False)
                if cm.shape[0] < 40:
                    failed.append((sid, "chroma 太短"))
                    continue
                cq = np.clip(cm * CHROMA_Q, 0, 255).astype(np.uint8)
                con.execute("INSERT OR REPLACE INTO chromafeat (song_id,frames,dur,data) "
                            "VALUES (?,?,?,?)",
                            (int(sid), int(cm.shape[0]), float(len(x)) / SR,
                             sqlite3.Binary(cq.tobytes())))
            con.commit()
            wrote += 1
            if progress:
                progress(sid, int((r1 and r1[0]) or 0) or int(x.shape[0]))
        except Exception as e:                                   # noqa: BLE001
            failed.append((sid, str(e)[:80]))
    con.close()
    return {"wrote": wrote, "skipped": skipped, "failed": failed, "db": path}


# ---------------------------------------------------------------- 运行期读取
class BandStore:
    """只读 + LRU。识别线程里只做字典命中/一次本地 SQLite 读（~100 KB）。"""

    def __init__(self, local_dir, cache=3):
        self.path = db_path(local_dir)
        self._cache = {}
        self._order = []
        self._ccache = {}          # chroma 单独一套 LRU（见 get_chroma）
        self._corder = []
        self._cap = max(1, int(cache))
        self._lock = threading.Lock()

    def ready(self):
        return os.path.exists(self.path)

    def get(self, sid):
        """返回 (矩阵 float32, vmin, vmax, 帧数) 或 None。"""
        sid = int(sid)
        with self._lock:
            if sid in self._cache:
                self._order.remove(sid)
                self._order.append(sid)
                return self._cache[sid]
        if not self.ready():
            return None
        try:
            con = sqlite3.connect("file:%s?mode=ro" % self.path.replace("\\", "/"), uri=True)
            row = con.execute("SELECT frames,vmin,vmax,data FROM bandfeat WHERE song_id=?",
                              (sid,)).fetchone()
            con.close()
        except sqlite3.Error:
            return None
        if not row:
            return None
        frames, vmin, vmax, blob = row
        q = np.frombuffer(blob, dtype=np.uint8)
        if q.size != frames * BANDS:
            return None
        m = vmin + (q.reshape(frames, BANDS).astype(np.float32) / 255.0) * (vmax - vmin)
        val = (m, float(vmin), float(vmax), int(frames))
        with self._lock:
            self._cache[sid] = val
            self._order.append(sid)
            while len(self._order) > self._cap:
                self._cache.pop(self._order.pop(0), None)
        return val

    def get_chroma(self, sid):
        """返回 (chroma 矩阵 float32 [帧,12], 帧数) 或 None。

        与 `get()` 分开缓存（两套特征量纲不同，混在一个 LRU 里会让缓存命中互相挤掉）。
        """
        sid = int(sid)
        with self._lock:
            if sid in self._ccache:
                self._corder.remove(sid)
                self._corder.append(sid)
                return self._ccache[sid]
        if not self.ready():
            return None
        try:
            con = sqlite3.connect("file:%s?mode=ro" % self.path.replace("\\", "/"), uri=True)
            row = con.execute("SELECT frames,data FROM chromafeat WHERE song_id=?",
                              (sid,)).fetchone()
            con.close()
        except sqlite3.Error:
            return None
        if not row:
            return None
        frames, blob = row
        q = np.frombuffer(blob, dtype=np.uint8)
        if q.size != frames * 12:
            return None
        # 存储版是"未减均值、逐帧 L2 归一化"的 chroma（v ∈ [0,1]）
        cm = q.reshape(frames, 12).astype(np.float32) / CHROMA_Q
        val = (cm, int(frames))
        with self._lock:
            self._ccache[sid] = val
            self._corder.append(sid)
            while len(self._corder) > self._cap:
                self._ccache.pop(self._corder.pop(0), None)
        return val


# ---------------------------------------------------------------- 打分
def _whiten(m):
    """逐频带 z 归一化 —— 决定性的那一步（不做白化会被响的低频段支配）。"""
    m = m - m.mean(axis=0, keepdims=True)
    sd = m.std(axis=0, keepdims=True)
    return m / np.maximum(sd, 1e-6)


def _pearson(a, b):
    a = _whiten(a).ravel()
    b = _whiten(b).ravel()
    d = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b / d) if d > 1e-9 else 0.0


def score_at(q, ref, off_sec, rate):
    """参考侧按 ref_t = off + q*rate 规整后比对。返回 (平均分, 最差分)。"""
    n = int(q.shape[0])
    if n < 8 or ref is None or ref.shape[0] < 8:
        return -1.0, -1.0
    idx = np.round(off_sec * (SR / float(HOP)) + np.arange(n) * float(rate)).astype(int)
    if idx[0] < 0 or idx[-1] >= ref.shape[0]:
        return -1.0, -1.0
    warped = ref[idx]
    guard, width = 2, max(4, (n - 4) // 3)
    sc = [_pearson(q[guard + w * width: guard + (w + 1) * width],
                   warped[guard + w * width: guard + (w + 1) * width]) for w in range(3)]
    return float(np.mean(sc)), float(np.min(sc))


def verify(q, ref, off_sec, rates, span=0.30, step=0.10):
    """在 off±span 内、按 rate 档位找最佳分数。返回 (score, weakest, rate, off)。"""
    best = (-9.0, -9.0, 1.0, off_sec)
    offs = [off_sec] if span <= 0 else np.arange(off_sec - span, off_sec + span + 1e-6, step)
    for r in rates:
        for o in offs:
            o = max(0.0, float(o))
            s, w = score_at(q, ref, o, r)
            if s > best[0]:
                best = (s, w, float(r), o)
    return best


# ---------------------------------------------------------------- chroma 打分
# ★ 照 KDJ `fuzzy.rs` 的做法：逐帧 12 维点积的平均（因为 chroma 逐帧已 L2 归一化，
#   点积就是余弦相似度）。`slope` = 查询每前进 1 帧、参考前进几帧。
#   与 `score_at()` 的 16 频带 Pearson 是**两套独立证据**：那一套看频谱纹理，
#   这一套看音级分布；变速时两者都只轻微下降，而 landmark 票数会断崖。
CHROMA_SLOPE_LO = 0.80
CHROMA_SLOPE_HI = 1.25
CHROMA_SLOPE_STEP = 0.005      # KDJ 的 91 档
CHROMA_PHRASE = 120            # 12 秒 @ 100ms
CHROMA_MIN_FRAMES = 30


def chroma_slopes():
    """0.80~1.25 步长 0.005 的斜率表（91 档），跨调用复用。"""
    n = int(round((CHROMA_SLOPE_HI - CHROMA_SLOPE_LO) / CHROMA_SLOPE_STEP)) + 1
    return [round(CHROMA_SLOPE_LO + i * CHROMA_SLOPE_STEP, 3) for i in range(n)]


def chroma_score_at(q, ref, target_frame, slope, phrase=None):
    """查询 chroma 的前 `phrase` 帧 与 参考按 slope 对齐后的平均点积。越界返回 -1。"""
    if q is None or ref is None:
        return -1.0
    ph = CHROMA_PHRASE if phrase is None else int(phrase)
    n = min(ph, q.shape[0])
    if n < CHROMA_MIN_FRAMES or ref.shape[0] < 8:
        return -1.0
    idx = np.rint(np.arange(n) * float(slope)).astype(np.int64) + int(target_frame)
    if idx[0] < 0 or idx[-1] >= ref.shape[0]:
        return -1.0
    # ★★★ 2026-10-29 实测修正：**两端必须用"等长窗口各自减均值"**。
    #   查询端 `chroma_matrix()` 默认减的是"整段（可能 15 秒）"的均值，
    #   而参考侧只减 120 帧窗口的均值 —— 窗口不等长 ⇒ 基准又错位。
    #   实测同曲只拿到 0.44（完全同源时是 0.68）。
    #   ⇒ 这里对**两边都重新按 n 帧**减均值 + 归一化，做到严格对称。
    qw = q[:n]
    qw = qw - qw.mean(axis=0, keepdims=True)
    nrm = np.sqrt((qw ** 2).sum(axis=1, keepdims=True))
    qw = np.divide(qw, np.maximum(nrm, 1e-8), out=np.zeros_like(qw), where=nrm > 1e-8)
    rw = ref[idx]
    rw = rw - rw.mean(axis=0, keepdims=True)
    nrm = np.sqrt((rw ** 2).sum(axis=1, keepdims=True))
    rw = np.divide(rw, np.maximum(nrm, 1e-8), out=np.zeros_like(rw), where=nrm > 1e-8)
    return float((qw * rw).sum(axis=1).mean())


def chroma_best(q, ref, slopes=None, target_step=4):
    """全扫 → (最佳分数, 最佳 slope, 最佳起点帧)。`target_step` 是起点扫描步长（帧）。"""
    if q is None or ref is None or q.shape[0] < CHROMA_MIN_FRAMES:
        return -1.0, 1.0, 0
    sl = chroma_slopes() if slopes is None else list(slopes)
    best = (-2.0, 1.0, 0)
    for slope in sl:
        span = int(round(CHROMA_PHRASE * slope))
        last = ref.shape[0] - span
        if last <= 0:
            continue
        for t in range(0, last, max(1, int(target_step))):
            v = chroma_score_at(q, ref, t, slope)
            if v > best[0]:
                best = (v, float(slope), t)
    return best


# ---------------------------------------------------------------- 子短语一致性
# ★★★ 2026-10-29：这是 `unambiguous_spans` 的等价物，**不是可选项**。
#   实测（tools/_test_chroma_falsepos.py，94 首全库）：
#     库外曲最高 0.383 / 正向对照（它自己）只有 0.450 —— 单阈值只差 17%，分不开。
#   根因：全库自由扫描时，重复段落/巧合能凑出很高的单点分。KDJ 的解法是把
#   "分数"降级成"提候选"，最终判定交给**多个候选映射必须落在同一条线上**：
#     第 i 个子短语 ↔ 参考的第 (off + i*stride*slope) 帧
#   重复短语只会让**某一个**子短语产生多个候选，不会让所有子短语都指向同一条错线。
CHROMA_SEG_LEN = 60        # 子短语长度（帧）= 6 秒
CHROMA_SEG_STRIDE = 30     # 子短语间隔（帧）= 3 秒 ⇒ 相邻重叠 50%
CHROMA_SUB_TOPK = 3        # 每个子短语保留几个候选
CHROMA_CONSENSUS_TOL = 3   # 同一条线的容差（帧）= 0.3 秒
CHROMA_CONSENSUS_MIN = 2   # 至少几个子短语落在同一条线上才算数
# ★★★ 2026-10-29 实测（同一首 Hardstyle 自比自，4 个子短语）：
#   a=0   0.258 @ slope 1.205（落在重复段落上，错的）
#   a=30  0.963 @ slope 0.995  off=450.1  ← 对
#   a=60  0.312 @ slope 0.955（错的）
#   a=90  0.935 @ slope 0.995  off=450.4  ← 对
#   ⇒ 硬性要求 3 个一致**永远凑不齐**（这首歌有一半段落是循环的）。
#   但**对的那些段分数高达 0.94~0.96，错段只有 0.26~0.31，库外曲整体最高 0.38** ——
#   所以正确的判据是「≥2 段一致 **且** 这些段的均分 ≥ 0.55」，而不是只数段数。
#   这也正是 KDJ 逐段判定的价值：整体查询会把好坏段平均掉（0.45），拆开才看得见。
CHROMA_CONSENSUS_SCORE = 0.55   # 一致组的平均分门槛（异曲 0.10~0.22，正向 0.94+）


def chroma_topk(qsub, ref, k=3, slopes=None, target_step=4, min_score=0.02):
    """单个子短语对参考全扫，返回 top-k `(score, slope, target)`（按分数降序）。"""
    if qsub is None or ref is None or qsub.shape[0] < 8:
        return []
    sl = chroma_slopes() if slopes is None else list(slopes)
    n = qsub.shape[0]
    out = []
    for slope in sl:
        span = int(round(n * slope))
        last = ref.shape[0] - span
        if last <= 0:
            continue
        for t in range(0, last, max(1, int(target_step))):
            v = chroma_score_at(qsub, ref, t, slope, phrase=n)
            if v >= min_score:
                out.append((v, float(slope), int(t)))
    out.sort(key=lambda x: -x[0])
    # 简单去重：位置太近的只留最高的那个，避免 top-k 全挤在同一处
    kept = []
    for v, sl_, t in out:
        if any(abs(t - t2) < 8 and abs(sl_ - sl2) < 0.02 for _v2, sl2, t2 in kept):
            continue
        kept.append((v, sl_, t))
        if len(kept) >= k:
            break
    return kept


def chroma_consensus(q, ref, seg_len=None, stride=None, k=None, tol=None):
    """子短语一致性判定。返回 `(slope, ref_offset_frame, support, mean_score)` 或 None。

    `ref_offset_frame` = 查询第 0 帧对应的参考帧。
    只有「≥ CHROMA_CONSENSUS_MIN 个子短语落在同一条 (slope, offset) 线上」才返回。
    """
    if q is None or ref is None:
        return None
    seg_len = CHROMA_SEG_LEN if seg_len is None else int(seg_len)
    stride = CHROMA_SEG_STRIDE if stride is None else int(stride)
    k = CHROMA_SUB_TOPK if k is None else int(k)
    tol = CHROMA_CONSENSUS_TOL if tol is None else int(tol)
    n = q.shape[0]
    starts = list(range(0, max(1, n - seg_len + 1), max(1, stride)))
    if len(starts) < CHROMA_CONSENSUS_MIN:
        return None
    cands = []
    for a in starts:
        for v, slope, t in chroma_topk(q[a:a + seg_len], ref, k=k):
            # 线上的一点：off = 参考帧 - 查询帧*slope（以查询第 0 帧为基准）
            cands.append((float(t) - a * slope, v, slope, a))
    if not cands:
        return None
    # ★ 不做「按桶聚类」—— 实测会因边界把同一条线拆开：
    #   off 桶宽 3 帧时，448/3→149、450/3→150、452/3→151，同一条真线落三个桶，
    #   正向查询因此全部返回 None（4 个子短语一个都凑不齐）。
    #   ⇒ 改成**以每个候选为中心做容差聚类**（O(N²)，N = 子短语数×k，很小）。
    best = None
    for off0, _v0, sl0, _a0 in cands:
        group = [c for c in cands
                 if abs(c[2] - sl0) <= 0.02 and abs(c[0] - off0) <= float(tol)]
        per = {}
        for off, v, slope, a in group:
            # 同一个子短语只留贡献最高的那个，避免自己给自己刷票
            if a not in per or v > per[a][1]:
                per[a] = (off, v, slope)
        subs = len(per)
        if subs < CHROMA_CONSENSUS_MIN:
            continue
        off_m = float(np.mean([q[0] for q in per.values()]))
        sl_m = float(np.mean([q[2] for q in per.values()]))
        sc_m = float(np.mean([q[1] for q in per.values()]))
        if sc_m < CHROMA_CONSENSUS_SCORE:
            continue
        if best is None or (subs, sc_m) > (best[2], best[3]):
            best = (sl_m, off_m, subs, sc_m)
    return best


# ---------------------------------------------------------------- 现场接口
# ★★★ 2026-10-29 定案（实测数据见下）：**用现有 4.5 秒窗就够，不需要长窗**。
#   同一首 Hardstyle（真值 target=450 帧 = 45 秒），窗口长度 vs 分数：
#     窗长    同曲1.00  同曲1.08  同曲1.20   异曲
#     4.5s     0.978     0.837     0.797    0.183    ← 最好
#     6.0s     0.978     0.861     0.815    0.184
#     9.0s     0.972     0.780     0.768    0.151
#     12.0s    0.966     0.683     0.750    0.141
#   分离度 4.4~5.3 倍。
#
#   ⚠⚠ **target 必须细扫到 1 帧**：chroma 是 100ms 一帧，target 步长 4（0.4 秒）
#      就足以让同曲分数从 0.978 掉到 0.347（Hardstyle 150BPM 半拍的内容差异）。
#      之前所有"chroma 不行"的结论都是这个原因，不是特征的问题。
#   ⇒ 两阶段：粗扫（步长 16 + slope 0.02）框范围 → 精扫（步长 1 + slope 0.005）。
CHROMA_VERIFY_SCORE = 0.55      # 现场放行门槛（同曲 0.80~0.98、异曲 0.14~0.18）
CHROMA_COARSE_STEP = 16         # 粗扫 target 步长（帧）
CHROMA_COARSE_SLOPE = 0.02      # 粗扫 slope 步长
CHROMA_FINE_TARGET = 20         # 精扫 target 半宽（帧）
CHROMA_FINE_SLOPE = 0.005       # 精扫 slope 步长
CHROMA_FINE_SLOPE_HALF = 0.02   # 精扫 slope 半宽
# 变速必须"明显更好"才采信（防止原速时凑出 1.08，实测会发生）
# ★★ 2026-10-29 真机标定：0.10 太严。真机上（WASAPI 回环 + 播放器 keylock）chroma 的
#   区分度比离线低一大截 —— 实测 0.80× 播放时最佳 slope 0.72、slope=1.0 处 0.65，
#   只差 0.07 ⇒ 被 0.10 挡住 ⇒ 一律报原速 ⇒ 前馈倍率停在 1.000。
#   离线复查（3 首 × 原速/0.80/1.20）：0.06 时**原速仍全部报 1.000**，不会误跳。
CHROMA_SLOPE_MARGIN = 0.06


def chroma_verify(q, ref, coarse_step=None):
    """对**单首**参考做两阶段扫描。返回 `(score, slope, target_frame)`。

    这是现场唯一该调的接口：指纹先给出候选曲目（top-3），再对每首调它一次。
    ⚠ 不要拿它去扫全库 —— 全库的粗筛交给指纹票数，chroma 只负责"确认 + 定位 + 估倍率"。
    """
    if q is None or ref is None:
        return -1.0, 1.0, 0
    n = q.shape[0]
    if n < CHROMA_MIN_FRAMES or ref.shape[0] < n + 8:
        return -1.0, 1.0, 0
    step = CHROMA_COARSE_STEP if coarse_step is None else max(1, int(coarse_step))
    # ---- 粗扫：框出候选 (slope, target) ----
    coarse = []
    sl = CHROMA_SLOPE_LO
    while sl <= CHROMA_SLOPE_HI + 1e-9:
        span = int(round(n * sl))
        last = ref.shape[0] - span
        if last > 0:
            for t in range(0, last, step):
                v = chroma_score_at(q, ref, t, sl, phrase=n)
                if v > 0.02:
                    coarse.append((v, sl, t))
        sl = round(sl + CHROMA_COARSE_SLOPE, 4)
    if not coarse:
        return -1.0, 1.0, 0
    coarse.sort(key=lambda x: -x[0])
    # ---- 精扫：对前几个候选细化到 1 帧 ----
    best = (-2.0, 1.0, 0)
    seen = []
    for _v, csl, ct in coarse[:4]:
        if any(abs(ct - t0) <= CHROMA_FINE_TARGET and abs(csl - s0) <= CHROMA_FINE_SLOPE_HALF
               for t0, s0 in seen):
            continue
        seen.append((ct, csl))
        sl0 = max(CHROMA_SLOPE_LO, csl - CHROMA_FINE_SLOPE_HALF)
        sl1 = min(CHROMA_SLOPE_HI, csl + CHROMA_FINE_SLOPE_HALF)
        a = sl0
        while a <= sl1 + 1e-9:
            span = int(round(n * a))
            lo = max(0, ct - CHROMA_FINE_TARGET)
            hi = min(ref.shape[0] - span, ct + CHROMA_FINE_TARGET)
            for t in range(lo, max(lo + 1, hi + 1)):
                v = chroma_score_at(q, ref, t, a, phrase=n)
                if v > best[0]:
                    best = (v, float(a), int(t))
            a = round(a + CHROMA_FINE_SLOPE, 4)
    return best


def chroma_verify_near(q, ref, target_hint, rate_hint=1.0,
                       target_span=30, rate_span=None):
    """**现场用的版本**：位置限定在已知附近，倍率**全量程扫**（不靠猜）。

    为什么倍率不能靠 `rate_hint` 限范围（2026-10-29 实测）：
      `rate_hint` 来自上一帧，而变速工况下 `_rate_est` 常年停在 1.000
      ⇒ 若按 ±0.08 限定，范围只有 0.92~1.08，**1.20 永远扫不到**，
      实测 1.20× 的三首歌全部退化成 0.28/0.12/0.51。
      ⇒ 倍率改用「全量程粗扫（0.80~1.25 步长 0.02）+ 对 top-4 精扫（0.005）」，
        位置仍由 `target_hint` 限定（这一步才是省时间的关键，也防止跳到重复段落）。

    :param target_hint: 查询第 0 帧对应的参考帧（引擎给，单位 100ms 帧）
    :param rate_hint:   仅用于「与 1.0 的边际比较」时的参考，不限制搜索范围
    :return: `(score, slope, target_frame)`；全越界时返回 (-1, rate_hint, -1)
    """
    if q is None or ref is None:
        return -1.0, float(rate_hint or 1.0), -1
    n = q.shape[0]
    if n < CHROMA_MIN_FRAMES or ref.shape[0] < n + 8:
        return -1.0, float(rate_hint or 1.0), -1

    def _best_at(slope, step=1):
        span = int(round(n * slope))
        lo = max(0, int(target_hint) - int(target_span))
        hi = min(ref.shape[0] - span, int(target_hint) + int(target_span))
        bb = (-2.0, float(slope), -1)
        for t in range(lo, max(lo + 1, hi + 1), step):
            v = chroma_score_at(q, ref, t, slope, phrase=n)
            if v > bb[0]:
                bb = (v, float(slope), int(t))
        return bb

    # ---- 1. 粗扫：slope 限定在「上一帧倍率 ±rate_span」，位置步长 10 帧 ----
    # ★★ 2026-10-29 真机（`cr=1.230` 在原速播放时出现，直接造成一次位置窜动 4.27 拍/秒）：
    #   全量程扫会让 chroma 一步跳到 1.23 —— 因为 hint 位置本身有偏差时，
    #   「换个倍率 + 换个位置」总能凑出更高的分。而引擎的前馈会被它带跑。
    #   ⇒ 倍率只允许**逐步爬升**：搜索范围 = 上一帧 ±0.12。
    #     原速时范围 0.88~1.12（够不到 1.23）⇒ 不会被带跑；
    #     真变速时两窗内就能爬到 1.20（1.00 → 1.12 → 1.20）。
    _rs = 0.12 if rate_span is None else float(rate_span)
    _rh = float(rate_hint or 1.0)
    if not (CHROMA_SLOPE_LO <= _rh <= CHROMA_SLOPE_HI):
        _rh = 1.0
    coarse = []
    sl = max(CHROMA_SLOPE_LO, round(_rh - _rs, 4))
    sl_hi = min(CHROMA_SLOPE_HI, round(_rh + _rs, 4))
    while sl <= sl_hi + 1e-9:
        b = _best_at(sl, step=10)
        if b[2] >= 0 and b[0] > 0.02:
            coarse.append(b)
        sl = round(sl + CHROMA_COARSE_SLOPE, 4)
    if not coarse:
        return -1.0, float(rate_hint or 1.0), -1
    coarse.sort(key=lambda x: -x[0])
    # ---- 2. 精扫：对 top-4 细化 slope(0.005) 与位置(1 帧) ----
    best = (-2.0, 1.0, -1)
    seen = []
    for _v, csl, ct in coarse[:4]:
        if any(abs(ct - t0) <= 12 and abs(csl - s0) <= 0.03 for t0, s0 in seen):
            continue
        seen.append((ct, csl))
        a = max(CHROMA_SLOPE_LO, csl - 0.02)
        a_hi = min(CHROMA_SLOPE_HI, csl + 0.02)
        while a <= a_hi + 1e-9:
            span = int(round(n * a))
            lo = max(0, ct - 10)
            hi = min(ref.shape[0] - span, ct + 10)
            for t in range(lo, max(lo + 1, hi + 1)):
                v = chroma_score_at(q, ref, t, a, phrase=n)
                if v > best[0]:
                    best = (v, round(float(a), 4), int(t))
            a = round(a + CHROMA_FINE_SLOPE, 4)
    # ---- 3. 边际要求：变速必须明显优于 slope=1.0，否则报 1.0（不猜） ----
    if best[2] >= 0 and abs(best[1] - 1.0) > 1e-9:
        base = _best_at(1.0, step=1)
        if best[0] < base[0] + CHROMA_SLOPE_MARGIN:
            return base
    return best


# ---------------------------------------------------------------- 冷启动全曲扫描
# ★★★ 2026-10-29：变速时票会被摊到几十个位置，`fp_lock_gate` 的「领先 2 倍」过不去
#   （真机 0.80× 实测 ptot=166 vs 第二名 141，要求 332）⇒ 锁不上 ⇒ chroma 复核
#   根本没机会运行（它只在已锁定后工作）。要救冷启动只能让 chroma **自己找人**。
#
#   做法照 KDJ `fuzzy.rs` 的检索形态：先把查询与参考的**逐帧点积**一次性算成矩阵
#   `P = q · ref.T`（每帧都是 L2 归一化的 12 维，所以 P 就是余弦相似度），
#   再按 `target + round(i*slope)` 取对角线求平均 —— 这样 46 档斜率、全部 target
#   位置**一次算完**，不用 Python 循环（原来那样 6000+ 次调用要 300~400 ms）。
#
#   ⚠ 这里**不减窗口均值**（与 `chroma_score_at` 不同）。这是 KDJ 的原版形态，
#     它的 0.32/0.40 阈值就是在这一版上调出来的；减均值是为了抑制"同调"，
#     但那是滑窗操作、没法向量化，代价是几百倍耗时。
# 冷启动放行门槛。标定依据（tools/_test_chroma_cold.py，97 首全库）：
#   同曲各倍速                       0.41 ~ 0.95（最低是 Black Warrior 1.20x 的 0.411）
#   同一查询扫**库里别的歌**的最高分   0.20 ~ 0.30
#   ⇒ 取 0.34：比库外最高高 0.04、比同曲最低低 0.07，两边都有余量。
CHROMA_COLD_GATE = 0.34
CHROMA_COLD_TOPN = 3          # 只对指纹**总票**最高的 N 首做冷启动扫描
CHROMA_COLD_COOLDOWN = 2.0    # 最短间隔（秒）


def _unit_frames(m):
    """逐帧 L2 归一化。"""
    nrm = np.sqrt((m ** 2).sum(axis=1, keepdims=True))
    return np.divide(m, np.maximum(nrm, 1e-8), out=np.zeros_like(m), where=nrm > 1e-8)


def _scan_prep(q, ref):
    """冷启动用的两端预处理：**各减自己的整段均值**再逐帧归一化。

    ★★★ 2026-10-29 标定出来的（这是整条冷启动路的支点）：
      同曲 1.00 / 1.20 / 0.80 三种预处理方式下的分数：
        不减均值      0.258 / 0.232 / 0.166   ← 共同基线把区分度压平了
        减整段均值    0.938 / 0.792 / 0.737   ← 采用
        减滑窗均值    0.967 / 0.814 / —       ← 最准，但要构造 (T,n,12) 数组，慢几十倍
      「减整段均值」几乎追平精确版，而且能完全向量化（一次矩阵乘 + 对角线取值）。
      对查询来说「整段」就是那 4.5 秒窗，对参考来说是整首歌 —— 两边各自消除自己的
      调性基线，剩下的动态部分才是可比的。
    """
    qh = _unit_frames(q - q.mean(axis=0, keepdims=True))
    rh = _unit_frames(ref - ref.mean(axis=0, keepdims=True))
    return qh, rh


def chroma_scan_full(q, ref, slopes=None, step_t=1, target_range=None, topk=1):
    """全曲自由扫描（向量化）。返回 `[(score, slope, target_frame), ...]`（降序）。

    做法：先算逐帧点积矩阵 `P = q · ref.T`（两端都是单位向量 ⇒ P 就是余弦），
    再按 `target + round(i*slope)` 取**对角线**求平均 —— 46 档斜率 × 全部 target
    一次算完，不用 Python 循环。实测单曲 10 档只要 **5 ms**（原来 6000+ 次
    Python 调用要 300~400 ms）。

    :param step_t:       target 扫描步长（帧）。粗扫用 2，精扫用 1。
    :param target_range: `(lo, hi)` 限定 target 范围（精扫时用）
    """
    if q is None or ref is None:
        return []
    n = int(q.shape[0])
    if n < CHROMA_MIN_FRAMES or ref.shape[0] < n + 8:
        return []
    qh, rh = _scan_prep(q, ref)
    P = qh @ rh.T                                            # (n, T_ref)
    t_ref = rh.shape[0]
    i_idx = np.arange(n)[:, None]
    sl = chroma_slopes() if slopes is None else list(slopes)
    out = []
    for slope in sl:
        step = np.rint(np.arange(n) * float(slope)).astype(np.int64)
        span = int(step[-1]) + 1
        T = t_ref - span + 1
        if T <= 0:
            continue
        lo, hi = 0, T - 1
        if target_range is not None:
            lo = max(0, int(target_range[0]) // max(1, int(step_t)))
            hi = min(T - 1, int(target_range[1]) // max(1, int(step_t)))
            if hi < lo:
                continue
        st = max(1, int(step_t))
        idx_t = np.arange(lo, hi + 1) * st
        idx_t = idx_t[idx_t < T]
        if idx_t.size == 0:
            continue
        j_idx = idx_t[None, :] + step[:, None]                 # (n, K)
        sc = P[i_idx, j_idx].mean(axis=0)                      # (K,)
        k = int(np.argmax(sc))
        out.append((float(sc[k]), float(slope), int(idx_t[k])))
    out.sort(key=lambda x: -x[0])
    return out[:max(1, int(topk))]


def chroma_scan_refine(q, ref, slope0, target0, slope_half=0.02, target_span=6):
    """在 `(slope0, target0)` 附近精扫到 1 帧 / 0.005 档。返回 `(score, slope, target)`。"""
    sl = []
    a = max(CHROMA_SLOPE_LO, float(slope0) - float(slope_half))
    a_hi = min(CHROMA_SLOPE_HI, float(slope0) + float(slope_half))
    while a <= a_hi + 1e-9:
        sl.append(round(a, 4))
        a = round(a + CHROMA_FINE_SLOPE, 4)
    lo = max(0, int(target0) - int(target_span))
    hi = int(target0) + int(target_span)
    top = chroma_scan_full(q, ref, slopes=sl, step_t=1, target_range=(lo, hi), topk=1)
    if not top:
        return -1.0, float(slope0), int(target0)
    return top[0]


def chroma_coldstart(q, refs, slopes=None, step_t=2, refine=True):
    """冷启动：在一组候选参考里全库扫描，返回 `(sid, score, slope, target)` 或 None。

    :param refs: `[(sid, chroma_matrix), ...]` —— 必须由调用方按**指纹总票**排序，
                 总票第一的排最前（分数完全相同时用它决胜：库里有同源版本时
                 两首的 chroma 分数会一模一样，此时应该听指纹的）。
    """
    best = None
    for sid, cm in refs:
        top = chroma_scan_full(q, cm, slopes=slopes, step_t=step_t, topk=1)
        if not top:
            continue
        sc, sl, tg = top[0]
        if best is None or sc > best[1] + 1e-9:
            best = (int(sid), float(sc), float(sl), int(tg))
    if best is None or not refine:
        return best
    cm = next((c for s_, c in refs if int(s_) == int(best[0])), None)
    if cm is None:
        return best
    sc2, sl2, tg2 = chroma_scan_refine(q, cm, best[2], best[3])
    if sc2 >= 0:
        return (best[0], float(sc2), float(sl2), int(tg2))
    return best
