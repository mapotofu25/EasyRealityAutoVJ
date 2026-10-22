# -*- coding: utf-8 -*-
"""QA 辅助：定位"软解只有 ~14fps（正常 30fps）"的根因。

对比三种进入软解的方式，看**是否 `_try_gpu` 消费了首包**导致的退化：
  S1  _GPU_DISABLED 直通（_try_gpu 首行返回，**不消费首包**）
  S2  GLDecoder 构造失败（_try_gpu 走到最后、**消费了首包**才失败）
  S3  AvAlphaPlayer（原软解路径，基线）
用法：venv\\Scripts\\python.exe tools\\_qa_softrate.py
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
FILE = os.environ.get("QA_FILE", "001 (1).mov")
LIB = r"Y:\EasyRealityAutoVJ\素材库"


def measure(pl, secs=3.0, warm=0.6, label=""):
    pl.set_active(True)
    time.sleep(warm)
    last, n = pl.current(), 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < secs:
        img = pl.current()
        if img is not None and img is not last:
            last = img
            n += 1
        time.sleep(0.002)
    dt = time.perf_counter() - t0
    print("  %-34s gpu=%-5s %4d 帧 / %.1fs = %5.1f fps   reason=%s"
          % (label, getattr(pl, "gpu", "-"), n, dt, n / dt, getattr(pl, "reason", "")))
    pl.close()
    time.sleep(0.3)
    return n / dt


def main():
    import av
    import media_manager as mm
    import glctx

    path = os.path.join(LIB, FILE)
    print("素材 %s" % FILE)
    print("=" * 90)

    # S1: _GPU_DISABLED 直通（不消费首包）
    mm._GPU_STATUS = None
    mm._GPU_DISABLED = "probe: forced soft (S1)"
    mm.set_gpu_decode(True)
    measure(mm.GpuDxvPlayer(path, av, out_alpha=True), label="S1 强制软解(不消费首包)")
    mm._GPU_DISABLED = ""

    # S2: GLDecoder 构造失败（消费首包后才失败）
    class _FailDecoder:
        def __init__(self):
            self.ok = False
            self.err = "probe: 模拟拿不到 GL 上下文"

        def decode(self, *a, **k):
            return None

        def destroy(self):
            return False

    glctx.GLWorker._inst = None
    glctx.GLDecoder = _FailDecoder
    mm._GPU_STATUS = None
    mm._GPU_DISABLED = ""
    mm.set_gpu_decode(True)
    measure(mm.GpuDxvPlayer(path, av, out_alpha=True), label="S2 GL失败(已消费首包)")

    # S1' 同 S1 但**不预热、窗口 2.5s**（复刻 QA 用例的测法）
    mm._GPU_STATUS = None
    mm._GPU_DISABLED = "probe: S1 no-warm"
    mm.set_gpu_decode(True)
    measure(mm.GpuDxvPlayer(path, av, out_alpha=True), secs=2.5, warm=0.0,
            label="S1' 强制软解 无预热/2.5s")
    mm._GPU_DISABLED = ""

    # S3: AvAlphaPlayer 基线
    measure(mm.AvAlphaPlayer(path, av), label="S3 AvAlphaPlayer 基线")
    measure(mm.AvAlphaPlayer(path, av), secs=2.5, warm=0.0, label="S3' AvAlpha 无预热/2.5s")


if __name__ == "__main__":
    main()
