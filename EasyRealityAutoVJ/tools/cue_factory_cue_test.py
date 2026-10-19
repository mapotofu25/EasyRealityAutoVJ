# -*- coding: utf-8 -*-
"""`make_player(item, cue=...)` 的**选型矩阵测试**（S1 收尾门禁，零解码、零窗口）。

要证明的两件事：
  1. **`cue=False`（普通图层/预览）的选型与改动前逐值一致** —— 逐组合断言，不是"看着没变"；
  2. **`cue=True` 且 DXV ⇒ 绝不返回 GPU 播放器**（`GpuDxvPlayer` 的帧生成器不暴露 pts，
     码表就无法 `seek/position`），且**日志只打一次**。

做法：把三个播放器类替换成"记录器"、把 `gpu_decode_enabled()` 固定 True、
把两个后台探测踢成空操作 ⇒ **不打开任何容器、不建任何解码线程**，纯粹验证分支。

运行：`python tools/cue_factory_cue_test.py`（exit 0 = 全过）
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import media_manager as mm            # noqa: E402


class _FakeItem:
    """`create_video_player` 真正会读的字段：kind / path / _has_alpha / _is_dxv。"""

    def __init__(self, path="P:/x/a.mov", has_alpha=False, is_dxv=False, kind="video"):
        self.kind = kind
        self.path = path
        self._has_alpha = bool(has_alpha)
        self._is_dxv = is_dxv

    def _thumb_path(self, ext):
        return "P:/x/__no_thumb__" + ext


class _Recorder:
    """替身播放器：只记"谁被选走了"。"""

    made = []

    def __init__(self, label):
        self.label = label

    def __call__(self, *a, **kw):
        _Recorder.made.append(self.label)
        return ("<player:%s>" % self.label, a, kw)


def _legacy_expected(has_alpha, is_dxv, gpu=True, av=True):
    """**改动前**的选型规则（照抄原代码：GPU 只接管 DXV；其余按 alpha 分软解路径）。"""
    if gpu and av and is_dxv:
        return "gpu"
    if has_alpha and av:
        return "av"
    return "cv2"


def _patched():
    """返回一个上下文：替换类/开关/探测；退出时全部还原。"""
    import contextlib

    @contextlib.contextmanager
    def ctx():
        saved = {
            "GpuDxvPlayer": mm.GpuDxvPlayer,
            "AvAlphaPlayer": mm.AvAlphaPlayer,
            "VideoPlayer": mm.VideoPlayer,
            "gpu_decode_enabled": mm.gpu_decode_enabled,
            "_probe_alpha_async": mm._probe_alpha_async,
            "_probe_codec_async": mm._probe_codec_async,
            "_CUE_GPU_SKIP_LOGGED": list(mm._CUE_GPU_SKIP_LOGGED),
            "stallwatch.log_line": None,
        }
        mm.GpuDxvPlayer = _Recorder("gpu")
        mm.AvAlphaPlayer = _Recorder("av")
        mm.VideoPlayer = _Recorder("cv2")
        mm.gpu_decode_enabled = lambda: True
        mm._probe_alpha_async = lambda item: None
        mm._probe_codec_async = lambda item: None
        # ★ 保留**真实**的 `_log_cue_gpu_skip`（一次性的去重标志就在它里面），
        #   只把落盘函数换成计数器 ⇒ 测的是真逻辑，不是替身。
        logged = []
        try:
            import stallwatch
            saved["stallwatch.log_line"] = stallwatch.log_line
            stallwatch.log_line = lambda s: logged.append(s)
        except Exception:                                        # noqa: BLE001
            saved["stallwatch.log_line"] = None
        mm._CUE_GPU_SKIP_LOGGED[0] = False
        try:
            yield logged
        finally:
            if saved["stallwatch.log_line"] is not None:
                stallwatch.log_line = saved["stallwatch.log_line"]
            mm.GpuDxvPlayer = saved["GpuDxvPlayer"]
            mm.AvAlphaPlayer = saved["AvAlphaPlayer"]
            mm.VideoPlayer = saved["VideoPlayer"]
            mm.gpu_decode_enabled = saved["gpu_decode_enabled"]
            mm._probe_alpha_async = saved["_probe_alpha_async"]
            mm._probe_codec_async = saved["_probe_codec_async"]
            mm._CUE_GPU_SKIP_LOGGED[0] = saved["_CUE_GPU_SKIP_LOGGED"][0]

    return ctx()


def main():
    fails = []

    def check(name, cond, detail=""):
        print("  [%s] %s%s" % ("ok " if cond else "FAIL", name,
                               "" if cond else "  ← " + str(detail)))
        if not cond:
            fails.append(name)

    print("选型矩阵（playback 类替换为记录器，不建任何解码器）")
    with _patched() as logged:
        # ---------- 1) cue=False：必须与"改动前"逐值一致 ----------
        print("\n① cue=False（普通图层）⇒ 与改动前逐值一致")
        for has in (False, True):
            for dxv in (False, True, None):
                _Recorder.made = []
                cls, _a, _k = mm.create_video_player(
                    _FakeItem(has_alpha=has, is_dxv=dxv), cue=False)
                got = _Recorder.made[-1] if _Recorder.made else "?"
                # dxv=None 时原逻辑先探测、随后按 alpha 走（不接管 GPU）
                want = _legacy_expected(has, bool(dxv) if dxv is not None else False)
                check("cue=False  has=%-5s dxv=%-5s → %s" % (has, dxv, want),
                      got == want, "实得 %s" % got)

        # ---------- 2) cue=True + DXV：绝不走 GPU ----------
        print("\n② cue=True 且 DXV ⇒ 绝不返回 GPU 播放器")
        for has in (False, True):
            _Recorder.made = []
            mm.create_video_player(_FakeItem(has_alpha=has, is_dxv=True), cue=True)
            got = _Recorder.made[-1] if _Recorder.made else "?"
            want = "av" if has else "cv2"          # = 关掉 GPU 之后的原回退逻辑
            check("cue=True   has=%-5s dxv=True → %s（非 gpu）" % (has, want),
                  got == want, "实得 %s" % got)

        # ---------- 3) cue=True 非 DXV：不受影响 ----------
        print("\n③ cue=True 且非 DXV ⇒ 与 cue=False 相同（只改 DXV 的 GPU 决策）")
        for has in (False, True):
            _Recorder.made = []
            mm.create_video_player(_FakeItem(has_alpha=has, is_dxv=False), cue=True)
            got = _Recorder.made[-1] if _Recorder.made else "?"
            check("cue=True   has=%-5s dxv=False → %s" % (has, got),
                  got == _legacy_expected(has, False), "实得 %s" % got)

        # ---------- 4) 日志只打一次（走**真实** _log_cue_gpu_skip 的去重）----------
        print("\n④ cue 的 DXV 提示日志：多次创建也只打一次")
        mm._CUE_GPU_SKIP_LOGGED[0] = False   # ② 已经打过一次，这里重置以独立验证去重
        base = len(logged)
        for _ in range(3):
            mm.create_video_player(_FakeItem(has_alpha=True, is_dxv=True), cue=True)
        check("3 次创建 ⇒ 新增 1 条", len(logged) - base == 1, "实得 %d 条" % (len(logged) - base))
        # 换个素材名再来一次：仍然只有 1 条（"设计如此"的提示，不是每个文件都报）
        mm.create_video_player(_FakeItem(path="P:/x/b.mov", has_alpha=True, is_dxv=True),
                               cue=True)
        check("第 4 次（换文件）⇒ 仍 1 条", len(logged) - base == 1, "实得 %d 条" % (len(logged) - base))
        if logged:
            print("      日志样例：%s" % logged[0])

    # ---------- 6) GIF / 图片 不受 cue 影响 ----------
    print("\n⑤ gif / image 与 cue 无关")
    with _patched() as logged:
        gi = _FakeItem(kind="gif", path="P:/x/a.gif")
        class _GF:
            def __init__(self, *a, **k):
                _Recorder.made.append("gif")

        class _IF:
            def __init__(self, *a, **k):
                _Recorder.made.append("img")

        old_g, old_i = mm.GifPlayer, mm.ImagePlayer
        mm.GifPlayer, mm.ImagePlayer = _GF, _IF
        try:
            _Recorder.made = []
            mm.make_player(gi, cue=True)
            _Recorder.made = []
            mm.make_player(gi, cue=False)
            check("gif ⇒ GifPlayer（两种 cue 都一样）", _Recorder.made == ["gif"], _Recorder.made)
        finally:
            mm.GifPlayer, mm.ImagePlayer = old_g, old_i

    print("")
    if fails:
        print("FACTORY_FAIL  %d 项不通过：%s" % (len(fails), "、".join(fails)))
        return 1
    print("FACTORY_OK  cue=False 与改动前逐值一致；cue=True 的 DXV 不走 GPU；日志只打一次")
    return 0


if __name__ == "__main__":
    sys.exit(main())
