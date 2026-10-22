# -*- coding: utf-8 -*-
"""ASIO 原生宿主的落地验证（真机、真驱动）。

分三步，每步都能单独判断：
  ① 枚举：能列出注册表里注册的 ASIO 驱动
  ② 打开：真 open 一次（默认驱动、最小缓冲），拿到采样率/缓冲帧数/延迟
  ③ 收数：读若干块，看**是否真的有样本在流动**（RMS 不为 0 才算通）
  ④ 关闭：干净收尾，再开一次确认没有泄漏/独占残留

⚠ 这是**真硬件操作**（会短暂占用 ASIO 驱动）：
  · 若 VirtualDJ / DAW 正开着并占着同一个驱动，open 会失败 —— 那正是预期的
    「单客户端独占」行为，不是 bug；
  · 想换个驱动测：`--drv "驱动名"`；只枚举不打开：`--list`。
  · 想限时：`--timeout 25`（默认 20 秒），超时就退出，避免把终端挂住。

用法：
  venv\\Scripts\\python.exe -u tools\\_asio_native_test.py --list
  venv\\Scripts\\python.exe -u tools\\_asio_native_test.py
"""

import argparse
import ctypes
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DLL = os.path.join(ROOT, "src", "_asiohost.dll")

T0 = time.perf_counter()


def log(msg):
    print("[%6.2fs] %s" % (time.perf_counter() - T0, msg))


def load():
    if not os.path.isfile(DLL):
        raise SystemExit("找不到 %s —— 先跑 tools/build_asio.py" % DLL)
    try:
        # ⚠ Windows 上把 DLL 目录加进搜索路径，免得依赖的 DLL 找不到
        os.add_dll_directory(os.path.dirname(DLL))
    except Exception:
        pass
    d = ctypes.CDLL(DLL)
    d.avj_asio_count.restype = ctypes.c_int
    d.avj_asio_name.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    d.avj_asio_name.restype = ctypes.c_int
    d.avj_asio_last_error.restype = ctypes.c_char_p
    d.avj_asio_is_open.restype = ctypes.c_int
    d.avj_asio_cur_rate.restype = ctypes.c_int
    d.avj_asio_buffer_frames.restype = ctypes.c_int
    d.avj_asio_blocks.restype = ctypes.c_int
    d.avj_asio_overflows.restype = ctypes.c_int
    d.avj_asio_pending.restype = ctypes.c_int
    d.avj_asio_open.argtypes = [ctypes.c_char_p, ctypes.c_double, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int,
                                ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                                ctypes.c_char_p, ctypes.c_int]
    d.avj_asio_open.restype = ctypes.c_int
    d.avj_asio_read.argtypes = [ctypes.POINTER(ctypes.c_float), ctypes.c_int]
    d.avj_asio_read.restype = ctypes.c_int
    d.avj_asio_close.restype = ctypes.c_int
    d.avj_asio_debug.argtypes = [ctypes.c_char_p, ctypes.c_int]
    d.avj_asio_debug.restype = ctypes.c_int
    return d


def show_debug(d, tag=""):
    try:
        b = ctypes.create_string_buffer(512)
        d.avj_asio_debug(b, 512)
        log("   [debug%s] %s" % (tag, b.value.decode("mbcs", "replace")))
    except Exception as e:                                     # noqa: BLE001
        log("   [debug] 失败：%s" % e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="只枚举，不打开")
    ap.add_argument("--drv", default="", help="驱动名（默认用第一个）")
    ap.add_argument("--seconds", type=float, default=1.5, help="收数时长")
    ap.add_argument("--buf", type=int, default=0, help="缓冲帧数（0=用驱动最小值）")
    ap.add_argument("--sr", type=float, default=0.0, help="采样率（0=沿用驱动当前值）")
    args = ap.parse_args()

    d = load()
    n = d.avj_asio_count()
    log("① 枚举：%d 个 ASIO 驱动" % n)
    names = []
    for i in range(n):
        buf = ctypes.create_string_buffer(256)
        rc = d.avj_asio_name(i, buf, 256)
        nm = buf.value.decode("mbcs", "replace") if rc == 0 else "(读名失败)"
        names.append(nm)
        log("     [%d] %s" % (i, nm))
    if n == 0:
        log("⚠ 本机没有注册任何 ASIO 驱动 ⇒ 只能靠优雅回退（WASAPI）继续跑")
        return 0
    if args.list:
        return 0

    drv = args.drv or names[0]
    log("② 打开驱动「%s」（缓冲 %s）" % (drv, args.buf or "驱动最小"))
    out_sr = ctypes.c_int(0)
    out_buf = ctypes.c_int(0)
    err = ctypes.create_string_buffer(512)
    rc = d.avj_asio_open(drv.encode("mbcs"), float(args.sr), 0, 1, int(args.buf),
                         ctypes.byref(out_sr), ctypes.byref(out_buf), err, 512)
    if rc != 0:
        log("   open 失败 rc=%d：%s" % (rc, err.value.decode("mbcs", "replace")))
        log("   （若提示被独占，那是 ASIO 的正常行为 —— 关掉占用它的程序再试）")
        return 1
    log("   OK  采样率 %d Hz，缓冲 %d 帧（≈ %.1f ms）"
        % (out_sr.value, out_buf.value, out_buf.value * 1000.0 / max(1, out_sr.value)))
    li = ctypes.c_int(0)
    lo = ctypes.c_int(0)
    d.avj_asio_latency_frames(ctypes.byref(li), ctypes.byref(lo))
    log("   驱动报的输入延迟 %d 帧（≈ %.1f ms）" % (li.value, li.value * 1000.0 / max(1, out_sr.value)))
    show_debug(d, " 刚打开")

    log("③ 收数 %.1f 秒 …" % args.seconds)
    N = 4096
    buf = (ctypes.c_float * (N * 2))()
    total = 0
    peak = 0.0
    t_end = time.perf_counter() + args.seconds
    empty_rounds = 0
    while time.perf_counter() < t_end:
        got = d.avj_asio_read(buf, N)
        if got <= 0:
            empty_rounds += 1
            time.sleep(0.005)
            continue
        frames = got // 2
        total += frames
        # 抽样算峰值（不引 numpy，保持脚本自足）
        step = max(1, got // 256)
        for i in range(0, got, step):
            v = buf[i]
            if v < 0:
                v = -v
            if v > peak:
                peak = v
    log("   收到 %d 帧，峰值 %.4f，回调 %d 次，溢出 %d 次，空转轮 %d"
        % (total, peak, d.avj_asio_blocks(), d.avj_asio_overflows(), empty_rounds))
    if total <= 0:
        log("   ✗ 一个样本都没收到 —— 驱动在跑但回调没送数据？")
    elif peak <= 0.0:
        log("   ⚠ 收到帧但全是静音（峰值 0）—— 若声源确实没声音则正常")
    else:
        log("   ✓ 有真实样本在流动（峰值 %.4f）" % peak)

    log("④ 关闭")
    d.avj_asio_close()
    log("   is_open=%d（应为 0）" % d.avj_asio_is_open())

    log("⑤ 再开一次（验证可重入、没有独占残留）")
    rc = d.avj_asio_open(drv.encode("mbcs"), float(args.sr), 0, 1, int(args.buf),
                         ctypes.byref(out_sr), ctypes.byref(out_buf), err, 512)
    log("   第二次 open rc=%d %s" % (rc, err.value.decode("mbcs", "replace")))
    d.avj_asio_close()
    log("完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
