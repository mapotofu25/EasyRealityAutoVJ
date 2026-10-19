# -*- coding: utf-8 -*-
r"""复现并抓取 NDI「Object is not writable」的完整堆栈。

## 背景（2026-10-08 用户报「OBS 收不到视频」）
`output_health.log` 反复出现：
    NDI !! NDI 发送视频失败（只记一次，后续继续尝试）：Object is not writable.
而同一时刻 `NDI audio: ... conns=2`（**有接收端连着**）—— 所以不是"没人连"。

代码里已做保护：
    if hasattr(v, "get_write_available") and not v.get_write_available():
        continue            # 不可写就丢帧
    v.write_data(snap)      # snap = bytearray
    s.send_video_async()

⇒ 说明 `get_write_available()` 返回 True，但**后面某一步**仍抛错。
   cyndilib 的 .pyd 里**没有**这个字符串 ⇒ 不是 SDK 抛的，必须抓完整 traceback 才知道出在哪个调用。

## 用法
    venv\Scripts\python.exe tools/_ndi_err_probe.py
"""
import os
import sys
import time
import traceback

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

W, H = 1920, 1080
NAME = "AVJ_Probe_DoNotUse"


def show(tag, fn):
    """执行 fn 并打印完整堆栈（这是本脚本的全部意义 —— 日志只有 str(e)）。"""
    try:
        r = fn()
        print("  [%s] OK  -> %r" % (tag, r))
        return True
    except Exception as e:                                        # noqa: BLE001
        print("  [%s] 抛异常: %s: %s" % (tag, type(e).__name__, e))
        tb = traceback.format_exc()
        for line in tb.splitlines()[-6:]:
            print("        " + line)
        return False


def main():
    print("=" * 78)
    print("NDI 错误复现探针（1920x1080 BGRA，源名 %s）" % NAME)
    print("=" * 78)

    from cyndilib.sender import Sender
    from cyndilib.video_frame import VideoSendFrame

    print("\n① 建 sender + video frame（与 ndi_out._ensure 完全一致的参数）")
    s = Sender(NAME)
    v = VideoSendFrame()
    show("set_resolution", lambda: v.set_resolution(W, H))
    show("set_fourcc(BGRA=0x41524742)", lambda: v.set_fourcc(0x41524742))
    show("set_frame_rate(60)", lambda: v.set_frame_rate(60))
    show("set_progressive(True)", lambda: v.set_progressive(True))
    show("s.set_video_frame", lambda: s.set_video_frame(v))
    show("s.open()", lambda: s.open())
    time.sleep(0.5)

    print("\n② 帧信息")
    for k in ("get_resolution", "get_fourcc", "get_format_string",
              "get_line_stride", "get_buffer_size", "ndim", "shape"):
        try:
            print("     %-20s = %r" % (k, getattr(v, k)() if callable(
                getattr(v, k, None)) else getattr(v, k)))
        except Exception as e:                                    # noqa: BLE001
            print("     %-20s ! %s" % (k, e))

    print("\n③ 连接数")
    try:
        print("     conns = %r" % s.get_num_connections(0.0))
    except Exception as e:                                        # noqa: BLE001
        print("     conns 查询失败: %s" % e)

    print("\n④ 各种写法的对照（关键：找出哪种触发 'not writable'）")
    n = W * H * 4
    ba = bytearray(n)
    mv = memoryview(ba)
    print("     get_write_available() = %r" % (v.get_write_available(),))
    show("write_data(bytearray)", lambda: v.write_data(ba))
    print("     get_write_available() = %r（写完一次后）" % (v.get_write_available(),))
    show("write_data(memoryview)", lambda: v.write_data(mv))
    show("write_data(bytes)  ← 不可变，看是不是它", lambda: v.write_data(bytes(n)))
    show("send_video_async()", lambda: s.send_video_async())
    show("send_video()", lambda: s.send_video())

    print("\n⑤ 静默期（open 后 0.3s 内）复现")
    print("     （本进程已过静默期，仅作记录）")

    print("\n" + "=" * 78)
    try:
        s.destroy()
    except Exception:                                             # noqa: BLE001
        pass
    print("探针结束")
    print("=" * 78)


if __name__ == "__main__":
    main()
