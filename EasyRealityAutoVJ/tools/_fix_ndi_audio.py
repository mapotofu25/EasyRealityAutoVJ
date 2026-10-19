# -*- coding: utf-8 -*-
"""修 NDI 输出**没有音频**（用户 2026-10-01 报）。

实测（拿发行包里的 cyndilib 真跑一遍 sender）：
  · `AudioSendFrame.shape == (2, 4800)`、`channel_stride == 19200`
    ⇒ 音频帧是**平面(planar)**布局：`(声道数, 最大样本数)`；
  · `write_data((2, 1024))` → **成功**；`write_data((1024, 2))` → "number of channels must match"；
    而老代码传的是**一维交错**数组 → "Buffer has wrong number of dimensions (expected 2, got 1)"；
  · `AudioSendFrame` **没有 `set_sample_rate()` 方法**（`hasattr` = False），只有 `sample_rate` 属性；
  · 上面两个异常都被 `except Exception: pass` 吞掉 ⇒ **一个字节都发不出去，且无任何日志**。

⇒ 三处一起修：
  1. `set_sample_rate(sr)` → `a.sample_rate = sr`（属性赋值）；
  2. 交错 (n, ch) → **平面 (ch, n)** 且 C 连续再 write_data；
  3. 加 `get_write_available()` 守卫（缓冲满就丢这一块，不关输出），
     并把异常**记一次日志**（不静默）。
另外：帧要在 `open()` 之前定好采样率 ⇒ 新增 `set_audio_rate()` 供采集侧提前告知。
"""
import io
import os
import sys

P = r"P:\AutoVJ\src\ndi_out.py"
PAT = []


def rep(old, new, cnt=1):
    PAT.append((old, new, cnt))


# 1) __init__ 补状态
rep("""        self._dropped = 0        # 因缓冲未就绪而丢掉的帧数（诊断用，正常应接近 0）
""",
    """        self._dropped = 0        # 因缓冲未就绪而丢掉的帧数（诊断用，正常应接近 0）
        # ---- 音频（2026-10-01 修：原来三个错叠在一起，NDI 音频从来没响过）----
        self._audio_sr = 48000   # 采集采样率提示：帧必须在 open() 之前定好采样率
        self._audio_ok = 0       # 成功发出的音频块数（诊断）
        self._audio_dropped = 0  # 因缓冲未就绪丢掉的块数
        self._audio_err = ""     # 最近一次发送错误（诊断）
        self._audio_err_logged = None   # 只把**第一条**错误写日志，避免刷屏
""")

# 2) _ensure 里用提示采样率
rep("""            a = AudioSendFrame()
            try:
                a.sample_rate = 48000
                a.num_channels = 2
                a.set_max_num_samples(4800)
            except Exception:
                pass""",
    """            a = AudioSendFrame()
            try:
                # ⚠ 采样率必须**在 open() 之前**定好，所以用采集侧提前告知的值
                #   （`set_audio_rate()`；没告知就按 48k）。立体声、每帧最多 4800 样本。
                a.sample_rate = int(self._audio_sr or 48000)
                a.num_channels = 2
                a.set_max_num_samples(4800)
            except Exception:
                pass""")

# 3) 新增 set_audio_rate
rep("""    def feed_audio(self, pcm_interleaved, sr):""",
    """    def set_audio_rate(self, sr):
        \"\"\"采集侧提前告知采样率：音频帧的采样率**只能在 open() 之前设**。\"\"\"
        try:
            self._audio_sr = int(sr)
        except Exception:                                          # noqa: BLE001
            pass

    def feed_audio(self, pcm_interleaved, sr):""")

# 4) feed_audio 整体重写
rep('''        # 音频线程拿不到画布尺寸 → `_ensure()` 不带参数：
        # 发送端还没按视频分辨率建立时返回 False（这段音频先不发），建立之后返回 True。
        # NDI 是音画同步输出，视频没起来时音频也没有意义。
        if not self._ensure():
            return
        try:
            with self._lock:
                a = self._audio
                if a.sample_rate != sr:
                    a.set_sample_rate(sr)
                a.write_data(pcm_interleaved)
                self._sender.send_audio()
        except Exception:
            pass''',
    '''        # 音频线程拿不到画布尺寸 → `_ensure()` 不带参数：
        # 发送端还没按视频分辨率建立时返回 False（这段音频先不发），建立之后返回 True。
        # NDI 是音画同步输出，视频没起来时音频也没有意义。
        #
        # ⚠⚠ 2026-10-01 修（用户报「NDI 输出还是没有音频」，实测三个错叠在一起）：
        #   1. `AudioSendFrame` **没有 `set_sample_rate()` 方法**（只有 `sample_rate` 属性）
        #      ⇒ 老代码 `a.set_sample_rate(sr)` 直接 AttributeError；
        #   2. `write_data` 要的是 **(声道数, 样本数) 的平面(planar) 二维数组**
        #      （实测 `shape=(2,4800)`、`channel_stride=19200`；传一维会报
        #      `Buffer has wrong number of dimensions (expected 2, got 1)`，
        #      传 (n, ch) 交错二维会报 `number of channels must match`）；
        #   3. 上面两个异常都被 `except Exception: pass` 吞掉 ⇒ **一个字节都发不出去、
        #      而且完全没有日志**。三者叠加 = NDI 音频从来就没响过。
        if not self._ensure():
            return
        try:
            import numpy as _np
        except Exception:                                          # noqa: BLE001
            return
        try:
            with self._lock:
                a = self._audio
                if a is None:
                    return
                sr = int(sr or 0)
                if sr > 0 and int(a.sample_rate) != sr:
                    a.sample_rate = sr          # ★ 属性赋值（没有 set_sample_rate 方法）
                ch = int(a.num_channels or 2) or 2
                pcm = _np.asarray(pcm_interleaved, dtype=_np.float32)
                if pcm.ndim == 1:               # 交错一维 → (n, ch)
                    n = (pcm.size // ch) * ch
                    pcm = pcm[:n].reshape(-1, ch)
                elif pcm.shape[0] == ch and pcm.shape[1] != ch:
                    pcm = pcm.T                 # 已经是平面 (ch, n) → 转成 (n, ch) 统一处理
                # 交错 (n, ch) → 平面 (ch, n)，并保证 C 连续（cyndilib 直接按缓冲读）
                planar = _np.ascontiguousarray(pcm.T)
                # 与视频同样的坑：缓冲没空出来时 write_data 会抛 —— 这一块直接丢，不关输出
                if hasattr(a, "get_write_available") and not a.get_write_available():
                    self._audio_dropped += 1
                    return
                a.write_data(planar)
                self._sender.send_audio()
                self._audio_ok += 1
        except Exception as e:                                     # noqa: BLE001
            self._audio_err = "%s: %s" % (type(e).__name__, str(e)[:120])
            if self._audio_err_logged is None:
                self._audio_err_logged = self._audio_err
                _log("!! NDI 音频发送失败（只记一次）：%s" % self._audio_err)''')


def main():
    s = io.open(P, encoding="utf-8").read()
    for i, (old, new, cnt) in enumerate(PAT, 1):
        n = s.count(old)
        if n != cnt:
            print("✗ 第 %d 处命中 %d 次（应为 %d）：%s" % (i, n, cnt, old.strip()[:60]))
            return 1
    for old, new, cnt in PAT:
        s = s.replace(old, new)
    io.open(P, "w", encoding="utf-8", newline="\n").write(s)
    print("✓ ndi_out.py 已修（%d 处）" % len(PAT))
    import py_compile
    py_compile.compile(P, doraise=True)
    print("语法 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
