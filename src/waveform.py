# -*- coding: utf-8 -*-
r"""波形峰值包络（**数据层，零 Qt**）—— 把整轨音频压成「每列一个幅度」的峰值数组。

## 为什么是峰值数组 JSON，不是 PNG
标记的落点是「秒」（float，权威），波形只是「看着找结构」的背景。数值数组可做
「列↔秒」的精确换算（`col = sec × cps`），PNG 固定分辨率会丢精度、还需解码。
密度固定 `cps=100`（100 列/秒）；单色幅度包络（三频段配色留 P2 可选）。

## 算法（复用 ERCUE `ercue/audio/waveform.py` 的「峰值包络」思路，做单色简化）
  1) 输入是**已解码**的单声道 `sig`（复用 `beatgrid.load_mono` / erlib `scanner.decode`
     的同一次解码，每首只解码一次）；解码器用 `soundfile`，**不用 PyAV**（守住 ERLIB
     「不引 av/FFmpeg 省 160MB」的铁律）。
  2) 分列：`win = n // ncol`，`ncol ≈ dur × cps`（不重叠，只用完整窗口）。
  3) 每列幅度 `amp = sqrt(RMS × peak)`（几何平均：密度 + 瞬态兼顾）。
  4) 全局分位归一（`99 分位`，抗单个尖峰压平整轨）→ `0..1`，四舍五入到 3 位小数。

## 存储
  - 统一 `waveform` dict：`{v:1, cps:100.0, dur, amp:[0..1]}`（`v` 独立于 GRID_VER/FORMAT_VERSION）。
  - 序列化 = **zlib 压缩 JSON**（`waveform_encode` / `waveform_decode`）。
  - ERAVJ 本地缓存：`app_base_dir()/waveforms/<sha1(路径|mtime|size|v|cps)[:24]>.wj`；
  - `.eravjlib` 内按 `song_id` 存 `waveforms` 表 BLOB（同一套 encode/decode）。

可离线自检：`python -m waveform`
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zlib

import numpy as np

WAVEFORM_VERSION = 1           # 波形数据格式版本（独立于 GRID_VER / FORMAT_VERSION）
CPS_DEFAULT = 100.0            # 列/秒（100 列/秒）
AMP_PERCENTILE = 99.0          # 幅度全局归一参考分位（抗单尖峰）
AMP_ROUND = 3                  # 幅度小数位（保证两端逐字节一致 + 控制体积）


# ==========================================================================
# 峰值包络计算
# ==========================================================================
def waveform_from_signal(sig, sr, cps=CPS_DEFAULT):
    """从**已解码**的单声道信号算出峰值包络 → `{v, cps, dur, amp}`。

    `sig`：float 一维数组（任意采样率，由调用方保证已混成单声道）。
    `sr` ：采样率（Hz）。
    `cps`：列密度（列/秒）。
    失败返回 None（**绝不抛到调用方**）。
    """
    try:
        if sig is None:
            return None
        mono = np.asarray(sig, dtype=np.float32).reshape(-1)
        if mono.size < 2:
            return None
        sr = float(sr) if sr else 1.0
        if sr <= 0:
            return None
        cps = float(cps) if cps else CPS_DEFAULT
        if cps <= 0:
            cps = CPS_DEFAULT
        n = int(mono.size)
        dur = n / sr
        ncol = max(1, int(round(dur * cps)))
        win = max(1, n // ncol)
        ncol = max(1, n // win)
        seg = mono[:ncol * win].reshape(ncol, win).astype(np.float64, copy=False)
        rms = np.sqrt(np.mean(seg * seg, axis=1))
        peak = np.max(np.abs(seg), axis=1)
        amp = np.sqrt(rms * peak)                          # 几何平均：密度 + 瞬态
        ref = float(np.percentile(amp, AMP_PERCENTILE)) if amp.size else 0.0
        an = np.clip(amp / (ref + 1e-12), 0.0, 1.0)
        return {
            "v": int(WAVEFORM_VERSION),
            "cps": float(cps),
            "dur": round(float(dur), 4),
            "amp": [round(float(x), AMP_ROUND) for x in an],
        }
    except Exception:                                      # noqa: BLE001
        return None


# ==========================================================================
# 序列化（zlib 压缩 JSON）—— 本地 .wj 与 .eravjlib BLOB 共用同一套
# ==========================================================================
def waveform_encode(wf):
    """waveform dict → zlib 压缩 JSON 字节（供 .eravjlib BLOB / .wj 文件）。"""
    if not isinstance(wf, dict):
        return b""
    try:
        payload = json.dumps(wf, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return zlib.compress(payload, 6)
    except Exception:                                      # noqa: BLE001
        return b""


def waveform_decode(data):
    """zlib 压缩 JSON 字节 → waveform dict（失败/空返回 None）。"""
    try:
        if not data:
            return None
        payload = zlib.decompress(bytes(data))
        wf = json.loads(payload.decode("utf-8"))
        if isinstance(wf, dict) and isinstance(wf.get("amp"), list):
            return wf
        return None
    except Exception:                                      # noqa: BLE001
        return None


# ==========================================================================
# ERAVJ 本地 .wj 缓存
# ==========================================================================
def _base_dir():
    """数据目录：源码运行=项目根；打包 exe=%LOCALAPPDATA%/AutoVJ。不可用退系统临时目录。"""
    try:
        from config import app_base_dir
        return os.path.join(app_base_dir(), "waveforms")
    except Exception:                                      # noqa: BLE001
        return os.path.join(tempfile.gettempdir(), "eravj_waveforms")


def _file_sig(path):
    ap = os.path.abspath(str(path or ""))
    try:
        st = os.stat(ap)
        return ap, int(st.st_mtime), int(st.st_size)
    except OSError:
        return ap, 0, 0


def waveform_cache_key(path, cps=CPS_DEFAULT):
    """缓存键：`sha1(绝对路径 | mtime | size | v | cps)[:24]`（与架构 §6.5 一致）。"""
    ap, mt, sz = _file_sig(path)
    cps = float(cps) if cps else CPS_DEFAULT
    token = "%s|%d|%d|v%d|%.1f" % (ap, mt, sz, int(WAVEFORM_VERSION), cps)
    return hashlib.sha1(token.encode("utf-8", "replace")).hexdigest()[:24]


def waveform_cache_path(path, cps=CPS_DEFAULT):
    """本地 `.wj` 缓存文件路径（不保证目录已建，写时 `waveform_save` 会建）。"""
    d = _base_dir()
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = os.path.join(tempfile.gettempdir(), "eravj_waveforms")
    return os.path.join(d, waveform_cache_key(path, cps) + ".wj")


def waveform_save(path, wf):
    """把 waveform 写成本地 `.wj`（原子替换）。返回是否成功。"""
    try:
        blob = waveform_encode(wf)
        if not blob:
            return False
        p = waveform_cache_path(path, (wf or {}).get("cps") or CPS_DEFAULT)
        d = os.path.dirname(p)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "wb") as f:
            f.write(blob)
        os.replace(tmp, p)
        return True
    except Exception:                                      # noqa: BLE001
        return False


def waveform_load(path, cps=CPS_DEFAULT):
    """读本地 `.wj` 缓存 → waveform dict（没有/损坏/版本不符返回 None）。"""
    try:
        p = waveform_cache_path(path, cps)
        if not os.path.isfile(p):
            return None
        with open(p, "rb") as f:
            wf = waveform_decode(f.read())
        if not isinstance(wf, dict):
            return None
        if int(wf.get("v") or 0) != WAVEFORM_VERSION:
            return None
        return wf
    except Exception:                                      # noqa: BLE001
        return None


# ==========================================================================
# 自检
# ==========================================================================
def selftest():
    """合成一段 55Hz 鼓点，验证「时长对齐 + 峰值包络 + 压缩往返」。"""
    sr = 22050
    secs = 3.0
    n = int(sr * secs)
    t = np.arange(n) / sr
    # 4-on-the-floor 150BPM 鼓点 + 持续底音
    beat = 60.0 / 150.0
    sig = 0.25 * np.sin(2 * np.pi * 55.0 * t).astype(np.float32)
    klen = int(sr * 0.05)
    kt = np.arange(klen) / sr
    kick = (np.sin(2 * np.pi * 55.0 * kt) * np.exp(-kt * 60.0)).astype(np.float32)
    pos = 0.0
    while pos < secs:
        j = int(pos * sr)
        e = min(n, j + klen)
        if e > j:
            sig[j:e] += 0.7 * kick[:e - j]
        pos += beat
    sig = sig / (np.abs(sig).max() + 1e-6)

    wf = waveform_from_signal(sig, sr)
    assert wf is not None, "waveform_from_signal 应成功"
    assert abs(wf["dur"] - secs) < 0.05, wf["dur"]
    assert wf["cps"] == CPS_DEFAULT, wf["cps"]
    assert len(wf["amp"]) > 0 and all(0.0 <= a <= 1.0 for a in wf["amp"]), "amp 应在 0..1"

    blob = waveform_encode(wf)
    assert blob and blob[:2] == b"x\x9c", "zlib 头应为 x\\x9c"
    wf2 = waveform_decode(blob)
    assert wf2 is not None and wf2["dur"] == wf["dur"] and wf2["amp"] == wf["amp"]

    # 缓存往返（把 _base_dir 指到临时目录，避免污染真实缓存目录）
    import tempfile as _tf
    import waveform as _wf
    tmpdir = _tf.mkdtemp(prefix="eravj_wf_")
    _orig_base_dir = _wf._base_dir
    _wf._base_dir = lambda: tmpdir
    try:
        p = os.path.join(tmpdir, "tone.wav")
        try:
            with open(p, "wb"):
                pass
            os.utime(p, (1234567890.0, 1234567890.0))
        except Exception:                                    # noqa: BLE001
            pass
        assert waveform_save(p, wf), "waveform_save 应成功"
        wf3 = waveform_load(p)
        assert wf3 is not None and wf3["amp"] == wf["amp"], "缓存读回应一致"
    finally:
        _wf._base_dir = _orig_base_dir
    print("[waveform] 自检通过 ✓  列数=%d 时长=%.3fs" % (len(wf["amp"]), wf["dur"]))
    return True


if __name__ == "__main__":
    selftest()
