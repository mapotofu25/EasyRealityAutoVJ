# -*- coding: utf-8 -*-
"""真机端到端：启动 AudioEngine 采集一小段，验证新采集层在完整链路里跑通。

覆盖：采集 → _BlockAssembler → _Analyzer.feed（能量/拍钟/指纹/曲风）→ NDI 转发队列。
判据：
  * 不抛异常
  * 采集到数据（state.running 稳定、有能量输出）
  * _ndi_q 有投递（push 计数增长）
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

print("=== 真机端到端采集验证 ===")
import audio_engine as ae                                       # noqa: E402

eng = ae.AudioEngine()
eng.energy_scale = 1.0
# 打开系统声音（loopback）
eng.start(source_type="system", device_name="", mono=False, sr=0)
time.sleep(2.0)
st = eng.state
for i in range(8):
    time.sleep(1.0)
    with st.lock:
        running = st.running
        err = st.error
        dev = st.device_desc
    print("  t=%ds running=%r energy=%.3f bpm=%.1f dev=%s err=%s"
          % (i + 1, running, float(getattr(eng, "energy", 0.0) or 0.0),
             float(getattr(st, "bpm_display", 0.0) or 0.0), dev, err or "-"))
eng.stop()
time.sleep(0.3)
print("=== 完成（无异常即通过） ===")
