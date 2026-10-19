# -*- coding: utf-8 -*-
"""定位那 ~9 条 Qt6Core.dll 线程（合计约 1.2 核，占进程 40%）是谁在用。

⚠ 上一版实验失效：采样时主线程 `time.sleep()` → **Qt 事件循环停了**，
   界面/预览/输出全都没在跑（MainThread 只有 0.009 核），所以"关预览/关输出"当然没差别。
本版**边泵事件循环边采样**（与真实 app.exec() 一致）。

分层：
  S0 只有 QApplication（不建窗口）
  S1 建好 MainWindow，**未**开始 VJ
  S2 开始 VJ（引擎 + 音频 + 解码都在跑）
  S3 停止 VJ（引擎停、音频停）
  S4 再限制 QThreadPool 后重测 S2

用法： python tools/qt_threads_probe.py [预热秒] [每层秒]
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
    """边泵 Qt 事件循环边采样（这样界面/预览/输出才真的在跑）"""
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
        if s1 is None:
            continue
        rows.append((tid, max(0.0, s1 - s0) / el))
    rows.sort(key=lambda x: -x[1])
    mods = ta.thread_modules()
    tot = (pb - pa) / el if (pa is not None and pb is not None) else sum(r[1] for r in rows)

    agg = Counter()
    qt = []
    for tid, cores in rows:
        m = (mods.get(tid) or "?").lower()
        if "qt6" in m:
            agg["Qt6"] += cores
            qt.append(cores)
        elif "openblas" in m:
            agg["OpenBLAS"] += cores
        elif "cv2" in m or "opencv" in m:
            agg["OpenCV"] += cores
        elif "python" in m or "ucrtbase" in m:
            agg["Python"] += cores
        else:
            agg[(mods.get(tid) or "?")] += cores

    w("-- %s（%.0f 秒）--" % (tag, el))
    w("   进程合计 %.2f 核（占整机 %.1f%%）" % (tot, tot / (os.cpu_count() or 1) * 100))
    w("   模块汇总: " + "  ".join("%s=%.2f" % (k, v) for k, v in agg.most_common(6)))
    if qt:
        w("   Qt6 线程 %d 条，合计 %.2f 核（单条 %.3f ~ %.3f），TID=%s"
          % (len(qt), sum(qt), min(qt), max(qt),
             ",".join(str(t) for t, c in rows if "qt6" in (mods.get(t) or "").lower())[:120]))
    else:
        w("   没有 Qt6 线程")
    return tot, agg.get("Qt6", 0.0)


def main():
    warm = float(sys.argv[1]) if len(sys.argv) > 1 else 18.0
    sec = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
    w("=" * 88)
    w("Qt6Core 线程归因 v2（边泵事件边采样）   %s" % time.strftime("%H:%M:%S"))
    w("=" * 88)

    res = []
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QThreadPool

    # ---------- S0 只有 QApplication ----------
    app = QApplication([])
    w("")
    w("[S0] 只有 QApplication（无窗口）")
    res.append(("S0 仅 QApplication", sample_pump(app, sec, "S0")))

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

        w("")
        w("[S1] 建好 MainWindow，未开始 VJ")
        res.append(("S1 未开始", sample_pump(app, sec, "S1")))

        # 播音乐
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
                    if len(fl) * 1024 > 48000 * 120:
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
                w("  已播音频 %.0f 秒" % (len(pcm) / 48000))
        except Exception as e:  # noqa
            w("  没播成：%s" % str(e)[:60])

        try:
            win.toggle_run()
        except Exception as e:  # noqa
            w("  toggle_run 失败：%s" % e)
        w("预热 %.0f 秒…" % warm)
        t = time.perf_counter()
        while time.perf_counter() < t + warm:
            app.processEvents()
            time.sleep(0.004)

        w("")
        w("[S2] 开始 VJ（引擎 + 音频 + 解码都在跑）")
        res.append(("S2 运行中", sample_pump(app, sec, "S2")))

        try:
            win.toggle_run()
        except Exception:
            pass
        time.sleep(1.5)
        w("")
        w("[S3] 停止 VJ")
        res.append(("S3 已停止", sample_pump(app, sec, "S3")))

        # ---------- S4 限制 QThreadPool 后重测运行中 ----------
        QThreadPool.globalInstance().setMaxThreadCount(2)
        try:
            win.toggle_run()
        except Exception:
            pass
        t = time.perf_counter()
        while time.perf_counter() < t + 6:
            app.processEvents()
            time.sleep(0.004)
        w("")
        w("[S4] 运行中，但 QThreadPool 限到 2 线程")
        res.append(("S4 QThreadPool=2", sample_pump(app, sec, "S4")))

        w("")
        w("=" * 88)
        w("对照汇总")
        w("=" * 88)
        w("   %-22s %12s %12s" % ("场景", "进程(核)", "Qt6(核)"))
        for name, (tot, qt) in res:
            w("   %-22s %12.2f %12.2f" % (name, tot, qt))
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
    open(os.path.join(ROOT, "_qt_threads.txt"), "w", encoding="utf-8").write("\n".join(out))
    os._exit(0)


if __name__ == "__main__":
    main()
