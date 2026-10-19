# -*- coding: utf-8 -*-
"""回归：`_capture_loop` 在「record() 一直返回空」时**必须退避**，绝不空转烧 CPU。

## 背景（2026-10-08 现场事故）
用户报「软件非常卡」。取证日志里 `empty=122334607`（两分钟 1.2 亿次）——
`_capture_loop` 里 `if data is None or len(data)==0: _blk_empty+=1; continue`
**没有 sleep**。老路径 `soundcard.record()` 会**阻塞**，`continue` 不空转；
新的局域网 / `_BlockAssembler` 路径**立即返回 None** ⇒ 裸 `continue` = 无 sleep
死循环，吃满一个核 + 长期持 GIL ⇒ 主线程每 500ms 抢引擎锁被饿死 ⇒ 界面卡成幻灯片。

## 判据
用假 recorder（永远返回 None）跑约 1.2 秒：
  - record 调用次数必须**受控**（无退避时是百万量级/秒）；
  - CPU 时间必须**远小于**墙钟（说明真在 sleep）。
两者都满足才算通过。

另有一个「偶发空档后恢复」用例：必须照样出块（退避不误伤正常路径）。

用法：python tools/_test_capture_backoff.py
"""
import os
import sys
import threading
import time

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

import audio_engine as ae                                       # noqa: E402


class _Q:
    """极简队列替身（只有 put_nowait / __len__）。"""
    def __init__(self):
        self._d = []

    def put_nowait(self, x):
        self._d.append(x)
        if len(self._d) > 8:
            self._d.pop(0)

    def __len__(self):
        return len(self._d)


class _EmptyRec:
    def __init__(self):
        self.calls = 0

    def record(self, numframes=0):
        self.calls += 1
        return None


class _BreathingRec:
    """空 N 次后给一块真数据（模拟网络偶发空档后恢复正常）。"""
    def __init__(self, empty_n=20):
        self.calls = 0
        self.empty_n = empty_n
        self.gave = 0

    def record(self, numframes=0):
        self.calls += 1
        if self.calls % (self.empty_n + 1) == 0:
            self.gave += 1
            return np.zeros((int(numframes or 1024), 2), dtype=np.float32)
        return None


class _Mic:
    def __init__(self, rec):
        self._rec = rec

    def recorder(self, samplerate=48000, channels=2):
        return _Ctx(self._rec)


class _Ctx:
    def __init__(self, rec):
        self._rec = rec

    def __enter__(self):
        return self._rec

    def __exit__(self, *a):
        return False


def _make_engine(gen, stop):
    """构造只满足 `_capture_loop` 依赖的最小引擎替身。"""
    eng = object.__new__(ae.AudioEngine)
    eng.state = ae.AudioState()
    eng._stop = stop
    eng._gen = gen
    eng._cap_gen = gen
    eng.energy_scale = 1.0
    eng._genre_lock = threading.Lock()
    eng._genre_sr = 48000
    eng._genre_buf = ae._AudioRing(48000 * 12)
    eng._ndi_q = _Q()
    return eng


def _run(rec, gen, secs):
    stop = threading.Event()
    eng = _make_engine(gen, stop)
    th = threading.Thread(
        target=eng._capture_loop,
        args=(_Mic(rec), True, 48000, gen, False),
        daemon=True)
    t0 = time.perf_counter()
    c0 = time.process_time()
    th.start()
    time.sleep(secs)
    stop.set()
    th.join(3.0)
    wall = time.perf_counter() - t0
    cpu = time.process_time() - c0
    return wall, cpu


def main():
    # 让 _Analyzer 变轻（本测试只关心采集循环的节奏，不关心分析）
    class _LightAn:
        def __init__(self, *a, **k):
            self.energy_scale = 1.0

        def feed(self, data, st_):
            pass

    ae._Analyzer = _LightAn

    ok = True

    print("① record() 永远返回空：应当退避，不能空转")
    rec = _EmptyRec()
    wall, cpu = _run(rec, object(), 1.2)
    print("   record 调用 = %d 次 / 墙钟 %.2fs（= %.0f 次/秒）"
          % (rec.calls, wall, rec.calls / max(wall, 1e-6)))
    print("   CPU 时间 = %.3fs（占墙钟 %.1f%%）"
          % (cpu, (cpu / max(wall, 1e-6)) * 100))
    if rec.calls > 20000:
        print("   ✗ 调用次数过多（%d）—— 疑似仍在空转" % rec.calls)
        ok = False
    else:
        print("   ✓ 调用次数受控（%d ≤ 20000）" % rec.calls)
    if cpu > wall * 0.5:
        print("   ✗ CPU 占比过高（%.1f%%）—— 没在睡"
              % ((cpu / max(wall, 1e-6)) * 100))
        ok = False
    else:
        print("   ✓ CPU 占比低（%.1f%% ⇒ 在退避 sleep）"
              % ((cpu / max(wall, 1e-6)) * 100))

    print("")
    print("② 偶发空档（每 20 次空给 1 块真数据）：必须照常出块")
    rec2 = _BreathingRec(empty_n=20)
    gen2 = object()
    wall2, cpu2 = _run(rec2, gen2, 1.5)
    print("   record 调用 = %d 次，给出真块 = %d 次" % (rec2.calls, rec2.gave))
    # 出块会写进 _genre_buf（间接证据）；这里只要"确实有正常块流过"
    if rec2.gave >= 2:
        print("   ✓ 空档后仍正常取到数据块（%d 块）" % rec2.gave)
    else:
        print("   ✗ 正常数据块太少（%d）—— 退避可能误伤正常路径" % rec2.gave)
        ok = False

    print("")
    print("结果：" + ("全部通过 ✓" if ok else "未通过 ✗"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
