# -*- coding: utf-8 -*-
"""Step5 UI 自检：码表对话框（导入 / 手动绑歌 / 4 类错误 / 最近文件 / 卸下 / 手动起拍）。

**不弹真窗口**（`QT_QPA_PLATFORM=offscreen`），但走的是**真代码**：
`cue_ui.CueSheetDialog` + 真 `cuesheet.load` + 真 `engine.set_cue_sheet/clear_cue_sheet`
（解码器用假播放器，不碰磁盘）。

覆盖团队给的 5 项判据：
  1. **手动绑歌 → 播 → 画面跟着拍位走**（钟起拍后 `play_sec` 随时间递增，且 `cs_states` 非空）
  2. **4 类错误各有明确提示**（损坏包 / 版本不符 / 缺素材 / 认不出歌）
  3. **最近文件持久化**（重启后还在 = 重新读配置文件仍在）
  4. **卸下后**：`_cue_players` 清空、解码器全关、时钟停
  5. 守卫/parity/单测另跑（本文件只做 UI 面）

运行：`python tools/cue_ui_test.py`（exit 0 = 全过）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from PySide6.QtGui import QImage, QColor                          # noqa: E402
from PySide6.QtWidgets import QApplication                        # noqa: E402

import engine                                                     # noqa: E402
import cuesheet                                                   # noqa: E402
import cuesheet_runtime as crt                                    # noqa: E402
import cue_ui                                                     # noqa: E402
from cue_seek_policy import CueSeekPolicy                         # noqa: E402

W, H = 320, 180          # 小画布：只为让 compose 便宜
BPM = 120.0
BL = 0.5


# ==========================================================================
class FakeCfg:
    """最小 Config 替身：支持 `cfg["cue"]` / 赋值 / `save()`，**真的落盘**（测持久化）。"""

    def __init__(self, path):
        self.path = path
        self.data = {}
        if os.path.isfile(path):
            try:
                self.data = json.load(open(path, encoding="utf-8"))
            except Exception:                                    # noqa: BLE001
                self.data = {}
        self.saves = 0

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v

    def save(self, force=False):
        self.saves += 1
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False)


class FakePlayer:
    created = []
    closed = 0

    def __init__(self, img):
        self._img = img
        self._active = False
        self.closed_flag = False
        FakePlayer.created.append(self)

    def current(self):
        return self._img

    def set_active(self, a):
        self._active = bool(a)

    def warm_up(self):
        pass

    def set_max_size(self, w, h):
        pass

    def set_speed(self, s):
        pass

    def position(self):
        return 0.0

    def seek(self, sec):
        return True

    def close(self):
        if not self.closed_flag:
            self.closed_flag = True
            FakePlayer.closed += 1


def new_engine(layers):
    eng = engine.AutoVJEngine.__new__(engine.AutoVJEngine)
    eng.canvas = QImage(W, H, QImage.Format_ARGB32)
    eng.mode = "normal"
    eng.blackout = False
    eng.cfg = {"auto": {"intensity": 1, "transition": None},
               "mode": {"transition": ""}, "color": {}, "postfx": {}}
    eng._silent_now_debounced = False
    eng._health_log = None
    eng._cue_tick_fail = 0
    eng._cue_err_at = 0.0
    eng._cue_pos_beats = None
    eng._cue_pos_src = ""
    eng._cue_sheet = None
    eng._cue_track = None
    eng._cue_media = {}
    eng._cue_jumped = False
    eng._cue_closed_last = 0
    eng._cue_policy = CueSeekPolicy(cfg={})
    eng._cue_rt = None
    eng.layers = list(layers)
    return eng


class Owner:
    def __init__(self, cfg, eng):
        self.cfg = cfg
        self.engine = eng


def make_sheet(n_tracks=1, with_items=True, fmt=1, min_app=""):
    s = cuesheet.CueSheet(title="UI 自检", format_version=fmt, min_app_version=min_app,
                          res_w=W, res_h=H)
    for k in range(n_tracks):
        te = cuesheet.TrackEntry(track_key="k%d" % k)
        te.identity = cuesheet.Identity(title="曲目%d" % k, artist="艺术家",
                                        duration_sec=200.0 + k, bpm=BPM)
        te.identity.ensure_meta_id()
        L = cuesheet.CompLayer(lyr_id="L%d" % k, name="层%d" % k, z=0)
        if with_items:
            L.items.append(cuesheet.CueItem(item_id="i%d" % k, clip_id="c1",
                                            at_beats=0.0, src_in_sec=0.0,
                                            src_out_sec=30.0, speed=1.0,
                                            trans_in="cut", trans_out="cut").normalized())
        te.layers.append(L)
        s.tracks.append(te)
    s.media.append(cuesheet.MediaRef(clip_id="c1", name="a.mp4"))
    return s


def save_pkg(sheet, path, embed=False):
    cuesheet.save(sheet, path, app_version="2026.10.17.12")
    return path


# ==========================================================================
def main():
    fails = []

    def check(name, cond, detail=""):
        print("  [%s] %s%s" % ("ok  " if cond else "FAIL", name,
                               "" if cond else "   ← " + str(detail)))
        if not cond:
            fails.append(name)

    app = QApplication.instance() or QApplication(sys.argv)

    class _FDStub:
        """用户永远"取消"的文件对话框（避免无头环境弹模态框挂住）。"""

        @staticmethod
        def getOpenFileNames(*a, **k):
            return ([], "")

        @staticmethod
        def getExistingDirectory(*a, **k):
            return ""

    real_fd0 = cue_ui.QFileDialog
    cue_ui.QFileDialog = _FDStub
    tmp = tempfile.mkdtemp(prefix="cueui_")
    cfg_path = os.path.join(tmp, "cfg.json")
    cfg = FakeCfg(cfg_path)

    # 假播放器（不碰磁盘）
    real_make = engine.make_player
    img = QImage(W, H, QImage.Format_ARGB32)
    img.fill(QColor(30, 200, 90, 255))

    def _make(item, cue=False):
        p = FakePlayer(img)
        p.cue_flag = bool(cue)
        return p

    engine.make_player = _make
    try:
        # ---------------- ① 手动绑歌 → 起拍 → 画面跟拍 ----------------
        print("① 单曲目码表：自动绑定 + 手动起拍 ⇒ 画面跟着拍位走")
        lay = engine.Layer("码表层")
        lay.is_cuesheet = True
        eng = new_engine([lay])
        owner = Owner(cfg, eng)
        pkg1 = save_pkg(make_sheet(1), os.path.join(tmp, "one.ercue"))
        # 包内没有内嵌 media：给一个真实存在的同名空文件 + 记下 media_dir
        # （否则 missing=True ⇒ 子层被跳过 ⇒ 画面黑；黑是"缺素材"的正确表现，但这里要测出画）
        media_root = os.path.join(tmp, "media")
        os.makedirs(media_root, exist_ok=True)
        open(os.path.join(media_root, "a.mp4"), "wb").close()
        cue_ui.cue_cfg(cfg)["media_dir"] = media_root
        dlg = cue_ui.CueSheetDialog(owner)
        ok = dlg.import_one(pkg1)
        check("导入成功并建立运行时", ok is True and eng._cue_rt is not None,
              (ok, eng._cue_rt))
        check("单曲目自动绑定（未认歌但按唯一曲目绑定）",
              dlg.track is not None and "唯一曲目" in "\n".join(dlg.warnings),
              dlg.warnings)
        st = eng.cuesheet_status()
        check("状态：已装载 / 片段数 1 / 已绑图层 1 个",
              st["loaded"] and st["rt_items"] == 1 and len(st["bound_layers"]) == 1, st)

        dlg.start_clock()
        time.sleep(0.35)
        app.processEvents()
        pos1 = eng._cue_pos_beats
        eng._cuesheet_tick()
        ps1 = lay.cs_states[0].play_sec if lay.cs_states else None
        time.sleep(0.30)
        app.processEvents()
        pos2 = eng._cue_pos_beats
        eng._cuesheet_tick()
        ps2 = lay.cs_states[0].play_sec if lay.cs_states else None
        dlg.stop_clock()
        print("      拍位 %.2f → %.2f（Δ%.2f）；play_sec %.2f → %.2f（Δ%.2f）"
              % (pos1 or -1, pos2 or -1, (pos2 or 0) - (pos1 or 0),
                 ps1 or -1, ps2 or -1, (ps2 or 0) - (ps1 or 0)))
        check("起拍后拍位随时间前进", pos1 is not None and pos2 is not None
              and pos2 > pos1 + 0.2, (pos1, pos2))
        check("画面状态跟着走（play_sec 递增）",
              ps1 is not None and ps2 is not None and ps2 > ps1 + 0.1, (ps1, ps2))
        check("拍位换算正确（Δt/0.5 ⇒ Δ拍位≈2×Δt）",
              abs(((pos2 - pos1) * BL) - 0.30) <= 0.12,
              ((pos2 - pos1) * BL))
        eng._compose({"bass": 0.0})
        c = eng.canvas.pixelColor(W // 2, H // 2)
        check("确实画出码表内容（中心是假播放器的绿）",
              c.green() > 120 and c.red() < 120, c.getRgb())

        # ---------------- ③ 最近文件持久化 ----------------
        print("\n③ 最近文件：入列 / 去重 / 落盘 / 重启后仍在")
        lst = cue_ui.cue_cfg(cfg)["recent"]
        check("导入后写入 recent", lst and os.path.abspath(pkg1) == lst[0], lst)
        cue_ui.add_recent(cfg, pkg1)
        check("重复导入不重复入列", len(cue_ui.cue_cfg(cfg)["recent"]) == 1,
              cue_ui.cue_cfg(cfg)["recent"])
        for i in range(12):
            cue_ui.add_recent(cfg, os.path.join(tmp, "x%d.ercue" % i))
        check("最多 10 条", len(cue_ui.cue_cfg(cfg)["recent"]) == 10,
              len(cue_ui.cue_cfg(cfg)["recent"]))
        cfg2 = FakeCfg(cfg_path)          # ← "重启"：重新读文件
        check("重启后 recent 还在（真的落盘了）",
              len(cue_ui.cue_cfg(cfg2)["recent"]) == 10,
              cue_ui.cue_cfg(cfg2)["recent"][:2])

        # ---------------- ④ 卸下 ----------------
        print("\n④ 卸下：解码器全关 / 时钟停 / 状态清空")
        eng._get_player(lay, eng._cue_item(lay, "P:/x/a.mp4", None))
        n_cache = len(lay._cue_players)
        before = FakePlayer.closed
        dlg.start_clock()
        dlg.on_uninstall()
        check("卸下后 _cue_players 清空", lay._cue_players == {}, lay._cue_players)
        check("解码器全部 close()", FakePlayer.closed - before == n_cache,
              (FakePlayer.closed - before, n_cache))
        check("时钟已停（_t0 is None）", dlg._t0 is None, dlg._t0)
        check("状态显示回到未装载", "未装载" in dlg.lbl_status.text(),
              dlg.lbl_status.text())

        # ---------------- ② 四类错误 ----------------
        print("\n② 四类错误的可见提示")
        # (a) 损坏包
        bad = os.path.join(tmp, "broken.ercue")
        with open(bad, "wb") as f:
            f.write(b"not a zip at all")
        ok = dlg.import_one(bad)
        check("(a) 损坏包：导入失败且错误可见",
              ok is False and "导入失败" in dlg.lbl_error.text(), dlg.lbl_error.text()[:80])
        # (b) 版本不符（format_version 更高 + min_app_version 更高）
        fut = make_sheet(1, fmt=cuesheet.FORMAT_VERSION + 5, min_app="2099.01.01.00")
        fut.requires_features = ["clip_crop", "未来的能力"]
        pkg_f = save_pkg(fut, os.path.join(tmp, "future.ercue"))
        if True:
            # (b)(c) 要复现"缺素材"：先把 media_dir 清掉（否则会按目录扫到 a.mp4）
            cue_ui.cue_cfg(cfg)["media_dir"] = ""
            ok = dlg.import_one(pkg_f)
            txt = dlg.lbl_error.text()
            check("(b) 版本不符：仍尽力装载 + 两条告警可见",
                  ok is True and ("更新版本" in txt) and ("要求" in txt), txt[:120])
            # (c) 缺素材
            check("(c) 缺素材：提示缺 N 个",
                  ("缺 1 个素材" in txt) or ("有 1 个素材没找到" in txt), txt[:150])

            # (d) 认不出歌 + 用户取消选择 ⇒ 装表但不接管
            two = make_sheet(2)
            pkg2 = save_pkg(two, os.path.join(tmp, "two.ercue"))
            real_pick = cue_ui.pick_track
            cue_ui.pick_track = lambda sheet, parent=None, current_title="": None
            try:
                ok2 = dlg.import_one(pkg2)
                msg = dlg.lbl_error.text()
                check("(d1) 认不出歌 + 用户取消 ⇒ 不接管且提示可见",
                      ok2 is False and "没有绑定曲目" in msg, msg[:120])
            finally:
                cue_ui.pick_track = real_pick
            # (d2) 认不出歌 + 用户选了第 2 首 ⇒ 绑定成功
            lay2 = engine.Layer("码表层2")
            lay2.is_cuesheet = True
            eng2 = new_engine([lay2])
            dlg2 = cue_ui.CueSheetDialog(Owner(cfg, eng2))
            cue_ui.pick_track = lambda sheet, parent=None, current_title="": sheet.tracks[1]
            try:
                ok3 = dlg2.import_one(pkg2)
                _t = getattr(getattr(dlg2.track, "identity", None), "title", None)
                check("(d2) 用户选第 2 首 ⇒ 绑定成功并接管",
                      ok3 is True and _t == "曲目1", (ok3, _t))
                check("状态里显示所选曲目名",
                      "曲目1" in dlg2.lbl_status.text(), dlg2.lbl_status.text()[:120])
            finally:
                cue_ui.pick_track = real_pick
        dlg.close()
        dlg2.close()
    finally:
        engine.make_player = real_make
        cue_ui.QFileDialog = real_fd0

    print("")
    if fails:
        print("UI_FAIL  %d 项不通过：%s" % (len(fails), "、".join(fails)))
        return 1
    print("UI_OK  手动绑歌/起拍跟拍 · 4 类错误提示 · 最近文件持久化 · 卸下清理 全过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
