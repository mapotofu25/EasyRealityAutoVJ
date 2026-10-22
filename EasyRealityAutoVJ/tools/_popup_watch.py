# -*- coding: utf-8 -*-
"""盯着 ERAVJ 的窗口：每秒枚举一次顶层窗口，记录「出现又消失」的弹窗。

用户报「一直有个弹窗在闪」——闪 = 出现→消失→出现，单次快照抓不到。
本脚本连续采样 N 秒，把**每次新出现**的窗口（标题/类名/尺寸）都打印出来，
并统计出现次数最多的那个。

用法：python tools/_popup_watch.py [秒数] [输出日志]
"""
import ctypes
import ctypes.wintypes as W
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

u = ctypes.windll.user32
u.SetProcessDPIAware()

WNDENUMPROC = ctypes.WINFUNCTYPE(
    ctypes.c_bool, W.HWND, W.LPARAM)

# 需要拿到窗口所属 pid
u.GetWindowThreadProcessId.restype = W.DWORD
u.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
u.GetWindowTextLengthW.argtypes = [W.HWND]
u.IsWindowVisible.argtypes = [W.HWND]

gwl_exstyle = -20
WS_EX_TOOLWINDOW = 0x00000080


def top_windows():
    """返回 {hwnd: (title, cls, rect, pid)}（只含 IsWindowVisible 的顶层窗口）。"""
    out = {}

    def cb(h, _lp):
        try:
            if not u.IsWindowVisible(h):
                return True
            n = u.GetWindowTextLengthW(h)
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(h, buf, n + 1)
            title = buf.value
            cbuf = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(h, cbuf, 256)
            cls = cbuf.value
            r = W.RECT()
            u.GetWindowRect(h, ctypes.byref(r))
            pid = W.DWORD(0)
            u.GetWindowThreadProcessId(h, ctypes.byref(pid))
            out[int(h)] = (title, cls, (r.left, r.top, r.right, r.bottom),
                           int(pid.value))
        except Exception:
            pass
        return True

    u.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    out_path = sys.argv[2] if len(sys.argv) > 2 else None
    lines = []

    def emit(s):
        print(s, flush=True)
        lines.append(s)

    # 找到 ERAVJ 主进程 pid
    import subprocess
    tgt_pid = None
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process EasyRealityAutoVJ -ErrorAction SilentlyContinue | "
             "Select-Object -First 1 -ExpandProperty Id)"],
            capture_output=True, text=True, timeout=10)
        v = (r.stdout or "").strip()
        if v.isdigit():
            tgt_pid = int(v)
    except Exception as e:
        emit("取 pid 失败: %r" % (e,))
    emit("目标 ERAVJ pid = %s" % tgt_pid)

    t0 = time.perf_counter()
    seen = {}          # key -> 首次出现时刻
    counts = {}        # key -> 出现次数（新出现计数）
    prev = set()
    samples = 0
    while time.perf_counter() - t0 < secs:
        cur = top_windows()
        keys = set(cur.keys())
        new = keys - prev
        gone = prev - keys
        for h in new:
            title, cls, rect, pid = cur[h]
            key = (title, cls, pid)
            counts[key] = counts.get(key, 0) + 1
            if key not in seen:
                seen[key] = time.perf_counter() - t0
                w = rect[2] - rect[0]
                hh = rect[3] - rect[1]
                mark = " <== ERAVJ" if pid == tgt_pid else ""
                emit("[+%5.1fs] NEW  hwnd=%d pid=%d  cls=%r  title=%r  %dx%d%s"
                     % (time.perf_counter() - t0, h, pid, cls, title, w, hh, mark))
        for h in gone:
            if h in prev:
                pass   # 消失不打印（太多噪声）
        prev = keys
        samples += 1
        time.sleep(0.05)
    emit("")
    emit("采样 %d 次 / %.1fs" % (samples, secs))
    emit("=== 反复出现的窗口（出现次数 > 1）===")
    hot = [(c, k) for k, c in counts.items() if c > 1]
    hot.sort(reverse=True)
    for c, (title, cls, pid) in hot[:20]:
        mark = " <== ERAVJ" if pid == tgt_pid else ""
        emit("  x%-4d pid=%-6d cls=%-28r title=%r%s" % (c, pid, cls, title, mark))
    if not hot:
        emit("  （没有反复出现的窗口）")
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print("saved", out_path)


if __name__ == "__main__":
    main()
