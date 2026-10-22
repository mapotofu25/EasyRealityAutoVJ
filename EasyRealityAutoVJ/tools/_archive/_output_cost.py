"""开 Spout / NDI 的 CPU 代价（真实引擎，运行中顺序切换）

为什么要单独量：记忆里只有「Spout 上传一帧 1.91ms、NDI 无接收端 ~0ms」这种**单帧**数字，
换算成整机口径（核当量）还得实测。而且这两条路本质不同：
  · Spout 走 CPU 共享内存（sendImage）→ 每帧一次全画布拷贝 + 共享内存写入
  · NDI 要在发送端编码（SpeedHQ/H.264）+ 额外发音频 → 开销与「有没有接收端」有关

做法：一个进程里跑真实 MainWindow + 引擎，预热到解码缓存填满（基线不稳会误判），
然后**运行中切换配置**（`cfg["output"]["spout_enabled"]` / `ndi_enabled` 是每帧读的），
按「都关 → 只 Spout → 只 NDI → 都开 → 回到都关」顺序各量一段 ——
最后一阶段用来检验有没有漂移（顺序效应）。

⚠ 只采进程 CPU（不做线程枚举），自身开销可忽略。

用法：venv\\Scripts\\python.exe tools\\_output_cost.py [每阶段秒数=60] [预热秒=90]
"""

import os
import shutil
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

try:
    import main  # noqa: F401  与真实运行一致
except Exception:
    pass

import ctypes
import ctypes.wintypes as wt

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.c_void_p] * 4
k32.GetCurrentProcess.restype = wt.HANDLE
NCORE = os.cpu_count() or 4
OUT = []


class FILETIME(ctypes.Structure):
    _fields_ = [("lo", wt.DWORD), ("hi", wt.DWORD)]


def w(s=""):
    print(s, flush=True)
    OUT.append(str(s))


def proc_cpu():
    c, e, k, u = FILETIME(), FILETIME(), FILETIME(), FILETIME()
    if not k32.GetProcessTimes(k32.GetCurrentProcess(), ctypes.byref(c), ctypes.byref(e),
                               ctypes.byref(k), ctypes.byref(u)):
        return 0.0
    return ((k.hi << 32 | k.lo) + (u.hi << 32 | u.lo)) / 1e7


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    warm = float(sys.argv[2]) if len(sys.argv) > 2 else 90.0

    src_cfg = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "config.json")
    tmp_cfg = os.path.join(ROOT, "config.json")
    if os.path.exists(src_cfg) and not os.path.exists(tmp_cfg):
        shutil.copy2(src_cfg, tmp_cfg)

    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    import config as cfgmod
    import theme
    cfg = cfgmod.Config()
    theme.apply(app, cfg["theme"])
    from ui_main import MainWindow
    win = MainWindow()
    w("MainWindow 就绪")

    try:
        import soundcard as sc
        import av
        import numpy as np
        flac = r"W:\临时结界相关\2of\tofu\Rolling Contact - Euclid Number.flac"
        if os.path.exists(flac):
            c = av.open(flac)
            st = c.streams.audio[0]
            res = av.AudioResampler(format="fltp", layout="stereo", rate=48000)
            frames = []
            for fr in c.decode(st):
                for f in res.resample(fr):
                    frames.append(f.to_ndarray().T.astype(np.float32))
                if len(frames) * 1024 > 48000 * 60:
                    break
            c.close()
            pcm = np.concatenate(frames, axis=0) * 0.35
            spk = sc.default_speaker()

            def _play():
                try:
                    with spk.player(samplerate=48000, channels=2) as p:
                        p.play(pcm)
                except Exception:
                    pass
            threading.Thread(target=_play, daemon=True).start()
            w("播放测试音频：%s" % spk.name)
    except Exception as e:                                            # noqa: BLE001
        w("（没播成测试音频：%s）" % str(e)[:60])

    win.toggle_run()
    w("已开始 VJ")

    def pump(until):
        while time.perf_counter() < until:
            app.processEvents()
            time.sleep(0.004)

    w("预热 %.0f 秒（等解码缓存填满）…" % warm)
    pump(time.perf_counter() + warm)

    w("")
    w("=" * 96)
    w("各阶段（每段 %.0f 秒，运行中切换配置）" % secs)
    w("=" * 96)
    w("   %-14s %10s %10s   %s" % ("阶段", "平均核", "后半段核", "发送端状态"))

    stages = [("① 都关", 0, 0), ("② 只 Spout", 1, 0), ("③ 只 NDI", 0, 1),
              ("④ 都开", 1, 1), ("⑤ 回到都关", 0, 0)]
    res = []
    for tag, sp, nd in stages:
        win.cfg["output"]["spout_enabled"] = bool(sp)
        win.cfg["output"]["ndi_enabled"] = bool(nd)
        pump(time.perf_counter() + 3)          # 让发送端有机会建立
        c0, t0 = proc_cpu(), time.perf_counter()
        marks = []
        while time.perf_counter() - t0 < secs:
            pump(time.perf_counter() + secs / 6.0)
            marks.append(proc_cpu())
        c1 = proc_cpu()
        wall = time.perf_counter() - t0
        avg = (c1 - c0) / wall
        # 后半段（后 3 个采样点）= 更接近稳态
        late = (marks[-1] - marks[-4]) / (wall * 3.0 / 6.0) if len(marks) >= 4 else avg
        e = win.engine
        state = "Spout=%s(失败:%s) NDI=%s(失败:%s)" % (
            "开" if e._spout is not None else "关", getattr(e, "_spout_fail", False),
            "开" if e._ndi is not None else "关", getattr(e, "_ndi_fail", False))
        w("   %-14s %9.2f 核 %9.2f 核   %s" % (tag, avg, late, state))
        res.append((tag, avg, late))

    w("")
    w("=" * 96)
    base = res[0][1]
    back = res[-1][1]
    w("基线（都关）%.2f 核；末尾回到都关 %.2f 核（漂移 %+.2f）" % (base, back, back - base))
    w("")
    for tag, avg, late in res[1:4]:
        w("  %-10s 相对基线 %+.2f 核（%+.1f%%）   稳态 %+.2f 核" % (tag, avg - base, (avg / base - 1) * 100, late - base))
    w("")
    w("读法：单帧成本 × 帧率 ≈ 核当量。60fps 下 1.0 ms/帧 = 0.06 核。")
    w("⚠ NDI 的开销与「有没有接收端」有关：本机没有接收端时，发送端可能跳过实际编码，")
    w("   所以这里的数字是**下限**；真接上另一台机器收流时会更贵。")

    with open(os.path.join(ROOT, "_output_cost.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    print("\n→ 已写入 _output_cost.txt")


if __name__ == "__main__":
    main()
