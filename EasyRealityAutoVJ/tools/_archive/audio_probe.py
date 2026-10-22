# -*- coding: utf-8 -*-
"""音源诊断 —— 查清「明明选了设备，却采不到声音」。

【这个工具回答什么】
  1) 配置里记的音源类型 / 设备名 / 按类型记忆的设备分别是啥
  2) soundcard 枚举到的**每一个**设备：名字 + 它被算成「系统声音」还是「麦克风」
  3) 按引擎（`audio_engine._run`）的逻辑复刻匹配一遍，指出**实际会被打开的设备**
  4) 真的打开它采 3 秒，报采样率和电平峰值

【为什么需要它】
软件按「是不是 loopback（回环）」把设备分两类：
  · 选「系统声音」→ 只在 loopback 设备里找
  · 选「麦克风」  → 只在**非** loopback 设备里找
VoiceMeeter 的 Out B1 在 Windows 里既是播放设备、又出现在录制设备列表，
很容易被算成 loopback —— 那它就不在「麦克风」这一类里，于是选它必然匹配失败。
而匹配失败时引擎会**静默退回候选列表的第一个设备**，界面上只有一个不显眼的
「当前采集设备」提示，看起来就像「选了但没声音」。

⚠ soundcard 必须在**同一条后台线程**里 import / 枚举 / 录音（见 audio_engine 顶部注释），
  本工具照做：所有 soundcard 操作都丢给一条 worker 线程。

用法：音源诊断.exe            （双击即可，结果同时写入 audio_probe.txt）
"""
import json
import os
import queue
import sys
import threading
import time

SRC = None
for cand in (os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"),
             os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"),
             os.path.dirname(os.path.abspath(__file__))):
    if os.path.isdir(cand):
        SRC = cand
        break
if SRC and SRC not in sys.path:
    sys.path.insert(0, SRC)

OUT = []
RECORD_SECS = 3.0


def p(s=""):
    """打印。⚠ 打包成 exe 后 stdout 是 **GBK(cp936)** —— 直接 print 一个 GBK 里没有的
    符号（→ ⚠ 等）会抛 UnicodeEncodeError 把整个工具崩掉（源码模式下 stdout 是
    UTF-8 所以不会暴露，这种坑只在打包版出现）。这里统一兜底。"""
    t = str(s)
    try:
        print(t, flush=True)
    except UnicodeEncodeError:
        try:
            print(t.encode("gbk", "replace").decode("gbk"), flush=True)
        except Exception:                                        # noqa: BLE001
            print(t.encode("ascii", "replace").decode("ascii"), flush=True)
    OUT.append(t)


def cfg_path():
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "config.json")


def engine_pick(devs, source_type, device_name):
    """**照抄 `audio_engine._run` 的选设备逻辑**（诊断用，必须与引擎一致）。

    devs: [(name, is_loopback), ...]
    返回 (选中的设备, 候选列表, 是否靠名字匹配上)
    """
    if source_type == "system":
        cands = [d for d in devs if d[1]]
    else:
        cands = [d for d in devs if not d[1]]
    if not cands:
        return None, [], False
    mic = cands[0]
    matched = False
    if device_name:
        for d in cands:
            if device_name in d[0]:          # ⚠ 引擎是**单向**包含（UI 那边是双向）
                mic = d
                matched = True
                break
    return mic, cands, matched


def main():
    p("=" * 74)
    p("音源诊断          %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    p("=" * 74)

    # ---------------------------------------------------------- 配置
    cp = cfg_path()
    p("")
    p("[1] 配置    %s" % cp)
    a = {}
    if os.path.exists(cp):
        try:
            with open(cp, encoding="utf-8") as f:
                a = (json.load(f) or {}).get("audio") or {}
        except Exception as e:                                   # noqa: BLE001
            p("    ★读配置失败：%s" % str(e)[:120])
    else:
        p("    （配置文件不存在，用的是默认值）")
    stype = a.get("source_type", "system")
    dname = a.get("device_name") or ""
    dbs = a.get("device_by_source") or {}
    p("    音源类型        : %s%s" % (stype, "（麦克风）" if stype != "system" else "（系统声音）"))
    p("    设备名 device_name : %r" % dname)
    for k in ("system", "mic"):
        p("    按类型记忆 %-7s: %r" % (k, dbs.get(k) or ""))

    # ---------------------------------------------------------- 枚举 + 匹配 + 实测
    q = queue.Queue()

    def worker():
        try:
            import soundcard as sc
            mics = sc.all_microphones(include_loopback=True)
            devs, objs = [], {}
            for m in mics:
                devs.append((m.name, bool(m.isloopback)))
                objs[m.name] = m
            q.put(("devs", devs, objs))
        except Exception as e:                                   # noqa: BLE001
            q.put(("err", str(e), None))

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    kind, devs, objs = q.get(timeout=30)

    if kind == "err":
        p("")
        p("★枚举设备失败：%s" % devs)
        return

    p("")
    p("[2] soundcard 枚举到的设备（共 %d 个）" % len(devs))
    p("    #   归类        loopback  设备名")
    for i, (nm, lb) in enumerate(devs, 1):
        p("    %-3d %-11s %-9s %s" % (i, "系统声音" if lb else "麦克风",
                                     "是" if lb else "否", nm))

    p("")
    p("[3] 按引擎逻辑模拟（用配置里的设备和类型）")
    mic, cands, matched = engine_pick(devs, stype, dname)
    p("    音源类型「%s」→ 候选设备 %d 个" % (stype, len(cands)))
    for nm, _lb in cands:
        p("        · %s" % nm)
    if not cands:
        p("    ★这一类里一个设备都没有 —— 引擎会直接报「找不到设备」")
    else:
        p("    用 device_name 匹配：%s" % ("**成功**" if matched else "**失败**（名字对不上）"))
        p("    → 实际会被打开的设备：%s" % mic[0])
        if not matched and dname:
            p("    → 这就是问题所在：你选的是")
            p("         %s" % dname)
            p("       而引擎找不到它，于是静默用了候选里的第一个。")
        if not dname:
            p("    → device_name 是空的（没保存进去）→ 引擎只能用候选里的第一个。")

    # ---------------------------------------------------------- 真的采一段
    if mic is not None:
        p("")
        p("[4] 实际采集测试（打开上面那台设备，录 %.0f 秒）" % RECORD_SECS)
        res = queue.Queue()

        def rec_worker():
            try:
                import ctypes
                # ⚠⚠ soundcard 只在**首次 import 的那条线程**做了 CoInitializeEx(MTA)
                #   （见 audio_engine 顶部注释）。录音跑在另一条线程 → 必须自己再
                #   CoInitializeEx，否则打开设备直接报 `Error 0x800401f0`（CO_E_NOTINITIALIZED）。
                try:
                    ctypes.windll.ole32.CoInitializeEx(None, 0x0)     # COINIT_MULTITHREADED
                except Exception:                                    # noqa: BLE001
                    pass
                import soundcard as sc
                obj = objs[mic[0]]
                sr = None
                try:
                    from soundcard.mediafoundation import get_native_samplerate  # noqa: F401
                except Exception:                                    # noqa: BLE001
                    pass
                try:
                    sr = int(obj.samplerate) if hasattr(obj, "samplerate") else None
                except Exception:                                    # noqa: BLE001
                    sr = None
                sr = sr or 48000
                with obj.recorder(samplerate=sr, channels=1) as rec:
                    data = rec.record(numframes=int(sr * RECORD_SECS))
                try:
                    peak = float(abs(data).max()) if hasattr(data, "max") else 0.0
                except Exception:                                    # noqa: BLE001
                    peak = 0.0
                res.put(("ok", sr, peak))
            except Exception as e:                                   # noqa: BLE001
                res.put(("err", str(e)[:200], 0.0))

        t = threading.Thread(target=rec_worker, daemon=True)
        t.start()
        try:
            k, v1, v2 = res.get(timeout=30)
        except Exception:                                        # noqa: BLE001
            k, v1, v2 = "err", "超时（30 秒没返回）", 0.0
        if k == "ok":
            p("    采样率 %s   电平峰值 %.4f" % (v1, v2))
            if v2 < 1e-4:
                p("    ★电平是 0 —— 这台设备上**没有声音进来**（或它不是你期望的那台）")
            else:
                p("    有声音！这台设备是活的（峰值 %.2f）" % v2)
        else:
            p("    ★打不开：%s" % v1)

    # ---------------------------------------------------------- 结论
    p("")
    p("=" * 74)
    p("怎么读这个结果")
    p("  · 如果你要的设备出现在「麦克风」那一列，却在第 3 步匹配失败 →")
    p("    在软件里**重新选一次**并点「应用」（不是直接关掉对话框）。")
    p("  · 如果你要的设备被算成了「系统声音」（loopback=是）→")
    p("    把软件里的「音源类型」改成「系统声音（Loopback）」，再选它。")
    p("  · 如果 device_name 是空的 → 说明上次没保存成功，重新选 + 点「应用」。")
    p("  · 第 4 步电平为 0 且设备确实是你要的那台 → 是设备侧没声音（VBAN 那边）。")
    p("=" * 74)

    with open("audio_probe.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(OUT))
    p("")
    p("→ 已写入 audio_probe.txt（同目录）")


if __name__ == "__main__":
    try:
        main()
    except Exception:                                            # noqa: BLE001
        import traceback
        traceback.print_exc()
    if getattr(sys, "frozen", False) and sys.stdin is not None:
        try:
            input("\n按回车键关闭窗口…")
        except Exception:                                        # noqa: BLE001
            pass
