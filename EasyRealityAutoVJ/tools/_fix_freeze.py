# -*- coding: utf-8 -*-
"""修两个现场问题（2026-10-01）：

① 「不在逐拍为什么有两个框」—— 我上一轮改高亮时把 `trans_active` 条件去掉了，
   而引擎**过渡结束后不会把 `prev_idx` 重置成 -1**（只在换池/硬切时置 -1）
   ⇒ 永远有两个金框。修：只在 `lay.trans_active` 为真时才把 prev_idx 也算上。

② 「素材会突然卡住不动，直到下一个切进来才恢复」——
   `_loop` 里 `_open()` 失败直接 `return`、取帧生成器收工直接 `break`
   ⇒ 这个播放器**永久死掉**，画面停在最后一帧，直到下次切换新建播放器才活。
   修：打开失败 / 取帧收工 ⇒ **退避后重开容器再试**（最多 `_REOPEN_MAX` 轮），
   只有连续失败那么多次才真收工；期间一律 sleep，不忙等。
"""
import io
import os
import sys

ROOT = r"P:\AutoVJ"
MM = os.path.join(ROOT, "src", "media_manager.py")
PN = os.path.join(ROOT, "src", "panels.py")
PAT = []


def rep(rel, old, new, cnt=1):
    PAT.append((rel, old, new, cnt))


# ---------------- ① 高亮：只在过渡中才算上一个 ----------------
rep("src/panels.py",
    """            for idx in (getattr(lay, "cur", -1), getattr(lay, "prev_idx", -1)):""",
    """            # ⚠ `prev_idx` 只在**过渡进行中**才算"正在使用"：引擎过渡结束后**不会**
            #   把它重置成 -1（只在换池/硬切时置 -1）⇒ 无条件带上它就会
            #   **永远有两个金框**（用户 2026-10-01 反馈「不在逐拍为什么有两个框」）。
            idxs = [getattr(lay, "cur", -1)]
            if getattr(lay, "trans_active", False):
                idxs.append(getattr(lay, "prev_idx", -1))
            for idx in idxs:""")

# ---------------- ② _loop 自愈 ----------------
rep("src/media_manager.py",
    """_SOFT_EMPTY_GIVEUP = 30            # 连续空转这么多趟 ⇒ 放弃这个素材""",
    """_SOFT_EMPTY_GIVEUP = 30            # 连续空转这么多趟 ⇒ 放弃这个素材
# 解码线程连续"打不开容器 / 拿不到帧"这么多次 ⇒ 才真的收工（期间退避重试，见 `_loop`）。
# 用户 2026-10-01 报「素材会突然卡住不动，直到下一个切进来才恢复」就是因为它原来**一次
# 失败就永久死亡**；现在会自愈重开，只有连续失败这么多次才放弃。
_REOPEN_MAX = 6""")

rep("src/media_manager.py",
    """    def _loop(self):
        try:
            self.c, self.st = self._open()
        except Exception:
            return
        if self._dead:                     # 打开期间就被关掉了
            self._release()
            return
        interval = 1.0 / max(self.fps, 1.0)
        gen_gpu = (self.gpu or self.gpu_want) and gpu_decode_enabled()
        frames = self._gpu_frames() if gen_gpu else self._soft_frames()
        while not self._dead:
            # ★ 只有"该不该走 GPU"这一件事允许中途换生成器：设置里开关 GPU 解码
            #   立刻生效（以前只在建播放器那一刻定一次 ⇒ 改完开关得重开软件才行）。
            want = (self.gpu or self.gpu_want) and gpu_decode_enabled()
            if want != gen_gpu:
                gen_gpu = want
                frames = self._gpu_frames() if want else self._soft_frames()
            if not self._active:
                if self._warm <= 0:
                    time.sleep(0.05)
                    continue
                self._warm -= 1            # 预热：不活跃也只解这一帧
            t0 = time.perf_counter()
            try:
                img = next(frames)
            except StopIteration:
                break
            except Exception:
                time.sleep(0.02)
                continue
            if img is not None:
                self._img = img
            wait = float(interval / self.speed - (time.perf_counter() - t0))
            if wait > 0:
                time.sleep(wait)
        self._release()""",
    '''    def _loop(self):
        """解码线程主体。

        ⚠⚠ 必须能**自愈重开**（2026-10-01 用户报「素材会突然卡住不动，直到下一个切进来才恢复」）：
          原来 `_open()` 一抛异常就 `return`、取帧生成器一收工就 `break`
          ⇒ 这个播放器**永久死掉**，画面停在最后一帧（引擎拿的是 `_img` 的旧值），
          直到下一次切素材新建播放器才恢复正常 —— 与用户描述完全一致。
          现在：打开失败 / 取帧收工 ⇒ **退避后重开容器再试**（最多 `_REOPEN_MAX` 轮），
          只有连续失败那么多次才真的收工；期间一律 sleep 退避，**绝不忙等**。
        \"\"\"
        tries = 0
        while not self._dead:
            # ---- ① 打开容器：失败就退避重试，而不是直接死掉 ----
            try:
                self.c, self.st = self._open()
            except Exception:                                      # noqa: BLE001
                tries += 1
                if tries >= _REOPEN_MAX:
                    _log_decoder_giveup(self.path, "连续 %d 次打开失败" % tries)
                    break
                time.sleep(min(2.0, 0.25 * tries))
                continue
            if self._dead:                 # 打开期间就被关掉了
                break
            interval = 1.0 / max(self.fps, 1.0)
            gen_gpu = (self.gpu or self.gpu_want) and gpu_decode_enabled()
            frames = self._gpu_frames() if gen_gpu else self._soft_frames()
            produced = False
            while not self._dead:
                # ★ 只有"该不该走 GPU"这一件事允许中途换生成器：设置里开关 GPU 解码
                #   立刻生效（以前只在建播放器那一刻定一次 ⇒ 改完开关得重开软件才行）。
                want = (self.gpu or self.gpu_want) and gpu_decode_enabled()
                if want != gen_gpu:
                    gen_gpu = want
                    frames = self._gpu_frames() if want else self._soft_frames()
                if not self._active:
                    if self._warm <= 0:
                        time.sleep(0.05)
                        continue
                    self._warm -= 1        # 预热：不活跃也只解这一帧
                t0 = time.perf_counter()
                try:
                    img = next(frames)
                except StopIteration:
                    break                  # 生成器收工 ⇒ 走下面"重开再试"
                except Exception:                                      # noqa: BLE001
                    time.sleep(0.02)
                    continue
                if img is not None:
                    self._img = img
                    produced = True
                wait = float(interval / self.speed - (time.perf_counter() - t0))
                if wait > 0:
                    time.sleep(wait)
            if self._dead:
                break
            # ---- ② 取帧收工：出过帧说明素材是好的，重置计数；否则累加 ----
            if produced:
                tries = 0
            else:
                tries += 1
            if tries >= _REOPEN_MAX:
                _log_decoder_giveup(self.path, "连续 %d 轮取帧收工" % tries)
                break
            self._release()
            time.sleep(min(2.0, 0.25 * tries))
        self._release()''')


def main():
    for i, item in enumerate(PAT, 1):
        if item is None:
            continue
        rel, old, new, cnt = item
        s = io.open(os.path.join(ROOT, rel), encoding="utf-8").read()
        n = s.count(old)
        if n != cnt:
            print("✗ 第 %d 处（%s）命中 %d 次（应为 %d）" % (i, rel, n, cnt))
            print("   锚点首行：", old.splitlines()[0][:80])
            return 1
    for item in PAT:
        if item is None:
            continue
        rel, old, new, cnt = item
        p = os.path.join(ROOT, rel)
        s = io.open(p, encoding="utf-8").read()
        io.open(p, "w", encoding="utf-8", newline="\n").write(s.replace(old, new))
        print("✓", rel)
    import py_compile
    for f in ("src/media_manager.py", "src/panels.py"):
        py_compile.compile(os.path.join(ROOT, f), doraise=True)
    print("语法 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
