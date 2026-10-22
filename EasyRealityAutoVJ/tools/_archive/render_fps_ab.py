# -*- coding: utf-8 -*-
"""渲染帧率 60 vs 30 的**同进程** A/B —— 消除进程间噪声。

为什么这么做：跨进程交替测时，同一配置的读数能差 1.2 个核（本机有 30+ 虚拟显示器，
属于远程/开发环境），0.7 核级的信号完全被淹没。
同进程内切换帧率、连续采样，才能看出真实差异。

采样时只盯两件事：
  · 进程总核数（会受解码干扰）
  · **Qt6 / 引擎渲染线程的核数**（这才是渲染帧率直接影响的部分）

用法： python tools/render_fps_ab.py [每段秒数] [轮数]
"""
import os
import sys
import time
import shutil

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

try:
    import main  # noqa: F401
except Exception:
    pass

out = []


def w(s=""):
    print(s, flush=True)
    out.append(str(s))


def sample_pump(app, sec):
    """边泵事件循环边采样，返回 (进程核数, Qt6核数, Python核数, 解码线程核数)"""
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
    mods = ta.thread_modules()

    rows = []
    for tid, s0 in a.items():
        s1 = b.get(tid)
        if s1 is not None:
            rows.append((tid, max(0.0, s1 - s0) / el))
    tot = (pb - pa) / el if (pa is not None and pb is not None) else sum(r[1] for r in rows)
    qt = sum(c for tid, c in rows if "qt6" in (mods.get(tid) or "").lower())
    py = sum(c for tid, c in rows
             if "ucrtbase" in (mods.get(tid) or "").lower()
             or "python" in (mods.get(tid) or "").lower())
    cv = sum(c for tid, c in rows if "cv2" in (mods.get(tid) or "").lower())
    return tot, qt, py, cv


def main():
    sec = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    w("=" * 84)
    w("渲染帧率 60 vs 30 —— 同进程 A/B   %s" % time.strftime("%H:%M:%S"))
    w("=" * 84)

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
        win.toggle_run()
        w("预热 15 秒（等解码稳定、图层选好素材）…")
        t = time.perf_counter()
        while time.perf_counter() < t + 15:
            app.processEvents()
            time.sleep(0.004)

        w("")
        w("   %-14s %10s %10s %10s %10s" % ("段", "进程(核)", "Qt6(核)", "Python(核)", "OpenCV(核)"))
        res = {60: [], 30: []}
        for r in range(rounds):
            for fps, idx in ((60, 0), (30, 1)):
                win.set_render_fps(idx)
                t = time.perf_counter()
                while time.perf_counter() < t + 3:      # 等切换生效
                    app.processEvents()
                    time.sleep(0.005)
                tot, qt, py, cv = sample_pump(app, sec)
                res[fps].append((tot, qt))
                w("   %-14s %10.2f %10.2f %10.2f %10.2f"
                  % ("第%d轮 %dfps" % (r + 1, fps), tot, qt, py, cv))

        w("")
        w("=" * 84)
        w("汇总（同进程，直接比较）")
        w("=" * 84)
        for fps in (60, 30):
            tots = [x[0] for x in res[fps]]
            qts = [x[1] for x in res[fps]]
            w("   %3dfps   进程 %s 核   渲染(Qt6) %s 核"
              % (fps, ["%.2f" % v for v in tots], ["%.2f" % v for v in qts]))
        if all(res[60]) and all(res[30]):
            t60 = sum(x[0] for x in res[60]) / len(res[60])
            t30 = sum(x[0] for x in res[30]) / len(res[30])
            q60 = sum(x[1] for x in res[60]) / len(res[60])
            q30 = sum(x[1] for x in res[30]) / len(res[30])
            w("")
            w("   进程：60fps %.2f → 30fps %.2f   （省 %.2f 核）" % (t60, t30, t60 - t30))
            w("   渲染：60fps %.2f → 30fps %.2f   （省 %.2f 核）" % (q60, q30, q60 - q30))
            w("")
            w("   渲染那行才是帧率直接控制的量；进程那行还夹着解码等与帧率无关的开销。")
    finally:
        if win is not None:
            try:
                win.close()
            except Exception:
                pass
        if made:
            try:
                os.remove(tmp_cfg)
            except OSError:
                pass
    open(os.path.join(ROOT, "_render_fps_ab.txt"), "w", encoding="utf-8").write("\n".join(out))
    os._exit(0)


if __name__ == "__main__":
    main()
