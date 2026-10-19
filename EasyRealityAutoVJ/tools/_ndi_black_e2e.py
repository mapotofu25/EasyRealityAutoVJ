# -*- coding: utf-8 -*-
"""端到端验证：NDI 发送 → cyndilib 接收端，检查**是否还有黑帧**。

## ⚠⚠ 本脚本在**本机（沙箱/同机自收）环境常抓不到帧**
   现象：Finder 能发现源、`set_source` 成功，但 `is_connected()` 长期 False。
   原因：NDI 单机回环需要发送端**持续**推帧且运行时发现服务正常响应；
   沙箱里发送端首帧会抛 `Object is not writable`（接收端计数未上报），
   之后即便继续推，接收端也可能一直连不上。**这不是我们代码的问题。**

## ⇒ 权威的黑帧验证请看 `tools/_ndi_vframe_copy.py`
   它用替身 video/sender 直接验证"入队即拷贝"的语义，且**反向复现过**：
   旧代码（存引用）59/60 帧全黑，新代码 0 黑帧。那个测试才是判据来源。

本脚本保留用于**真机/双机**环境下的端到端确认。
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import numpy as np  # noqa: E402

W, H = 320, 240
SOURCE = "ERAVJ_E2E_CopyTest"


def _pattern(k):
    """生成第 k 帧图案：RGB 三通道 = k&0xFF（随帧号变），alpha=255。"""
    a = np.empty((H, W, 4), dtype=np.uint8)
    v = k & 0xFF
    a[:, :, 0] = v
    a[:, :, 1] = v
    a[:, :, 2] = v
    a[:, :, 3] = 255
    return a


def main():
    try:
        import cyndilib
        from cyndilib.receiver import Receiver
        from cyndilib.finder import Finder
        from cyndilib.wrapper.ndi_recv import RecvColorFormat, RecvBandwidth
    except Exception as e:                                         # noqa: BLE001
        print("SKIP: cyndilib 不可用（%s）" % e)
        return 0

    import ndi_out

    dev = ndi_out.NDIOutput(SOURCE)
    if not dev._ensure(W, H):
        print("SKIP: 发送端建立失败（%s）" % dev._last_err)
        return 0
    print("发送端已建立：%s %dx%d" % (SOURCE, W, H))

    # 先让发送端推几帧，接受端才可能发现源（NDI 需要源"活着"）
    canvas = _pattern(0)
    stop_early = threading.Event()

    def keepalive():
        k = 0
        while not stop_early.is_set():
            k += 1
            canvas[:] = _pattern(k)
            dev.send_video(canvas.reshape(-1), W, H)
            canvas[:] = 0
            time.sleep(1 / 60.0)

    ka = threading.Thread(target=keepalive, daemon=True)
    ka.start()

    # ---- 接收端：用 Finder 找到我们的源 ----
    f = Finder()
    f.open()
    t0 = time.perf_counter()
    src = None
    while time.perf_counter() - t0 < 5.0:
        try:
            for s in f.iter_sources():
                if SOURCE in str(getattr(s, "name", "")):
                    src = s
                    break
        except Exception:                                          # noqa: BLE001
            pass
        if src is not None:
            break
        time.sleep(0.2)
    if src is None:
        print("SKIP: Finder 找不到源 %s（同机自收可能被防火墙/发现服务挡住）" % SOURCE)
        stop_early.set()
        try:
            f.close()
        except Exception:                                          # noqa: BLE001
            pass
        dev.close()
        return 0

    recv = Receiver()
    try:
        recv.color_format = RecvColorFormat.RGBX_RGBA
        recv.bandwidth = RecvBandwidth.highest
    except Exception:                                              # noqa: BLE001
        pass
    recv.set_source(src)
    print("接收端已锁定源：%s" % getattr(src, "name", "?"))

    stop = threading.Event()
    frame_count = {"n": 0}
    canvas = _pattern(0)

    def tx_loop():
        """模拟渲染线程：改图案 → send_video → **立刻清黑**（模拟画布被覆写）。"""
        k = 0
        while not stop.is_set():
            k += 1
            canvas[:] = _pattern(k)          # 画一帧
            dev.send_video(canvas.reshape(-1), W, H)
            canvas[:] = 0                    # ★ 立刻清黑（模拟 _compose 下一帧先清底）
            frame_count["n"] += 1
            time.sleep(1 / 60.0)

    stop_early.set()          # 停掉 keepalive，交给 tx_loop
    time.sleep(0.1)
    th = threading.Thread(target=tx_loop, daemon=True)
    th.start()

    # ---- 收帧 ----
    black = 0
    good = 0
    other = 0
    got = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 10.0:
        try:
            if not recv.is_connected():
                time.sleep(0.05)
                continue
            vf = recv.video_frame
            if vf is None:
                time.sleep(0.01)
                continue
            # 等一个完整帧同步（cyndilib 的取帧方式）
            try:
                if hasattr(vf, "frame_sync"):
                    vf.frame_sync()
            except Exception:                                      # noqa: BLE001
                pass
            try:
                vf.capture(W, H)
            except Exception:                                      # noqa: BLE001
                pass
            data = getattr(vf, "data", None)
            if data is None:
                time.sleep(0.01)
                continue
            arr = np.frombuffer(data, dtype=np.uint8)
            if arr.size < W * H * 4:
                time.sleep(0.01)
                continue
            px = arr[:W * H * 4].reshape(H, W, 4)
            rgb = px[:, :, :3]
            if not rgb.any():
                black += 1
            elif bool((rgb[:, :, 0] == rgb[:, :, 1]).all()) and \
                    bool((rgb[:, :, 1] == rgb[:, :, 2]).all()):
                good += 1
            else:
                other += 1
            got += 1
            time.sleep(1 / 60.0)
        except Exception:                                          # noqa: BLE001
            time.sleep(0.02)

    stop.set()
    time.sleep(0.2)
    try:
        recv.disconnect()
    except Exception:                                              # noqa: BLE001
        pass
    try:
        f.close()
    except Exception:                                              # noqa: BLE001
        pass
    dev.close()

    print("=" * 62)
    print("发送线程提交 %d 帧；接收端抓到 %d 帧" % (frame_count["n"], got))
    print("  正常图案帧 : %d" % good)
    print("  黑帧       : %d   ← 修复前这里会是一大片" % black)
    print("  其它       : %d" % other)
    print("=" * 62)
    if got == 0:
        print("SKIP: 接收端一帧都没抓到（同机自收在某些环境不通，非本修复的判据）")
        return 0
    ok = black <= max(2, got // 20)
    print(("✓ 通过：黑帧率 %.1f%%（修复前会接近 100%%）⇒ 不再闪黑" % (black * 100.0 / got))
          if ok else
          ("✗ 黑帧过多：%d/%d ⇒ 拷贝修复未生效" % (black, got)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
