# -*- coding: utf-8 -*-
"""**本地端到端认歌测试台**：不依赖声卡、不依赖用户，跑真正的那条循环。

做法：
  1. 读一个真音频文件，重采样到 48k（模拟系统回环的 48k）
  2. 预填 12s 进真 `_AudioRing`
  3. 一个**实时喂料线程**每 30ms 写 1440 点（与生产 `_block` 同量级）
  4. 另一个线程跑**真的** `AudioEngine._recognize_loop`
  5. 轮询 `state.recognized_song_id`，看多久确认、确认成谁

这样"改完能不能认出来"在本地就能定，不用再让用户装包试。
"""
from __future__ import annotations
import hashlib
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

# 隔离取证日志到临时目录（别污染用户真实日志）
os.environ["LOCALAPPDATA"] = os.path.join(tempfile.gettempdir(), "recogtest_fixed")
os.makedirs(os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ"), exist_ok=True)
_LOG = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ", "cue_health.log")
try:
    os.remove(_LOG)
except OSError:
    pass

import numpy as np
import soundfile as sf

import audio_engine as AE
import fp as fpvj
import cue_fp_pack as C
from charge_engine import ChargeBarEngine

SR = 48000
CHUNK = 1440          # 30ms @48k，与生产 _block 同量级
PRE = 12.0            # 预填 12s
WAIT = 25.0           # 最多等 25s 看是否确认


def to48k(path):
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    mono = np.ascontiguousarray(data.mean(axis=1), dtype=np.float32)
    if sr != SR:
        x = np.linspace(0, len(mono) - 1, max(1, int(len(mono) * SR / sr)))
        mono = np.interp(x, np.arange(len(mono)), mono).astype(np.float32)
    return mono


def make_engine(db, mono):
    ae = object.__new__(AE.AudioEngine)
    ae._fp_db = db
    ae._recognizer_engine = ChargeBarEngine()
    ae._rate_est = AE._RateEstimate() if hasattr(AE, "_RateEstimate") else None
    if ae._rate_est is None:
        # 兜底：找到模块里那个 rate 估计类
        for n in dir(AE):
            if "Rate" in n and isinstance(getattr(AE, n), type):
                ae._rate_est = getattr(AE, n)()
                break
    ae.state = AE.AudioState()
    ae.state.running = True
    ae._genre_lock = threading.Lock()
    ae._genre_stop = threading.Event()
    ae._fp_sr = SR
    ae._fp_buf = AE._AudioRing(int(SR * 12))
    ae._fp_buf.write(mono[: int(PRE * SR)])
    return ae


def run_one(db, name, path, want_sid):
    mono = to48k(path)
    ae = make_engine(db, mono)
    stop = threading.Event()

    def feeder():
        pos = int(PRE * SR)
        t0 = time.perf_counter()
        while not stop.is_set() and pos < len(mono):
            # 按实时节奏推进
            target = t0 + (pos - int(PRE * SR)) / SR
            delay = target - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            ae._fp_buf.write(mono[pos:pos + CHUNK])
            pos += CHUNK

    tf = threading.Thread(target=feeder, daemon=True)
    tr = threading.Thread(target=AE.AudioEngine._recognize_loop, args=(ae,), daemon=True)
    tf.start()
    tr.start()
    t0 = time.perf_counter()
    got, t_confirm = -1, None
    while time.perf_counter() - t0 < WAIT:
        time.sleep(0.1)
        sid = int(getattr(ae.state, "recognized_song_id", -1) or -1)
        if sid >= 0:
            got, t_confirm = sid, time.perf_counter() - t0
            break
    stop.set()
    ae._genre_stop.set()
    ok = (got == want_sid)
    print("  %-34s want=%-12s got=%-12s %s%s"
          % (name[:34], want_sid, got,
             ("✅ %.1fs 确认" % t_confirm) if ok else "❌ 未确认",
             "" if ok or t_confirm is None else "（认成了别的）"))
    return ok


def main():
    db = fpvj.FingerprintDB(os.path.join(os.environ["LOCALAPPDATA"], "fp.db")) \
        if False else fpvj.FingerprintDB(
            os.path.join(tempfile.mkdtemp(), "fp.db"))
    # 用真实库的副本（只读用）
    import shutil
    real = os.path.join(os.path.expanduser("~"), "AppData", "Local", "AutoVJ",
                        "fingerprints.db")
    if not os.path.isfile(real):
        real = r"C:/Users/Administrator/AppData/Local/AutoVJ/fingerprints.db"
    db.close()
    tmp = os.path.join(tempfile.mkdtemp(), "fp.db")
    shutil.copyfile(real, tmp)
    db = fpvj.FingerprintDB(tmp)

    CASES = [
        ("揽月（码表包·合成id）", r"Y:/EasyRealityAutoVJ/码表素材/揽月 - Dreamira.flac",
         C.fp_song_id("fp2-617615efb8f43911")),
        ("Nimbus（码表包·合成id）", r"Y:/EasyRealityAutoVJ/码表素材/Nimbus Spark 1999 - Supa7onyz.flac",
         C.fp_song_id("fp2-f6ebed7d34af40e3")),
        ("Cosmic String（曲库歌）",
         r"W:/临时结界相关/2of/tofu/Rolling Contact - Cosmic String.flac", None),
        ("Black Warrior（曲库歌）",
         r"W:/临时结界相关/2of/tofu/Rolling Contact - Black Warrior (2025 Remaster).flac", None),
    ]
    ok = 0
    n = 0
    for name, path, want in CASES:
        if not os.path.isfile(path):
            print("  %-34s （文件不存在，跳过）" % name); continue
        if want is None:
            # ★ 期望值必须用**配置里那条路径字符串**算：config 里混用了 `/` 与 `\\\\`，
            #   同一个文件换一种写法 md5 就不同（我第一版就是这里算错，导致误判"认成别的歌"）。
            import json as _json
            _cfg = _json.load(open(os.path.join(
                r"C://Users//Administrator//AppData//Local//AutoVJ", "config.json"),
                encoding="utf-8"))
            _base = os.path.basename(path)
            _p = next((q for q in (_cfg.get("music_library") or [])
                       if os.path.basename(q) == _base), path)
            want = int(hashlib.md5(_p.encode("utf-8")).hexdigest()[:8], 16)
        n += 1
        ok += 1 if run_one(db, name, path, want) else 0
    db.close()
    print("\n通过 %d / %d" % (ok, n))
    print("日志：", _LOG)
    return 0 if ok == n else 1


if __name__ == "__main__":
    sys.exit(main())
