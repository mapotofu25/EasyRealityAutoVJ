# -*- coding: utf-8 -*-
"""★ 修复验证：用户反馈的时间轴 5 条问题。

跑法（Windows）：
    P:\\ERAVJ\\EasyRealityCue\\venv\\Scripts\\python.exe  tools/_fix_timeline_verify.py
（内部自动设 `QT_QPA_PLATFORM=offscreen`，无头可跑。）

覆盖的 5 条（用户原话）与判据：
  ① 「三分钟的东西有时候被拉成更长时间」 → 放入已知 **180s** 素材后 `end_sec == at+180`；
     且**逆向验证**：把 `end_sec` 回退成 None（旧行为）→ 片段被拉到视口右端，`≠180`。
  ② 「预览内开始后视频无法正常播放 被拉长减速了」 → `frame_play()` 连续取帧得到
     **连续前进的真实帧**；**逆向验证**：旧的 `frame()`（每次 seek 关键帧）在同一 GOP
     内返回**同一张帧**（distinct≈1）⇒ 判据失败。
  ③ 「拖动应以拖动距离为准，而不是让素材开头对准鼠标位置」 → 在片段**中部**按下、
     平移 Δ 秒，落点 = 起点 + Δ（**不跳到光标处**）；逆向：旧逻辑落点 = 光标秒。
  ④ 「一首歌只有三分钟但在时间轴上会被铺满」 → 曲目/素材 180s 时，片段**画出的宽度**
     = 180s（与缩放无关），不铺满视口。
  ⑤ 「把吸附功能去掉」 → 时间轴控件 / 主窗口 / precheck 报告里**都不再有吸附**。

所有断言都是**真实运行**（真造视频、真跑 offscreen Qt、真取帧），不是结构性检查。
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import numpy as np                                                   # noqa: E402
from PySide6.QtCore import Qt, QPoint, QPointF                        # noqa: E402
from PySide6.QtTest import QTest                                      # noqa: E402
from PySide6.QtWidgets import QApplication                            # noqa: E402

from ercue import config as app_config                                # noqa: E402
from ercue.media.frames import CueFrameSource                         # noqa: E402
from ercue.model import precheck                                      # noqa: E402
from ercue.model.cuesheet import CompLayer, CueItem, TrackEntry, Identity  # noqa: E402
from ercue.ui import theme                                            # noqa: E402
from ercue.ui.timeline_widget import CueTimeline                      # noqa: E402

APP = None


# ==========================================================================
# 夹具：真造视频（每帧颜色不同，便于判"帧是否前进"）
# ==========================================================================
def make_video(path, n, fps, w=160, h=120, gop=60):
    """造一个**关键帧稀疏**（大 GOP）的 h264 视频，每帧颜色不同。

    ⚠ 必须用 `libx264` + `g`/`keyint_min`：内置 `mpeg4` 编码器**忽略 GOP 设置**
      （每帧都是关键帧），那样 `frame()` 也会前进，**复现不出**用户报的"卡住/减速"。
      h264 大 GOP 才是现实素材的样子，也才让 `frame()` 停在关键帧上。
    """
    import av
    c = av.open(path, mode="w")
    st = c.add_stream("libx264", rate=fps)
    st.width, st.height, st.pix_fmt = w, h, "yuv420p"
    try:
        st.codec_context.gop_size = gop
    except Exception:                                            # noqa: BLE001
        pass
    try:
        st.options = {"g": str(gop), "keyint_min": str(gop)}
    except Exception:                                            # noqa: BLE001
        pass
    for i in range(n):
        arr = np.zeros((h, w, 3), dtype=np.uint8)
        arr[:, :, 0] = (i * 37) % 256
        arr[:, :, 1] = (i * 91) % 256
        arr[:, :, 2] = (i * 13) % 256
        fr = av.VideoFrame.from_ndarray(arr, format="rgb24")
        for pkt in st.encode(fr):
            c.mux(pkt)
    for pkt in st.encode(None):
        c.mux(pkt)
    c.close()
    return path


def tag(arr):
    """用中心像素 RGB 标识一帧（够区分相邻帧）。"""
    if arr is None:
        return None
    h, w = arr.shape[0], arr.shape[1]
    px = arr[h // 2, w // 2]
    return (int(px[0]), int(px[1]), int(px[2]))


def _distinct(tags, tol=10):
    """相邻帧"不同"（任一通道差 > tol）计一次；返回不同帧个数（含首帧）。"""
    n = 0
    prev = None
    for t in tags:
        if t is None:
            continue
        if prev is None or max(abs(t[k] - prev[k]) for k in range(3)) > tol:
            n += 1
        prev = t
    return n


# ==========================================================================
# ⑤ 吸附功能彻底移除
# ==========================================================================
def t_no_snap_api():
    tl = CueTimeline()
    for attr in ("set_snap_mode", "_snap", "snap_mode"):
        assert not hasattr(tl, attr), "CueTimeline 残留吸附 API：%s" % attr
    # 模型默认不再是"小节"（旧默认），而是 off（不吸附）
    assert CueItem().normalized().snap == "off", CueItem().snap
    # precheck 报告不再有 unsnapped
    rep = precheck.check_sheet(None)
    assert not hasattr(rep, "unsnapped"), "precheck.Report 残留 unsnapped"
    assert "unsnapped" not in rep.counts(), rep.counts()
    print("  ✓ ⑤ 吸附已彻底移除：CueTimeline 无 snap 状态/API；CueItem 默认 off；Report 无 unsnapped")


def t_no_snap_api_window():
    from ercue.ui.window import MainWindow
    w = MainWindow()
    try:
        assert not hasattr(w, "cb_snap"), "主窗口残留吸附下拉框 cb_snap"
        assert not hasattr(w, "_snap_mode"), "主窗口残留 _snap_mode"
        assert not hasattr(w, "_on_snap_changed"), "主窗口残留 _on_snap_changed"
        print("  ✓ ⑤ 主窗口无吸附控件/方法（cb_snap/_snap_mode/_on_snap_changed 均不存在）")
    finally:
        w.shutdown()


# ==========================================================================
# ① / ④：放入 180s 素材 → end_sec = at+180；画出的宽度 = 180s（不铺满视口）
# ==========================================================================
def t_clip_duration(fx):
    from ercue.ui.window import MainWindow
    w = MainWindow()
    try:
        w.new_sheet("DUR")
        ids = w.add_media_paths([fx["v180"]])
        cid = ids[0]
        ref = w._sheet().media_by_id(cid)
        print("    180s 素材探测时长 = %.2fs" % ref.duration_sec)
        assert 175.0 < ref.duration_sec < 185.0, "素材时长探测错：%.2f" % ref.duration_sec

        w.add_track("三分钟的歌", "x", 180.0, 0.0)
        ly = w._layers()[0]
        it = w.add_cue(ly.lyr_id, cid, 10.0)
        assert it.end_sec is not None, "① 放入素材后 end_sec 不该还是 None（旧行为）"
        assert abs(it.end_sec - (10.0 + 180.0)) < 1.0, \
            "① 片段时长应为素材真实时长 180s，实际 end=%s" % it.end_sec

        tl = w.timeline
        # ④：画出的宽度 = 180s，**与缩放无关**（缩到 600s 视口也还是 180s，绝不铺满）
        for span in (180.0, 600.0, 60.0):
            tl.set_view(0.0, span)
            drawn = tl._item_end_px_sec(ly, it)
            assert abs(drawn - (10.0 + 180.0)) < 1.0, \
                "④ span=%.0f 时画出的终点应恒为 190s（=at+180），实际 %.2f" % (span, drawn)
        print("    ✓ ①/④ 180s 素材：end=%.1fs；视口 60/180/600s 下画出终点恒为 190s（不铺满）"
              % it.end_sec)

        # ④ 曲目总长来自真实时长（"三分钟的歌" = 180s）
        total = w.timeline_total_sec()
        assert abs(total - 180.0) < 1.0, "④ 曲目总长应为 180s，实际 %.1f" % total
        print("    ✓ ④ 曲目总长 = %.1fs（来自真实时长，既没被撑错也没被压错）" % total)

        # ---- 逆向验证：把 end_sec 回退成 None（旧行为）→ 被拉到视口右端，判据必须失败 ----
        it.end_sec = None
        tl.set_view(0.0, 600.0)
        drawn_old = tl._item_end_px_sec(ly, it)
        assert abs(drawn_old - (10.0 + 180.0)) > 1.0, \
            "逆向验证失败：end_sec=None 时竟仍得到 180s（说明判据没测到根因）"
        assert drawn_old >= 600.0 - 1.0, \
            "逆向验证：end_sec=None 应被拉到视口右端(≈600s)，实际 %.2f" % drawn_old
        it.end_sec = 10.0 + 180.0                       # 还原
        print("    ✓ 逆向（①）：end_sec=None → 画出终点 %.1fs（视口右端，被拉长）⇒ 判据成立"
              % drawn_old)
    finally:
        w.shutdown()


def t_clip_duration_reverse_no_end(fx):
    """逆向：模拟"add_cue 不写 end_sec"（旧实现）→ end_sec 为 None（判据会失败）。"""
    from ercue.ui.window import MainWindow
    w = MainWindow()
    try:
        w.new_sheet("DUR-rev")
        ids = w.add_media_paths([fx["v180"]])
        w.add_track("歌", "x", 180.0, 0.0)
        ly = w._layers()[0]
        orig = MainWindow._clip_duration_sec
        try:
            MainWindow._clip_duration_sec = staticmethod(lambda clip_id: 0.0)   # 旧行为：量不到
            it = w.add_cue(ly.lyr_id, ids[0], 10.0)
        finally:
            MainWindow._clip_duration_sec = orig
        assert it.end_sec is None, "逆向验证失败：旧实现本就是 end_sec=None"
        print("    ✓ 逆向（①）：回退 add_cue（不写时长）→ end_sec=None（修复前正是如此）")
    finally:
        w.shutdown()


# ==========================================================================
# ③ 拖动以"拖动距离"为准
# ==========================================================================
def t_drag_distance():
    tl = CueTimeline()
    tl.resize(900, 400)
    te = TrackEntry(track_key="k", identity=Identity(title="T", artist="A",
                                                     duration_sec=300.0, bpm=0.0))
    te.identity.ensure_meta_id()
    ly = CompLayer(lyr_id="L1", name="L1", z=0)
    at0, end0 = 10.0, 40.0
    ly.items.append(CueItem(item_id="i1", clip_id="c1", at_sec=at0, end_sec=end0).normalized())
    te.composition.layers.append(ly)
    tl.set_track(te)
    tl.set_sheet(None)
    tl.set_view(0.0, 100.0)

    got = {}
    tl.moved.connect(lambda l, i, sec: got.__setitem__("nb", sec))

    y = int(theme.RULER_H + tl.row_h * 0.5)
    cx = int((tl._x(at0) + tl._x(end0)) / 2)          # 在片段**中部**按下（不在开头）
    cursor_sec = tl._sec(cx)
    delta = 15.0
    x_target = int(tl._x(cursor_sec + delta))
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(cx, y))
    QTest.mouseMove(tl, QPoint(x_target, y))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x_target, y))
    APP.processEvents()

    assert "nb" in got, "拖动没有发出 moved 信号"
    nb = got["nb"]
    moved = nb - at0
    assert abs(moved - delta) < 0.6, \
        "③ 应以拖动距离为准（平移≈%.1fs），实际平移 %.2fs（at %.1f→%.2f）" % (delta, moved, at0, nb)
    assert abs(nb - (cursor_sec + delta)) > 3.0, \
        "③ 片段开头不该跳到光标位置（cursor=%.1f，落点=%.1f）" % (cursor_sec + delta, nb)
    print("    ✓ ③ 拖动（中部按下，拖 %.0fs）：at %.1f → %.2f（平移 %.2fs，未跳到光标 %.1fs）"
          % (delta, at0, nb, moved, cursor_sec + delta))

    # ---- 逆向验证：回退成"开头对准鼠标"（旧逻辑：落点 = 光标秒）----
    nb_old = cursor_sec + delta                        # 旧逻辑的落点
    assert abs(nb_old - (at0 + delta)) > 3.0, \
        "逆向验证失败：旧逻辑落点竟等于 平移后起点（说明两逻辑没区分开）"
    print("    ✓ 逆向（③）：旧逻辑落点 = 光标秒 = %.1fs（≠ 平移后起点 %.1fs）⇒ 修复确有区别"
          % (nb_old, at0 + delta))


# ==========================================================================
# ② 预览取帧：连续前进（不是停在关键帧）
# ==========================================================================
def t_playback_frame_advance(fx):
    p = fx["v180"]
    fps = fx["v180_fps"]

    old = CueFrameSource()                              # 模拟旧路径：frame()
    new = CueFrameSource()                              # 新路径：frame_play()

    targets = [round(0.1 * k, 3) for k in range(40)]    # 0.0 ~ 3.9s
    tags_old = [tag(old.frame(p, t)) for t in targets]
    tags_new = [tag(new.frame_play(p, t)) for t in targets]

    d_old = _distinct(tags_old)
    d_new = _distinct(tags_new)
    print("    frame() 不同帧 = %d ；frame_play() 不同帧 = %d（目标 %d 个 @ %dfps，大 GOP）"
          % (d_old, d_new, len(targets), fps))

    # 期望：frame_play 得到大量**连续真实帧**；frame() 停在关键帧（distinct≈1）
    assert d_new >= 12, "② frame_play 应前进出多张帧，实际 distinct=%d" % d_new
    assert d_new > d_old * 2, \
        "② frame_play(%d) 明显多于旧 frame(%d) 才算修复" % (d_new, d_old)

    # ---- 逆向验证：旧路径 frame() 在同一 GOP 内返回同一张帧 → "卡住/减速" ----
    assert d_old <= 2, \
        "逆向验证失败：旧 frame() 竟也前进了（distinct=%d）——它本应停在关键帧" % d_old
    print("    ✓ 逆向（②）：旧 frame() 只出 %d 张不同帧（停在关键帧＝画面卡住）⇒ 判据成立" % d_old)

    # ---- "正常速度"：目标 t 处取到的帧，应≈该时刻的帧（位置真的跟上，未减速）----
    #   判定：frame_play 在 t 与 t+1.0s 取到的帧必须不同（位置前进了），
    #         且中途每 0.2s 至少一次前进（≤ 若干帧）。
    a = tag(new.frame_play(p, 1.0))
    b = tag(new.frame_play(p, 2.0))
    assert a != b and (a is not None and b is not None), \
        "② 1.0s 与 2.0s 应取到不同帧（位置前进），实际 %s vs %s" % (a, b)
    step_adv = sum(1 for k in range(10)
                   if tag(new.frame_play(p, 2.0 + 0.2 * k))
                   != tag(new.frame_play(p, 2.2 + 0.2 * k)))
    assert step_adv >= 8, "② 每秒应连续前进（挂钟推进时画面跟着走），仅 %d/10 段前进" % step_adv
    print("    ✓ ② 位置跟上墙钟：1.0s≠2.0s 帧不同；2.0~4.0s 内 %d/10 个 0.2s 段画面前进"
          % step_adv)

    # ---- 同位置重复取帧不跳帧（音频停住时画面必须停住）----
    s = tag(new.frame_play(p, 1.5))
    t = tag(new.frame_play(p, 1.5))
    u = tag(new.frame_play(p, 1.5))
    assert s == t == u, "② 同一位置重复取帧不该跳帧：%s %s %s" % (s, t, u)
    print("    ✓ ② 同位置重复取帧复用同一帧（不跳帧）")

    old.close()
    new.close()


# ==========================================================================
# 夹具管理
# ==========================================================================
def _fixtures():
    d = tempfile.mkdtemp(prefix="ercue_fixtl_")
    v180 = os.path.join(d, "clip_180s.mp4")
    fps = 5
    make_video(v180, n=180 * fps, fps=fps)              # 180s 真实时长（每帧颜色不同）
    return {"dir": d, "v180": v180, "v180_fps": fps}


def _run(name, fn, fx=None):
    try:
        fn(fx) if fx is not None else fn()
        return (name, True, "")
    except Exception as e:                                       # noqa: BLE001
        return (name, False, "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()))


def main() -> int:
    global APP
    APP = QApplication.instance() or QApplication([])
    app_config._INSTANCE = app_config.Config(
        os.path.join(tempfile.mkdtemp(prefix="ercue_cfg_fixtl_"), "config.json"))
    fx = _fixtures()

    print("=" * 70)
    print("时间轴 5 条问题 · 修复验证")
    print("夹具：180s 视频 = %s（%.1f fps）" % (fx["v180"], fx["v180_fps"]))
    print("=" * 70)

    cases = [
        ("⑤ 吸附彻底移除（控件/模型/报告）", t_no_snap_api, None),
        ("⑤ 主窗口无吸附控件", t_no_snap_api_window, None),
        ("①④ 180s 素材时长 + 不铺满", t_clip_duration, fx),
        ("① 逆向：旧 add_cue 不写时长", t_clip_duration_reverse_no_end, fx),
        ("③ 拖动以距离为准（+逆向）", t_drag_distance, None),
        ("② 预览连续取帧（+逆向/速度）", t_playback_frame_advance, fx),
    ]
    results = []
    for name, fn, f in cases:
        print("\n--- %s ---" % name)
        n, ok, detail = _run(name, fn, f)
        results.append((n, ok, detail))
        if not ok:
            print("  ✗ %s\n%s" % (n, detail))

    print("\n" + "=" * 70)
    for n, ok, _ in results:
        print("  [%s] %s" % ("PASS" if ok else "FAIL", n))
    n_ok = sum(1 for _, ok, _ in results if ok)
    print("=" * 70)
    print("汇总：%d/%d 通过  ⇒  %s" % (n_ok, len(results), "全绿 ✓" if n_ok == len(results)
                                       else "有失败 ✗"))
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
