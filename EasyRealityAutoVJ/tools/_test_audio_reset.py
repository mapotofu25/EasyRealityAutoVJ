# -*- coding: utf-8 -*-
r"""验证「换音源时电平/能量必须清零」—— 用户报「切到没声音的输入后总电平卡住、画面继续放」。

## 被测行为
`audio_engine._capture_loop()` 是**三条音源路径（系统 / ASIO / 局域网）的唯一汇合点**，
它开头必须把 `AudioState` 里的电平/能量类字段清零。否则新音源在"还没出第一块数据"期间
（系统声音没在播 / 局域网没收到包 / 设备刚打开），界面与引擎读到的仍是**上一个音源的残值**
⇒ 总电平卡住不归零 + 画面被继续驱动。

## 反向复现（证明测试不是空过）
`--old` 模式：**不进入 `_capture_loop`**（= 模拟修复前的行为，即启动新音源时不清零）
⇒ 断言 `st.level` 应**保持**残值 0.8 ⇒ 测试判 FAIL。
修复后：进入 `_capture_loop` ⇒ `st.level` 应变成 0.0 ⇒ PASS。

## 用法
    venv\Scripts\python.exe tools/_test_audio_reset.py
    venv\Scripts\python.exe tools/_test_audio_reset.py --old
"""
import os
import sys
import threading

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

import audio_engine as AE                                        # noqa: E402

GEN = 999          # 故意让 _capture_loop 里的 `while self._gen == gen` 第一次就为假 ⇒ 立刻退出


class _FakeRec:
    """模拟一个「永远没有数据」的新音源（record 返回 None）。

    ⚠ 必须自带 `__enter__/__exit__`：`_capture_loop` 里写的是
      `with mic.recorder(samplerate=..., channels=2) as rec:` ——
      上下文管理器协议是加在 **recorder() 的返回值**上的，不是 mic 上。
    """
    last_qpc = 0

    def record(self, numframes=1024):
        return None

    def clear(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeMic:
    name = "FAKE_新音源_无数据"

    def recorder(self, samplerate=48000, channels=2):
        return _FakeRec()


def make_engine():
    """造一个最小可用的 AudioEngine（只填 _capture_loop 会碰到的属性）。"""
    eng = object.__new__(AE.AudioEngine)
    eng.state = AE.AudioState()
    eng._gen = 0                     # ≠ GEN ⇒ 循环体一次都不跑
    eng._genre_lock = threading.Lock()
    eng._genre_sr = 0
    eng._genre_buf = AE._AudioRing(48000 * 12)
    eng.energy_scale = 1.0
    return eng


def poison(st):
    """塞入"上一个音源"的残留电平（模拟切走局域网音频那一刻的状态）。"""
    with st.lock:
        st.level = 0.8
        st.bass = 0.7
        st.mid = 0.6
        st.high = 0.5
        st.energy = 0.75
        st.spectrum = [0.9] * 24
        st.energy_trend = 0.5
        st.vocalness = 0.4
        st.silent_sec = 0.0
        st.lv_db = -6.0
        st.segment = "peak"
        st.drop = True
        st.beat_active = True


def main():
    old = "--old" in sys.argv
    print("=" * 74)
    print("换音源清零验证" + ("   ★ --old 模式（应失败）" if old else ""))
    print("=" * 74)

    eng = make_engine()
    st = eng.state
    poison(st)
    print("\n① 切走前的残留值（模拟局域网音频有声音时的状态）")
    print("   level=%.2f energy=%.2f lv_db=%.1f segment=%s spectrum[0]=%.2f"
          % (st.level, st.energy, st.lv_db, st.segment, st.spectrum[0]))

    if old:
        print("\n② --old：**不进入** _capture_loop（等价于修复前「启动新音源时不清零」）")
    else:
        print("\n② 进入 _capture_loop（新音源 = 永远没数据；gen 立刻过期所以循环体不跑）")
        eng._capture_loop(_FakeMic(), mono=False, sr=48000, gen=GEN, wrap_native=False)

    print("\n③ 结果")
    print("   level=%.2f energy=%.2f lv_db=%.1f segment=%s spectrum[0]=%.2f"
          % (st.level, st.energy, st.lv_db, st.segment, st.spectrum[0]))
    print("   drop=%r beat_active=%r silent_sec=%.1f"
          % (st.drop, st.beat_active, st.silent_sec))

    cleared = (st.level == 0.0 and st.energy == 0.0 and st.lv_db == -80.0
               and st.segment == "none" and max(st.spectrum) == 0.0
               and st.drop is False and st.beat_active is False)
    print("\n" + "=" * 74)
    if old:
        ok = not cleared
        print("反向复现：%s（残值被保留 = 这正是用户看到的「总电平卡住」）"
              % ("✅ 成功" if ok else "❌ 未复现"))
    else:
        ok = cleared
        print("修复验证：%s —— 电平/能量/频谱/静音计时全部归零，"
              "引擎据此判定静音 ⇒ 画面不再被残留能量驱动"
              % ("✅ 通过" if ok else "❌ 未清零"))
    print("=" * 74)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
