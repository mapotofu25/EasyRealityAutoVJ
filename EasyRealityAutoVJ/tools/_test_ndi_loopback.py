# -*- coding: utf-8 -*-
"""端到端回环：本进程内起 NDI 接收端连自己的发送端，验证**音频真的能收到**。

这是"NDI 到底有没有声音"的唯一硬证据：
  NDIOutput（我们修好的实现）──网络──> cyndilib Receiver ──> 统计收到的音频样本数。
用法：venv/Scripts/python.exe -u tools/_test_ndi_loopback.py
"""
import os
import sys
import time

# 项目根：按脚本自身位置推导（项目已从 P:\AutoVJ 迁移到 P:\ERAVJ\AutoVJ）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "dist_new", "EasyRealityAutoVJ", "_internal"))

import numpy as np                                               # noqa: E402
import ndi_out                                                   # noqa: E402

NAME = "autovj_looptest"
W, H, SR = 320, 180, 48000

FAILS = []


def check(name, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


print("① 起发送端（我们的 NDIOutput）")
o = ndi_out.NDIOutput(NAME)
o.set_audio_rate(SR)
o._ensure(W, H)
time.sleep(0.4)
check("发送端就绪", bool(o.enabled), "enabled=%s err=%s" % (o.enabled, o._last_err))

print("\n② 起接收端并连上")
from cyndilib.finder import Finder                               # noqa: E402
from cyndilib.receiver import Receiver, ReceiveFrameType        # noqa: E402
from cyndilib.audio_frame import AudioRecvFrame                  # noqa: E402
from cyndilib.video_frame import VideoRecvFrame                  # noqa: E402

print("   ReceiveFrameType 成员：", [x for x in dir(ReceiveFrameType) if not x.startswith("_")])
fd = Finder()
fd.open()
fd.wait_for_sources(5.0)
names = list(fd.get_source_names())
print("   发现源：", names)
# ⚠ NDI 的源名带机器名前缀（如 'MACHINE (autovj_looptest)'）⇒ 按包含匹配找
src = None
for nm in names:
    if NAME in nm:
        src = fd.get_source(nm)
        break
if src is None:
    print("✗ 没找到自己的源（Finder 可能没扫到）—— 跳过后续")
    o.close()
    sys.exit(1)

r = Receiver(source=src)
af = AudioRecvFrame()
vf = VideoRecvFrame()
r.set_audio_frame(af)
r.set_video_frame(vf)       # ⚠ 不设视频接收帧时，连接建立不起来（连接数一直是 0）
for _ in range(60):
    if o._conns() > 0:
        break
    try:
        r.receive(ReceiveFrameType.recv_all, 50)      # 接收端要主动收，连接才建立
    except Exception:                                          # noqa: BLE001
        pass
    time.sleep(0.1)
print('   is_connected =', end=' ')
try:
    print(r.is_connected(), end='  ')
except Exception as e:                                        # noqa: BLE001
    print('?', e, end='  ')
print()
check("接收端已连上（连接数 > 0）", o._conns() > 0, "connections=%d" % o._conns())

print("\n③ 一边喂音频一边收")
pcm = (np.sin(2 * np.pi * 440 * np.arange(1024 * 2) / SR) * 0.3).astype(np.float32)
video = bytes(W * H * 4)
got_samples = 0
recv_err = ""
for i in range(80):
    o.feed_audio(pcm, SR)
    if i % 3 == 0:
        o.send_video(video, W, H)
    try:
        for t in (ReceiveFrameType.recv_audio, ReceiveFrameType.recv_all):
            try:
                r.receive(t, 30)
                break
            except Exception:
                continue
    except Exception as e:                                        # noqa: BLE001
        recv_err = str(e)[:80]
    try:
        n = int(af.read_length)
        if n:
            got_samples += n
    except Exception:                                             # noqa: BLE001
        pass
    time.sleep(0.02)

print("   发送端：audio_ok=%d dropped=%d err=%r"
      % (o._audio_ok, o._audio_dropped, o._audio_err))
print("   接收端：收到音频样本 %d 个%s" % (got_samples, ("  err=" + recv_err) if recv_err else ""))
check("音频块确实发出去了（有接收端时不再被门控丢掉）", o._audio_ok > 0,
      "audio_ok=%d" % o._audio_ok)
check("★ 接收端真的收到了音频样本", got_samples > 0, "样本数 %d" % got_samples)
check("发送端没有错误", not o._audio_err, o._audio_err)

for x in (r,):
    try:
        x.disconnect()
    except Exception:                                             # noqa: BLE001
        pass
try:
    fd.close()
except Exception:                                                 # noqa: BLE001
    pass
o.close()

print()
print("结果：%s" % ("全部通过 ✓" if not FAILS else "失败 %d 项 ✗ %s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
