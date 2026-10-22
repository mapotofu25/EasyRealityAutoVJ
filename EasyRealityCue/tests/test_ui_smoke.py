# -*- coding: utf-8 -*-
"""M3 · UI 冒烟 + 端到端可复跑测试。

跑法（无头）：
    set QT_QPA_PLATFORM=offscreen
    P:\\AutoVJ\\venv\\Scripts\\python.exe  P:\\EasyRealityCue\\tests\\test_ui_smoke.py

覆盖（对应任务书验收标准）：
  1. 建 QApplication + 主窗口（offscreen）**不崩**
  2. **端到端闭环**：新建码表 → 加 2 素材 → 加曲目 → 2 层 2 片段 → 改属性（含**入/出点**）
     → 移播放头 → 合成出**非空画面** → 导出 `.ercue` → 重新导入 → 字段一致
  3. **B1 哨兵**：`_refresh_all()` 连续 100 次，断言下拉框**没有级联信号**（回调 0 次）
  4. **B2 哨兵**：数据回填（`refresh()`）期间**不触发写回**（`changed` 不发、`commit` 不跑）
  5. **C3 哨兵**：5 分钟曲目 → 时间轴总长 ≈ 300s（不是 10~20s）
  6. **标尺自适应**：`ruler_plan()` 纯函数多缩放级别取值（阈值 34/2/7/7/6px）
  7. **多码表隔离哨兵**（§3.3）：建 A 加片段 → 建 B 改 B → 切回 A，A 数据一条不少
  8. **撤销哨兵**：改属性 → undo → 回到原值
  9. **截图**：offscreen 渲染主窗口 `grab()` → `_shots/`（空状态 + 有素材片段状态）

说明：测试夹具（小 mp4 / png）用 PyAV 现场生成到**系统临时目录**，不往项目里写；
      真实素材库只读引用不参与本测试。
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import traceback

# ★ T02（第三轮）测试隔离（P0）：自动保存会"启动即恢复上次工作区" ⇒ 若不隔离，
#   本测试之间/与真实 autosave 目录会互相污染（R8）。这里**全程关掉自动保存**
#   （`ERCUE_NO_AUTOSAVE=1` 下 session_store 全部 API 变 no-op），并把目录也指到临时目录。
os.environ["ERCUE_NO_AUTOSAVE"] = "1"
os.environ.setdefault("ERCUE_AUTOSAVE_DIR",
                      os.path.join(tempfile.gettempdir(), "ercue_autosave_test_ui_smoke"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
_SHOTS = os.path.join(_ROOT, "_shots")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import numpy as np                                             # noqa: E402
import av                                                      # noqa: E402

from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt      # noqa: E402
from PySide6.QtGui import (QColor, QDropEvent, QFont, QFontDatabase,  # noqa: E402
                             QWheelEvent)
from PySide6.QtTest import QTest                               # noqa: E402
from PySide6.QtWidgets import QApplication                    # noqa: E402

from ercue import config as app_config                        # noqa: E402
from ercue.audio.player import AudioPlayer                     # noqa: E402
from ercue.model import precheck                              # noqa: E402
from ercue.model.ercue_io import JSON_NAME                    # noqa: E402
from ercue.ui import theme                                    # noqa: E402
from ercue.ui.window import MainWindow                        # noqa: E402
from ercue.ui.timeline_widget import CLIP_MIME, CueTimeline, ruler_plan  # noqa: E402
from ercue.model.cuesheet import (CompLayer, CueItem, MediaRef,  # noqa: E402
                                 TrackEntry, Identity)

APP = None


# ==========================================================================
# 测试夹具：系统临时目录里造 2 个 mp4 + 1 个 png（不写项目目录）
# ==========================================================================
def _make_mp4(path, frames=10, fps=10, w=320, h=180, shift=True):
    c = av.open(path, "w")
    st = c.add_stream("mpeg4", rate=fps)
    st.width, st.height = w, h
    st.pix_fmt = "yuv420p"
    for i in range(frames):
        img = np.zeros((h, w, 3), np.uint8)
        img[:, :, 2] = 180
        if shift:
            x = (i * 30) % max(1, w - 30)
            img[:, x:x + 30, 1] = 255
        else:
            img[:, :, 1] = 120
        fr = av.VideoFrame.from_ndarray(img, format="rgb24")
        for pk in st.encode(fr):
            c.mux(pk)
    for pk in st.encode():
        c.mux(pk)
    c.close()


def _make_png(path, w=160, h=120):
    c = av.open(path, "w")
    st = c.add_stream("png")
    st.width, st.height = w, h
    st.pix_fmt = "rgba"
    img = np.zeros((h, w, 4), np.uint8)
    img[..., 0] = 220
    img[..., 1] = 160
    img[..., 3] = 255
    fr = av.VideoFrame.from_ndarray(img, format="rgba")
    for pk in st.encode(fr):
        c.mux(pk)
    for pk in st.encode():
        c.mux(pk)
    c.close()


def _make_wav(path, sec=3.0, sr=44100, ch=1):
    import wave
    n = int(sec * sr)
    with wave.open(path, "wb") as f:
        f.setnchannels(ch)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(b"\x00\x00" * n * ch)


def _fixtures():
    d = tempfile.mkdtemp(prefix="ercue_ui_")
    a = os.path.join(d, "clip_a.mp4")
    b = os.path.join(d, "clip_b.mp4")
    img = os.path.join(d, "pic_c.png")
    wav = os.path.join(d, "song_d.wav")
    wav_long = os.path.join(d, "song_long.wav")
    _make_mp4(a, shift=True)
    _make_mp4(b, shift=False)
    _make_png(img)
    _make_wav(wav, sec=3.0)
    _make_wav(wav_long, sec=90.0, sr=22050, ch=1)     # 够长，供"播放中"截图用
    return {"dir": d, "a": a, "b": b, "img": img, "wav": wav, "wav_long": wav_long}


def _new_window():
    w = MainWindow()
    w.thumbs_enabled = False        # 测试里关掉后台缩略图线程，保证确定性、更快
    w._interactive = False          # ★ 无头：绝不弹对话框（导出前检查 / 缺素材提示都不阻塞）
    return w


def _wait_until(pred, timeout=2.0, interval=0.01):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


# ==========================================================================
# 1. 主窗口能起来、不崩
# ==========================================================================
def test_window_boots():
    w = _new_window()
    assert w.cb_sheet.count() >= 1
    assert w._sheet() is not None and w.timeline is not None and w.preview is not None
    w.shutdown()
    print("  ✓ 主窗口构建成功，下拉框 %d 项" % w.cb_sheet.count())


# ==========================================================================
# 2. 端到端闭环
# ==========================================================================
def test_end_to_end(fx):
    w = _new_window()
    try:
        w.new_sheet("E2E")
        ids = w.add_media_paths([fx["a"], fx["b"]])
        assert len(ids) == 2, ids
        # 加曲目（5 分钟）→ 自动带一层
        w.add_track("E2E 曲", "测试", 300.0, 120.0)
        w.add_layer()                                  # 第二层
        lyrs = w._layers()
        assert len(lyrs) >= 2, [l.lyr_id for l in lyrs]
        # 两层各加一个片段
        it1 = w.add_cue(lyrs[0].lyr_id, ids[0], 12.0)
        it2 = w.add_cue(lyrs[1].lyr_id, ids[1], 30.0)
        assert it1 is not None and it2 is not None
        # 改属性（含入/出点）—— 直接改模型再回填，等价于面板编辑
        w._select(lyrs[0].lyr_id, it1.item_id)
        p = w.props_panel
        p.sp_at.setValue(12.0)          # 触发 changed → commit
        p.sp_src_in.setValue(1.5)
        p.sp_src_out.setValue(6.5)
        p.sp_speed.setValue(1.5)
        p.chk_crop.setChecked(True)
        p.sp_crop[0].setValue(0.1)
        # 播放头 + 合成非空画面
        # ★ 片段现在带**真实时长**（end_sec=at+素材时长），故 30.0s 时 it2 刚入点、
        #   alpha=0（淡入起点）；取 30.5s（淡入中段）才是"有内容"的一帧。
        w.preview.resize(640, 360)
        w._set_pos(30.5)
        w.preview._compose()
        img = w.preview._img
        assert img is not None and not img.isNull(), "合成画面为空"
        c = img.pixelColor(img.width() // 2, img.height() // 2)
        assert (c.red() + c.green() + c.blue()) > 0, "合成画面全黑"
        # 导出 → 重新导入 → 字段一致
        out = os.path.join(fx["dir"], "e2e.ercue")
        assert w.export_sheet(out) and os.path.isfile(out)
        before = w._sheet().to_state_dict()
        w.import_sheet(out)
        after = w._sheet().to_state_dict()
        _assert_state_equal(before, after, tol=1e-6)
        print("  ✓ 端到端闭环：画面非空 + 导出重导入字段一致（%d 首 / 层 %d）"
              % (len(after["tracks"]), len(after["tracks"][0]["composition"]["layers"])))
    finally:
        w.shutdown()


def _assert_state_equal(a, b, tol=1e-6):
    assert a["title"] == b["title"], (a["title"], b["title"])
    assert len(a["media"]) == len(b["media"])
    assert len(a["tracks"]) == len(b["tracks"])
    for ta, tb in zip(a["tracks"], b["tracks"]):
        assert ta["identity"]["title"] == tb["identity"]["title"]
        la = ta["composition"]["layers"]
        lb = tb["composition"]["layers"]
        assert len(la) == len(lb)
        for xa, xb in zip(la, lb):
            assert len(xa["items"]) == len(xb["items"])
            for ia, ib in zip(xa["items"], xb["items"]):
                for k in ("at_sec", "speed", "src_in_sec", "src_out_sec"):
                    va, vb = ia.get(k), ib.get(k)
                    if va is None or vb is None:
                        assert va == vb, (k, va, vb)
                    else:
                        assert abs(float(va) - float(vb)) < tol, (k, va, vb)
                assert ia["clip_id"] == ib["clip_id"]
                ca, cb = ia.get("crop"), ib.get("crop")
                if ca is None or cb is None:
                    assert ca == cb
                else:
                    assert all(abs(float(x) - float(y)) < tol for x, y in zip(ca, cb))


# ==========================================================================
# 3. B1 哨兵：刷新 100 次不引发级联信号
# ==========================================================================
def test_b1_refresh_no_signal_storm(fx):
    w = _new_window()
    try:
        w.new_sheet("B1")
        w.add_media_paths([fx["a"]])
        w.add_track("B1 曲", "x", 120.0, 128.0)
        w.add_cue(w._layers()[0].lyr_id, w._sheet().media[0].clip_id, 5.0)
        w._sig["sheet"] = 0
        for _ in range(100):
            w._refresh_all()
        assert w._sig["sheet"] == 0, "重填下拉框引发了级联信号：%d 次" % w._sig["sheet"]
        assert w.cb_sheet.currentData() == w.current_key, "刷新后选中项没还原"
        print("  ✓ B1：_refresh_all() x100，下拉框 currentIndexChanged 回调 0 次，选中项还原")
    finally:
        w.shutdown()


# ==========================================================================
# 4. B2 哨兵：回填期间不写回
# ==========================================================================
def test_b2_no_self_excite(fx):
    w = _new_window()
    try:
        w.new_sheet("B2")
        w.add_media_paths([fx["a"]])
        w.add_track("B2 曲", "x", 120.0, 120.0)
        it = w.add_cue(w._layers()[0].lyr_id, w._sheet().media[0].clip_id, 5.0)
        w._select(w._layers()[0].lyr_id, it.item_id)
        p = w.props_panel
        emitted = []
        p.changed.connect(lambda: emitted.append(1))
        n0 = p.commit_calls
        for _ in range(50):
            p.refresh()
        assert len(emitted) == 0, "回填触发了写回（changed 发了 %d 次）" % len(emitted)
        assert p.commit_calls == n0, "回填期间 commit 被调用了"
        # 守卫置位期间 commit 必须直接 return False
        p._loading = True
        try:
            assert p.commit() is False
        finally:
            p._loading = False
        print("  ✓ B2：refresh() x50 期间 changed 发 0 次、commit 未跑；守卫期间 commit 拒绝写回")
    finally:
        w.shutdown()


# ==========================================================================
# 5. C3 哨兵：总长来自音频真实时长
# ==========================================================================
def test_c3_total_from_audio_duration(fx):
    w = _new_window()
    try:
        w.new_sheet("C3")
        w.add_track("五分钟的歌", "x", 300.0, 128.0)
        total = w.timeline_total_sec()
        assert abs(total - 300.0) < 1e-6, "总长应为 300s，实际 %.1f" % total
        assert total > 100.0, "5 分钟的歌被撑错/压错了"
        # 无音频无片段时 → 下限 10s
        w2 = _new_window()
        try:
            assert abs(w2.timeline_total_sec() - 10.0) < 1e-6
        finally:
            w2.shutdown()
        # duration_sec 无效（0）→ **probe 实测音频文件**（§6.9 真根因的兜底分支）
        w3 = _new_window()
        try:
            w3.new_sheet("C3b")
            w3.add_track("没填时长", "x", 0.0, 0.0)
            w3.session.track_audio[0] = fx["wav"]
            t3 = w3.timeline_total_sec()
            assert 2.5 < t3 < 3.5, "应 probe 出约 3s，实际 %.2f" % t3
        finally:
            w3.shutdown()
        print("  ✓ C3：300s 曲目 → %.1fs；空码表 → 10s；duration 无效 → probe=%.1fs"
              % (total, t3))
    finally:
        w.shutdown()


# ==========================================================================
# 6. 标尺自适应纯函数
# ==========================================================================
def test_ruler_plan():
    # 每秒 100px：一切都能画
    step, line, lab, bar, ph, beat = ruler_plan(100.0)
    assert step == 1 and line == 1 and lab and bar and ph and beat, (step, line, lab, bar, ph, beat)
    # 每秒 1px：数字抽稀到 60s，长线 2s，●◆ 与拍刻度放不下
    step, line, lab, bar, ph, beat = ruler_plan(1.0)
    assert step == 60 and line == 2, (step, line)
    assert lab is True and bar is False and ph is False and beat is False, (lab, bar, ph, beat)
    # 每秒 0.5px：数字抽稀到 120s，长线 5s
    step, line, lab, bar, ph, beat = ruler_plan(0.5)
    assert step == 120 and line == 5, (step, line)
    assert lab is True and bar is False and ph is False and beat is False
    # 每秒 0.1px：数字抽稀到 600s；长线 30s
    step, line, lab, bar, ph, beat = ruler_plan(0.1)
    assert step == 600 and line == 30, (step, line)
    assert lab is True and bar is False and ph is False and beat is False
    # 每秒 20px：数字步进 2s（1×20<34, 2×20≥34）
    step, line, lab, bar, ph, beat = ruler_plan(20.0)
    assert step == 2 and line == 1, (step, line)
    assert lab and bar and ph and beat
    # 缩小时"排数"越来越少（单调抽稀）
    steps = [ruler_plan(p)[0] for p in (200, 50, 5, 1, 0.2, 0.05)]
    assert steps == sorted(steps), steps
    print("  ✓ 标尺自适应：ruler_plan 各缩放级别取值正确，缩小时单调抽稀 %s" % steps)


# ==========================================================================
# 6b. 时间轴几何 / 命中 / 吸附（步长唯一来源 = units.snap_grid）
# ==========================================================================
def test_timeline_geometry():
    tl = CueTimeline()
    tl.resize(800, 400)
    te = TrackEntry(track_key="k", identity=Identity(title="T", artist="A",
                                                     duration_sec=100.0, bpm=120.0))
    te.identity.ensure_meta_id()
    ly = CompLayer(lyr_id="L1", name="L1", z=0)
    ly.items.append(CueItem(item_id="i1", clip_id="c1", at_sec=0.0,
                            end_sec=20.0).normalized())
    te.composition.layers.append(ly)
    tl.set_track(te)
    tl.set_sheet(None)
    tl.set_bpm(120.0)
    tl.set_view(0.0, 100.0)

    # 坐标换算往返
    assert abs(tl._x(0.0) - theme.GUTTER) < 1e-6
    assert abs(tl._sec(tl._x(37.5)) - 37.5) < 1e-6
    assert abs(tl._x(100.0) - tl.width()) < 1e-6

    # 命中：标尺区不算命中
    assert tl._hit(QPointF(100.0, 5.0))[2] == -1
    # 命中：片段区（第一层行的中部）
    y = theme.RULER_H + tl.row_h * 0.5
    l2, it2, row, edge = tl._hit(QPointF(tl._x(10.0), y))
    assert it2 is ly.items[0] and row == 0 and edge == "", (it2, row, edge)
    # 命中：片段左边缘（容差内）→ edge="l"
    _l, _i, _r, e2 = tl._hit(QPointF(tl._x(0.0) + 2, y))
    assert e2 == "l", e2
    # 命中：越界行（y 很下面）→ 无命中
    assert tl._hit(QPointF(100.0, 900.0))[2] == -1

    # ★ 吸附功能已按用户要求**移除**：控件上不该再有任何吸附 API / 状态
    assert not hasattr(tl, "set_snap_mode"), "CueTimeline 不应再有 set_snap_mode"
    assert not hasattr(tl, "_snap"), "CueTimeline 不应再有 _snap"
    assert not hasattr(tl, "snap_mode"), "CueTimeline 不应再有 snap_mode"
    print("  ✓ 时间轴几何/命中：坐标往返、标尺不命中、边缘容差；吸附 API 已移除")


# ==========================================================================
# 7. 多码表隔离哨兵（§3.3）
# ==========================================================================
def test_multi_sheet_isolation(fx):
    w = _new_window()
    try:
        # A：加素材 + 曲目 + 片段
        keyA = w.new_sheet("A")
        ids = w.add_media_paths([fx["a"]])
        w.add_track("A 曲", "a", 200.0, 120.0)
        layA = w._layers()[0].lyr_id
        w.add_cue(layA, ids[0], 10.0)
        w.add_cue(layA, ids[0], 20.0)
        a_items = sum(len(l.items) for l in w._layers())
        a_media = len(w._sheet().media)
        # B：新建，改 B
        keyB = w.new_sheet("B")
        assert keyB != keyA
        w.add_track("B 曲", "b", 100.0, 100.0)
        w.add_cue(w._layers()[0].lyr_id, "", 3.0)
        assert len(w._sheet().media) == 0        # B 没有 A 的素材（无共享引用）
        # 切回 A
        w._switch_to(keyA)
        assert w.current_key == keyA
        assert sum(len(l.items) for l in w._layers()) == a_items, "切回 A 后片段数变了"
        assert len(w._sheet().media) == a_media, "切回 A 后素材数变了"
        # 再切到 B，B 的数据也还在
        w._switch_to(keyB)
        assert sum(len(l.items) for l in w._layers()) == 1
        print("  ✓ 多码表隔离：A(%d 片段/%d 素材) 与 B 各自完好，切换无串味"
              % (a_items, a_media))
    finally:
        w.shutdown()


# ==========================================================================
# 8. 撤销哨兵
# ==========================================================================
def test_undo(fx):
    w = _new_window()
    try:
        w.new_sheet("UNDO")
        ids = w.add_media_paths([fx["a"]])
        w.add_track("U 曲", "x", 120.0, 120.0)
        lay = w._layers()[0].lyr_id
        it = w.add_cue(lay, ids[0], 5.0)
        iid = it.item_id
        w._select(lay, iid)
        assert abs(w.props_panel.item.at_sec - 5.0) < 1e-9
        w.props_panel.sp_at.setValue(9.0)        # 改属性（走 changed → commit）
        cur = next(x for l in w._layers() for x in l.items if x.item_id == iid)
        assert abs(cur.at_sec - 9.0) < 1e-6, cur.at_sec
        w.undo()                                 # Ctrl+Z
        cur = next((x for l in w._layers() for x in l.items if x.item_id == iid), None)
        assert cur is not None and abs(cur.at_sec - 5.0) < 1e-6, "撤销没回到原值"
        w.redo()
        cur = next((x for l in w._layers() for x in l.items if x.item_id == iid), None)
        assert abs(cur.at_sec - 9.0) < 1e-6, "重做没回到 9.0"
        print("  ✓ 撤销/重做：改属性→撤销回 5.0→重做回 9.0")
    finally:
        w.shutdown()


def _save_shot(widget, path) -> bool:
    """保存截图，**三级自愈**（某些沙箱/权限环境会拒绝「覆盖已存在文件」）：

      ① 直接存（正常桌面：覆盖即可）；
      ② 先删旧文件再存（`os.remove` 允许，而覆盖被拒的环境）；
      ③ 写 `.tmp.png` 新文件 → `os.replace` 原子替换（连 `os.remove` 都被拒的环境）。

    ⚠ 关键：**任一步成功即返回 True**；删除失败不会让整个测试变红（team-lead 要求）。
    """
    img = widget.grab()

    def _try() -> bool:
        try:
            return bool(img.save(path))
        except Exception:                                  # noqa: BLE001
            return False

    if _try():                                             # ①
        return True
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass
    if _try():                                             # ②
        return True
    try:                                                   # ③
        tmp = path + ".tmp.png"
        if img.save(tmp):
            os.replace(tmp, path)
            return os.path.isfile(path)
    except Exception:                                      # noqa: BLE001
        pass
    return False


# ==========================================================================
# 9. 截图（供用户看成果）
# ==========================================================================
def test_screenshots(fx):
    assert APP is not None
    os.makedirs(_SHOTS, exist_ok=True)
    # 空状态
    w = _new_window()
    try:
        w.resize(1240, 800)
        w.show()
        APP.processEvents()
        w._refresh_all()
        APP.processEvents()
        p1 = os.path.join(_SHOTS, "m3_empty.png")
        assert _save_shot(w, p1), "截图保存失败: %s" % p1
    finally:
        w.shutdown()
    # 有素材与片段
    w = _new_window()
    try:
        w.new_sheet("演示码表")
        ids = w.add_media_paths([fx["a"], fx["b"], fx["img"]])
        w.add_track("演示曲目", "Demo Artist", 300.0, 128.0)
        w.add_layer()
        lyrs = w._layers()
        w.add_cue(lyrs[0].lyr_id, ids[0], 4.0)
        w.add_cue(lyrs[0].lyr_id, ids[1], 40.0)
        w.add_cue(lyrs[1].lyr_id, ids[2], 12.0)
        w.add_cue(lyrs[1].lyr_id, ids[0], 60.0)
        w._select(lyrs[1].lyr_id, lyrs[1].items[0].item_id)
        w.resize(1240, 800)
        w.show()
        APP.processEvents()
        w._set_pos(12.0)
        w.preview.resize(700, 394)
        w.preview._compose()
        w._refresh_all()
        APP.processEvents()
        p2 = os.path.join(_SHOTS, "m3_arranged.png")
        assert _save_shot(w, p2), "截图保存失败: %s" % p2
    finally:
        w.shutdown()
    for p in (p1, p2):
        assert os.path.isfile(p) and os.path.getsize(p) > 0, "截图不存在或为空: %s" % p
    print("  ✓ 截图已生成：%s (%d B) / %s (%d B)"
          % (p1, os.path.getsize(p1), p2, os.path.getsize(p2)))


# ==========================================================================
# 10. 缩略图真的出图（M3 缺口①：M3 为确定性把 thumbs_enabled 关了）
# ==========================================================================
def test_thumbnail_produced(fx):
    assert APP is not None
    w = MainWindow()                          # thumbs_enabled 默认 True（本测试就是要它开）
    w._interactive = False                    # ★ 无头：不弹对话框
    w._thumb_dir = os.path.join(tempfile.mkdtemp(prefix="ercue_thumbs_"), "cache")
    try:
        w.new_sheet("THUMB")
        ids = w.add_media_paths([fx["a"]])    # 触发后台缩略图线程
        cid = ids[0]
        ok = False
        for _ in range(500):                  # 等后台生成（绝不在 GUI 线程做）
            APP.processEvents()
            if cid in w.media_panel._thumbs_ready:
                ok = True
                break
            time.sleep(0.01)
        assert ok, "缩略图没有生成（_thumbs_ready 为空）"
        icon = w.media_panel._thumbs_ready[cid]
        assert icon is not None and not icon.isNull(), "缩略图 QIcon 为空"
        pm = icon.pixmap(96, 54)
        assert not pm.isNull() and pm.width() > 0 and pm.height() > 0, "缩略图 pixmap 为空"
        # 列表项也真的挂上了图标（不只是缓存在字典里）
        lst = w.media_panel.lst
        item = next(lst.item(i) for i in range(lst.count())
                    if str(lst.item(i).data(Qt.UserRole)) == cid)
        assert item is not None and not item.icon().isNull(), "素材列表项没挂上缩略图"
        print("  ✓ 缩略图出图：clip %s → %dx%d pixmap，素材列表项已挂图标"
              % (cid, pm.width(), pm.height()))
    finally:
        w.shutdown()


# ==========================================================================
# 11. 素材拖放 + 时间轴鼠标拖拽（M3 缺口②：用 QTest 模拟事件）
#      · 拖动改位置（moved）· 拉边改时长（resized）· 跨层拖动（layer_moved）
#      · 滚轮缩放 / Shift 平移 · 素材拖放落层（clip_dropped）· ClipList mime 契约
# ==========================================================================
def test_drag_drop_and_timeline_mouse(fx):
    assert APP is not None
    w = _new_window()
    try:
        w.new_sheet("DRAG")
        ids = w.add_media_paths([fx["a"], fx["b"]])
        w.add_track("DRAG 曲", "x", 300.0, 120.0)
        w.add_layer()                                     # 第二层
        lyrs = w._layers()
        it1 = w.add_cue(lyrs[0].lyr_id, ids[0], 4.0)
        it1.end_sec = 14.0
        it2 = w.add_cue(lyrs[1].lyr_id, ids[1], 30.0)
        it2.end_sec = 40.0
        # ★ 吸附功能已移除：主窗口不该再有吸附下拉框
        assert not hasattr(w, "cb_snap"), "主窗口不应再有吸附下拉框"
        tl = w.timeline
        w.resize(1240, 800)
        w.show()
        APP.processEvents()
        tl.set_view(0.0, 60.0)
        APP.processEvents()

        def row_of(layer):
            return tl._layers().index(layer)

        def y_of(row):
            return int(theme.RULER_H + tl.row_h * row + tl.row_h / 2)

        r1 = row_of(lyrs[0])
        y1 = y_of(r1)
        assert tl.width() > 300, "时间轴宽度异常：%d" % tl.width()

        # --- (1a) ★ 第七轮（用户要求）：**单击**边缘 ⇒ **不改长短**（按移动整块处理）---
        _at0, _end0 = float(it1.at_sec), float(it1.end_sec)
        xr0 = tl._x(_end0)
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(xr0) - 2, y1))
        APP.processEvents()
        assert tl._resize is None, "单击边缘不应进 `_resize`（第七轮用户要求：单击不能拖两端）"
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(xr0) - 2, y1))
        APP.processEvents()
        assert abs(float(it1.end_sec) - _end0) < 0.05 and abs(float(it1.at_sec) - _at0) < 0.05, \
            "单击（不拖）不应改变几何：at %.2f→%.2f end %.2f→%.2f" % (
                _at0, float(it1.at_sec), _end0, float(it1.end_sec))
        print("    ✓ 单击边缘：不进 resize、几何不变（改长短须双击）")

        # --- (1) 拉边改时长（右边缘）：**双击进剪辑态**后才能拖（第七轮用户要求）---
        xr = tl._x(it1.end_sec)
        QTest.mouseDClick(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(xr) - 2, y1))
        APP.processEvents()
        assert tl._edit_mode is not None, "双击片段应进入剪辑态"
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(xr) - 2, y1))
        QTest.mouseMove(tl, QPoint(int(tl._x(20.0)), y1))
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(20.0)), y1))
        APP.processEvents()
        it1b = next(x for l in w._layers() if l.lyr_id == lyrs[0].lyr_id
                    for x in l.items if x.item_id == it1.item_id)
        # ⚠ 本夹具素材 = 10 帧 @ 10fps = 1.0s ⇒ trim（双击路径，带源长校验）会把 end
        #   **钳到源长**：end = at + src_dur/speed = 4.0 + 1.0 = 5.0。
        #   （旧实现走 `_resize`、不做源长校验，所以能拉到 20s —— 那是旧的宽松行为。）
        assert it1b.end_sec is not None and abs(it1b.end_sec - 5.0) < 0.4, \
            "双击 trim 后 end 应被钳到源长（≈5.0），实际 %s" % it1b.end_sec
        assert abs(it1b.at_sec - 4.0) < 1e-6, "拉右边不应改起点，实际 %s" % it1b.at_sec
        print("    拉边改时长：右边缘 14s → %.2fs（时长 %.1fs）"
              % (it1b.end_sec, it1b.end_sec - it1b.at_sec))

        # --- (2) 拖动改位置：**以拖动距离为准**（整块平移；开头不跳到光标处；时长保持）---
        # ⚠ 上一步 trim 会把片段钳到源长（夹具素材仅 1.0s）⇒ 本步先手动放长回 14s：
        #   本步测的是 `_drag`（移动，不校验源长）；片段太短会让
        #   "整块平移"与"开头对齐光标"两种实现不可区分（差 = 片段半长）。
        it1b.end_sec = 14.0
        APP.processEvents()
        it1b = next(x for l in w._layers() if l.lyr_id == lyrs[0].lyr_id
                    for x in l.items if x.item_id == it1.item_id)
        at0 = it1b.at_sec
        end0 = it1b.end_sec
        x0 = tl._x(at0)
        x1 = tl._x(end0)
        cx = int((x0 + x1) / 2)                     # ★ 故意在**片段中部**按下（不在开头）
        sec_press = tl._sec(cx)
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(cx, y1))
        # 鼠标向右移动"12 秒对应像素" → 整块应**平移 12 秒**（而不是"开头对齐到光标"）
        QTest.mouseMove(tl, QPoint(int(tl._x(sec_press + 12.0)), y1))
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier,
                           QPoint(int(tl._x(sec_press + 12.0)), y1))
        APP.processEvents()
        it1b = next(x for l in w._layers() if l.lyr_id == lyrs[0].lyr_id
                    for x in l.items if x.item_id == it1.item_id)
        moved = it1b.at_sec - at0
        assert abs(moved - 12.0) < 1.0, \
            "拖动应平移≈12s（以拖动距离为准），实际平移 %.2fs（at %.2f→%.2f）" % (
                moved, at0, it1b.at_sec)
        # ★ 关键回归（用户反馈③）：**片段开头绝不跳到鼠标位置**
        assert abs(it1b.at_sec - (sec_press + 12.0)) > 4.0, \
            "片段开头不该跳到鼠标位置（cursor=%.1f，at=%.1f）" % (
                sec_press + 12.0, it1b.at_sec)
        assert (it1b.end_sec is not None
                and abs((it1b.end_sec - it1b.at_sec) - (end0 - at0)) < 1e-6), \
            "拖动应保持时长不变：at=%.2f end=%s" % (it1b.at_sec, it1b.end_sec)
        print("    拖动改位置（以距离为准）：at %.2f → %.2f（平移 %.2fs，时长保持 %.1fs）"
              % (at0, it1b.at_sec, moved, it1b.end_sec - it1b.at_sec))

        # --- (3) 跨层拖动 ---
        src_row = row_of(lyrs[1])
        dst_row = row_of(lyrs[0])
        assert src_row != dst_row
        xs = int((tl._x(it2.at_sec) + tl._x(it2.end_sec)) / 2)
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xs, y_of(src_row)))
        QTest.mouseMove(tl, QPoint(xs + 60, y_of(dst_row)))
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xs + 60, y_of(dst_row)))
        APP.processEvents()
        in_dst = any(x.item_id == it2.item_id for x in lyrs[0].items)
        in_src = any(x.item_id == it2.item_id for x in lyrs[1].items)
        assert in_dst and not in_src, "跨层拖动失败：dst=%s src=%s" % (in_dst, in_src)
        print("    跨层拖动：%s → %s" % (lyrs[1].lyr_id, lyrs[0].lyr_id))

        # --- (4) 滚轮：Ctrl 缩放（以鼠标为锚）---
        anchor = QPointF(tl.width() * 0.5, theme.RULER_H + 10)
        gpos = QPointF(tl.mapToGlobal(QPoint(int(anchor.x()), int(anchor.y()))))
        span0 = tl.view_span
        tl.wheelEvent(QWheelEvent(anchor, gpos, QPoint(0, 0), QPoint(0, 120),
                                  Qt.NoButton, Qt.ControlModifier, Qt.NoScrollPhase, False))
        assert tl.view_span < span0, "Ctrl+滚轮应放大（span 变小）：%.1f → %.1f" % (span0, tl.view_span)
        tl.wheelEvent(QWheelEvent(anchor, gpos, QPoint(0, 0), QPoint(0, -120),
                                  Qt.NoButton, Qt.ControlModifier, Qt.NoScrollPhase, False))
        assert abs(tl.view_span - span0) < 1e-6, "Ctrl+反向滚轮应还原 span"
        # --- (5) 滚轮：Shift 平移 ---
        tl.set_view(10.0, 60.0)
        tl.wheelEvent(QWheelEvent(anchor, gpos, QPoint(0, 0), QPoint(0, 120),
                                  Qt.NoButton, Qt.ShiftModifier, Qt.NoScrollPhase, False))
        assert tl.view0 < 10.0, "Shift+滚轮应平移：view0=%.2f" % tl.view0
        print("    滚轮：Ctrl 缩放 span %.1f，Shift 平移 view0 → %.2f" % (span0, tl.view0))

        # --- (6) 素材拖放落层 ---
        tl.set_view(0.0, 60.0)
        r1 = row_of(lyrs[0])
        md = QMimeData()
        md.setData(CLIP_MIME, ids[0].encode("utf-8"))
        before = len(lyrs[0].items)
        drop_ev = QDropEvent(QPointF(tl._x(50.0), y_of(r1)), Qt.CopyAction,
                             md, Qt.LeftButton, Qt.NoModifier)
        tl.dropEvent(drop_ev)
        APP.processEvents()
        assert len(lyrs[0].items) == before + 1, "拖放没在目标层加出片段"
        # _clip_from_event 的 mime 分支（规格书 §6.1 的回归守卫）
        assert CueTimeline._clip_from_event(drop_ev) == ids[0]
        print("    素材拖放：层 %s 片段数 %d → %d" % (lyrs[0].lyr_id, before, before + 1))

        # --- (7) ClipList mime 契约（B5/B6：UserRole=clip_id）---
        lst = w.media_panel.lst
        assert lst.count() >= 1
        it0 = lst.item(0)
        cid0 = str(it0.data(Qt.UserRole))
        md2 = lst.mimeData([it0])
        assert md2.hasFormat(CLIP_MIME), "ClipList.mimeData 没放私有 mime"
        assert bytes(md2.data(CLIP_MIME)).decode("utf-8") == cid0, "mime 里应是 clip_id"
        print("    ClipList mime 契约：UserRole=%s = mime 内容" % cid0)
        print("  ✓ 拖放/拖拽：拖动改位置、拉边改时长、跨层、滚轮缩放平移、素材落层、mime 契约 全部通过")
    finally:
        w.shutdown()


# ==========================================================================
# 11b. ★ 用户反馈（第七轮）：改长短**必须双击**（单击不能拖）；长片段视口边缘把手可抓
#      —— `_hit()` 钳位修复 + "单击不进 trim"两条语义的固化物
# ==========================================================================
def test_trim_handle_double_click_when_edge_offscreen(fx):
    """双击进剪辑态后才能拖两端；且**长片段右端在视口外**时，视口边缘的把手也要能抓到。

    背景：① `_hit()` 曾用**未钳位**坐标判把手 ⇒ 长片段某端在视口外时，把手画在视口边却命不中
    ⇒ 落到"移动整块"（用户报"拖两边没效果"）—— 修法：命中与绘制共用同一套钳位坐标。
    ② 第七轮用户明确要求：**单击不能拖两端**（曾支持"单击拖边缘 ⇒ `_resize`"，现已取消）。
    """
    assert APP is not None
    w = _new_window()
    try:
        w.new_sheet("TRIMEDGE")
        ids = w.add_media_paths([fx["a"]])
        w.add_track("T", "x", 300.0, 120.0)
        lyrs = w._layers()
        it = w.add_cue(lyrs[0].lyr_id, ids[0], 4.0)
        it.end_sec = 120.0                     # 长片段：右端远超视口
        tl = w.timeline
        w.resize(1240, 800)
        w.show()
        APP.processEvents()
        tl.set_view(0.0, 30.0)                 # 视口 0~30s ⇒ 120s 处右端在视口外
        APP.processEvents()
        row = tl._layers().index(lyrs[0])
        y = int(theme.RULER_H + tl.row_h * row + tl.row_h / 2)
        xv = int(tl.width()) - 2               # 视口右缘（把手被钳到这里）
        assert tl.width() > 300, "时间轴宽度异常：%d" % tl.width()
        at_before = float(it.at_sec)
        end_before = float(it.end_sec)
        before_n = len(lyrs[0].items)

        # --- ① 单击**不**进 trim（第七轮用户要求：单击不能拖两端）---
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xv, y))
        APP.processEvents()
        assert tl._trim is None and tl._resize is None, \
            "单击不应进 trim/resize（改长短须双击）：trim=%s resize=%s" % (
                tl._trim is not None, tl._resize is not None)
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xv, y))
        APP.processEvents()

        # --- ② 双击进剪辑态 ---
        QTest.mouseDClick(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xv, y))
        APP.processEvents()
        assert tl._edit_mode is not None, "双击片段应进入剪辑态"

        # --- ③ 剪辑态下按把手并拖动 ---
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xv, y))
        APP.processEvents()
        assert tl._trim is not None and tl._drag is None, \
            "剪辑态按把手应进 `_trim`：trim=%s drag=%s" % (
                tl._trim is not None, tl._drag is not None)
        QTest.mouseMove(tl, QPoint(xv - 150, y))
        APP.processEvents()
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xv - 150, y))
        APP.processEvents()
        # ⚠ 夹具素材仅 1.0s ⇒ 拖到远超源长时，trim 会**钳到源长**（end = at + src_dur = 4 + 1 = 5.0）
        assert it.end_sec is not None and abs(float(it.end_sec) - 5.0) < 0.4, \
            "剪辑态拉把手后 end 应被钳到源长（≈5.0）：%.1f → %s" % (end_before, it.end_sec)
        assert float(it.end_sec) < end_before - 1.0, \
            "end 应确实变小（120 → %s）" % it.end_sec
        assert abs(float(it.at_sec) - at_before) < 0.05, \
            "拉右边不应改起点：%.3f → %.3f" % (at_before, float(it.at_sec))
        assert len(lyrs[0].items) == before_n, "拉边不该增删片段"
        print("    ✓ 单击不进 trim / 双击后拉视口边缘把手：end %.0f → %.1fs，at 保持 %.1f"
              % (end_before, it.end_sec, float(it.at_sec)))
    finally:
        w.shutdown()


# ==========================================================================
# 12. 窗口走带由**音频驱动**（M4）：位置来自"已交声卡帧号"，不是挂钟
# ==========================================================================
class _GateSink:
    """假声卡出口：每写一块先拿一个 permit，帧号才计入（不喂就不前进）。"""

    def __init__(self):
        self.gate = threading.Semaphore(0)
        self.frames = 0

    def write(self, block):
        if not self.gate.acquire(timeout=5.0):
            raise TimeoutError("gate timeout")
        self.frames += int(block.shape[0])

    def close(self):
        pass


def test_playback_driven_by_audio(fx):
    w = _new_window()
    sink = _GateSink()
    try:
        w.new_sheet("AU")
        w.add_track("音频驱动", "x", 30.0, 120.0)
        w.session.track_audio[0] = fx["wav"]                 # 3s 音频
        # 用可控假 sink 替换真实声卡出口（无头 / 无声卡机器上也确定）
        w.audio = AudioPlayer(sink_factory=lambda sr, ch: sink, device_probe=lambda sr: True)
        assert w.audio.load(fx["wav"], wait=True), "音频应后台解码就绪"
        w._set_pos(0.0)
        w.toggle_play()
        assert w._playing and w._audio_backed, "应进入音频驱动播放"
        assert w.audio.is_playing(), "音频线程应在跑"

        # ★ 音频还没交帧 → 画面位置**不该**前进（挂钟会走；这正是旧实现的病）
        for _ in range(6):
            w._play_tick()
            time.sleep(0.02)
        assert w._pos == 0.0, "音频未交帧时位置不该动（否则又变成挂钟驱动）：%.3f" % w._pos

        # 喂一块 → 位置跟上"已交声卡帧号/采样率"
        sink.gate.release()
        assert _wait_until(lambda: w.audio.position_sec() > 0.0, timeout=2.0)
        w._play_tick()
        exp = sink.frames / w.audio.samplerate
        assert abs(w._pos - exp) < 1e-6, "窗口位置应 = 已交声卡帧号/采样率：%.4f vs %.4f" % (w._pos, exp)
        print("    ✓ 音频驱动：未喂帧位置=0；喂 %d 帧后位置=%.4fs（=帧号/采样率）"
              % (sink.frames, w._pos))
        assert w.btn_play.text() == "⏸"

        # 暂停 → 音频停
        w.toggle_play()
        assert not w._playing and not w.audio.is_playing()
        assert w.btn_play.text() == "▶"
        print("    ✓ 暂停：音频线程停下，传输条回到 ▶")
    finally:
        for _ in range(30):                                  # 放行假 sink 里排队的块，线程自然收尾
            sink.gate.release()
        try:
            w.audio.stop()
        except Exception:                                     # noqa: BLE001
            pass
        w.shutdown()


# ==========================================================================
# 13. 截图：**正在播放**状态（传输条 ⏸ + 播放头在走）
# ==========================================================================
def test_screenshot_playing(fx):
    assert APP is not None
    os.makedirs(_SHOTS, exist_ok=True)
    w = _new_window()
    try:
        w.new_sheet("播放演示")
        ids = w.add_media_paths([fx["a"], fx["b"], fx["img"]])
        w.add_track("播放中的曲目", "Demo Artist", 90.0, 128.0)
        w.session.track_audio[0] = fx["wav_long"]         # 90s 音频（起播点落在音频内）
        w.add_layer()
        lyrs = w._layers()
        w.add_cue(lyrs[0].lyr_id, ids[0], 4.0)
        w.add_cue(lyrs[0].lyr_id, ids[1], 40.0)
        w.add_cue(lyrs[1].lyr_id, ids[2], 12.0)
        w._select(lyrs[1].lyr_id, lyrs[1].items[0].item_id)
        w._preload_audio()
        assert w.audio.wait_loaded(10.0), "音频应后台解码就绪"
        w.resize(1240, 800)
        w.show()
        APP.processEvents()
        w._set_pos(12.0)
        w.preview.resize(700, 394)
        w.preview._compose()
        w.toggle_play()                                      # ▶ → ⏸（开始播放）
        time.sleep(0.35)
        APP.processEvents()
        w._play_tick()                                       # 播放头往前走一点
        w.preview._compose()
        w._refresh_all()
        APP.processEvents()
        assert w._playing and w.btn_play.text() == "⏸", "截图前应处于播放中（▶/⏸）"
        p = os.path.join(_SHOTS, "m4_playing.png")
        assert _save_shot(w, p), "截图保存失败: %s" % p
        assert os.path.isfile(p) and os.path.getsize(p) > 0
        print("  ✓ 播放中截图：%s (%d B)  按钮=%s  位置=%.2fs"
              % (p, os.path.getsize(p), w.btn_play.text(), w._pos))
        w.toggle_play()
    finally:
        w.shutdown()


# ==========================================================================
# 14. 回归哨兵：移动片段**保持时长**（P0 修复）
# ==========================================================================
def _find_item(w, lyr_id, item_id):
    for ly in w._layers():
        if ly.lyr_id == lyr_id:
            for it in ly.items:
                if it.item_id == item_id:
                    return ly, it
    return None, None


def test_move_preserves_duration(fx):
    w = _new_window()
    try:
        w.new_sheet("MOVE")
        ids = w.add_media_paths([fx["a"]])
        w.add_track("MOVE 曲", "x", 300.0, 120.0)
        w.add_layer()
        lyrs = w._layers()
        it = w.add_cue(lyrs[0].lyr_id, ids[0], 4.0)
        it.end_sec = 10.0                                  # at=4, end=10 ⇒ 时长 6s
        iid = it.item_id
        w._select(lyrs[0].lyr_id, iid)

        # (A) 拖到 15（**超过原 end_sec**）→ 时长必须仍是 6s，绝不压成 0
        w._on_timeline_moved(lyrs[0].lyr_id, iid, 15.0)
        _ly, cur = _find_item(w, lyrs[0].lyr_id, iid)
        assert abs(cur.at_sec - 15.0) < 1e-6, cur.at_sec
        assert cur.end_sec is not None and abs(cur.end_sec - 21.0) < 1e-6, \
            "拖动后时长应保持 6s（end=21），实际 end=%s" % cur.end_sec
        print("    拖动 4→15：at=%.1f end=%.1f（时长 %.1fs，未被压成 0）"
              % (cur.at_sec, cur.end_sec, cur.end_sec - cur.at_sec))

        # (B) 再往后拖到 100 → 时长不变
        w._select(lyrs[0].lyr_id, iid)
        w._on_timeline_moved(lyrs[0].lyr_id, iid, 100.0)
        _ly, cur = _find_item(w, lyrs[0].lyr_id, iid)
        assert abs(cur.at_sec - 100.0) < 1e-6 and abs(cur.end_sec - 106.0) < 1e-6, \
            (cur.at_sec, cur.end_sec)

        # (C) 跨层移动同样保持时长
        w._select(lyrs[0].lyr_id, iid)
        w._on_timeline_layer_moved(lyrs[0].lyr_id, iid, lyrs[1].lyr_id, 50.0)
        lyF, cur = _find_item(w, lyrs[1].lyr_id, iid)
        assert lyF is not None, "跨层后应落在目标层"
        assert abs(cur.at_sec - 50.0) < 1e-6 and abs(cur.end_sec - 56.0) < 1e-6, \
            "跨层移动后时长应保持 6s：at=%.1f end=%s" % (cur.at_sec, cur.end_sec)
        assert not any(x.item_id == iid for x in lyrs[0].items), "源层不该还留着"
        print("    跨层移动：at=%.1f end=%.1f（时长 %.1fs）"
              % (cur.at_sec, cur.end_sec, cur.end_sec - cur.at_sec))

        # (D) end_sec is None（"到下一片段/曲终"）→ 移动后仍为 None，不凭空造值
        #     ★ add_cue 现在会写入素材真实时长，故这里**显式**清成 None 来测"开放结尾"语义
        it2 = w.add_cue(lyrs[0].lyr_id, ids[0], 8.0)
        it2.end_sec = None
        assert it2.end_sec is None
        w._select(lyrs[0].lyr_id, it2.item_id)
        w._on_timeline_moved(lyrs[0].lyr_id, it2.item_id, 20.0)
        _ly, cur2 = _find_item(w, lyrs[0].lyr_id, it2.item_id)
        assert abs(cur2.at_sec - 20.0) < 1e-6 and cur2.end_sec is None, \
            "end_sec=None 应保持 None，实际 %s" % cur2.end_sec

        # (E) 坏数据（end < at）不产生负时长
        it3 = w.add_cue(lyrs[0].lyr_id, ids[0], 60.0)
        it3.end_sec = 55.0                                  # 坏数据
        w._select(lyrs[0].lyr_id, it3.item_id)
        w._on_timeline_moved(lyrs[0].lyr_id, it3.item_id, 70.0)
        _ly, cur3 = _find_item(w, lyrs[0].lyr_id, it3.item_id)
        assert cur3.end_sec is not None and cur3.end_sec >= cur3.at_sec, \
            "不得产生负时长：at=%.1f end=%s" % (cur3.at_sec, cur3.end_sec)
        print("  ✓ 移动保持时长：拖远不压零、跨层不变、None 保持 None、坏数据钳成 0")
    finally:
        w.shutdown()


# ==========================================================================
# 15. 回归哨兵：滑杆拖动**防抖**，不每帧重启音频（P1 修复）
# ==========================================================================
class _AlwaysSink:
    """立即吞下每一块（非阻塞）—— 让假播放器能顺利起播。"""

    def write(self, block):
        pass

    def close(self):
        pass


def test_slider_seek_debounced(fx):
    w = _new_window()
    try:
        w.new_sheet("SEEK")
        w.add_track("SEEK 曲", "x", 300.0, 120.0)
        w.session.track_audio[0] = fx["wav"]
        w.audio = AudioPlayer(sink_factory=lambda sr, ch: _AlwaysSink(),
                              device_probe=lambda sr: True)
        assert w.audio.load(fx["wav"], wait=True)
        w._set_pos(0.0)
        w.toggle_play()
        assert w._playing and w._audio_backed, "应处于音频驱动播放"

        # 统计音频重启次数
        calls = []
        orig = w.audio.play
        w.audio.play = lambda sec: (calls.append(float(sec)), orig(sec))[1]

        # 模拟连续 200 次 valueChanged（快速拖动）
        for i in range(200):
            w._on_slider(1 + (i % 50))
        assert len(calls) == 0, "拖动过程中不该重启音频（防抖未生效）：%d 次" % len(calls)
        # 静默后落地 → 只起播一次
        w._flush_audio_seek()
        assert len(calls) == 1, "防抖落地应只重启一次，实际 %d 次" % len(calls)
        print("    200 次 valueChanged → 拖动中 0 次重启，静默落地后 %d 次" % len(calls))

        # 再来一轮 + 松手（sliderReleased 也会 flush）→ 总数 ≤2
        for i in range(200):
            w._on_slider(1 + (i % 50))
        w._flush_audio_seek()                       # 模拟 sliderReleased
        assert len(calls) <= 2, "两轮拖动总重启次数应 ≤2，实际 %d" % len(calls)

        # ★ 暂停状态下拖滑杆**不该起播**（只移动播放头）
        w._stop_play()
        assert not w._playing
        calls.clear()
        for i in range(200):
            w._on_slider(1 + (i % 40))
        w._flush_audio_seek()
        assert len(calls) == 0, "暂停时拖滑杆不该起播音频：%d 次" % len(calls)
        print("  ✓ 滑杆防抖：200 次拖动仅 1 次重启；暂停拖动 0 次起播（只移播放头）")
    finally:
        w.shutdown()


# ==========================================================================
# 16. M5 哨兵：导出前检查（纯函数；缺素材 与 超长 被区分开）
# ==========================================================================
def test_pre_export_check(fx):
    w = _new_window()
    try:
        w.new_sheet("PRE")
        ids = w.add_media_paths([fx["a"]])
        w.session.sheet.media.append(MediaRef(clip_id="cX", name="不存在.mp4"))
        w.add_track("预检曲", "x", 60.0, 120.0)
        ly = w._layers()[0]
        it_ok = w.add_cue(ly.lyr_id, ids[0], 4.0)          # 正常片段
        it_long = w.add_cue(ly.lyr_id, ids[0], 100.0)      # 起点 100 > 曲目总长 60 → 超长
        rep = w.pre_export_report()
        # 缺素材：只有 cX 找不到
        assert len(rep.missing) == 1 and rep.missing[0].name == "不存在.mp4", rep.missing
        assert rep.blocking is True
        # 超长：只有 it_long 超出曲目总长
        long_ids = [o.item_id for o in rep.overlong]
        assert it_long.item_id in long_ids, long_ids
        assert it_ok.item_id not in long_ids, long_ids
        # ★ 吸附功能已移除：报告里不再有"未吸附"这一类
        assert not hasattr(rep, "unsnapped"), "Report 不应再有 unsnapped（吸附已移除）"
        assert rep.counts()["missing"] == 1, rep.counts()
        lines = "\n".join(rep.summary_lines())
        assert "缺素材" in lines and "超长" in lines, lines
        assert "未吸附" not in lines, lines
        # 纯函数直接调用（同一份清单）
        rep2 = precheck.check_sheet(w._sheet(), w.session.media_paths)
        assert len(rep2.missing) == 1 and not hasattr(rep2, "unsnapped"), rep2
        print("  ✓ 导出前检查：缺素材 %d / 超长 %d（吸附已移除，报告无 unsnapped）"
              % (len(rep.missing), len(rep.overlong)))
    finally:
        w.shutdown()


# ==========================================================================
# 17. M5 哨兵：缺失素材 UI（时间轴红色「文件丢失」+ 列表红点/变灰/「（丢失）」）
# ==========================================================================
def test_missing_media_ui(fx):
    w = _new_window()
    try:
        w.new_sheet("MISS")
        ids = w.add_media_paths([fx["a"]])
        cid_ok = ids[0]
        w.session.sheet.media.append(MediaRef(clip_id="cM", name="missing.mp4"))
        w.add_track("缺失曲", "x", 60.0, 120.0)
        ly = w._layers()[0]
        it_bad = w.add_cue(ly.lyr_id, "cM", 4.0)
        w.add_cue(ly.lyr_id, cid_ok, 24.0)
        w._refresh_all()
        tl = w.timeline
        # (A) 内部判定：缺失 / 正常 分开
        assert tl.is_clip_missing("cM") is True
        assert tl.is_clip_missing(cid_ok) is False
        # (B) 时间轴走的是「丢失」绘制分支 + 渲染像素含红色
        tl.resize(1000, 220)
        tl.set_view(0.0, 60.0)
        APP.processEvents()
        tl._missing_paint_count = 0
        img = tl.grab().toImage()
        assert tl._missing_paint_count >= 1, "缺失片段没走丢失绘制分支"
        red_hit = False
        y_lo = theme.RULER_H + 2
        y_hi = min(img.height(), theme.RULER_H + int(tl.row_h))
        for x in range(theme.GUTTER, img.width(), 4):
            for y in range(y_lo, y_hi, 3):
                c = img.pixelColor(x, y)
                if c.red() > 110 and c.red() > c.green() + 30:
                    red_hit = True
                    break
            if red_hit:
                break
        assert red_hit, "缺失片段没渲染出红色覆盖"
        # (C) 片段仍占位、仍可选中（不阻断交互）
        tl.set_selected(ly.lyr_id, it_bad.item_id)
        assert tl.selected_item()[1] is it_bad
        # (D) 素材列表：红点 + 变灰 + 后缀「（丢失）」
        lst = w.media_panel.lst
        item = next(lst.item(i) for i in range(lst.count())
                    if str(lst.item(i).data(Qt.UserRole)) == "cM")
        assert "（丢失）" in item.text(), item.text()
        assert item.foreground().color().name() == QColor(theme.c("fg_dim")).name(), \
            item.foreground().color().name()
        assert not item.icon().isNull(), "缺失项前面应有红色圆点图标"
        item_ok = next(lst.item(i) for i in range(lst.count())
                       if str(lst.item(i).data(Qt.UserRole)) == cid_ok)
        assert "（丢失）" not in item_ok.text()
        print("  ✓ 缺失素材 UI：时间轴「丢失」分支(计数 %d + 红色像素) + 列表红点/灰字/（丢失）"
              % tl._missing_paint_count)
    finally:
        w.shutdown()


# ==========================================================================
# 18. M5 哨兵：导入兼容提示（格式超前仍导入成功，不阻断）
# ==========================================================================
def test_import_compat_warning(fx):
    import json as _json
    import zipfile as _zip
    w = _new_window()
    try:
        p = os.path.join(fx["dir"], "future.ercue")
        d = {"format_version": 999, "min_app_version": "2099.01.01.00",
             "requires_features": ["未来的能力", "clip_speed"],
             "title": "未来码表", "media": [], "tracks": []}
        with _zip.ZipFile(p, "w") as z:
            z.writestr(JSON_NAME, _json.dumps(d, ensure_ascii=False))
        key = w.import_sheet(p)
        assert key, "格式超前也必须导入成功（规格书 §3.2：一律不阻断）"
        assert w._sheet() is not None and w._sheet().title == "未来码表"
        assert len(w.last_import_warnings) >= 2, w.last_import_warnings
        assert any("更新版本的工具" in x for x in w.last_import_warnings), w.last_import_warnings
        assert any("本版没有的能力" in x for x in w.last_import_warnings), w.last_import_warnings
        assert any("以上版本" in x for x in w.last_import_warnings), w.last_import_warnings
        assert "兼容提示" in w.lbl_state.text(), w.lbl_state.text()
        print("  ✓ 兼容提示：v999 超前格式仍导入成功，warnings=%d 条：%s"
              % (len(w.last_import_warnings), " ｜ ".join(w.last_import_warnings)))
    finally:
        w.shutdown()


# ==========================================================================
# 19. M5 哨兵：内嵌素材开关 —— 超 1 GB 只**提醒**（不再硬性拒绝）；≤1GB 静默通过
# ==========================================================================
def test_embed_switch_limit(fx):
    w = _new_window()
    try:
        w.new_sheet("EMB")
        w.add_media_paths([fx["a"], fx["b"]])
        over, total, names = w._embed_size_check(size_of=lambda p: 1024)
        assert over is False and total == 2 * 1024, (over, total)
        # 600 MB ×2 = 1.2 GB ⇒ 超阈值（over=True）；单测"未超"需用 400 MB ×2 = 800 MB
        mid = 600 * 1024 * 1024
        over_mid, total_mid, _ = w._embed_size_check(size_of=lambda p: mid)
        assert over_mid is True and total_mid == 2 * mid, (over_mid, total_mid)
        small = 400 * 1024 * 1024
        over_small, total_small, _ = w._embed_size_check(size_of=lambda p: small)
        assert over_small is False and total_small == 2 * small, (over_small, total_small)
        # 1.5 GB：超阈值 ⇒ over=True（**仅提醒**，是否继续由弹窗/用户决定）
        big = 1536 * 1024 * 1024
        over, total, names = w._embed_size_check(size_of=lambda p: big)
        assert over is True and total == 2 * big and len(names) == 2, (over, total, names)
        over2, total2, names2 = precheck.check_embed_size(w._sheet(), w.session.media_paths,
                                                          size_of=lambda p: big)
        assert over2 is False and total2 == 2 * big and len(names2) == 2, (over2, total2, names2)
        ok3, total3, _ = precheck.check_embed_size(w._sheet(), w.session.media_paths,
                                                   size_of=lambda p: 10 * 1024 * 1024)
        assert ok3 is True and total3 == 20 * 1024 * 1024
        print("  ✓ 内嵌提醒：2×1.5GB ⇒ over=True（只提醒不拒绝）；2×600MB ⇒ 放行；正常体积放行")
    finally:
        w.shutdown()


# ==========================================================================
# 20. M5 哨兵：配置文件持久化 + 目录不可写不崩
# ==========================================================================
def test_config_persistence():
    d = tempfile.mkdtemp(prefix="ercue_cfgtest_")
    p = os.path.join(d, "config.json")
    c = app_config.Config(p)
    c.set("embed_media", True)
    c.add_recent(os.path.join(d, "x.ercue"))
    c2 = app_config.Config(p)
    assert c2.get("embed_media") is True, "写→读往返失败"
    assert any(str(x).endswith("x.ercue") for x in c2.recent()), c2.recent()
    # 目录不可写 → monkeypatch 写入函数抛异常，save() 必须吞掉且不崩
    cu = app_config.Config(os.path.join(d, "u.json"))
    orig = app_config._atomic_write_json

    def _boom(path, data):
        raise PermissionError("目录不可写（模拟）")

    app_config._atomic_write_json = _boom
    try:
        assert cu.save() is False and cu.writable is False, "不可写应优雅降级"
    finally:
        app_config._atomic_write_json = orig
    assert cu.save() is True, "恢复后应能再保存"
    print("  ✓ 配置持久化：写→读往返 OK；目录不可写时 save() 返回 False 且不崩溃")


# ==========================================================================
# 21. M5 截图：缺失素材红色「文件丢失」覆盖
# ==========================================================================
def test_screenshot_missing_media(fx):
    assert APP is not None
    os.makedirs(_SHOTS, exist_ok=True)
    w = _new_window()
    try:
        w.new_sheet("缺失素材演示")
        ids = w.add_media_paths([fx["a"], fx["img"]])
        w.session.sheet.media.append(MediaRef(clip_id="cGone", name="lost_clip.mp4"))
        w.add_track("演示曲目", "Demo", 120.0, 128.0)
        w.add_layer()
        lyrs = w._layers()
        w.add_cue(lyrs[0].lyr_id, ids[0], 4.0)
        w.add_cue(lyrs[0].lyr_id, "cGone", 20.0)     # 丢失片段：红覆盖「文件丢失」
        w.add_cue(lyrs[1].lyr_id, ids[1], 30.0)
        w._refresh_all()
        w.resize(1240, 800)
        w.show()
        APP.processEvents()
        w._set_pos(20.0)
        w.preview.resize(700, 394)
        w.preview._compose()
        w._refresh_all()
        APP.processEvents()
        p = os.path.join(_SHOTS, "m5_missing_media.png")
        assert _save_shot(w, p), "截图保存失败: %s" % p
        assert os.path.isfile(p) and os.path.getsize(p) > 0, "截图为空"
        assert w.timeline._missing_paint_count >= 1, "截图里应有丢失片段"
        print("  ✓ 缺失素材截图：%s (%d B)" % (p, os.path.getsize(p)))
    finally:
        w.shutdown()


# ==========================================================================
# 22. M5 哨兵：给用户看的报告文本 report_text（两类问题都出现、条数正确）
# ==========================================================================
def test_report_text():
    from ercue.model import precheck as _pc
    from ercue.ui import dialogs

    rep = _pc.Report()
    rep.missing = [_pc.MissingClip(clip_id="c1", name="a.mp4")]
    rep.overlong = [
        _pc.OverlongItem(track_index=0, lyr_id="L1", item_id="i1", reason="beyond_track"),
        _pc.OverlongItem(track_index=0, lyr_id="L1", item_id="i2", reason="beyond_media",
                         media_duration_sec=10.0),
    ]
    txt = dialogs.report_text(rep)
    # 两类都出现
    assert "缺素材" in txt and "a.mp4" in txt, txt
    assert "超长" in txt, txt
    # ★ 吸附已移除：报告文本里不再有"未吸附"
    assert "未吸附" not in txt, txt
    # 条数正确
    assert "1 个素材没找到" in txt, txt
    assert "2 个片段" in txt, txt
    # 超长的两种子因都在
    assert "超出曲目总长" in txt and "超出素材本身时长" in txt, txt
    # 缺素材是"阻止导出"的重要提示
    assert "阻止导出" in txt, txt
    # 干净报告
    clean = dialogs.report_text(_pc.Report())
    assert "检查通过" in clean and "未吸附" not in clean, clean
    # 没用检查结果时也不崩
    assert "没有检查结果" in dialogs.report_text(None)
    print("  ✓ report_text：缺素材1/超长2 两类齐全 + 阻挡提示 + 干净报告（无「未吸附」） %r"
          % clean[:16])


# ==========================================================================
# 23. M5 哨兵：素材目录**递归扫描**（深层扫到 / 跳过系统目录 / 限深 / 同名先到先得）
# ==========================================================================
def test_scan_media_index():
    from ercue.ui.window import SCAN_MAX_DEPTH, _scan_media_index

    root = tempfile.mkdtemp(prefix="ercue_scan_")

    def touch(*parts):
        p = os.path.join(root, *parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(b"x")
        return p

    deep = touch("a", "b", "c", "deep.mp4")                 # 第 3 层：应扫到
    touch(".git", "in_git.mp4")                             # 跳过
    touch("$RECYCLE.BIN", "in_bin.mp4")                     # 跳过
    touch("Windows", "in_win.mp4")                          # 跳过
    touch("node_modules", "in_nm.mp4")                      # 跳过
    too_deep = os.path.join(root, "d1", "d2", "d3", "d4", "d5", "toodeep.mp4")
    os.makedirs(os.path.dirname(too_deep), exist_ok=True)   # 第 5 层：超过上限，不扫
    with open(too_deep, "wb") as f:
        f.write(b"x")
    root_same = touch("same.mp4")                           # 根层，先到
    touch("sub", "same.mp4")                                # 更深，后到 → 不覆盖

    idx = _scan_media_index(root, SCAN_MAX_DEPTH)
    assert idx.get("deep.mp4") == deep, "深层（第 3 层）文件应被扫到"
    assert "in_git.mp4" not in idx, "应跳过 .git"
    assert "in_bin.mp4" not in idx, "应跳过 $RECYCLE.BIN"
    assert "in_win.mp4" not in idx, "应跳过 Windows"
    assert "in_nm.mp4" not in idx, "应跳过 node_modules"
    assert "toodeep.mp4" not in idx, "超过深度上限的不该进"
    assert idx.get("same.mp4") == root_same, "同名先到先得（根层先于子目录）"
    # 不存在的目录 → 空表，不抛
    assert _scan_media_index(os.path.join(root, "nope")) == {}
    print("  ✓ 递归扫描：深层命中=%s / 跳过 .git·$RECYCLE.BIN·Windows·node_modules / 限深 / 同名先到先得"
          % os.path.basename(idx["deep.mp4"]))


# ==========================================================================
# 24. M5 哨兵：缺失素材「逐个指定新路径」的落库（media_paths 更新 + 不再走丢失分支）
# ==========================================================================
def test_missing_media_remap(fx):
    from ercue.ui import dialogs
    w = _new_window()
    try:
        w.new_sheet("REMAP")
        w.add_media_paths([fx["a"]])
        w.session.sheet.media.append(MediaRef(clip_id="cM", name="lost.mp4"))
        w.add_track("重挂曲", "x", 60.0, 120.0)
        ly = w._layers()[0]
        w.add_cue(ly.lyr_id, "cM", 4.0)
        w._refresh_all()
        assert w.timeline.is_clip_missing("cM") is True
        assert "cM" not in w.session.media_paths
        # 模拟「逐个指定新路径…」的指认结果（模态对话框无头驱动不了，直接喂映射）
        orig = dialogs.ask_missing_media
        dialogs.ask_missing_media = lambda parent, names: {"lost.mp4": fx["b"]}
        try:
            w._prompt_missing_media(["lost.mp4"])
        finally:
            dialogs.ask_missing_media = orig
        # 落库断言
        assert w.session.media_paths.get("cM") == os.path.abspath(fx["b"]), w.session.media_paths
        w._refresh_all()
        assert w.timeline.is_clip_missing("cM") is False, "重挂后不该再走丢失分支"
        # 重绘时间轴：本帧丢失分支计数应为 0
        w.timeline.resize(1000, 220)
        w.timeline.set_view(0.0, 60.0)
        APP.processEvents()
        w.timeline._missing_paint_count = 0
        w.timeline.grab()
        assert w.timeline._missing_paint_count == 0, "重挂后不应再有「文件丢失」覆盖"
        # 「用占位色块先跑」（返回空映射）→ 不改动 media_paths，也不崩
        # ⚠ 必须 monkeypatch：真实 ask_missing_media 在有 QApplication 时会 exec() 模态框而阻塞
        w.session.media_paths.pop("cM", None)
        w._refresh_all()
        assert w.timeline.is_clip_missing("cM") is True
        dialogs.ask_missing_media = lambda parent, names: {}
        try:
            w._prompt_missing_media(["lost.mp4"])
        finally:
            dialogs.ask_missing_media = orig
        assert w.timeline.is_clip_missing("cM") is True, "占位色块分支不该改映射"
        print("  ✓ 缺失素材重挂：{name:path} → media_paths 更新 + 时间轴不再走丢失分支")
    finally:
        w.shutdown()


# ==========================================================================
# 25. M5 哨兵：最近文件子菜单（写入后数量/顺序正确，清除后为空）
# ==========================================================================
def test_recent_menu():
    w = _new_window()
    try:
        d = tempfile.mkdtemp(prefix="ercue_recent_")
        p1 = os.path.join(d, "first.ercue")
        p2 = os.path.join(d, "second.ercue")
        for p in (p1, p2):
            with open(p, "w", encoding="utf-8") as f:
                f.write("{}")

        def labels():
            return [a.text() for a in w.recent_menu.actions() if not a.isSeparator()]

        w.cfg.set("recent_files", [])
        w._fill_recent_menu()
        assert labels() == ["（暂无）"], labels()
        # 写入两条 → 新在前
        w.cfg.add_recent(p1)
        w.cfg.add_recent(p2)
        w._fill_recent_menu()
        assert labels() == ["second.ercue", "first.ercue", "清除最近列表"], labels()
        # 清除
        w._clear_recent()
        assert w.cfg.recent() == [], w.cfg.recent()
        assert labels() == ["（暂无）"], labels()
        # 已消失的文件不会出现在菜单里（过滤 isfile）
        w.cfg.add_recent(os.path.join(d, "gone.ercue"))
        w._fill_recent_menu()
        assert labels() == ["（暂无）"], labels()
        print("  ✓ 最近文件子菜单：空态→写入(新在前)→清除为空→失效文件被过滤")
    finally:
        w.shutdown()


# ==========================================================================
_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",    # 黑体
    r"C:\Windows\Fonts\simsun.ttc",    # 宋体
)


def _install_cjk_font(app) -> str:
    """给无头 offscreen 平台装一套中文字体。

    ⚠ offscreen 平台插件**不加载系统字体库**（实测 `QFontDatabase.families()` 返回 0 个），
    于是截图里所有中文都渲染成方框 —— 那是**采集环境**的问题，不是界面代码的问题。
    这里显式把系统字体喂给 Qt，只为让截图可读；真实桌面平台走系统字体，不需要这一步。
    """
    fonts = QFontDatabase.families()
    if any("YaHei" in f or "SimHei" in f or "SimSun" in f for f in fonts):
        return "(系统字体已可用)"
    for path in _FONT_CANDIDATES:
        if not os.path.isfile(path):
            continue
        fid = QFontDatabase.addApplicationFont(path)
        fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
        if fams:
            app.setFont(QFont(fams[0], 9))
            return "%s -> %s" % (os.path.basename(path), fams[0])
    return "(未找到可用中文字体，截图中文仍会是方框)"


def main() -> int:
    global APP
    APP = QApplication.instance() or QApplication([])
    # 配置隔离：把配置单例指向临时目录（绝不碰用户真实 %LOCALAPPDATA%\EasyRealityCue）
    app_config._INSTANCE = app_config.Config(
        os.path.join(tempfile.mkdtemp(prefix="ercue_cfg_ui_"), "config.json"))
    fx = _fixtures()
    print("=" * 70)
    print("M3 · UI 冒烟 + 端到端测试  (python %s, QT_QPA_PLATFORM=%s)"
          % (sys.version.split()[0], os.environ.get("QT_QPA_PLATFORM")))
    print("字体: %s" % _install_cjk_font(APP))
    print("夹具目录: %s" % fx["dir"])
    print("截图目录: %s" % _SHOTS)
    print("=" * 70)
    tests = [
        ("主窗口构建", test_window_boots, False),
        ("端到端闭环", test_end_to_end, True),
        ("B1 哨兵", test_b1_refresh_no_signal_storm, True),
        ("B2 哨兵", test_b2_no_self_excite, True),
        ("C3 哨兵", test_c3_total_from_audio_duration, True),
        ("标尺自适应", test_ruler_plan, False),
        ("时间轴几何/命中（吸附已移除）", test_timeline_geometry, False),
        ("多码表隔离", test_multi_sheet_isolation, True),
        ("撤销/重做", test_undo, True),
        ("缩略图真的出图", test_thumbnail_produced, True),
        ("素材拖放+时间轴鼠标拖拽", test_drag_drop_and_timeline_mouse, True),
        ("★ 双击拖两端（含视口边缘把手）", test_trim_handle_double_click_when_edge_offscreen, True),
        ("窗口走带由音频驱动", test_playback_driven_by_audio, True),
        ("移动片段保持时长", test_move_preserves_duration, True),
        ("滑杆 seek 防抖", test_slider_seek_debounced, True),
        ("截图", test_screenshots, True),
        ("截图·正在播放", test_screenshot_playing, True),
        ("M5 导出前检查", test_pre_export_check, True),
        ("M5 缺失素材 UI", test_missing_media_ui, True),
        ("M5 兼容提示不阻断", test_import_compat_warning, True),
        ("M5 内嵌开关上限", test_embed_switch_limit, True),
        ("M5 配置持久化", test_config_persistence, False),
        ("M5 截图·缺失素材", test_screenshot_missing_media, True),
        ("M5 报告文本", test_report_text, False),
        ("M5 素材目录递归扫描", test_scan_media_index, False),
        ("M5 缺失素材重挂落库", test_missing_media_remap, True),
        ("M5 最近文件子菜单", test_recent_menu, False),
    ]
    ok_all = True
    for name, fn, needs_fx in tests:
        print("\n--- %s ---" % name)
        try:
            fn(fx) if needs_fx else fn()
        except Exception as e:                                  # noqa: BLE001
            ok_all = False
            print("  ✗ %s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()))
    print("\n" + "=" * 70)
    print("汇总：%s" % ("全部通过 ✓" if ok_all else "有失败 ✗"))
    print("=" * 70)
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
