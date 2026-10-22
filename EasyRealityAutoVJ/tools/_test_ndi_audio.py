# -*- coding: utf-8 -*-
"""① 让采集侧把采样率提前告诉 NDI 发送端（音频帧只能在 open() 之前定采样率）。
   ② 端到端验证：用**真实 NDI sender** 跑我们修好的 `NDIOutput.feed_audio`，
      断言音频块真的被接受并发出（`_audio_ok` 增长、`_audio_err` 为空）。
"""
import io
import os
import sys
import time

# 项目根：按脚本自身位置推导（项目已从 P:\AutoVJ 迁移到 P:\ERAVJ\AutoVJ）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------- ① ui_main：告知采样率 ----------------
P = os.path.join(ROOT, "src", "ui_main.py")
OLD = '''    def _ndi_audio_feed(self, pcm, sr):
        ndi = self.engine._ndi
        if ndi is not None and self.cfg["output"].get("ndi_enabled"):
            ndi.feed_audio(pcm, sr)'''
NEW = '''    def _ndi_audio_feed(self, pcm, sr):
        ndi = self.engine._ndi
        if ndi is not None and self.cfg["output"].get("ndi_enabled"):
            # ⚠ 音频帧的采样率只能在 `open()` **之前**定好 ⇒ 每次都把当前采集率告诉它
            #   （`set_audio_rate` 只是存一个提示值，开销可忽略；非 48k 设备靠它才对）
            try:
                ndi.set_audio_rate(sr)
            except Exception:                                     # noqa: BLE001
                pass
            ndi.feed_audio(pcm, sr)'''


def patch_ui():
    s = io.open(P, encoding="utf-8").read()
    if s.count(OLD) == 0 and "ndi.set_audio_rate(sr)" in s:
        print("· ui_main 已改过，跳过")
        return True
    if s.count(OLD) != 1:
        print("✗ ui_main 锚点命中 %d 次" % s.count(OLD))
        return False
    io.open(P, "w", encoding="utf-8", newline="\n").write(s.replace(OLD, NEW))
    print("✓ ui_main._ndi_audio_feed 已告知采样率")
    return True


def main():
    if not patch_ui():
        return 1
    import py_compile
    py_compile.compile(P, doraise=True)

    # ---------------- ② 端到端实测 ----------------
    sys.path.insert(0, os.path.join(ROOT, "src"))
    sys.path.insert(0, os.path.join(ROOT, "dist_new", "EasyRealityAutoVJ", "_internal"))
    import numpy as np
    import ndi_out

    ok, why = ndi_out.probe_runtime()
    print("NDI 运行库：", ok, why)
    if not ok:
        print("（没有 NDI 运行库，跳过端到端测试）")
        return 0

    FAILS = []

    def check(name, good, extra=""):
        print(("  ✓ " if good else "  ✗ ") + name + (("   " + str(extra)) if extra else ""))
        if not good:
            FAILS.append(name)

    print("\n① 建立发送端（先喂一帧视频，音频才有意义）")
    o = ndi_out.NDIOutput("autovj_audiotest")
    o.set_audio_rate(48000)
    # 直接按视频分辨率把发送端建起来（没有接收端时写视频帧会失败，与音频无关）
    o._ensure(320, 180)
    time.sleep(0.4)          # 等过 open() 后的静默期（见 ndi_out._WARMUP_SECS）
    check("NDI 发送端已就绪", bool(o.enabled), "enabled=%s err=%s" % (o.enabled, o._last_err))

    print("\n② 交错一维 PCM（采集侧就是这个格式）")
    pcm = (np.random.default_rng(1).standard_normal(1024 * 2) * 0.1).astype(np.float32)
    for _ in range(5):
        o.feed_audio(pcm, 48000)
    check("音频块被接受并发出（不再是静默失败）", o._audio_ok >= 5,
          "_audio_ok=%d err=%r" % (o._audio_ok, o._audio_err))
    check("没有错误", not o._audio_err, o._audio_err)

    print("\n③ 换采样率 44100（非 48k 设备的情况）")
    o2 = ndi_out.NDIOutput("autovj_audiotest44")
    o2.set_audio_rate(44100)
    o2._ensure(320, 180)
    time.sleep(0.4)
    pcm44 = (np.random.default_rng(2).standard_normal(1024 * 2) * 0.1).astype(np.float32)
    for _ in range(3):
        o2.feed_audio(pcm44, 44100)
    check("44.1k 也能发（老代码这里 AttributeError 静默丢弃）", o2._audio_ok >= 3,
          "_audio_ok=%d err=%r" % (o2._audio_ok, o2._audio_err))

    print("\n④ 满帧 4800 样本（缓冲上限）")
    big = np.zeros(4800 * 2, dtype=np.float32)
    for _ in range(3):
        o.feed_audio(big, 48000)
    check("4800 样本的整块也能发", o._audio_ok >= 8,
          "_audio_ok=%d dropped=%d err=%r" % (o._audio_ok, o._audio_dropped, o._audio_err))

    for x in (o, o2):
        try:
            x.close()
        except Exception:                                          # noqa: BLE001
            pass
    print()
    print("结果：%s" % ("全部通过 ✓" if not FAILS else "失败 %d 项 ✗ %s" % (len(FAILS), FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
