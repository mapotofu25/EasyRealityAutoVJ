# -*- coding: utf-8 -*-
r"""CPU 占用拆解：这个软件到底吃你几个核、吃在哪。

用户的核心诉求不是"帧率更高"，而是**把 CPU 解放出来**（CPU 要同时跑 VDJ、直播、录制）。
所以要回答两件事：
  ① 这个进程总共占了多少（换算成任务管理器口径的百分比）
  ② 这些占用花在哪些线程上（解码？合成？后台 AI 识别？界面？）

采样方式：Windows `GetThreadTimes` 读每个线程的内核+用户时间（结合 Python 线程名），
         **同时**用 `GetProcessTimes` 读进程合计 —— 只统计 Python 线程会漏掉
         OpenCV / onnxruntime 的内部线程池（cv2 默认就开 CPU 核数条）。

场景：跑**真实的 MainWindow**（完整初始化 + 音频采集 + 曲风/指纹识别线程），
     调用 `toggle_run()` 真的开始 VJ，并播一段音乐让音频链路有数据。

用法：venv\Scripts\python.exe tools\cpu_profile.py [预热秒] [采样秒]
输出：P:\AutoVJ\_cpu_profile.txt
"""
import ctypes
import os
import shutil
import sys
import threading
import time
from ctypes import wintypes

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

# 走**真实入口的模块级代码**（main.py 顶部会设 OPENBLAS_NUM_THREADS 等）——
# 否则本脚本测的是"没做线程限制"的版本，与用户实际跑的 exe 不一致。
# 必须在 import numpy 之前执行，所以放在这里（此时还没引 numpy）。
try:
    import main  # noqa: F401
except Exception as _e:
    print("注意：import main 失败（环境变量限制可能未生效）：%s" % _e)

k32 = ctypes.windll.kernel32
# ⚠ 必须显式声明 restype：默认 c_int 会把 64 位 HANDLE 截断成 32 位，
# 拿到无效句柄 → GetThreadTimes 失败、CloseHandle 可能直接崩（踩过：Segmentation fault）
k32.GetCurrentProcess.restype = wintypes.HANDLE
k32.OpenThread.restype = wintypes.HANDLE
k32.OpenProcess.restype = wintypes.HANDLE
k32.CloseHandle.argtypes = [wintypes.HANDLE]
THREAD_QUERY_INFORMATION = 0x0040
PROCESS_QUERY_INFORMATION = 0x0400

out = []


def w(s=""):
    out.append(str(s))
    print(s, flush=True)


class FILETIME(ctypes.Structure):
    _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]


_LPFILETIME = ctypes.POINTER(FILETIME)
k32.GetProcessTimes.argtypes = [wintypes.HANDLE, _LPFILETIME, _LPFILETIME, _LPFILETIME, _LPFILETIME]
k32.GetThreadTimes.argtypes = [wintypes.HANDLE, _LPFILETIME, _LPFILETIME, _LPFILETIME, _LPFILETIME]
k32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]


def _ft(v):
    return (v.hi << 32) | v.lo


def _times(handle):
    c, e, k, u = FILETIME(), FILETIME(), FILETIME(), FILETIME()
    ok = k32.GetThreadTimes(handle, ctypes.byref(c), ctypes.byref(e),
                            ctypes.byref(k), ctypes.byref(u))
    if not ok:
        return None
    return (_ft(k) + _ft(u)) / 1e7


def thread_cpu_sec(tid):
    h = k32.OpenThread(THREAD_QUERY_INFORMATION, False, tid)
    if not h:
        return None
    try:
        return _times(h)
    finally:
        k32.CloseHandle(h)


def proc_cpu_sec():
    p = k32.GetCurrentProcess()
    c, e, k, u = FILETIME(), FILETIME(), FILETIME(), FILETIME()
    if not k32.GetProcessTimes(p, ctypes.byref(c), ctypes.byref(e),
                               ctypes.byref(k), ctypes.byref(u)):
        return None
    return (_ft(k) + _ft(u)) / 1e7


def sample():
    """枚举**进程内全部**线程的 CPU 时间（含 OpenCV/FFmpeg/OpenBLAS 等 C 库线程）。

    ⚠ 以前只用 `threading.enumerate()`，只能看到 Python 线程 —— 于是「进程 4.8 核
    但线程相加只有 0.7 核」那个 4 核的黑洞谁也说不清。改用 Toolhelp32 快照枚举，
    C 库线程也能落到具体 TID 上（并标注起始模块，看它属于哪个 DLL）。
    """
    py = {getattr(t, "native_id", None): t.name for t in threading.enumerate()}
    res = {}
    try:
        import thread_attrib as ta
        for tid, cpu in ta.enum_threads().items():
            res[tid] = (py.get(tid, "非Python"), cpu)
    except Exception:
        # 退回旧方式（只 Python 线程）
        for t in threading.enumerate():
            tid = getattr(t, "native_id", None)
            if tid is None:
                continue
            v = thread_cpu_sec(tid)
            if v is not None:
                res[tid] = (t.name, v)
    return res


def thread_mods():
    try:
        import thread_attrib as ta
        return ta.thread_modules()
    except Exception:
        return {}


def report(tag, a, b, pa, pb, sec, mods=None):
    cores = os.cpu_count() or 1
    data = []
    for tid, (name, s0) in a.items():
        v = b.get(tid, (name, None))[1]
        if v is None:
            continue
        data.append((max(0.0, v - s0), name, tid))
    data.sort(reverse=True)
    w("")
    w("-- %s（采样 %.1f 秒）--" % (tag, sec))
    if pa is not None and pb is not None and sec > 0:
        # ⚠ 必须除以采样时长：GetProcessTimes 给的是「这段时间累计消耗的 CPU 秒」，
        # 直接当 ms/秒 用会放大 sec 倍（踩过：显示成 67 核）
        cores_used = (pb - pa) / sec
        w("   【进程合计】%.0f ms/秒  = 占 **%.2f 个核**  = 占整机 **%.1f%%**（%d 核）"
          % (cores_used * 1000, cores_used, cores_used * 100 / cores, cores))
        w("   任务管理器口径就是这个百分比；换到 4 核机器同负载约 %.0f%%"
          % (min(100.0, cores_used * 100 / 4)))
        w("   （下面单条线程相加 %.0f ms/秒 = %.2f 核，差值是采样期间新建/退出的线程与"
          % (sum(i[0] for i in data) * 1000 / sec, sum(i[0] for i in data) / sec))
        w("     GetThreadTimes 抓不到的系统线程）")
    w("")
    w("   %-30s %10s %9s %9s  %s" % ("线程", "ms/秒", "占1核%", "占整机%", "起始模块"))
    for used, name, tid in data:
        per = used / sec          # 每秒消耗的 CPU 秒 = 占几个核
        if per * 1000 < 1.0:
            continue
        w("   %-30s %10.1f %8.1f%% %8.2f%%  %s"
          % ("%s(%d)" % (name, tid), per * 1000, per * 100, per * 100 / cores,
             (mods or {}).get(tid, "")))
    return sum(i[0] for i in data) / sec


def main():
    warm = float(sys.argv[1]) if len(sys.argv) > 1 else 22.0
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
    cores = os.cpu_count() or 1
    w("=" * 86)
    w("CPU 占用拆解（真实 MainWindow）   %s   本机 %d 核" % (time.strftime("%H:%M:%S"), cores))
    w("=" * 86)

    src_cfg = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ", "config.json")
    tmp_cfg = os.path.join(ROOT, "config.json")
    made_tmp = False
    if os.path.exists(src_cfg) and not os.path.exists(tmp_cfg):
        shutil.copy2(src_cfg, tmp_cfg)
        made_tmp = True
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication([])
        import config as cfgmod
        import theme
        cfg = cfgmod.Config()
        theme.apply(app, cfg["theme"])
        from ui_main import MainWindow
        w("构造 MainWindow…")
        t0 = time.perf_counter()
        win = MainWindow()
        w("  MainWindow 构造 %.2fs" % (time.perf_counter() - t0))

        # 播一段真实音乐（让音频链路 + 曲风识别真的在干活）
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
                spk = None
                for s in sc.all_speakers():
                    if "Realtek" in s.name:
                        spk = s
                        break
                spk = spk or sc.default_speaker()

                def _play():
                    try:
                        with spk.player(samplerate=48000, channels=2) as p:
                            p.play(pcm)
                    except Exception:
                        pass
                threading.Thread(target=_play, daemon=True).start()
                w("  播放测试音频：%s" % spk.name)
        except Exception as e:  # noqa
            w("  （没播成测试音频：%s）" % str(e)[:70])

        w("开始 VJ（toggle_run）…")
        try:
            win.toggle_run()
        except Exception as e:  # noqa
            w("  toggle_run 失败：%s" % e)

        # 手动跑 Qt 事件循环（不用 app.exec，方便主线程采样）
        def pump(until):
            while time.perf_counter() < until:
                app.processEvents()
                time.sleep(0.004)

        w("预热 %.0f 秒（等模型加载、解码稳定）…" % warm)
        pump(time.perf_counter() + warm)

        pa, a = proc_cpu_sec(), sample()
        t1 = time.perf_counter()
        pump(t1 + secs)
        b, pb = sample(), proc_cpu_sec()
        report("真实运行（开始 VJ 后稳定态）", a, b, pa, pb, time.perf_counter() - t1, thread_mods())

        w("")
        w("=" * 86)
        w("怎么看这张表")
        w("=" * 86)
        w("· **这张表现在包含 C 库线程**（Toolhelp32 枚举）：以前只列 Python 线程，")
        w("  于是「进程 4.8 核 / 线程相加 0.7 核」那个 4 核黑洞没人说得清 —— 现在能落到具体 TID。")
        w("· 无名 Thread-N = 解码线程（每个活动图层一条）；autovj-audio/genre/recognize = 音频与后台 AI")
        w("· 「起始模块」= 该线程从哪个 DLL 起跑（ffmpeg? cv2? onnxruntime? python? 系统?）")
        w("")
        w("可以搬到 GPU 的优先级（按能省多少排）：")
        w("  1) 解码线程 —— 最大块；DXT 解压可直接交给 GPU（BC 纹理）")
        w("  2) autovj-genre 的 ONNX 推理 —— 换 DirectML 就能走显卡")
        w("  3) 合成/输出（engine 线程 + 界面预览缩放）")
        try:
            win.close()
        except Exception:
            pass
    finally:
        if made_tmp:
            try:
                os.remove(tmp_cfg)
            except Exception:
                pass
    open(r"P:\AutoVJ\_cpu_profile.txt", "w", encoding="utf-8").write("\n".join(out))
    os._exit(0)


if __name__ == "__main__":
    main()
