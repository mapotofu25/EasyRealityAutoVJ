# -*- coding: utf-8 -*-
"""★ 素材**音轨波形**（需求 11 数据侧）—— 零 Qt。

**为什么需要**：视频素材自带音轨，把它的波形画在**片段块内**（而不是另开一条轨），
用户才能把素材里的鼓点/人声与歌曲音频轨对齐。

**纪律**：
  · **零 Qt**（`media/` 铁律）。
  · **不发明第二套数据格式**：数据一律是 `audio.waveform.WaveformData`（`columns/amp/r/g/b/
    duration_sec`），与歌曲音频轨**同一套**；绘制侧（T04）只需一套读法。
  · **不新开重解码链**：解码 + 重采样 + 分块 FFT **全部复用** `audio/waveform.py` 的既有实现
    （它按"第一个 audio 流"解码 ⇒ **视频文件同样可用**）。
  · **不重算**：复用 `audio/waveform` 的**磁盘缓存**（`cache/waveform/*.npz`，键含
    路径|mtime|size|版本|列数）；本模块再加一层"**没音轨**"的**进程内负缓存**，
    免得重复开关对"没有音轨的素材"反复开文件。
"""
from __future__ import annotations

import logging
import os

from ..audio import waveform as _wf
from ..audio.waveform import WaveformData          # noqa: F401  （对外再导出，保持"单一格式"）

log = logging.getLogger("ercue.media.media_wave")

#: 片段内波形默认列密度：与歌曲音频轨**一致**（`None` = 按 `waveform.COLUMNS_PER_SEC`）。
#: ⚠ 用同一密度 ⇒ 同一文件若既作曲目又作素材，**缓存与格式完全共用**，不产生分叉。
DEFAULT_COLUMNS = None

# 进程内**负缓存**：`abspath -> True` 表示"该文件没有可用的音轨 / 算不出波形"。
# 只在本次会话有效（文件可能被替换）；用文件名+mtime 做键的子集即可 —— 这里用 abspath，
# 因为"素材被换掉"时素材本身会重新导入（clip_id 变），旧键自然失效。
_NO_AUDIO = {}


def has_audio_stream(path) -> bool:
    """该素材**有没有音轨**（廉价探测：只开容器看 stream 列表，不解码）。

    没有音轨 / 打不开 ⇒ False。**绝不抛**。
    """
    p = str(path or "")
    if not p or not os.path.isfile(p):
        return False
    if os.path.abspath(p) in _NO_AUDIO:
        return False
    try:
        import av
    except Exception:                                   # noqa: BLE001
        return False
    try:
        with av.open(p, metadata_errors="replace") as c:
            return any(s.type == "audio" for s in c.streams)
    except Exception:                                   # noqa: BLE001
        return False


def no_audio_reason(path) -> str:
    """给 UI 用的"为什么没有波形"文案（空字符串 = 应该有）。"""
    p = str(path or "")
    if not p:
        return "这个素材没有落盘路径"
    if not os.path.isfile(p):
        return "素材文件不在磁盘上"
    if not has_audio_stream(p):
        return "这个素材没有音轨"
    return ""


def analyze(path, columns=DEFAULT_COLUMNS, progress=None):
    """算（或从缓存取）该素材的**音轨波形**；失败 / 无音轨返回 `None`（**绝不抛**）。

    · 命中 `audio/waveform` 的磁盘缓存 ⇒ 直接返回（**不再解码**）；
    · 无音轨/算不出 ⇒ 记进**负缓存**，同一会话内不再重复开文件；
    · `progress(fraction)` 可选（0..1），**在调用方线程内同步执行** ⇒ 由调用方放到后台线程跑。
    """
    p = str(path or "")
    if not p or not os.path.isfile(p):
        return None
    ap = os.path.abspath(p)
    if ap in _NO_AUDIO:
        return None
    data = _wf.load_or_compute(ap, columns=columns, progress=progress)
    if data is None:
        _NO_AUDIO[ap] = True                            # ★ 负缓存：别对"没音轨"的素材反复开文件
        log.info("素材无可用音轨波形：%s（%s）", os.path.basename(ap), no_audio_reason(ap))
    return data


def cache_path_for(path, columns=DEFAULT_COLUMNS) -> str:
    """该素材波形缓存的落盘路径（诊断/清理用）。"""
    return _wf.cache_path_for(str(path or ""), columns)


def clear_negative_cache() -> None:
    """清空"没音轨"负缓存（文件被就地替换后需要时调）。"""
    _NO_AUDIO.clear()


def selftest() -> bool:
    """零 Qt 自检：坏路径/无音轨安全；有音轨能算且**两次一致**（第二次走缓存）。"""
    import tempfile
    import wave

    import numpy as np

    assert analyze("__definitely_not_exist__.mp4") is None, "坏路径应返回 None"
    assert analyze("") is None
    assert has_audio_stream("__nope__.mov") is False
    assert no_audio_reason("") != ""

    # 造一段 3 秒 1kHz 立体声 WAV（有音轨）⇒ 能算出波形
    d = tempfile.mkdtemp(prefix="ercue_mw_")
    p = os.path.join(d, "tone.wav")
    sr = 44100
    t = np.arange(int(sr * 3.0)) / sr
    x = np.clip(0.4 * np.sin(2 * np.pi * 1000.0 * t), -1.0, 1.0)
    pcm = (np.stack([x, x], axis=1) * 32767.0).astype("<i2")
    with wave.open(p, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())

    assert has_audio_stream(p), "刚造的 WAV 应该有音轨"
    a = analyze(p)
    assert a is not None and a.columns > 0, "有音轨的素材应算出波形"
    assert abs(a.duration_sec - 3.0) < 0.2, a.duration_sec
    assert len(a.amp) == a.columns
    b = analyze(p)                                       # 第二次：磁盘缓存命中
    assert b is not None and b.columns == a.columns, "缓存命中应与首次一致"
    assert not any(v == -1.0 for v in a.amp[:10]), "幅度应在 [0,1]"
    try:
        os.remove(p)
    except OSError:
        pass
    print("[media_wave] 自检通过 ✓  坏路径/无音轨安全；有音轨可算且缓存一致（columns=%d, dur=%.2fs）"
          % (a.columns, a.duration_sec))
    return True


if __name__ == "__main__":
    selftest()
