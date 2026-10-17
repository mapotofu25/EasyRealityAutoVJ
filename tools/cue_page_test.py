# -*- coding: utf-8 -*-
"""码表管理页（第 4 页签）+ 码表图层 + 图层头码表下拉 —— 自检脚本。

判据（团队给定，2026-10 用户更正版）：
  1. 第 4 页签「码表」：列表（表名/覆盖曲目/片段/大小/修改时间/已绑图层）+ 按钮区 → **截图**
  2. 添加：导入 `.ercue` ⇒ 列表出现该行（覆盖曲目 / 片段 / 大小 都对）
  3. 删除：从列表移除 ⇒ 行消失、**磁盘文件仍在**
  4. 双击 ⇒ 详情窗口（只读，显示曲目 + BPM + 素材清单）→ **截图**
  5. 新建「码表图层」⇒ 选表 ⇒ 图层出现且带青绿码表下拉 → **截图**
  6. 旧能力不退化（另跑 cue_ui_test / cue_settings_test / cue_badge_test）
  7. 回归 13 项（另跑）
  8. 图层头显示带表名的下拉（未绑时「（无码表）」）→ **截图**
  9. 下拉切换：换表 ⇒ 引擎的表跟着换；选「（无码表）」⇒ 卸下
 10. 「⋯ 更多」菜单里**不再有**码表项（源码级断言）

运行：`python tools/cue_page_test.py`（exit 0 = 全过）
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
SHOT_DIR = os.path.join(HERE, "_cueshots")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from PySide6.QtWidgets import QApplication, QAbstractItemView          # noqa: E402

import cuesheet                                                       # noqa: E402
import cue_page                                                       # noqa: E402
import cue_ui                                                         # noqa: E402
import engine                                                         # noqa: E402
import panels                                                         # noqa: E402
from cue_seek_policy import CueSeekPolicy                             # noqa: E402

W, H, BPM = 320, 180, 120.0


# ==========================================================================
class FakeCfg:
    def __init__(self):
        self.data = {}

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v

    def __contains__(self, k):
        return k in self.data

    def get(self, k, d=None):
        return self.data.get(k, d)

    def save(self, force=False):
        pass


class FakePlayer:
    """码表运行时只需要 `current/set_active/...` 这几个方法。"""

    def __init__(self, img):
        self._img = img

    def current(self):
        return self._img

    def set_active(self, a):
        pass

    def warm_up(self):
        pass

    def set_max_size(self, w, h):
        pass

    def set_speed(self, s):
        pass

    def position(self):
        return 0.0

    def seek(self, s):
        return True

    def close(self):
        pass


def new_engine(layers):
    eng = engine.AutoVJEngine.__new__(engine.AutoVJEngine)
    eng.canvas = None
    eng.mode = "normal"
    eng.blackout = False
    eng.cfg = FakeCfg()
    eng.cfg["auto"] = {"intensity": 1, "transition": None}
    eng.cfg["mode"] = {"transition": ""}
    eng.cfg["color"] = {}
    eng.cfg["postfx"] = {}
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
    eng._cue_index = None
    eng.layers = list(layers)
    eng.library = {}
    return eng


class Owner:
    """`CueSheetPanel` / `CueStackPanel` 需要的最小宿主。"""

    def __init__(self, eng, cfg):
        self.engine = eng
        self.cfg = cfg
        self.layers_panel = None
        self.saved = 0
        self.opened_dialog = 0

    def save_layers(self):
        self.saved += 1
        try:
            self.engine.push_layers_to_cfg()
        except Exception:                                            # noqa: BLE001
            pass

    def open_cuesheet(self):
        self.opened_dialog += 1

    def __getattr__(self, name):
        def _noop(*a, **k):
            return None
        return _noop


def make_sheet(title="测试码表", track_title="夏夜星律", bpm=BPM, n_items=3,
               media_name="a.mp4", media_dur=30.0):
    """造一张迷你码表：1 首曲目 + 2 层 + n_items 个片段 + 1 个素材。"""
    s = cuesheet.CueSheet(title=title, res_w=W, res_h=H)
    te = cuesheet.TrackEntry()
    te.identity = cuesheet.Identity(title=track_title, artist="测试者",
                                    duration_sec=180.0, bpm=bpm)
    te.identity.ensure_meta_id()
    for li, per in enumerate((n_items - 1, 1)):
        L = cuesheet.CompLayer(lyr_id="L%d" % li, name="层%d" % (li + 1), z=li)
        for k in range(max(per, 0)):
            L.items.append(cuesheet.CueItem(
                item_id="i%d_%d" % (li, k), clip_id="c1",
                at_beats=float(k * 4), src_in_sec=0.0, src_out_sec=8.0,
                trans_in="cut", trans_out="cut").normalized())
        te.layers.append(L)
    s.tracks.append(te)
    s.media.append(cuesheet.MediaRef(clip_id="c1", name=media_name,
                                     duration_sec=media_dur, has_alpha=False))
    return s


class FakeBox:
    """替换 QMessageBox：不弹真窗口，`exec()` 直接返回「Yes」。"""

    Yes = 1
    No = 0
    Question = 0
    Warning = 0
    Information = 0

    def __init__(self, *a, **k):
        self.lines = []

    def setWindowTitle(self, *a):
        pass

    def setIcon(self, *a):
        pass

    def setText(self, *a):
        pass

    def setInformativeText(self, *a):
        pass

    def setStandardButtons(self, *a):
        pass

    def setDefaultButton(self, *a):
        pass

    def button(self, *a):
        return self

    def exec(self):
        return FakeBox.Yes

    @staticmethod
    def information(*a, **k):
        return None

    @staticmethod
    def warning(*a, **k):
        return None


def shot(widget, name, w=980, h=460):
    os.makedirs(SHOT_DIR, exist_ok=True)
    widget.resize(w, h)
    widget.show()
    for _ in range(8):
        QApplication.instance().processEvents()
    p = os.path.join(SHOT_DIR, name)
    ok = widget.grab().save(p, "PNG")
    return ok, p


# ==========================================================================
def main():
    fails = []

    def check(name, cond, detail=""):
        print("  [%s] %s%s" % ("ok  " if cond else "FAIL", name,
                               "" if cond else "   ← " + str(detail)))
        if not cond:
            fails.append(name)

    os.environ.pop("QT_QPA_PLATFORM", None)      # 真平台（offscreen 没 CJK 字体）
    app = QApplication.instance() or QApplication(sys.argv)
    tmp = tempfile.mkdtemp(prefix="cuepage_")

    # 造两个码表包（+ 素材目录，避免"缺素材"弹目录选择）
    media_root = os.path.join(tmp, "media")
    os.makedirs(media_root, exist_ok=True)
    open(os.path.join(media_root, "a.mp4"), "wb").close()
    open(os.path.join(media_root, "b.mp4"), "wb").close()

    pa = os.path.join(tmp, "测试码表.ercue")
    pb = os.path.join(tmp, "第二张.ercue")
    cuesheet.save(make_sheet("测试码表", "夏夜星律", n_items=3, media_name="a.mp4"),
                  pa, app_version="2026.10.17.12")
    cuesheet.save(make_sheet("第二张", "另一首", n_items=2, media_name="b.mp4"),
                  pb, app_version="2026.10.17.12")

    cfg = FakeCfg()
    cue_ui.cue_cfg(cfg)["media_dir"] = media_root
    lay_cue = engine.Layer("码表图层")
    eng = new_engine([lay_cue])
    # 让码表运行时能建起来（不真的解码）
    engine.make_player = lambda item, cue=False: FakePlayer(None)
    owner = Owner(eng, cfg)

    # ================= ① 第 4 页签 + 列表 =================
    print("① 第 4 页签「码表」：列表 + 按钮区")
    check("页签索引常量 = 3（第 4 个）", cue_page.CUE_PAGE_INDEX == 3,
          cue_page.CUE_PAGE_INDEX)
    src_ui = open(os.path.join(SRC, "ui_main.py"), encoding="utf-8").read()
    check("ui_main 的页签条里有「🎞 码表」", "🎞 码表" in src_ui)
    check("switch_page 钳位到 3", "max(0, min(3, int(i)))" in src_ui)
    check("页签数量钳位 0..3", "idx if 0 <= idx <= 3 else 0" in src_ui)

    panel = cue_page.CueSheetPanel(owner)
    owner.layers_panel = None

    # 用真实 on_add（把文件选择框替换成"用户选了这两张"）
    real_open = cue_page.QFileDialog.getOpenFileNames
    cue_page.QFileDialog.getOpenFileNames = staticmethod(
        lambda *a, **k: ([pa, pb], ""))
    # ⚠ 全程替换 QMessageBox：`exec()` 直返 Yes、`warning/information` 不弹窗（否则会卡住）
    real_box = cue_page.QMessageBox
    real_box_panels = panels.QMessageBox
    cue_page.QMessageBox = FakeBox
    panels.QMessageBox = FakeBox
    try:
        panel.on_add()
    finally:
        cue_page.QFileDialog.getOpenFileNames = real_open
    panel.ensure_frag_counts()
    for _ in range(6):
        app.processEvents()

    check("列表有 2 行", panel.tbl.rowCount() == 2, panel.tbl.rowCount())
    rows = {}
    for r in range(panel.tbl.rowCount()):
        rows[panel.tbl.item(r, 0).text()] = [
            panel.tbl.item(r, c).text() for c in range(panel.tbl.columnCount())]
    print("      %s" % " ｜ ".join("%s" % v for v in rows.values()))
    check("表名正确（测试码表 / 第二张）",
          "测试码表" in rows and "第二张" in rows, list(rows))
    ok_row = rows.get("测试码表") or ["", "", "", "", "", ""]
    check("覆盖曲目 = 1 首", ok_row[1] == "1 首", ok_row)
    check("片段数 = 3（层1 2 个 + 层2 1 个）", ok_row[2] == "3", ok_row)
    check("大小非空且带单位", ok_row[3].endswith(("B", "KB", "MB", "GB")), ok_row[3])
    check("修改时间形如 YYYY-MM-DD HH:MM", len(ok_row[4]) == 16 and ok_row[4][4] == "-",
          ok_row[4])
    ok_s, path_s = shot(panel, "page4_cuesheet_list.png")
    check("截图已落盘 page4_cuesheet_list.png", ok_s, path_s)

    # ================= ② 详情（双击） =================
    print("\n② 双击 ⇒ 只读详情（曲目 + BPM + 素材清单）")
    e = panel._entry_for(pa)
    dlg = cue_page.CueSheetDetailDialog(owner, e, None)
    check("详情读到曲目 1 行", dlg.tbl_tracks.rowCount() == 1, dlg.tbl_tracks.rowCount())
    check("详情读到素材 1 行", dlg.tbl_media.rowCount() == 1, dlg.tbl_media.rowCount())
    check("曲目名 = 夏夜星律", dlg.tbl_tracks.item(0, 0).text() == "夏夜星律",
          dlg.tbl_tracks.item(0, 0).text())
    check("BPM = 120", dlg.tbl_tracks.item(0, 2).text() == "120",
          dlg.tbl_tracks.item(0, 2).text())
    check("每层片段数含 层1:2 / 层2:1",
          "层1:2" in dlg.tbl_tracks.item(0, 5).text()
          and "层2:1" in dlg.tbl_tracks.item(0, 5).text(),
          dlg.tbl_tracks.item(0, 5).text())
    check("表格只读（不可编辑）",
          dlg.tbl_tracks.editTriggers() == QAbstractItemView.NoEditTriggers)
    check("素材清单里有 a.mp4", dlg.tbl_media.item(0, 0).text() == "a.mp4",
          dlg.tbl_media.item(0, 0).text())
    ok_s, path_s = shot(dlg, "page4_detail.png", 760, 600)
    check("截图已落盘 page4_detail.png", ok_s, path_s)
    dlg.close()

    # ================= ⑤ 新建「码表图层」 =================
    print("\n⑤ 新建图层里可选「码表图层」（NewLayerDialog）")
    nod = panels.NewLayerDialog(None, "图层 1")
    check("有 rb_cue 单选", hasattr(nod, "rb_cue"))
    nod.rb_cue.setChecked(True)
    nod.name_edit.setText("")                       # 走"默认名"分支
    nod._on_type()
    data = nod.result_data()
    check("result_data 是 5 元组", len(data) == 5, data)
    check("is_cue = True", data[4] is True, data)
    check("默认名 = 码表图层", nod.name_edit.text() == "码表图层", nod.name_edit.text())
    nod.close()

    # 与 ui_main.layer_add 相同的步骤：选表 → 建层 → 装表
    pick = cue_page.pick_sheet
    cue_page.pick_sheet = staticmethod(lambda *a, **k: panel._entry_for(pa))
    try:
        entry = cue_page.pick_sheet(owner, None, "选表")
    finally:
        cue_page.pick_sheet = pick
    check("pick_sheet 返回选中的码表", entry is not None and entry.get("path") == pa,
          entry.get("path") if entry else None)

    lay2 = engine.Layer("码表图层")
    lay2.is_cuesheet = True
    lay2.cuesheet_id = os.path.basename(pa)
    eng.layers = [lay2, engine.Layer("普通层")]
    ok, msg = cue_ui.load_and_bind(owner, pa, lay2, parent=None)
    print("      load_and_bind ok=%s msg=%r" % (ok, msg))
    check("装表成功（引擎有运行时）", ok is True, msg)
    check("图层被标成码表层", lay2.is_cuesheet is True)
    check("cuesheet_id = 包名", lay2.cuesheet_id == "测试码表.ercue", lay2.cuesheet_id)

    # 图层头：码表层显示带表名的青绿下拉；普通层没有（隐藏、不占位）
    class PanelOwner:
        def __init__(self, eng2, cfg2):
            self.engine = eng2
            self.cfg = cfg2
            self.saved = 0

        def save_layers(self):
            self.saved += 1

        def __getattr__(self, name):
            def _noop(*a, **k):
                return None
            return _noop

    po = PanelOwner(eng, cfg)
    lsp = panels.LayerStackPanel(po)
    lsp.rebuild()
    hdr_cue, hdr_plain = lsp.rows[0][0], lsp.rows[1][0]
    check("码表层有码表下拉 _cmb_cue", hasattr(hdr_cue, "_cmb_cue"))
    check("码表层的下拉**可见**", not hdr_cue._cmb_cue.isHidden())
    check("下拉显示表名「测试码表」", hdr_cue._cmb_cue.currentText() == "测试码表",
          hdr_cue._cmb_cue.currentText())
    check("下拉宽度紧凑（≤92px）", hdr_cue._cmb_cue.width() <= 92,
          hdr_cue._cmb_cue.width())
    check("普通层没有可见的码表下拉", hdr_plain._cmb_cue.isHidden(),
          hdr_plain._cmb_cue.isHidden())
    items = [hdr_cue._cmb_cue.itemText(i) for i in range(hdr_cue._cmb_cue.count())]
    print("      下拉项 = %s" % items)
    check("下拉含「（无码表）」", "（无码表）" in items, items)
    check("下拉含两张码表", "测试码表" in items and "第二张" in items, items)
    check("下拉末尾是「导入新码表…」", items[-1] == "导入新码表…", items)
    ok_s, path_s = shot(lsp, "page4_layer_combo.png", 940, 240)
    check("截图已落盘 page4_layer_combo.png", ok_s, path_s)

    # ================= ⑨ 下拉切换（换表 / 卸下） =================
    print("\n⑨ 下拉切换：换表 ⇒ 引擎的表变；选「（无码表）」⇒ 卸下")
    cmb = hdr_cue._cmb_cue
    i_b = [k for k in range(cmb.count()) if cmb.itemText(k) == "第二张"]
    check("下拉里有「第二张」", bool(i_b))
    if i_b:
        cmb.setCurrentIndex(i_b[0])
        lsp._on_cue_combo(0)                     # 0 = 第一行（码表层）
        st = eng.cuesheet_status()
        check("换表后引擎里的表 = 第二张", (st.get("title") or "") == "第二张",
              st.get("title"))
        check("换表后 cuesheet_id 跟着变",
              lay2.cuesheet_id == "第二张.ercue", lay2.cuesheet_id)
        check("换表后下拉显示「第二张」", cmb.currentText() == "第二张",
              cmb.currentText())
    # 卸下
    cmb.setCurrentIndex(0)
    lsp._on_cue_combo(0)
    check("卸下后图层不再是码表层", lay2.is_cuesheet is False, lay2.is_cuesheet)
    check("卸下后引擎的表被清空", eng._cue_rt is None, eng._cue_rt)
    check("卸下后下拉变回「（无码表）」", cmb.currentText() == "（无码表）",
          cmb.currentText())
    check("卸下后下拉隐藏（普通图层布局不变）", cmb.isHidden(), cmb.isHidden())

    # 再绑回来，给"已绑图层"列截图
    lay2.is_cuesheet = True
    lay2.cuesheet_id = os.path.basename(pa)
    cue_ui.load_and_bind(owner, pa, lay2, parent=None)
    lsp.refresh_header()
    panel.refresh()
    ok_s, path_s = shot(panel, "page4_cuesheet_list_bound.png")
    check("截图已落盘 page4_cuesheet_list_bound.png", ok_s, path_s)

    # ================= ③ 删除（只从列表移除，不删文件） =================
    print("\n③ 删除：从列表移除 ⇒ 行消失、磁盘文件仍在")
    panel.tbl.selectRow(1)                       # 选「第二张」
    before = os.path.isfile(pb)
    panel.on_remove()
    names = [panel.tbl.item(r, 0).text() for r in range(panel.tbl.rowCount())]
    check("列表只剩 1 行", panel.tbl.rowCount() == 1, panel.tbl.rowCount())
    check("「第二张」已从列表移除", "第二张" not in names, names)
    check("磁盘上的 .ercue 仍在（未被删除）", before and os.path.isfile(pb), pb)
    panel.refresh()

    # ================= ⑩ 「⋯ 更多」里没有码表项 =================
    print("\n⑩ 「⋯ 更多」菜单里不再有码表项")
    check("菜单里不再 addAction 码表项",
          'addAction("码表（演出端）…")' not in src_ui)
    check("源码里没有 act_cuesheet", "act_cuesheet" not in src_ui)
    check("open_cuesheet（对话框）仍保留（手动起拍在其中）",
          "def open_cuesheet(self)" in src_ui)
    check("码表页有通往对话框的「更多操作…」", hasattr(panel, "on_open_dialog"))
    panel.on_open_dialog()
    check("「更多操作…」确实打开了对话框入口", owner.opened_dialog >= 1,
          owner.opened_dialog)

    print("")
    cue_page.QMessageBox = real_box
    panels.QMessageBox = real_box_panels
    if fails:
        print("CUE_PAGE_FAIL  %d 项不通过：%s" % (len(fails), "、".join(fails)))
        return 1
    print("CUE_PAGE_OK  第4页签列表/添加/删除/详情/码表图层/下拉换表卸下/更多菜单 全过")
    print("截图目录：%s" % SHOT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
