# -*- coding: utf-8 -*-
"""组 B 修复自测（**离屏 Qt 实测**，不是源码结构断言）。

覆盖：
  B1  关窗 / 关码表前对未保存改动弹三选（保存 / 不保存 / 取消）；取消 => e.ignore() 数据还在
  B2  内嵌素材包导入时先解压 `media/`，导入后 media_paths 能映射到真实文件（换机可打开）
  B3  撤销 / 重做不再把当前曲目打回第 1 首（`_ti` 纳入快照并 clamp）
  B4  素材加入时调用 `probe.probe()` → `MediaRef.duration_sec` / `has_alpha` 落地，
      使 precheck「超出素材时长」分支可达

反向验证（改回旧写法 ⇒ 断言失败）：
  · B1：把 `closeEvent` 换回"只 shutdown"的旧实现 ⇒ "脏数据+取消必须不关窗"断言失败
  · B2：把 `_extract_embedded_media` 换回"永远返回 None"的旧行为 ⇒ "导入后素材齐全"断言失败
  · B3：把 `_restore_snapshot` 换回无条件 `_ti = 0` ⇒ "撤销后仍停在第 2 首"断言失败

跑法：
    P:\\ERAVJ\\EasyRealityCue\\venv\\Scripts\\python.exe  tools/_fix_b_verify.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from PySide6.QtGui import QCloseEvent                              # noqa: E402
from PySide6.QtWidgets import QApplication                        # noqa: E402

_APP = QApplication.instance() or QApplication([])

from ercue.model import ercue_io, precheck                        # noqa: E402
from ercue.model.cuesheet import (                                # noqa: E402
    CompLayer, CueItem, CueSheet, Identity, MediaRef, TrackEntry,
)
from ercue.ui.window import MainWindow                            # noqa: E402

_RESULTS = []


def check(name, cond, detail=""):
    _RESULTS.append((name, bool(cond), detail))
    tag = "PASS" if cond else "FAIL"
    print("%s  %s%s" % (tag, name, ("   << " + detail) if detail and not cond else ""))


class DummyCfg:
    """替掉真 config，避免污染 `%LOCALAPPDATA%\\EasyRealityCue`。"""

    def __init__(self):
        self.d = {"last_dir": "", "embed_media": False, "recent_files": []}

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v, save=True):
        self.d[k] = v

    def recent(self):
        return list(self.d.get("recent_files", []))

    def add_recent(self, p):
        p = os.path.abspath(str(p))
        self.d["recent_files"] = [p] + [x for x in self.d["recent_files"] if x != p]
        return self.d["recent_files"]


def make_window():
    w = MainWindow()
    w._interactive = True          # 我们要走"会弹窗"的分支，但把弹窗本身替换掉
    w.cfg = DummyCfg()
    return w


def make_real_video(path, seconds=3.0, fps=25):
    """用 PyAV 真造一个 `seconds` 秒的 mp4（B4 要真实时长）。失败返回 False。"""
    import numpy as np
    import av
    try:
        c = av.open(path, mode="w")
        st = c.add_stream("mpeg4", rate=fps)
        st.width, st.height, st.pix_fmt = 64, 48, "yuv420p"
        for i in range(int(seconds * fps)):
            arr = (np.zeros((48, 64, 3), np.uint8) + (i * 5) % 255)
            f = av.VideoFrame.from_ndarray(arr, format="rgb24")
            for pk in st.encode(f):
                c.mux(pk)
        for pk in st.encode():
            c.mux(pk)
        c.close()
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except Exception:                                          # noqa: BLE001
        traceback.print_exc()
        return False


def build_two_track_sheet():
    sheet = CueSheet(title="两首")
    for n in ("A", "B"):
        te = TrackEntry(track_key="k" + n, identity=Identity(title=n, artist="x",
                                                             duration_sec=60.0, bpm=120.0))
        te.identity.ensure_meta_id()
        te.composition.layers.append(CompLayer(lyr_id="L" + n, name="层", z=0))
        sheet.tracks.append(te)
    return sheet


# ==========================================================================
# B1
# ==========================================================================
def b1_close_accepts(window, choice):
    """把 `_confirm_discard` 换成返回 `choice`，跑一次 closeEvent，返回事件的 accepted。"""
    window._confirm_discard = lambda _sessions, _label="": choice
    ev = QCloseEvent()
    window.closeEvent(ev)
    return ev.isAccepted(), ev


def test_b1():
    # --- 取消 ⇒ 不关窗（脏数据还在） ---
    w = make_window()
    w.session.dirty = True
    acc, _ev = b1_close_accepts(w, "cancel")
    check("B1 脏数据 + 取消 ⇒ 窗口不关（e.ignore）", acc is False, "accepted=%r" % acc)
    check("B1 取消后 session 仍在且仍标脏",
          w.session is not None and w.session.dirty, "session=%r" % (w.session,))
    w.shutdown()

    # --- 不保存 ⇒ 正常关（仍是丢数据，但用户显式选了"不保存"） ---
    w = make_window()
    w.session.dirty = True
    acc, _ev = b1_close_accepts(w, "discard")
    check("B1 脏数据 + 不保存 ⇒ 正常关窗", acc is True, "accepted=%r" % acc)
    w.shutdown()

    # --- 保存 ⇒ 文件落盘、dirty 清掉、正常关 ---
    w = make_window()
    d = tempfile.mkdtemp(prefix="ercue_b1_")
    p = os.path.join(d, "saved.ercue")
    w.session.backing_path = p
    w.session.dirty = True
    acc, _ev = b1_close_accepts(w, "save")
    check("B1 脏数据 + 保存 ⇒ 已写盘且 dirty=False",
          acc is True and os.path.isfile(p) and not w.session.dirty,
          "accepted=%r file=%r dirty=%r" % (acc, os.path.isfile(p), w.session.dirty))
    w.shutdown()

    # --- 无脏数据 ⇒ 不打扰 ---
    w = make_window()
    w.session.dirty = False
    called = {"n": 0}

    def _spy(_s, _l=""):
        called["n"] += 1
        return "cancel"

    w._confirm_discard = _spy
    ev = QCloseEvent()
    w.closeEvent(ev)
    check("B1 无脏数据 ⇒ 不弹窗、直接关", ev.isAccepted() and called["n"] == 0,
          "accepted=%r calls=%d" % (ev.isAccepted(), called["n"]))
    w.shutdown()

    # --- 关闭单张脏码表 ⇒ 取消则不关 ---
    w = make_window()
    w.session.dirty = True
    before = len(w.sessions)
    w._confirm_discard = lambda _s, _l="": "cancel"
    w._close_sheet()
    check("B1 关闭码表 + 取消 ⇒ 码表仍在",
          len(w.sessions) == before and w.session is not None,
          "before=%d after=%d" % (before, len(w.sessions)))
    w.shutdown()


# ==========================================================================
# B2
# ==========================================================================
def build_embedded_package(seconds=1.0):
    """造一个 embed=True 的包，返回 (干净的"另一台机器"目录里的包路径, 原素材路径)。"""
    ws = tempfile.mkdtemp(prefix="ercue_b2_src_")
    a = os.path.join(ws, "a.mp4")
    make_real_video(a, seconds)
    sheet = CueSheet(title="内嵌测试", media=[MediaRef(clip_id="c1", name="a.mp4")])
    te = TrackEntry(track_key="k", identity=Identity(title="T", artist="A",
                                                     duration_sec=10.0, bpm=120.0))
    te.identity.ensure_meta_id()
    ly = CompLayer(lyr_id="L1", name="层", z=0)
    ly.items.append(CueItem(item_id="i1", clip_id="c1", at_sec=0.0, end_sec=1.0).normalized())
    te.composition.layers.append(ly)
    sheet.tracks.append(te)
    pkg = os.path.join(ws, "pack.ercue")
    ercue_io.save(sheet, pkg, media_paths={"c1": a}, embed=True, app_version="test")
    # 模拟"换机"：只把 .ercue 带到一个干净目录，原素材不在
    dest = tempfile.mkdtemp(prefix="ercue_b2_dst_")
    pkg2 = os.path.join(dest, "pack.ercue")
    shutil.copy(pkg, pkg2)
    return pkg2, a


def test_b2():
    pkg2, a = build_embedded_package()
    check("B2 前置：包内含嵌入素材且原素材仍在（用于对照）", os.path.isfile(a))
    os.remove(a)                                               # 源素材从磁盘删掉 = 换机
    check("B2 前置：原素材已从磁盘移除", not os.path.isfile(a))

    w = make_window()
    w._interactive = False
    w.import_sheet(pkg2)
    s = w.session
    path = s.media_paths.get("c1") or ""
    missing = [m.name for m in s.sheet.media
               if not os.path.isfile(s.media_paths.get(m.clip_id) or "")]
    extracted = os.path.isdir(pkg2 + ".media")
    w.shutdown()
    check("B2 导入内嵌包 ⇒ 解出 <包>.media 目录", extracted)
    check("B2 导入内嵌包 ⇒ media_paths 映射到真实文件", bool(path) and os.path.isfile(path),
          "path=%r" % path)
    check("B2 导入内嵌包 ⇒ 无缺失素材", missing == [], "missing=%r" % missing)

    # 幂等：再导一次不重复解压（目录 mtime 不变即可简单验证不报错、仍齐全）
    w = make_window()
    w._interactive = False
    w.import_sheet(pkg2)
    s = w.session
    missing2 = [m.name for m in s.sheet.media
                if not os.path.isfile(s.media_paths.get(m.clip_id) or "")]
    w.shutdown()
    check("B2 重复导入（幂等）⇒ 依然无缺失", missing2 == [], "missing=%r" % missing2)


# ==========================================================================
# B3
# ==========================================================================
def test_b3():
    w = make_window()
    w._interactive = False
    w.add_track("第一首")                       # _ti = 0
    w.add_track("第二首")                       # _ti = 1
    check("B3 前置：已切到第 2 首", w._ti == 1, "_ti=%r" % w._ti)
    w.add_layer("在第二首加一层")               # 这一步 _push_undo 记录 ti=1
    check("B3 加层后仍在第 2 首", w._ti == 1, "_ti=%r" % w._ti)
    layers_before = len(w._track().layers)
    w.undo()
    check("B3 撤销后仍停在第 2 首（不打回第 1 首）", w._ti == 1, "_ti=%r" % w._ti)
    check("B3 撤销后确实回滚了那一层",
          len(w._track().layers) == layers_before - 1,
          "layers=%d (before=%d)" % (len(w._track().layers), layers_before))
    w.redo()
    check("B3 重做后仍在第 2 首且层恢复",
          w._ti == 1 and len(w._track().layers) == layers_before,
          "_ti=%r layers=%d" % (w._ti, len(w._track().layers)))

    # 删曲目后撤销：_ti 要在有效范围内（不越界）
    w.del_track()                                # 删第 2 首 → _ti = 0
    w.undo()
    n = len(w.session.sheet.tracks)
    check("B3 删曲目后撤销 ⇒ _ti 在有效范围且不越界",
          0 <= w._ti <= max(0, n - 1), "_ti=%r tracks=%d" % (w._ti, n))
    w.shutdown()


# ==========================================================================
# B4
# ==========================================================================
def test_b4():
    w = make_window()
    w._interactive = False
    ws = tempfile.mkdtemp(prefix="ercue_b4_")
    vp = os.path.join(ws, "clip.mp4")
    ok_video = make_real_video(vp, 3.0)
    check("B4 前置：合成 3 秒测试视频", ok_video, "ok=%r" % ok_video)

    w.add_track("歌", duration_sec=60.0, bpm=120.0)
    ids = w.add_media_paths([vp])
    ref = w.session.sheet.media_by_id(ids[0]) if ids else None
    dur = float(getattr(ref, "duration_sec", 0.0) or 0.0)
    check("B4 加入素材 ⇒ probe 写回 duration_sec（≈3s）", 2.5 <= dur <= 3.5, "dur=%r" % dur)
    check("B4 加入素材 ⇒ has_alpha 被显式写入（bool）",
          isinstance(getattr(ref, "has_alpha", None), bool), "has_alpha=%r" % getattr(ref, "has_alpha", None))

    # 放一个 10 秒的片段（> 素材 3 秒）⇒ precheck 应报 "超出素材本身时长"
    ly = w._layers()[0]
    it = w.add_cue(ly.lyr_id, ids[0], 0.0)
    it.end_sec = 10.0
    it.normalized()
    rep = precheck.check_sheet(w.session.sheet, w.session.media_paths)
    beyond = [o for o in rep.overlong if o.reason == "beyond_media"]
    check("B4 precheck「超出素材时长」分支可达（旧实现永久失效）",
          len(beyond) >= 1, "overlong=%r" % [(o.item_id, o.reason) for o in rep.overlong])
    w.shutdown()


# ==========================================================================
# 反向验证
# ==========================================================================
def reverse_b1():
    orig = MainWindow.closeEvent

    def old_close(self, e):                    # 旧实现：只 shutdown，不看 dirty
        self.shutdown()
        super(MainWindow, self).closeEvent(e)

    MainWindow.closeEvent = old_close
    try:
        w = make_window()
        w.session.dirty = True
        acc, _ev = b1_close_accepts(w, "cancel")
        w.shutdown()
    finally:
        MainWindow.closeEvent = orig
    # 旧实现下：脏数据 + 取消，窗口**照样关**（accept=True）⇒ 修复断言会失败
    return acc is False                        # 我们期望 False（证明修复有效）


def reverse_b2():
    orig = MainWindow._extract_embedded_media
    MainWindow._extract_embedded_media = staticmethod(lambda _path: None)   # 旧行为：从不解压
    try:
        pkg2, a = build_embedded_package()
        if os.path.isfile(a):
            os.remove(a)
        w = make_window()
        w._interactive = False
        w.import_sheet(pkg2)
        s = w.session
        missing = [m.name for m in s.sheet.media
                   if not os.path.isfile(s.media_paths.get(m.clip_id) or "")]
        w.shutdown()
    finally:
        MainWindow._extract_embedded_media = orig
    return missing == []                       # 旧行为下会非空 ⇒ 期望 False（证明修复有效）


def reverse_b3():
    orig = MainWindow._restore_snapshot

    def old_restore(self, sess, snap):         # 旧实现：无条件 _ti = 0
        sess.load_snapshot(snap)
        sess.dirty = True
        self.timeline.set_selected("", "")
        self.props_panel.item = None
        self.props_panel.layer = None
        self._ti = 0
        self._refresh_all()

    MainWindow._restore_snapshot = old_restore
    try:
        w = make_window()
        w._interactive = False
        w.add_track("第一首")
        w.add_track("第二首")
        w.add_layer("层")
        w.undo()
        ti = w._ti
        w.shutdown()
    finally:
        MainWindow._restore_snapshot = orig
    return ti == 1                             # 旧行为下 ti=0 ⇒ 期望 False（证明修复有效）


# ==========================================================================
def main():
    print("=" * 72)
    print("组 B 修复自测（离屏 Qt）")
    print("=" * 72)
    for name, fn in (("B1", test_b1), ("B2", test_b2), ("B3", test_b3), ("B4", test_b4)):
        try:
            fn()
        except Exception:                                      # noqa: BLE001
            traceback.print_exc()
            check("%s 未抛异常" % name, False, "见上方 traceback")

    print("-" * 72)
    print("反向验证（改回旧写法 ⇒ 对应修复断言必须失败）")
    for name, fn, desc in (
        ("B1", reverse_b1, "旧 closeEvent（不查 dirty）"),
        ("B2", reverse_b2, "旧 _extract_embedded_media（从不解压）"),
        ("B3", reverse_b3, "旧 _restore_snapshot（_ti 归零）"),
    ):
        try:
            still_ok = fn()
            # reverse_* 返回"修复后的期望是否成立"；旧写法下应为 False
            check("REVERSE %s：%s 下修复断言失败（证明修复有效）" % (name, desc),
                  still_ok is False, "still_ok=%r" % still_ok)
        except Exception:                                      # noqa: BLE001
            traceback.print_exc()
            check("REVERSE %s 未抛异常" % name, False, "见上方 traceback")

    print("=" * 72)
    n_fail = sum(1 for _n, ok, _d in _RESULTS if not ok)
    for n, ok, d in _RESULTS:
        if not ok:
            print("  FAIL: %s %s" % (n, d))
    print("总计 %d 项，失败 %d 项" % (len(_RESULTS), n_fail))
    print("=" * 72)
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
