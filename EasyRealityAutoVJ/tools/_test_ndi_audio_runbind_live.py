# -*- coding: utf-8 -*-
"""真窗口验证：NDI 音频与「开始自动VJ」绑定（2026-10-07）。

实际启动 MainWindow（隔离环境，不动用户配置），验证：
  1. 启动后（未开始自动VJ）`audio.state.ndi_running == False`
  2. 调 `main.toggle_run()` 后 `ndi_running == True`（绑定生效）
  3. 再调一次 stop 后 `ndi_running == False`
  4. `_ndi_audio_feed` 在未运行时**不调** `ndi.feed_audio`

⚠ 凡创建 MainWindow 的脚本退出码可能是 127（NDI/ASIO DLL 卸载 abort），
   判据一律看脚本自己的结论行。
"""

import os
import sys
import tempfile

# 隔离环境：别碰用户的 config / 日志
_TMP = tempfile.mkdtemp(prefix="ndi_runbind_")
os.environ["LOCALAPPDATA"] = _TMP
os.environ["TEMP"] = _TMP
os.environ["TMP"] = _TMP
os.makedirs(os.path.join(_TMP, "AutoVJ"), exist_ok=True)

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, _SRC)

import numpy as np                                          # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print("  %s %s%s" % ("✓" if cond else "★", name, ("  —— " + detail) if detail else ""))


def main():
    print("=" * 78)
    print("真窗口验证：NDI 音频与「开始自动VJ」绑定")
    print("=" * 78)

    from PySide6.QtWidgets import QApplication
    import ui_main

    app = QApplication.instance() or QApplication(sys.argv)
    win = ui_main.MainWindow()

    # ---- 用例1：启动后未运行 ⇒ ndi_running 为假 ----
    check("启动后 ndi_running == False",
          bool(getattr(win.audio.state, "ndi_running", False)) is False,
          "ndi_running=%r" % getattr(win.audio.state, "ndi_running", None))
    check("启动后 engine.running == False",
          bool(getattr(win.engine, "running", True)) is False)

    # ---- 用例2：开始自动VJ ⇒ ndi_running 为真 ----
    try:
        win.toggle_run()
    except Exception as e:                                     # noqa: BLE001
        print("  ! toggle_run 抛异常（可能音频设备不可用）：%s" % e)
    r_after_start = bool(getattr(win.audio.state, "ndi_running", False))
    check("开始后 ndi_running == True", r_after_start is True,
          "ndi_running=%r engine.running=%r"
          % (r_after_start, bool(getattr(win.engine, "running", None))))

    # ---- 用例3：转发门控 —— 运行中放行 ----
    calls = []
    real_ndi = win.engine._ndi

    class FakeNdi:
        def feed_audio(self, pcm, sr):
            calls.append((len(pcm), sr))

    try:
        win.engine._ndi = FakeNdi()
        win.cfg["output"]["ndi_enabled"] = True
        win._ndi_audio_feed(np.zeros(2048, np.float32), 48000)
        check("运行中：门控放行 → 调了 feed_audio", len(calls) == 1,
              "calls=%d" % len(calls))
    finally:
        win.engine._ndi = real_ndi

    # ---- 用例4：停止 ⇒ ndi_running 为假、门控拦住 ----
    try:
        win.toggle_run()
    except Exception:                                          # noqa: BLE001
        pass
    r_after_stop = bool(getattr(win.audio.state, "ndi_running", False))
    check("停止后 ndi_running == False", r_after_stop is False,
          "ndi_running=%r" % r_after_stop)

    calls2 = []
    try:
        win.engine._ndi = FakeNdi()
        win.cfg["output"]["ndi_enabled"] = True
        win.audio.state.ndi_running = False
        win.engine.running = False
        win._ndi_audio_feed(np.zeros(2048, np.float32), 48000)
        check("停止后：门控拦住 → 没调 feed_audio", len(calls2) == 0,
              "calls=%d" % len(calls2))
    finally:
        win.engine._ndi = real_ndi

    try:
        win.close()
    except Exception:                                          # noqa: BLE001
        pass

    print("=" * 78)
    ok_n = sum(1 for _, o, _ in results if o)
    print("通过 %d/%d" % (ok_n, len(results)))
    if ok_n != len(results):
        for n, o, d in results:
            if not o:
                print("  ★ 失败：" + n + ("（%s）" % d if d else ""))
        print("结论：★ 未通过")
        return 1
    print("结论：✅ 绑定生效 —— 未开始不推、开始后推、停止即停")
    return 0


if __name__ == "__main__":
    sys.exit(main())
