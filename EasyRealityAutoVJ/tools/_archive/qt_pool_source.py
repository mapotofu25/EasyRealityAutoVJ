# -*- coding: utf-8 -*-
"""定位「开始 VJ 后出现的 ~8 条 Qt6Core 线程（合计约 0.76 核）」是谁在投任务。

已知：
  · 只有"开始 VJ"之后才出现（未开始 VJ 时只有 1 条 = engine_thread）
  · 停止 VJ 后不立即消失（QThreadPool 的 worker 默认 30 秒才过期，属正常）
  · 把 QThreadPool.maxThreadCount 改小，这部分 CPU 从 0.53 降到 0.15 → 与 QThreadPool 相关
  · 代码里**没有任何** QtConcurrent / QThreadPool 调用 → 只能逐项排除

做法：**边泵 Qt 事件循环边采样**（否则界面根本没在跑，第一版实验就栽在这里），
     逐项关闭 预览 → 输出 → 状态栏 → 引擎 → 音频，看 Qt6 线程在哪一步掉下来。

用法： python tools/qt_pool_source.py [预热秒] [每步秒] [关完后额外等待秒]
"""
import os
import sys
import time
import shutil
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

try:
    import main  # noqa: F401
except Exception:
    pass

from collections import Counter  # noqa: E402

out = []


def w(s=""):
    print(s, flush=True)
    out.append(str(s))


def sample_pump(app, sec, tag):
    import thread_attrib as ta
    a = ta.enum_threads()
    pa = ta._proc_cpu()
    t0 = time.perf_counter()
    while time.perf_counter() < t0 + sec:
        app.processEvents()
        time.sleep(0.002)
    el = time.perf_counter() - t0
    b = ta.enum_threads()
    pb = ta._proc_cpu()

    rows = []
    for tid, s0 in a.items():
        s1 = b.get(tid)
        if s1 is not None:
            rows.append((tid, max(0.0, s1 - s0) / el))
    rows.sort(key=lambda x: -x[1])
    mods = ta.thread_modules()
    tot = (pb - pa) / el if (pa is not None and pb is not None) else sum(r[1] for r in rows)

    qt = [(tid, c) for tid, c in rows if "qt6" in (mods.get(tid) or "").lower()]
    py = sum(c for tid, c in rows if "ucrtbase" in (mods.get(tid) or "").lower()
             or "python" in (mods.get(tid) or "").lower())
    cv = sum(c for tid, c in rows if "cv2" in (mods.get(tid) or "").lower())
    w("   %-30s 进程 %5.2f 核   Qt6 %5.2f 核（%2d 条）  Python %5.2f   OpenCV %5.2f"
      % (tag, tot, sum(c for _, c in qt), len(qt), py, cv))
    if qt:
        w("        最大三条: %s" % ", ".join("%.3f" % c for _, c in qt[:3]))
    return tot, sum(c for _, c in qt), len(qt)


def main():
    warm = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0
    sec = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
    wait = float(sys.argv[3]) if len(sys.argv) > 3 else 40.0
    w("=" * 96)
    w("Qt6 线程任务来源定位   %s" % time.strftime("%H:%M:%S"))
    w("=" * 96)

    from PySide6.QtWidgets import QApplication
    app = QApplication([])

    src_cfg = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ", "config.json")
    tmp_cfg = os.path.join(ROOT, "config.json")
    made = False
    if os.path.exists(src_cfg) and not os.path.exists(tmp_cfg):
        shutil.copy2(src_cfg, tmp_cfg)
        made = True
    win = None
    try:
        import config as cfgmod
        import theme
        cfg = cfgmod.Config()
        theme.apply(app, cfg["theme"])
        from ui_main import MainWindow
        win = MainWindow()

        # 播音乐（让音频链路真的在跑）
        try:
            import soundcard as sc
            import av
            import numpy as np
            flac = r"W:\临时结界相关\2of\tofu\Rolling Contact - Euclid Number.flac"
            if os.path.exists(flac):
                c = av.open(flac)
                st = c.streams.audio[0]
                rsp = av.AudioResampler(format="fltp", layout="stereo", rate=48000)
                fl = []
                for fr in c.decode(st):
                    for f in rsp.resample(fr):
                        fl.append(f.to_ndarray().T.astype(np.float32))
                    if len(fl) * 1024 > 48000 * 150:
                        break
                c.close()
                pcm = np.concatenate(fl, axis=0) * 0.35
                spk = next((s for s in sc.all_speakers() if "Realtek" in s.name), None) or sc.default_speaker()

                def _play():
                    try:
                        with spk.player(samplerate=48000, channels=2) as p:
                            p.play(pcm)
                    except Exception:
                        pass
                threading.Thread(target=_play, daemon=True).start()
        except Exception as e:  # noqa
            w("（没播成音乐：%s）" % str(e)[:60])

        try:
            win.toggle_run()
        except Exception as e:  # noqa
            w("toggle_run 失败：%s" % e)

        w("预热 %.0f 秒…" % warm)
        t = time.perf_counter()
        while time.perf_counter() < t + warm:
            app.processEvents()
            time.sleep(0.004)

        w("")
        w("== 逐项累加关闭（每一步都保留前面的关闭）==")
        sample_pump(app, sec, "A 基线（全都开着）")

        import panels
        import output_window as owmod
        orig_preview = panels.PreviewPanel.set_frame
        orig_out = owmod.OutputWindow.set_frame

        panels.PreviewPanel.set_frame = lambda self, img: None
        sample_pump(app, sec, "B +关预览 set_frame")

        owmod.OutputWindow.set_frame = lambda self, img: None
        sample_pump(app, sec, "C +关输出 set_frame")

        try:
            win.status_timer.stop()
        except Exception:
            pass
        sample_pump(app, sec, "D +停状态栏 timer（HUD）")

        try:
            win.engine.timer.stop()
        except Exception:
            pass
        sample_pump(app, sec, "E +停引擎 60fps timer")

        try:
            win.audio.stop()
        except Exception:
            pass
        sample_pump(app, sec, "F +停音频采集")

        # 恢复
        panels.PreviewPanel.set_frame = orig_preview
        owmod.OutputWindow.set_frame = orig_out

        # ---- 关键：等 QThreadPool worker 自然过期，看是否"泄漏" ----
        if wait > 0:
            w("")
            w("== 停止后等待 %.0f 秒再采样（QThreadPool worker 默认 30 秒过期）==" % wait)
            t = time.perf_counter()
            while time.perf_counter() < t + wait:
                app.processEvents()
                time.sleep(0.02)
            sample_pump(app, sec, "G 等待后（判断是否泄漏）")

        w("")
        w("读法：哪一步 Qt6 核数掉下来，那一步关掉的东西就是任务来源；")
        w("      G 步若明显下降 → 只是池线程正常保留，不是泄漏。")
    finally:
        if win is not None:
            try:
                win.close()
            except Exception:
                pass
        if made:
            try:
                os.remove(tmp_cfg)
            except Exception:
                pass
    open(os.path.join(ROOT, "_qt_pool_source.txt"), "w", encoding="utf-8").write("\n".join(out))
    os._exit(0)


if __name__ == "__main__":
    main()
