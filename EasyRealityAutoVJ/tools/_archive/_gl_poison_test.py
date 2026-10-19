# -*- coding: utf-8 -*-
"""验证 GL 锁的「污染自愈」：某线程卡在驱动里持锁不放时，**不陪葬**。

背景（2026-09-26 夜场，19:46 版仍卡死的第二轮）：
    现场日志抓到 —— 1 个线程持锁卡在 `glfwDestroyWindow` 永不返回，
    于是 3 个线程卡在 `destroy()` 的 `with _CREATE_LOCK:`、2 个卡在 `_init()` 的同类语句
    （**无限等 ⇒ 陪葬**），其余卡在 `decode()` 反复超时。
    所有解码线程全冻 ⇒ 玩家永不出帧 ⇒ 界面「未响应」，只能重开软件。

修复：等锁一律带超时；某个线程持锁超过 `GL_LOCK_POISON_S`(5s) 就判定驱动挂死，
之后所有 GL 操作立即失败，逼上层转软解（画面继续，只是没了 GPU 加速）。

**本测试完全不碰 GL / GPU**（只操作那把锁），所以随时可跑、零风险、可 CI 化。

用法：venv\\Scripts\\python.exe tools\\_gl_poison_test.py
"""
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import glctx                                              # noqa: E402


def main():
    print("=" * 78)
    print("GL 锁「污染自愈」验证")
    print("=" * 78)
    print("POISON 阈值: %.1f 秒" % glctx.GL_LOCK_POISON_S)

    if glctx.gl_poisoned():
        print("[!] 本次导入前锁已是污染状态，测试无意义")
        return 1

    # 1) 一个线程拿住锁**永不释放** —— 精确模拟「卡在 NVIDIA 驱动里出不来」
    held = threading.Event()

    def holder():
        glctx._gl_enter(2.0)
        held.set()
        while True:                      # 永不 _gl_exit：模拟驱动挂死
            time.sleep(0.5)

    th = threading.Thread(target=holder, name="fake-hung-driver", daemon=True)
    th.start()
    if not held.wait(3.0):
        print("[!] 占位线程没能拿到锁，测试作废")
        return 1
    print("占位线程已拿到 GL 锁并「卡死」（不再释放）")

    # 2) 另一线程持续尝试进临界区，记录每次等待时长
    t0 = time.perf_counter()
    waits = []
    poison_at = None
    for _ in range(24):
        a = time.perf_counter()
        ok = glctx._gl_enter(0.3)
        dt = time.perf_counter() - a
        if ok:
            glctx._gl_exit()
        waits.append(dt)
        if glctx.gl_poisoned() and poison_at is None:
            poison_at = time.perf_counter() - t0
            break
        time.sleep(0.25)

    poisoned = glctx.gl_poisoned()
    print()
    print("检测到污染: %s   用时约 %.1f 秒" % (poisoned, poison_at if poison_at else -1))
    print("前 4 次等待(秒): %s" % "  ".join("%.2f" % w for w in waits[:4]))
    print("后 4 次等待(秒): %s" % "  ".join("%.2f" % w for w in waits[-4:]))

    # 3) 污染后必须**零等待**（否则仍在陪葬）
    a = time.perf_counter()
    ok = glctx._gl_enter(0.3)
    tail = time.perf_counter() - a
    print("污染后再尝试: 返回 %s，耗时 %.4f 秒" % (ok, tail))

    print()
    ok_all = (poisoned
              and poison_at is not None
              and poison_at <= glctx.GL_LOCK_POISON_S + 2.0
              and (not ok)
              and tail < 0.05)
    if ok_all:
        print("结论: OK —— 挂死 5 秒内被识别，之后**立即失败**（上层可转软解，不会永久卡死）")
    else:
        print("结论: [!] 未达预期（见上面数字：应在 ~5s 内 poison，之后耗时≈0、返回 False）")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
