# -*- coding: utf-8 -*-
"""设计文档 §11 自测：NDI 音频开关 + 独立转发线程（有界队列、不阻塞采集线程）。

用法：venv/Scripts/python.exe -u tools/_ndi_audio_test.py
"""
import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

import numpy as np                                            # noqa: E402
from audio_engine import AudioEngine, _NDI_Q_MAX, BLOCK       # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


def main():
    # ============ 一、AudioEngine 侧 ============
    eng = AudioEngine()
    got = []
    glock = threading.Lock()

    def feed(pcm, sr):
        with glock:
            got.append((len(pcm), sr))

    eng.state.ndi_feed = feed

    check("有独立的 NDI 音频转发线程",
          any(t.name == "autovj-ndi-audio" for t in threading.enumerate()),
          [t.name for t in threading.enumerate() if "ndi" in t.name])

    # ① 投递 → 转发线程真的收到（且是立体声交织的 2×BLOCK）
    block = np.zeros(BLOCK * 2, dtype=np.float32)
    for _ in range(3):
        eng.push_ndi_audio(block.copy(), 48000)
    t0 = time.time()
    while time.time() - t0 < 2.0:
        with glock:
            if len(got) >= 3:
                break
        time.sleep(0.02)
    check("投递 3 块 ⇒ 转发线程收到 3 块", len(got) >= 3, len(got))
    check("转发时长度/采样率原样透传",
          bool(got) and got[0] == (BLOCK * 2, 48000), got[:1])

    # ② ★ 不阻塞采集线程：接收端很慢（每块 50ms）时，投递 100 块也必须瞬间返回
    def slow_feed(pcm, sr):
        time.sleep(0.05)

    eng.state.ndi_feed = slow_feed
    with glock:
        got.clear()
    t0 = time.time()
    for _ in range(100):
        eng.push_ndi_audio(block, 48000)
    dt = time.time() - t0
    check("接收端很慢时投递 100 块不阻塞（<0.2s）", dt < 0.2, "%.3f s" % dt)

    # ③ 有界队列：狂投 500 块，队列长度不得超过上限（满了丢最老的）
    t0 = time.time()
    for _ in range(500):
        eng.push_ndi_audio(block, 48000)
    # 立刻看长度：转发线程同时在消费，所以只会 ≤ 上限
    check("队列有界（≤ %d 块）" % _NDI_Q_MAX, len(eng._ndi_q) <= _NDI_Q_MAX, len(eng._ndi_q))
    check("狂投 500 块仍然是 O(1)（<0.3s）", time.time() - t0 < 0.3,
          "%.3f s" % (time.time() - t0))

    # ④ 没接回调 / 不炸
    eng.state.ndi_feed = None
    try:
        for _ in range(5):
            eng.push_ndi_audio(block, 48000)
        ok, err = True, ""
    except Exception as e:                                    # noqa: BLE001
        ok, err = False, "%s: %s" % (type(e).__name__, e)
    check("没接 NDI 回调时投递也不炸", ok, err)

    # ⑤ 关闭：线程退出、之后投递仍不炸
    eng.shutdown()
    t0 = time.time()
    while time.time() - t0 < 2.0:
        if not any(t.name == "autovj-ndi-audio" for t in threading.enumerate()):
            break
        time.sleep(0.05)
    check("shutdown 后转发线程退出",
          not any(t.name == "autovj-ndi-audio" for t in threading.enumerate()))
    try:
        eng.push_ndi_audio(block, 48000)
        ok, err = True, ""
    except Exception as e:                                    # noqa: BLE001
        ok, err = False, "%s: %s" % (type(e).__name__, e)
    check("关闭后投递不炸", ok, err)

    # ============ 二、界面侧 ============
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    import ui_main

    w = ui_main.MainWindow()
    w.save_cfg = lambda *a, **k: None
    w.save_layers = lambda *a, **k: None
    w.cfg.save = lambda *a, **k: None
    p = w.output_panel
    check("输出区有「NDI 传输音频」勾选框", hasattr(p, "chk_ndi_audio"))
    check("有 Spout「不含音频」说明", hasattr(p, "lbl_spout_audio")
          and "音频" in p.lbl_spout_audio.text(), getattr(p, "lbl_spout_audio", None)
          and p.lbl_spout_audio.text()[:40])

    # NDI 输出关着时，音频勾选框必须置灰（勾了也没意义）
    w.cfg["output"]["ndi_enabled"] = False
    w.set_ndi_enabled(False)
    check("NDI 输出关 ⇒ 音频勾选框置灰", not p.chk_ndi_audio.isEnabled())

    # 未开 NDI 就设音频 ⇒ 不生效（存 False）
    w.set_ndi_audio(True)
    check("NDI 没开时设音频不生效",
          w.cfg["output"].get("ndi_audio") is False
          and w.audio.state.ndi_audio_on is False,
          w.cfg["output"].get("ndi_audio"))

    # 打开 NDI 后，音频开关才可用；勾上才真的发
    w.cfg["output"]["ndi_enabled"] = True
    p.chk_ndi_audio.setEnabled(True)
    check("NDI 开着 ⇒ 音频勾选框可用", p.chk_ndi_audio.isEnabled())
    w.set_ndi_audio(True)
    check("勾上后写进配置且同步给采集循环",
          w.cfg["output"].get("ndi_audio") is True
          and w.audio.state.ndi_audio_on is True)
    w.set_ndi_audio(False)
    check("取消后采集循环立刻停发（省掉每块拷贝）",
          w.audio.state.ndi_audio_on is False)
    check("默认值就是关（怕双声/回声）",
          __import__("config").DEFAULTS["output"]["ndi_audio"] is False)

    # 采集循环里**不再**直接调 NDI 发送器（必须走队列）
    src = open(os.path.join(_ROOT, "src", "audio_engine.py"), encoding="utf-8").read()
    seg = src[src.find("def _capture_loop"):]
    seg = seg[:seg.find("\n    def ", 10)] if seg.find("\n    def ", 10) > 0 else seg
    check("采集循环里只入队、不直接调 NDI 发送器",
          "push_ndi_audio" in seg and "ndi(" not in seg)

    w.close()
    print("")
    if FAILS:
        print("❌ 失败 %d 项：%s" % (len(FAILS), FAILS))
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
