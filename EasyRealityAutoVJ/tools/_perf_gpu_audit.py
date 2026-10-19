# -*- coding: utf-8 -*-
"""只读性能取样：演出端 CPU 热点量化（GPU 迁移审计用）。

不放回源码，只 import 现有模块做计时；不改任何文件、不写库
（指纹库以 `mode=ro` 只读打开）。
"""
import os
import sqlite3
import statistics
import sys
import time

import numpy as np
import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.chdir(ROOT)

RESULT = []


def bench(name, fn, n=20, warm=3, freq_per_s=None, note=""):
    try:
        for _ in range(warm):
            fn()
        ts = []
        for _ in range(n):
            t0 = time.perf_counter()
            fn()
            ts.append((time.perf_counter() - t0) * 1000.0)
        med = statistics.median(ts)
        p90 = sorted(ts)[max(0, int(len(ts) * 0.9) - 1)]
        core = (med * freq_per_s / 1000.0) if freq_per_s else None
        line = "%-42s %8.3f ms  p90 %8.3f ms" % (name, med, p90)
        if freq_per_s:
            line += "  %6.1f/s -> %5.2f 核" % (freq_per_s, core)
        if note:
            line += "  " + note
        print(line, flush=True)
        RESULT.append((name, med, p90, freq_per_s, core, note))
    except Exception as e:  # noqa: BLE001
        print("%-42s FAIL %s: %s" % (name, type(e).__name__, e), flush=True)


print("=" * 100)
print("机器：%s 核，python %s，cv2 %s，numpy %s"
      % (os.cpu_count(), sys.version.split()[0], cv2.__version__, np.__version__))
print("=" * 100)

# ---------------------------------------------------------------- 音频实时分析
print("\n[1] 音频实时分析（Analyzer.feed，48kHz / 1024 采样块 ≈ 46.9 块/秒）")
import audio_engine as AE  # noqa: E402

an = AE._Analyzer(48000, block=1024)
st = AE.AudioState()
rng = np.random.RandomState(0)

# 造一段有节拍的信号（kick + 噪声），比纯噪声更接近真实
def _sig(sec=6.0, sr=48000):
    n = int(sec * sr)
    t = np.arange(n) / sr
    x = 0.25 * np.sin(2 * np.pi * 55 * t) * (np.mod(t, 0.5) < 0.12)
    x += 0.05 * rng.randn(n)
    x = np.clip(x, -1, 1).astype(np.float32)
    return np.stack([x, x], axis=1)


SIG = _sig()
blocks = [SIG[i:i + 1024] for i in range(0, len(SIG) - 1024, 1024)]
bi = {"i": 0}


def feed_one():
    b = blocks[bi["i"] % len(blocks)]
    bi["i"] += 1
    an.feed(b, st)


bench("Analyzer.feed(1024 样本)", feed_one, n=300, warm=50, freq_per_s=48000 / 1024.0)

# ---------------------------------------------------------------- 指纹
print("\n[2] 指纹（认歌线程每 0.5s 一窗：fingerprint_audio + SQLite 倒排查询）")
import fp as FP  # noqa: E402

seg = SIG[:int(4.5 * 48000), 0].copy()
seg44 = cv2.resize(seg.reshape(-1, 1), (1, int(4.5 * 44100)),
                   interpolation=cv2.INTER_LINEAR).ravel().astype(np.float32)
bench("fingerprint_audio(4.5s@44.1k)", lambda: FP.fingerprint_audio(seg44, 44100),
      n=10, warm=1, freq_per_s=2.0)

_spec = FP._spectrogram(seg44)
bench("  _spectrogram(4.5s) STFT 4096/2048", lambda: FP._spectrogram(seg44), n=10, warm=1)
peaks = FP._find_peaks(_spec)
print("  峰值数 = %d，STFT 帧数 = %d，频点 = %d" % (len(peaks), _spec.shape[1], _spec.shape[0]))
bench("  _find_peaks(41x41 滑动最大)", lambda: FP._find_peaks(_spec), n=10, warm=1)
bench("  _hashes(峰对+sha1)", lambda: FP._hashes(peaks), n=10, warm=1)

DB = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "fingerprints.db")
if os.path.exists(DB):
    hs = FP.fingerprint_audio(seg44, 44100)
    qmap = {}
    for h, off in hs:
        qmap.setdefault(h, []).append(off)
    hashes = [h for h, _ in hs]
    print("  查询哈希数 = %d（去重 %d），库 %.0f MB"
          % (len(hashes), len(qmap), os.path.getsize(DB) / 1e6))

    def db_query():
        conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
        try:
            votes = {}
            for i in range(0, len(hashes), 500):
                chunk = hashes[i:i + 500]
                ph = ",".join("?" * len(chunk))
                for (h, sid, dboff) in conn.execute(
                        "SELECT hash, song_id, offset FROM fingerprints "
                        "WHERE hash IN (%s)" % ph, chunk):
                    for qoff in qmap.get(h, ()):
                        key = (sid, dboff - qoff)
                        votes[key] = votes.get(key, 0) + 1
            return votes
        finally:
            conn.close()

    # 复用同一连接（生产侧 FingerprintDB 就是长连接）
    conn_ro = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)

    def db_query_keep():
        votes = {}
        for i in range(0, len(hashes), 500):
            chunk = hashes[i:i + 500]
            ph = ",".join("?" * len(chunk))
            for (h, sid, dboff) in conn_ro.execute(
                    "SELECT hash, song_id, offset FROM fingerprints "
                    "WHERE hash IN (%s)" % ph, chunk):
                for qoff in qmap.get(h, ()):
                    key = (sid, dboff - qoff)
                    votes[key] = votes.get(key, 0) + 1
        return votes

    bench("  SQLite IN 查询+投票(长连接)", db_query_keep, n=10, warm=1, freq_per_s=2.0)
    bench("  SQLite 每次重开连接", db_query, n=5, warm=0, freq_per_s=2.0)
    v = db_query_keep()
    print("  候选票表条目 = %d，最高票 = %d"
          % (len(v), max(v.values()) if v else 0))
    conn_ro.close()

# ---------------------------------------------------------------- 后处理
print("\n[3] 画面处理（1080p，每帧）")
H, W = 1080, 1920
frame = (rng.rand(H, W, 3) * 255).astype(np.uint8)
bgra = np.dstack([frame, np.full((H, W), 255, np.uint8)])
import postprocess as PP  # noqa: E402

bench("cv2.LUT 1080p (颜色渲染核心)", lambda: cv2.LUT(frame, np.arange(256, dtype=np.uint8)),
      n=30, freq_per_s=60.0)
bench("_np_view_bgr + write_back_bgr", lambda: PP.write_back_bgr.__call__ if False else None, n=1) if False else None
bench("fx_chromatic(0.6)", lambda: PP.fx_chromatic(frame, 0.6), n=20, freq_per_s=60.0)
bench("fx_bloom(0.6)", lambda: PP.fx_bloom(frame, 0.6), n=20, freq_per_s=60.0)
bench("fx_softfocus(0.6)", lambda: PP.fx_softfocus(frame, 0.6), n=20, freq_per_s=60.0)
bench("fx_exposure(0.6)", lambda: PP.fx_exposure(frame, 0.6), n=20, freq_per_s=60.0)
bench("fx_glitch(0.6)", lambda: PP.fx_glitch(frame, 0.6, 12345), n=20, freq_per_s=60.0)
bench("fx_grain(0.6)", lambda: PP.fx_grain(frame, 0.6, 12345), n=20, freq_per_s=60.0)
bench("fx_saturation(0.6)", lambda: PP.fx_saturation(frame, 0.6), n=20, freq_per_s=60.0)
bench("fx_pixelate(0.6)", lambda: PP.fx_pixelate(frame, 0.6), n=20, freq_per_s=60.0)
spec24 = list(np.linspace(0.1, 1.0, 24))
bench("fx_deform(0.6)", lambda: PP.fx_deform(frame, 0.6, spec24, 99), n=20, freq_per_s=60.0)
bench("fx_spectrum(0.6)", lambda: PP.fx_spectrum(frame, 0.6, spec24), n=20, freq_per_s=60.0)
bench("fx_trails(0.6)", lambda: PP.fx_trails(frame, 0.6), n=20, freq_per_s=60.0)
bench("cv2.subtract(整幅 4ch) 减少混合", lambda: cv2.subtract(bgra, bgra, dst=None), n=30, freq_per_s=60.0)

# colorfx apply（需要 QImage）
print("\n[4] QImage / QPainter 合成（1080p）")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtGui import QImage, QPainter, QColor, QGuiApplication  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402

app = QGuiApplication.instance() or QGuiApplication([])

canvas = QImage(W, H, QImage.Format_RGB32)
canvas.fill(Qt.black)
img = QImage(bgra.data, W, H, 4 * W, QImage.Format_ARGB32).copy()
img2 = QImage(bgra.data, W, H, 4 * W, QImage.Format_ARGB32).copy()


def colorapply():
    from colorfx import ColorRenderer
    cr = colorapply.cr
    cr.s = 0.6
    cr.color = (168.0, 85.0, 247.0)
    cr.apply(canvas, "lut")


colorapply.cr = __import__("colorfx").ColorRenderer()
colorapply.cr._lut_key = None
bench("colorfx.apply(cv2.LUT + 视图往返)", colorapply, n=20, freq_per_s=60.0)


def compose1():
    p = QPainter(canvas)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    p.drawImage(__import__("PySide6.QtCore", fromlist=["QRectF"]).QRectF(0, 0, W, H), img)
    p.end()


from PySide6.QtCore import QRectF  # noqa: E402


def compose_n(n):
    p = QPainter(canvas)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    for i in range(n):
        p.setOpacity(0.9 - 0.05 * i)
        p.drawImage(QRectF(0, 0, W, H), img if i % 2 else img2)
    p.end()


bench("QPainter 合成 1 图层(铺满)", lambda: compose_n(1), n=20, freq_per_s=60.0)
bench("QPainter 合成 3 图层(铺满)", lambda: compose_n(3), n=20, freq_per_s=60.0)
bench("QPainter 合成 6 图层(铺满)", lambda: compose_n(6), n=20, freq_per_s=60.0)
bench("canvas.copy() 1080p(整幅)", lambda: canvas.copy(), n=20, freq_per_s=60.0)
bench("QImage(arr).copy() 1080p(解码线程每帧)", lambda: QImage(bgra.data, W, H, 4 * W,
                                                              QImage.Format_ARGB32).copy(),
      n=20, freq_per_s=60.0)


def frame_copy():
    f = QImage(W, H, QImage.Format_RGB32)
    np.copyto(np.frombuffer(f.bits(), np.uint8, f.sizeInBytes()),
              np.frombuffer(canvas.constBits(), np.uint8, canvas.sizeInBytes()))


bench("engine 深拷贝帧(np.copyto 7.9MB)", frame_copy, n=20, freq_per_s=60.0)
from PySide6.QtGui import QPixmap  # noqa: E402

bench("QPixmap.fromImage(1080p) 预览", lambda: QPixmap.fromImage(canvas), n=20, freq_per_s=60.0)
bench("scaled 1080p->420x236 Smooth", lambda: canvas.scaled(420, 236, Qt.KeepAspectRatio,
                                                            Qt.SmoothTransformation),
      n=20, freq_per_s=60.0)

# 缩放（解码线程）
print("\n[5] 解码侧缩放/格式转换（每帧，解码线程）")
u4k = (rng.rand(2160, 3840, 4) * 255).astype(np.uint8)
u1080 = (rng.rand(1080, 1920, 4) * 255).astype(np.uint8)
u720 = (rng.rand(720, 1280, 4) * 255).astype(np.uint8)
bench("cv2.resize 4K->1080p INTER_AREA", lambda: cv2.resize(u4k, (1920, 1080),
                                                             interpolation=cv2.INTER_AREA),
      n=10, freq_per_s=30.0)
bench("cv2.resize 1080p->720p INTER_AREA", lambda: cv2.resize(u1080, (1280, 720),
                                                              interpolation=cv2.INTER_AREA),
      n=20, freq_per_s=30.0)
bench("cv2.resize 4K->1080p INTER_LINEAR", lambda: cv2.resize(u4k, (1920, 1080),
                                                              interpolation=cv2.INTER_LINEAR),
      n=10, freq_per_s=30.0)
bench("cv2.cvtColor BGR->BGRA 1080p", lambda: cv2.cvtColor(u1080[:, :, :3], cv2.COLOR_BGR2BGRA),
      n=20, freq_per_s=30.0)

# ---------------------------------------------------------------- 节拍/小节（低频）
print("\n[6] 节拍/小节分析（每次导入 / 每首歌，低频）")
import beatgrid as BG  # noqa: E402

mono = SIG[:int(60 * 48000), 0].astype(np.float32)
mono22 = cv2.resize(mono.reshape(-1, 1), (1, int(60 * 22050)),
                    interpolation=cv2.INTER_AREA).ravel().astype(np.float32)
bench("beatgrid.features(60s @22.05k)", lambda: BG.features(mono22, 22050), n=3, warm=1)
_f = BG.features(mono22, 22050)
bench("beatgrid.onset_env", lambda: BG.onset_env(_f), n=5, warm=1)
_env = BG.onset_env(_f)
bench("beatgrid.scan_bpm(粗+细)", lambda: BG.scan_bpm(_f, _env), n=3, warm=1)

import phrase_bt as PB  # noqa: E402

print("  phrase_bt 可用：%s" % (PB.available(),))
bench("phrase_bt.logmel(60s) CPU 部分", lambda: PB.logmel(mono22), n=3, warm=1)

# ---------------------------------------------------------------- 输出
print("\n[7] 输出（NDI/Spout）—— 仅测 CPU 侧开销")
bench("np.frombuffer 画布(零拷贝视图)", lambda: np.frombuffer(canvas.bits(), np.uint8,
                                                              count=canvas.bytesPerLine() * H),
      n=30, freq_per_s=60.0)
buf = np.frombuffer(canvas.bits(), np.uint8, count=canvas.bytesPerLine() * H)
bench("memcpy 8.3MB(NDI 队列拷贝)", lambda: bytes(buf), n=20, freq_per_s=60.0)

print("\n" + "=" * 100)
print("完成")
