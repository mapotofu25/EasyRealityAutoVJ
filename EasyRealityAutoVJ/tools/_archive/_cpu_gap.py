"""进程 CPU 与线程归因之间的「缺口」诊断

背景：`cpu_profile.py` 实测「进程 2.26 个核，但线程相加只有 1.40 个核」—— 38% 找不到主人。
那段提示写的是「采样期间新建/退出的线程」，但一直没人验证过。本工具验证它：

  · **每 100ms 轮询一次**全部线程，把每个 TID 的累计 CPU 持续记下来 ——
    短命线程（采样期间创建又销毁）就不会被漏掉，取它最后一次出现的值；
  · 采样期间**新建**的 TID 按「从 0 计」累计（它整个生命周期都在区间内）；
  · 同时统计线程的**诞生 / 消失速率** —— 每秒几十个就说明某处在反复创建线程，
    那是最典型、也最容易优化的浪费；
  · 最后把「进程总 CPU」与「所有见过的线程之和」对齐，看还剩多少真缺口。

用法：venv\\Scripts\\python.exe tools\\_cpu_gap.py [预热秒=12] [采样秒=20] [轮询ms=100]
"""

import ctypes
import ctypes.wintypes as wt
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
    import main  # noqa: F401  与真实运行一致（线程数限制）
except Exception:
    pass

try:
    import thread_attrib as ta      # 复用它的「起始地址 → DLL」能力
except Exception:
    ta = None

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
NCORE = os.cpu_count() or 4
TH32CS_SNAPTHREAD = 0x00000004
THREAD_QUERY_INFORMATION = 0x0040
PID = os.getpid()
OUT = []


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wt.DWORD), ("dwHighDateTime", wt.DWORD)]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ThreadID", wt.DWORD),
                ("th32OwnerProcessID", wt.DWORD), ("tpBasePri", wt.LONG),
                ("tpDeltaPri", wt.LONG), ("dwFlags", wt.DWORD)]


k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
k32.Thread32First.argtypes = [wt.HANDLE, ctypes.POINTER(THREADENTRY32)]
k32.Thread32Next.argtypes = [wt.HANDLE, ctypes.POINTER(THREADENTRY32)]
k32.OpenThread.restype = wt.HANDLE
k32.OpenThread.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.GetThreadTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(FILETIME)] * 4
k32.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(FILETIME)] * 4
k32.GetCurrentProcess.restype = wt.HANDLE
_u32 = ctypes.WinDLL("user32", use_last_error=True)
_u32.GetGuiResources.argtypes = [wt.HANDLE, wt.DWORD]
_u32.GetGuiResources.restype = wt.DWORD


def w(s=""):
    print(s, flush=True)
    OUT.append(str(s))


def _ft(f):
    return (f.dwHighDateTime << 32 | f.dwLowDateTime) / 1e7


def proc_cpu():
    c, e, k, u = FILETIME(), FILETIME(), FILETIME(), FILETIME()
    if not k32.GetProcessTimes(k32.GetCurrentProcess(), ctypes.byref(c), ctypes.byref(e),
                               ctypes.byref(k), ctypes.byref(u)):
        return 0.0
    return _ft(k) + _ft(u)


def gui_res(flag=1):
    """GDI(0) / USER(1) 对象数 —— 每个窗口都会占 USER 对象，用来查窗口泄漏（GL 隐藏窗口）。
    ⚠ 必须设 argtypes/restype，否则 HANDLE 会被当 int 截断 → 恒返回 0/失败。"""
    try:
        return int(_u32.GetGuiResources(k32.GetCurrentProcess(), flag))
    except Exception:
        return -1


def alive_tids():
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snap == wt.HANDLE(-1).value or not snap:
        return []
    out = []
    try:
        te = THREADENTRY32()
        te.dwSize = ctypes.sizeof(THREADENTRY32)
        ok = k32.Thread32First(snap, ctypes.byref(te))
        while ok:
            if te.th32OwnerProcessID == PID:
                out.append(te.th32ThreadID)
            ok = k32.Thread32Next(snap, ctypes.byref(te))
    finally:
        k32.CloseHandle(snap)
    return out


def tid_cpu(tid):
    h = k32.OpenThread(THREAD_QUERY_INFORMATION, False, tid)
    if not h:
        return None                      # 线程已退出 → 拿不到了
    try:
        c, e, k, u = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        if not k32.GetThreadTimes(h, ctypes.byref(c), ctypes.byref(e),
                                  ctypes.byref(k), ctypes.byref(u)):
            return None
        return _ft(k) + _ft(u)
    finally:
        k32.CloseHandle(h)


def main():
    warm = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
    poll = (float(sys.argv[3]) if len(sys.argv) > 3 else 100.0) / 1000.0

    w("=" * 92)
    w("进程 CPU ↔ 线程归因 缺口诊断   %s   本机 %d 核   轮询 %.0fms"
      % (time.strftime("%H:%M:%S"), NCORE, poll * 1000))
    w("=" * 92)

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
    t0 = time.perf_counter()
    win = MainWindow()
    w("MainWindow 构造 %.2fs" % (time.perf_counter() - t0))

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
        w("（没播成测试音频：%s）" % str(e)[:70])

    win.toggle_run()
    w("已开始 VJ（toggle_run）")
    if os.environ.get("NOAUDIO"):
        # 隔离实验：停掉音频采集，看「每秒十几个新线程」是不是它造的
        try:
            win.audio.stop()
            w("已按 NOAUDIO=1 停掉音频采集")
        except Exception as e:                                        # noqa: BLE001
            w("停音频失败：%s" % e)

    def pump(until):
        while time.perf_counter() < until:
            app.processEvents()
            time.sleep(0.004)

    w("预热 %.0f 秒…" % warm)
    pump(time.perf_counter() + warm)

    w("")
    w("-- 高频轮询（每 %.0f ms）--" % (poll * 1000))
    base, last, first_round = {}, {}, None
    born = died = 0
    prev = set()
    alive_hist = []
    from collections import Counter
    mod_birth = Counter()            # 哪个 DLL 在批量创建线程
    mod_alive = Counter()
    unknown = 0
    p0 = proc_cpu()
    t0 = time.perf_counter()
    rounds = 0
    last_report = -15.0
    prev_t, prev_c = 0.0, p0
    proc_only = bool(os.environ.get("PROC_ONLY"))
    while time.perf_counter() - t0 < secs:
        wall_now = time.perf_counter() - t0
        if wall_now - last_report >= 15:
            last_report = wall_now
            # 最近 15 秒的瞬时核当量（区间增量）+ 区间平均 —— 区分「一直在涨」和「一开始就这样」
            now_c = proc_cpu()
            inst = (now_c - prev_c) / max(wall_now - prev_t, 1e-6)
            prev_t, prev_c = wall_now, now_c
            # 后台任务状态：启动后的补缩略图 / 编码普查 / alpha 校正，都会额外吃 CPU
            def _busy(attr):
                o = getattr(win, attr, None)
                return "跑" if (o is not None and o.isRunning()) else "停"
            w("    t=%6.1fs   平均 %.2f 核   最近15s %.2f 核   线程 %3d   "
              "补图 %s  普查 %s  校正 %s  User对象 %d"
              % (wall_now, (now_c - p0) / wall_now, inst, len(alive_tids()),
                 _busy("thumb_backfill"), _busy("codec_probe"), _busy("alpha_mig"),
                 gui_res(1)))
        if proc_only:
            pump(time.perf_counter() + 0.2)
            continue
        tids = alive_tids()
        cur = set(tids)
        if first_round is None:
            first_round = set(tids)
        fresh = cur - prev
        born += len(fresh)
        died += len(prev - cur)
        # 新出现的线程立刻查它的起始模块 —— 这是找「谁在造线程」的关键
        for tid in fresh:
            h = k32.OpenThread(THREAD_QUERY_INFORMATION, False, tid)
            if not h:
                unknown += 1
                continue
            try:
                mod = ""
                if ta is not None:
                    mod = ta.module_of_addr(ta.thread_start_addr(h)) or "?"
                mod_birth[mod or "(未知)"] += 1
            finally:
                k32.CloseHandle(h)
        for tid in tids:
            if rounds % 5:                    # 线程 CPU 每 5 轮采一次（降工具自身开销）
                continue
            v = tid_cpu(tid)
            if v is None:
                continue
            if tid in first_round:
                base.setdefault(tid, v)          # 采样前就存在：只算区间增量
            else:
                base.setdefault(tid, 0.0)        # 采样中新建：整个生命周期都算
            if v > last.get(tid, 0.0):
                last[tid] = v
        alive_hist.append(len(tids))
        prev = cur
        rounds += 1
        pump(time.perf_counter() + poll)
    wall = time.perf_counter() - t0
    p1 = proc_cpu()

    total = p1 - p0
    attributed = sum(max(0.0, last[t] - base.get(t, 0.0)) for t in last)
    gap = total - attributed
    n_new = sum(1 for t in last if t not in first_round)
    n_fin = sum(1 for t in last if t in first_round)

    w("")
    w("=" * 92)
    w("结果（采样 %.1f 秒，%d 轮）" % (wall, rounds))
    w("=" * 92)
    w("  进程总 CPU           %7.1f ms/秒 = %.2f 个核 = 整机 %.1f%%"
      % (total / wall * 1000, total / wall, total / wall * 100 / NCORE))
    w("  线程归因（含已结束）  %7.1f ms/秒 = %.2f 个核" % (attributed / wall * 1000, attributed / wall))
    w("  ★ 缺口               %7.1f ms/秒 = %.2f 个核  （占进程 %.0f%%）"
      % (gap / wall * 1000, gap / wall, gap / max(total, 1e-9) * 100))
    w("")
    w("  线程诞生 %d 次 / 消失 %d 次  →  每秒新建 %.1f 个" % (born, died, born / wall))
    w("  见过的 TID：%d 个（采样前就存在 %d，采样期间新建 %d）" % (len(last), n_fin, n_new))
    w("  存活线程数：最少 %d / 最多 %d（平均 %.0f）"
      % (min(alive_hist), max(alive_hist), sum(alive_hist) / len(alive_hist)))
    w("")
    if born / wall > 3:
        w("  ★ 线程创建速率偏高（%.1f/秒）—— 有代码在反复新建线程，这是典型的可优化点：" % (born / wall))
        w("    每次创建/销毁要付栈分配 + 内核对象 + 调度器开销，且这些线程的 CPU「抓不稳」。")
        w("    按起始模块统计（谁造的线程）：")
        for mod, n in mod_birth.most_common(12):
            w("      %-26s %6d 次  （%.1f/秒）" % (mod, n, n / wall))
    else:
        w("  线程创建速率正常（<3/秒），缺口不是「反复建线程」造成的。")
    if gap > 0.15:
        w("  ★ 仍有 %.2f 核 无法归因：可能是 (a) 极短命线程（两次轮询之间生灭），" % gap)
        w("    (b) OpenThread 被拒的受保护线程，(c) 内核/DLL 在系统侧记的账。")
    else:
        w("  缺口已基本补齐 —— 说明 cpu_profile 里那 38% 就是「采样期间结束的线程」。")
    w("")
    w("  ⚠ 注意：本工具每轮要枚举 %d 个线程并逐个 OpenThread，**自身有开销**（可能 0.5 核+），"
      % (sum(alive_hist) // max(1, len(alive_hist))))
    w("    所以上面「进程总 CPU」偏高；线程创建速率与模块分布不受影响。")

    with open(os.path.join(ROOT, "_cpu_gap.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    print("\n→ 已写入 _cpu_gap.txt")


if __name__ == "__main__":
    main()
