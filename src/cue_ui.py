# -*- coding: utf-8 -*-
"""码表（演出端）UI —— **轻量对话框**：导入 / 最近 / 状态 / 绑图层 / 启用 / 卸下 / 手动起拍。

契约依据：`docs/码表实装_设计.md` §3.1 / §3.4；运行时约定见 `docs/码表实装_Step4_任务书草案.md`。

★★ 三个设计决定（都写在注释里，别改回去）
1. **轻量对话框，不是第 3 页签**：编排在 EasyRealityCue，演出端只负责执行。
   把"编排"形态搬回主程序，历史上出现过"码表把主程序拖垮"。
2. **手动绑歌（过渡方案）**：Step3（指纹认歌）落地前，导入后若认不出歌 ⇒
   让用户从**这张码表的曲目列表**里选一首 ⇒ **立刻可播**。
   Step3 接上后这条路径自然退化为"识别失败时的兜底"，不冲突。
3. **手动起拍（临时位置源）**：本版还没有位置源。给一个挂钟计时按钮：
   按下即以"这首歌从 0:00 开始"起算，把 `pos_beats` 喂给 `engine.set_cue_position()`。
   用于**现在就能验**"导入 → 绑曲 → 播放 → 画面跟不跟拍"；Step3 落地后本按钮会停用。

错误**绝不静默**：包损坏 / 版本不符 / 缺素材 / 认不出歌，都在对话框里直接显示（设计 §3.4）。
"""
from __future__ import annotations

import os
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QHBoxLayout, QLabel, QMessageBox,
                               QPushButton, QVBoxLayout, QWidget)

import cuesheet

CUE_CFG_DEFAULTS = {
    "recent": [],            # 最近打开的包（最多 10 条，最新在前）
    "media_dir": "",         # 上次指定的素材目录（包内无内嵌 media 时用）
    "seek_profile": "auto",  # auto / conservative / off（Step7 会做设置界面，这里先只读）
}
RECENT_MAX = 10


# ==========================================================================
# 配置辅助
# ==========================================================================
def cue_cfg(cfg):
    """取（并在缺失时初始化）`cfg["cue"]` 段。

    ★ 实现委托 `cue_index.cue_cfg()`：`recent/media_dir/seek_profile/sheets` 的默认值
      **只有一处**，避免两个模块各写一份 defaults 然后慢慢分叉。
    """
    import cue_index
    return cue_index.cue_cfg(cfg)


def add_recent(cfg, path):
    """把 `path` 放到最近文件首位（去重、上限 10、落盘）。返回新的列表。"""
    c = cue_cfg(cfg)
    p = os.path.abspath(str(path or ""))
    lst = [x for x in (c.get("recent") or []) if os.path.abspath(str(x)) != p]
    lst.insert(0, p)
    c["recent"] = lst[:RECENT_MAX]
    try:
        cfg.save()
    except Exception:                                            # noqa: BLE001
        pass
    return c["recent"]


def scan_media_dir(root, names):
    """在 `root` 下递归找 `names` 里的文件（basename 匹配）。返回 `{name: path}`。"""
    want = {str(n).lower(): str(n) for n in (names or []) if n}
    out = {}
    if not root or not os.path.isdir(root) or not want:
        return out
    try:
        for dp, _dn, fn in os.walk(root):
            for f in fn:
                key = f.lower()
                if key in want and want[key] not in out:
                    out[want[key]] = os.path.join(dp, f)
            if len(out) == len(want):
                break
    except Exception:                                            # noqa: BLE001
        pass
    return out


# ==========================================================================
# 手动绑歌：从**这张码表的曲目列表**里挑一首
# ==========================================================================
def pick_track(sheet, parent=None, current_title=""):
    """让用户从 `sheet.tracks` 里选一首；取消返回 None。

    ⚠ 为什么不是"从用户曲库挑"：`engine.set_cue_sheet(track=...)` 要的是**码表里的 TrackEntry**
      （它带 `identity.bpm`，决定拍长）。用户曲库里的歌只能告诉我们"现在放的是哪首"，
      那属于 Step3 的认歌，不是这里。
    """
    tracks = list(getattr(sheet, "tracks", None) or [])
    if not tracks:
        QMessageBox.warning(parent, "没有曲目",
                            "这张码表里没有任何曲目（tracks 为空），无法绑定。")
        return None
    dlg = QDialog(parent)
    dlg.setWindowTitle("选择要绑定的曲目")
    lay = QVBoxLayout(dlg)
    lay.addWidget(QLabel("这张码表包含以下曲目。选一首绑定到当前演出（Step3 接上后会自动认歌）："))
    box = QComboBox(dlg)
    for t in tracks:
        idn = getattr(t, "identity", None)
        title = str(getattr(idn, "title", "") or "(无标题)")
        artist = str(getattr(idn, "artist", "") or "")
        bpm = float(getattr(idn, "bpm", 0.0) or 0.0)
        n_items = sum(len(getattr(ly, "items", None) or []) for ly in getattr(t, "layers", []))
        box.addItem("%s — %s ｜ %s ｜ %d 片段"
                    % (artist or "(未知艺术家)", title,
                       ("%g BPM" % bpm) if bpm > 0 else "无 BPM（退化 1 拍=1 秒）", n_items))
    if current_title:
        for i in range(box.count()):
            if str(tracks[i].identity.title) == current_title:
                box.setCurrentIndex(i)
                break
    lay.addWidget(box)
    bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, dlg)
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    lay.addWidget(bb)
    if dlg.exec() != QDialog.Accepted:
        return None
    idx = box.currentIndex()
    return tracks[idx] if 0 <= idx < len(tracks) else None


# ==========================================================================
# 主对话框
# ==========================================================================
class CueSheetDialog(QDialog):
    """码表（演出端）—— 导入 / 最近 / 状态 / 绑图层 / 启用 / 卸下 / 手动起拍。

    `owner` 需要提供 `.cfg`（Config）与 `.engine`（AutoVJEngine）。
    """

    def __init__(self, owner, parent=None):
        # `owner` 一般是主窗（QWidget）；允许传非 QWidget 的"宿主对象"（便于无头测试）
        par = parent or (owner if isinstance(owner, QWidget) else None)
        super().__init__(par)
        self.owner = owner
        self.cfg = getattr(owner, "cfg", None)
        self.engine = getattr(owner, "engine", None)
        self.pkg = ""              # 当前装载的包路径
        self.sheet = None          # 当前 CueSheet
        self.track = None          # 当前绑定的 TrackEntry
        self.media_paths = {}      # clip_id → 路径
        self.warnings = []
        self.missing = []
        self._t0 = None            # 手动起拍的起点（monotonic）
        self._timer = QTimer(self)
        self._timer.setInterval(30)                # 33Hz 足够（引擎 60fps 会读最新值）
        self._timer.timeout.connect(self._tick_clock)
        self._build_ui()
        self._load_recent()
        self.refresh()

    # ---------------- UI ----------------
    def _build_ui(self):
        self.setWindowTitle("码表（演出端）")
        self.setMinimumWidth(620)
        root = QVBoxLayout(self)

        row1 = QHBoxLayout()
        self.btn_import = QPushButton("导入 .ercue…", self)
        self.btn_import.clicked.connect(self.on_import_clicked)
        row1.addWidget(self.btn_import)
        self.btn_media = QPushButton("指定素材目录…", self)
        self.btn_media.clicked.connect(self.on_pick_media_dir)
        row1.addWidget(self.btn_media)
        row1.addStretch(1)
        root.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("最近："))
        self.cmb_recent = QComboBox(self)
        self.cmb_recent.setMinimumWidth(420)
        row2.addWidget(self.cmb_recent, 1)
        self.btn_open_recent = QPushButton("打开", self)
        self.btn_open_recent.clicked.connect(self.on_open_recent)
        row2.addWidget(self.btn_open_recent)
        root.addLayout(row2)

        row3 = QHBoxLayout()
        row3.addWidget(QLabel("绑定到："))
        self.cmb_layer = QComboBox(self)
        self.cmb_layer.setMinimumWidth(240)
        row3.addWidget(self.cmb_layer, 1)
        self.chk_enable = QCheckBox("启用（接管画面）", self)
        self.chk_enable.setChecked(True)
        self.chk_enable.toggled.connect(self.on_toggle_enable)
        row3.addWidget(self.chk_enable)
        self.btn_uninstall = QPushButton("卸下", self)
        self.btn_uninstall.clicked.connect(self.on_uninstall)
        row3.addWidget(self.btn_uninstall)
        root.addLayout(row3)

        row4 = QHBoxLayout()
        self.btn_clock = QPushButton("手动起拍（临时位置源）", self)
        self.btn_clock.setCheckable(True)
        self.btn_clock.setToolTip("Step3（指纹认歌）落地前用它验跟拍：按下即认为"
                                  "「这首歌从 0:00 开始」，按挂钟喂拍位。")
        self.btn_clock.toggled.connect(self.on_toggle_clock)
        row4.addWidget(self.btn_clock)
        self.lbl_clock = QLabel("—", self)
        row4.addWidget(self.lbl_clock, 1)
        root.addLayout(row4)

        self.lbl_status = QLabel("（未装载码表）", self)
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.lbl_status)

        self.lbl_error = QLabel("", self)
        self.lbl_error.setWordWrap(True)
        self.lbl_error.setStyleSheet("color:#c62828;")
        root.addWidget(self.lbl_error)

        bb = QDialogButtonBox(QDialogButtonBox.Close, self)
        bb.rejected.connect(self.reject)
        bb.clicked.connect(lambda _b: self.close())
        root.addWidget(bb)

    # ---------------- 状态显示 ----------------
    def refresh(self):
        self._fill_layers()
        st = {}
        try:
            st = self.engine.cuesheet_status() if self.engine is not None else {}
        except Exception:                                        # noqa: BLE001
            st = {}
        if not st.get("loaded"):
            self.lbl_status.setText("（未装载码表）")
        else:
            idn = getattr(self.track, "identity", None) if self.track is not None else None
            bpm = float(getattr(idn, "bpm", 0.0) or 0.0) if idn is not None else 0.0
            self.lbl_status.setText(
                "码表：%s ｜ 画布 %dx%d ｜ 曲目：%s ｜ BPM：%s ｜ 片段：%d ｜ 已绑图层：%s"
                % (st.get("title") or "(无标题)", int(st.get("res", (1920, 1080))[0]),
                   int(st.get("res", (1920, 1080))[1]),
                   ("%s — %s" % (getattr(idn, "artist", ""), getattr(idn, "title", "")))
                   if idn is not None else "**未绑定**（不接管）",
                   ("%g" % bpm) if bpm > 0 else "无（退化 1 拍=1 秒）",
                   int(st.get("rt_items", 0) or 0),
                   "、".join(st.get("bound_layers") or []) or "无")
                + ("　｜　覆盖 %d 首" % self._cover_count() if self._cover_count() else ""))
        err = []
        if self.warnings:
            err.extend(self.warnings)
        if self.missing:
            err.append("缺 %d 个素材：%s%s"
                       % (len(self.missing), "、".join(self.missing[:6]),
                          "…" if len(self.missing) > 6 else ""))
        self.lbl_error.setText(("\n".join(err)) if err else "")
        # 时钟读数
        if self._t0 is not None and self.track is not None:
            bl = self._beat_len()
            pos = (time.monotonic() - self._t0) / bl
            self.lbl_clock.setText("拍位 %.2f（%.2fs @ %s BPM）"
                                   % (pos, pos * bl,
                                      ("%g" % (60.0 / bl)) if bl > 0 else "退化"))

    def _cover_count(self):
        """当前包在索引里覆盖多少首曲目（多曲目码表一眼可见）。"""
        try:
            idx = self.engine.cue_index() if callable(getattr(self.engine,
                                                              "cue_index", None)) else None
            if idx is None:
                return 0
            p = os.path.abspath(self.pkg) if self.pkg else ""
            for e in idx.entries:
                if e.get("path") == p:
                    return int(e.get("n_tracks", 0) or 0)
        except Exception:                                        # noqa: BLE001
            pass
        return 0

    def _fill_layers(self):
        cur = self.cmb_layer.currentIndex()
        self.cmb_layer.clear()
        lays = list(getattr(self.engine, "layers", None) or []) if self.engine else []
        for i, ly in enumerate(lays):
            mark = "（码表层）" if getattr(ly, "is_cuesheet", False) else ""
            self.cmb_layer.addItem("%d. %s%s" % (i + 1, getattr(ly, "name", "?"), mark))
        if lays:
            # 优先选已标记的码表层，否则选最上层（index 0）
            prefer = next((i for i, ly in enumerate(lays)
                           if getattr(ly, "is_cuesheet", False)), 0)
            self.cmb_layer.setCurrentIndex(prefer)
        elif cur >= 0:
            self.cmb_layer.setCurrentIndex(0)

    def _load_recent(self):
        self.cmb_recent.clear()
        c = cue_cfg(self.cfg) if self.cfg is not None else {"recent": []}
        for p in (c.get("recent") or []):
            self.cmb_recent.addItem(os.path.basename(p), p)

    def _current_layer(self):
        lays = list(getattr(self.engine, "layers", None) or []) if self.engine else []
        i = self.cmb_layer.currentIndex()
        return lays[i] if 0 <= i < len(lays) else None

    # ---------------- 导入 ----------------
    def on_import_clicked(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "导入码表", "",
                                                "码表包 (*.ercue *.json);;所有文件 (*)")
        if paths:
            self.import_paths(paths)

    def on_open_recent(self):
        p = self.cmb_recent.currentData()
        if p:
            self.import_paths([p])

    def import_paths(self, paths):
        """多选导入：逐个装；**任何一个失败都不影响其它**，错误直接显示在对话框里。"""
        for p in (paths or []):
            self.import_one(p)

    def import_one(self, path):
        """导入一个包；返回 True = 成功装表。所有错误都在 `lbl_error` 里可见。"""
        self.warnings, self.missing = [], []
        try:
            sheet, info = cuesheet.load(path)
        except Exception as e:                                   # noqa: BLE001
            self._error("导入失败：%s\n%s" % (os.path.basename(str(path)), e))
            return False
        self.pkg = str(path)
        self.sheet = sheet
        self.warnings = list(info.get("warnings") or [])
        # ★ `cuesheet.load()` 不认识"本程序版本"（它不 import 版本信息），所以
        #   「这张码表要求 X 以上版本」那条告警必须**由 UI 侧补**一次 —— 否则用户永远看不到。
        try:
            from version import APP_VERSION
            _w, _ = cuesheet.capability_report(
                sheet, app_version=str(APP_VERSION),
                missing_media=info.get("missing_media") or [])
            for x in _w:
                if x not in self.warnings:
                    self.warnings.append(x)
        except Exception:                                        # noqa: BLE001
            pass
        self.media_paths = dict(info.get("present_media") or {})
        self.missing = list(info.get("missing_media") or [])

        # 缺素材：先按上次目录找；找不到再让用户指定
        if self.missing:
            c = cue_cfg(self.cfg) if self.cfg is not None else {}
            found = scan_media_dir(c.get("media_dir") or "", self.missing)
            if found:
                by_id = {}
                for m in sheet.media:
                    if m.name in found:
                        by_id[m.clip_id] = found[m.name]
                self.media_paths.update(by_id)
                self.missing = [n for n in self.missing if n not in found]
            if self.missing:
                d = QFileDialog.getExistingDirectory(
                    self, "有 %d 个素材没找到，请指定素材目录（可取消）"
                    % len(self.missing), "")
                if d:
                    if self.cfg is not None:
                        cue_cfg(self.cfg)["media_dir"] = d
                        try:
                            self.cfg.save()
                        except Exception:                        # noqa: BLE001
                            pass
                    found = scan_media_dir(d, self.missing)
                    by_id = {}
                    for m in sheet.media:
                        if m.name in found:
                            by_id[m.clip_id] = found[m.name]
                    self.media_paths.update(by_id)
                    self.missing = [n for n in self.missing if n not in found]

        # 认歌：没有位置源/曲目信息时**必然**认不出 ⇒ 直接走"手动绑歌"
        track = self._auto_match(sheet)
        if track is None:
            track = self._ask_track(sheet)
        self.track = track
        layer = self._current_layer()
        ok = False
        try:
            ok = bool(self.engine.set_cue_sheet(
                sheet, media_paths=self.media_paths, track=track, layer=layer,
                sheet_id=os.path.basename(self.pkg)))
        except Exception as e:                                   # noqa: BLE001
            self._error("装载失败（引擎）：%s" % e)
            return False
        if self.cfg is not None:
            add_recent(self.cfg, self.pkg)
            self._load_recent()
        # ★ 建索引（只存摘要）：让引擎能回答"这首歌有哪些码表可用"（Step2/Step3 用）
        try:
            idx = self.engine.cue_index() if callable(getattr(self.engine,
                                                              "cue_index", None)) else None
            if idx is not None:
                idx.add(self.pkg)
                idx.save_cfg(self.cfg)
        except Exception:                                        # noqa: BLE001
            pass
        if self.chk_enable.isChecked():
            self._enable(True)
        self.refresh()
        self._notify_owner()
        if not ok:
            self._error("已装载码表，但**没有绑定曲目** ⇒ 图层不接管（请选一首曲目）。")
        return ok

    def _auto_match(self, sheet):
        """先用"当前曲目信息"认歌。Step3 未落地 ⇒ 这里只能拿到空信息。

        ★ 单曲目码表：直接用它（否则每次导入都要弹窗，体验很差）；
          多曲目码表：交给用户选（弹一次）。
        """
        try:
            tr, level = sheet.match_track()          # 无信息 ⇒ (None, "")
            if tr is not None:
                self.warnings.append("已按 %s 命中曲目。" % (level or "?"))
                return tr
        except Exception:                                        # noqa: BLE001
            pass
        tracks = list(getattr(sheet, "tracks", None) or [])
        if len(tracks) == 1:
            self.warnings.append("未认歌（Step3 未接）：已按表中唯一曲目绑定 —— %s"
                                 % (getattr(tracks[0].identity, "title", "") or "(无标题)"))
            return tracks[0]
        return None

    def _ask_track(self, sheet):
        tr = pick_track(sheet, self)
        if tr is None:
            self.warnings.append("未选择曲目 ⇒ 已装表但**不接管**（画面仍走普通自动调度）。")
        return tr

    # ---------------- 绑定 / 启停 / 卸下 ----------------
    def on_toggle_enable(self, on):
        self._enable(bool(on))

    def _enable(self, on):
        lay = self._current_layer()
        if lay is None or self.engine is None:
            return
        try:
            if on:
                lay.is_cuesheet = True
                self.engine.set_cue_sheet(
                    self.sheet, media_paths=self.media_paths, track=self.track,
                    layer=lay, sheet_id=os.path.basename(self.pkg))
            else:
                lay.is_cuesheet = False
                lay.cs_states = []
                lay.cs_active = False
                self.engine._cue_set_active(lay, set())
        except Exception as e:                                   # noqa: BLE001
            self._error("切换启用失败：%s" % e)
        self.refresh()
        self._notify_owner()

    def on_uninstall(self):
        self.stop_clock()
        try:
            self.engine.clear_cue_sheet()
        except Exception as e:                                   # noqa: BLE001
            self._error("卸下失败：%s" % e)
        self.warnings, self.missing = [], []
        self.pkg, self.sheet, self.track, self.media_paths = "", None, None, {}
        self.lbl_clock.setText("—")
        self.refresh()
        self._notify_owner()

    # ---------------- 手动起拍（临时位置源） ----------------
    def _beat_len(self):
        bpm = 0.0
        if self.track is not None:
            bpm = float(getattr(self.track.identity, "bpm", 0.0) or 0.0)
        return (60.0 / bpm) if bpm > 0 else 1.0     # 退化 1 拍 = 1 秒（与 Cue 端同口径）

    def on_toggle_clock(self, on):
        if on:
            self.start_clock()
        else:
            self.stop_clock()

    def start_clock(self):
        if self.track is None:
            self.btn_clock.setChecked(False)
            self._error("先绑定曲目再起拍（没有曲目就不知道拍长）。")
            return
        # ★ 告诉引擎"现在用手动起拍"：自动位置（指纹）让位 —— 否则 0.2s 后会被自动值覆盖
        try:
            fn = getattr(self.engine, "set_cue_manual_clock", None)
            if callable(fn):
                fn(True)
        except Exception:                                        # noqa: BLE001
            pass
        self._t0 = time.monotonic()
        self._timer.start()
        if not self.btn_clock.isChecked():
            self.btn_clock.setChecked(True)

    def stop_clock(self):
        self._t0 = None
        self._timer.stop()
        try:
            fn = getattr(self.engine, "set_cue_manual_clock", None)
            if callable(fn):
                fn(False)          # 交还给自动跟拍（若指纹可用）
        except Exception:                                        # noqa: BLE001
            pass
        if self.btn_clock.isChecked():
            self.btn_clock.setChecked(False)

    def _tick_clock(self):
        if self._t0 is None or self.engine is None:
            return
        bl = self._beat_len()
        pos = (time.monotonic() - self._t0) / bl
        try:
            self.engine.set_cue_position(pos, "wallclock")
        except Exception:                                        # noqa: BLE001
            self.stop_clock()
            return
        self.lbl_clock.setText("拍位 %.2f（%.2fs @ %s）"
                              % (pos, pos * bl,
                                 ("%g BPM" % (60.0 / bl)) if bl > 0 else "退化"))

    # ---------------- 杂项 ----------------
    def on_pick_media_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择素材目录", "")
        if not d:
            return
        if self.cfg is not None:
            cue_cfg(self.cfg)["media_dir"] = d
            try:
                self.cfg.save()
            except Exception:                                    # noqa: BLE001
                pass
        if self.sheet is None:
            return
        names = [m.name for m in self.sheet.media]
        found = scan_media_dir(d, names)
        by_id = {}
        for m in self.sheet.media:
            if m.name in found:
                by_id[m.clip_id] = found[m.name]
        self.media_paths.update(by_id)
        self.missing = [n for n in self.missing if n not in found]
        try:
            self.engine.set_cue_sheet(self.sheet, media_paths=self.media_paths,
                                      track=self.track, layer=self._current_layer(),
                                      sheet_id=os.path.basename(self.pkg))
        except Exception:                                        # noqa: BLE001
            pass
        self.refresh()

    def _notify_owner(self):
        """通知主窗刷新图层面板 —— 徽标 与 对话框里的"绑定到"必须**双向一致**。

        （徽标在 `panels.LayerStackPanel._build_header` 里；这里只调它的公开刷新入口，
          不直接改控件，避免 UI 层互相依赖。）
        """
        panel = getattr(self.owner, "layers_panel", None)
        fn = getattr(panel, "refresh_header", None)
        if callable(fn):
            try:
                fn()
            except Exception:                                    # noqa: BLE001
                pass
        fn2 = getattr(self.owner, "refresh_playing", None)
        if callable(fn2):
            try:
                fn2()
            except Exception:                                    # noqa: BLE001
                pass

    def _error(self, msg):
        """**绝不静默**：错误直接写进对话框（设计 §3.4）。"""
        self.lbl_error.setText(str(msg))
        try:
            log = getattr(self.engine, "_health_log", None)
            if log is not None:
                log.write("[码表] %s" % msg)
        except Exception:                                        # noqa: BLE001
            pass

    def closeEvent(self, ev):
        self.stop_clock()
        super().closeEvent(ev)


def open_cuesheet_dialog(owner):
    """打开码表对话框（ui_main 的「⋯ 更多」菜单调用它）。"""
    dlg = CueSheetDialog(owner)
    dlg.exec()
    return dlg
