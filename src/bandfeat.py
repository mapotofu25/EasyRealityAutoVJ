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


# ---------------------------------------------------------------- 建库（离线）
def build_db(local_dir, items, progress=None):
    """items = [(song_id, path)]；返回写入/跳过的统计。"""
    path = db_path(local_dir)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS bandfeat ("
                "song_id INTEGER PRIMARY KEY, sr INTEGER, hop INTEGER, frames INTEGER,"
                "vmin REAL, vmax REAL, dur REAL, data BLOB)")
    con.commit()
    wrote, skipped, failed = 0, 0, []
    for sid, p in items:
        try:
            if not os.path.exists(p):
                failed.append((sid, "找不到文件"))
                continue
            row = con.execute("SELECT frames FROM bandfeat WHERE song_id=?", (int(sid),)).fetchone()
            if row and row[0]:
                skipped += 1
                continue
            import soundfile as sf
            d, s0 = sf.read(p, dtype="float32", always_2d=True)
            x = to_mono8k(d, s0)
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
            con.commit()
            wrote += 1
            if progress:
                progress(sid, m.shape[0])
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
