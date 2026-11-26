# -*- coding: utf-8 -*-
"""音频解码兜底：soundfile 读不了时改用 PyAV/FFmpeg。

现场遇到过 `sf.info()` 能读、`sf.read()` 却报
`flac decoder lost sync` 的 FLAC（文件局部坏帧/编码器差异）；
同一文件 PyAV 能正常解码。扫描网格和波形都走这里，避免整首歌
因为没有网格/波形而被误判成“没扫描”。
"""

from __future__ import annotations

import numpy as np


def _decode_av(path, maxsec=None):
    """用 PyAV 解成单声道 float32。返回 (sig, sr, dur)。"""
    import av

    with av.open(path) as c:
        st = c.streams.audio[0]
        sr = int(st.codec_context.sample_rate or 0)
        if sr <= 0:
            raise RuntimeError("PyAV 读不到采样率")
        try:
            dur = float(st.duration * st.time_base) if st.duration else 0.0
        except Exception:                                      # noqa: BLE001
            dur = 0.0
        chunks = []
        total = 0
        limit = int(maxsec * sr) if maxsec else 0
        for frame in c.decode(audio=0):
            arr = frame.to_ndarray()
            ch = int(getattr(frame.layout, "nb_channels", 1) or 1)
            if arr.ndim == 2:
                if arr.shape[0] == ch:
                    arr = arr.mean(axis=0)                     # planar
                elif arr.shape[0] == 1 and ch > 1:
                    arr = arr.reshape(-1, ch).mean(axis=1)     # packed
                else:
                    arr = arr.reshape(-1)
            else:
                arr = arr.reshape(-1)
            if arr.dtype.kind in "iu":
                # PyAV 的 s16/s32 是整数；归一化到 -1..1，和 soundfile 的 float32 口径一致
                scale = 32768.0 if arr.dtype.itemsize <= 2 else 2147483648.0
                arr = arr.astype(np.float32) / scale
            else:
                arr = arr.astype(np.float32, copy=False)
            if arr.size:
                chunks.append(arr)
                total += arr.size
            if limit and total >= limit:
                break
        if not chunks:
            raise RuntimeError("PyAV 没有解出音频帧")
        sig = np.concatenate(chunks)
        if limit:
            sig = sig[:limit]
        return np.ascontiguousarray(sig, dtype=np.float32), sr, (dur or len(sig) / float(sr))


def decode_mono(path, maxsec=None):
    """解码为单声道 float32；优先 soundfile，失败自动退回 PyAV。

    :param maxsec: 只解前 N 秒（None=整首）
    :return: (sig, sr, dur)
    """
    path = str(path)
    try:
        import soundfile as sf
        info = sf.info(path)
        sr0 = int(info.samplerate)
        frames = -1 if maxsec is None else int(maxsec * sr0)
        data, _ = sf.read(path, dtype="float32", always_2d=True, frames=frames)
        sig = np.mean(data, axis=1).astype(np.float32, copy=False)
        dur = float(info.duration)
        return np.ascontiguousarray(sig), sr0, dur
    except Exception as e_sf:                                  # noqa: BLE001
        try:
            return _decode_av(path, maxsec=maxsec)
        except Exception as e_av:                              # noqa: BLE001
            raise RuntimeError("soundfile: %s; PyAV: %s" % (e_sf, e_av)) from e_av
