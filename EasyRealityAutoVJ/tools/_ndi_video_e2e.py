# -*- coding: utf-8 -*-
r"""NDI 视频提交端到端验证 —— 证明「OBS 收不到视频」已修好。

## 验什么
`NDIOutput.send_video()` → `_video_loop` 真正把帧提交给 SDK 的次数（`_vsend_ok`）。
修复前该值**恒为 0**（每帧都抛 `BufferError: Object is not writable.`）；
修复后应随发送帧数同步增长，且 `_last_err` 保持空。

## 反向复现（本脚本内置，证明测试不是空过）
`--old` 用 monkeypatch 把拷贝写法换回旧版（`snap[:] = bgra` / `snap = bytes(bgra)`）
⇒ `_vsend_ok` 必须掉到 0、`_last_err` 必须出现 "not writable"。
**"改回旧写法测试仍然通过" = 测试无效** —— 所以这一步必须有。

## 用法
    venv\Scripts\python.exe tools/_ndi_video_e2e.py
    venv\Scripts\python.exe tools/_ndi_video_e2e.py --old
"""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

import numpy as np                                              # noqa: E402
import ndi_out                                                  # noqa: E402

NAME = "AVJ_VIDEO_E2E_DO_NOT_USE"
W, H = 1920, 1080
NFRAMES = 120


def patched_send_video_old(self, bgra, w, h):
    """旧写法（本轮修复前）：`snap[:] = bgra` + 兜底 `snap = bytes(bgra)`。"""
    if not self._ensure(w, h):
        return
    try:
        need = int(w) * int(h) * 4
        if len(bgra) != need:
            self._last_err = "帧数据 %d 字节 != 期望 %d" % (len(bgra), need)
            return
        try:
            snap = self._vbuf_pool.acquire(len(bgra))
            snap[:] = bgra                       # ← numpy 数组：必抛 TypeError
        except Exception:                        # noqa: BLE001
            snap = bytes(bgra)                   # ← 变成不可变 ⇒ write_data 抛 not writable
        with self._vcv:
            self._vq = (snap, int(w), int(h))
            self._vcv.notify()
        self._ensure_vthread()
    except Exception as e:                                       # noqa: BLE001
        self._last_err = str(e)[:160]


def main():
    old = "--old" in sys.argv
    if old:
        ndi_out.NDIOutput.send_video = patched_send_video_old
        print("★ 已启用 --old（monkeypatch 成修复前写法）—— 本模式**应该失败**")
    print("=" * 78)
    print("NDI 视频提交端到端验证   %dx%d  发 %d 帧   源名 %s"
          % (W, H, NFRAMES, NAME))
    print("=" * 78)

    o = ndi_out.NDIOutput(NAME)
    arr = np.zeros((H, W, 4), dtype=np.uint8)
    arr[:, :, 0] = 40        # B
    arr[:, :, 1] = 120       # G
    arr[:, :, 2] = 220       # R
    arr[:, :, 3] = 255       # A
    bgra = arr.reshape(-1)   # 1D BGRA 视图（与 engine._ndi_send 同形）

    print("\n① 首发（触发 _ensure 建 sender）")
    o.send_video(bgra, W, H)
    time.sleep(0.6)          # 过 0.3s 开门静默期

    print("② 连发 %d 帧（60fps）" % NFRAMES)
    t0 = time.perf_counter()
    for i in range(NFRAMES):
        arr[:, :, 1] = (i * 2) % 256     # 每帧改点内容，便于肉眼区分
        o.send_video(bgra, W, H)
        time.sleep(1.0 / 60.0)
    time.sleep(0.8)          # 等提交线程追平

    dt = time.perf_counter() - t0
    print("\n③ 结果")
    print("   _vsend_ok = %d   （真正提交给 SDK 的帧数）" % o._vsend_ok)
    print("   _dropped  = %d" % o._dropped)
    print("   _last_err = %r" % o._last_err)
    print("   enabled   = %r" % o.enabled)
    try:
        print("   conns     = %r   （0 = 当前没有接收端；不影响本次验证）"
              % o._conns())
    except Exception as e:                                       # noqa: BLE001
        print("   conns     = 查询失败 %s" % e)
    print("   耗时 %.2fs（%.1f fps 入队）" % (dt, NFRAMES / dt))

    print("\n" + "=" * 78)
    if old:
        ok = (o._vsend_ok == 0) and ("not writable" in (o._last_err or ""))
        print("反向复现：%s —— _vsend_ok=%d  _last_err=%r"
              % ("✅ 成功复现坏结果（证明测试有效）" if ok else "❌ 未复现（测试可能无效！）",
                 o._vsend_ok, o._last_err))
    else:
        ok = (o._vsend_ok >= NFRAMES * 0.8) and not (o._last_err or "")
        print("修复验证：%s —— 提交 %d/%d 帧，_last_err=%r"
              % ("✅ 视频帧能正常提交" if ok else "❌ 仍有问题",
                 o._vsend_ok, NFRAMES, o._last_err))
    print("=" * 78)
    try:
        o.close()
    except Exception:                                            # noqa: BLE001
        pass
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
