# -*- coding: utf-8 -*-
"""UI 第二步：音乐曲库从**弹窗**改成**整页**，左侧加排序 / 筛选栏。

用户要求（2026-10-01）：
  「这里的曲库管理改成全屏的，然后在左边增加一个排序和筛选的选项。」
  先做的两组筛选：**BPM 区间** + **时长区间 / 只看文件缺失**（用户勾选）。

实现要点：
  · 新增 `MusicLibraryPanel(QWidget)`（可嵌入的主页面板），成员名与旧的
    `MusicLibraryDialog` **刻意对齐**（`refresh()` / `progress` / `lbl` / `btn_scan` / `list`）
    ⇒ `ui_main` 里那 5 处「扫描进度 / 刷新」代码**一行都不用改**（它们都是
    `getattr(self, "music_dialog")` 再调这几个成员）；主窗口把 `music_dialog` 指向这个面板即可。
  · BPM 与**时长**都取自 `cfg["music_meta"][路径]["grid"]`（`bpm` / `dur` 秒），
    曲库扫描建库时已写入；文件是否还在直接 `os.path.isfile`（几百首很快）。
  · 顺手把「解不出网格 / 没有时长 / 文件缺失」三类问题曲目做成可见的筛选 —— 这是
    现场最有用的两份"待处理清单"。
"""
import io
import os
import sys

ROOT = r"P:\AutoVJ"
PN = os.path.join(ROOT, "src", "panels.py")
UI = os.path.join(ROOT, "src", "ui_main.py")
PAT = []


def rep(rel, old, new, cnt=1):
    PAT.append((rel, old, new, cnt))


# ============================== ① panels.py：新面板类 ==============================
rep("src/panels.py",
    """class LibraryGrid(MediaGrid):""",
    '''class MusicLibraryPanel(QWidget):
    """音乐曲库**整页**面板：左栏「排序 / 筛选」+ 右侧曲目列表。

    ★ 用户要求（2026-10-01）：「曲库管理改成全屏的，左边增加一个排序和筛选的选项」。
      原来是 660×540 的弹窗（`MusicLibraryDialog`），这里做成能铺满主区的整页。

    ⚠ 成员名与旧弹窗**刻意对齐**（`refresh()` / `progress` / `lbl` / `btn_scan` / `list`）：
      主窗口里那 5 处「扫描进度 / 刷新列表」的代码直接复用，不用改一行
      （它们都是 `getattr(self, "music_dialog")` 之后调这几个成员）。

    数据来源：`cfg["music_library"]`（路径列表）+ `cfg["music_meta"][路径]`：
      · **BPM 与时长都在 `meta[路径]["grid"]` 里**（`bpm` / `dur` 秒），曲库扫描建库时写入
        （VDJ 库优先，读不到才本地算）；
      · 文件是否还在 = `os.path.isfile`（几百首很快，不做缓存）。
    """

    # 左栏候选（先按用户点的两组：BPM 区间 + 时长区间/文件缺失）
    BPM_FILTERS = [("全部", None), ("无节拍网格", "none"), ("<100", (0, 100)),
                   ("100–128", (100, 128)), ("128–150", (128, 150)),
                   ("150–180", (150, 180)), ("≥180", (180, 1e9))]
    DUR_FILTERS = [("全部", None), ("时长未知", "none"), ("<3 分钟", (0, 180)),
                   ("3–5 分钟", (180, 300)), ("5–7 分钟", (300, 420)),
                   ("≥7 分钟", (420, 1e9))]
    SORTS = [("原顺序", "orig"), ("曲名", "title"), ("艺人", "artist"),
             ("BPM 从低到高", "bpm"), ("BPM 从高到低", "bpm_d"),
             ("时长从短到长", "dur"), ("时长从长到短", "dur_d")]

    def __init__(self, main, parent=None):
        super().__init__(parent)
        self.main = main
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        # ---------------- 左栏：排序 / 筛选 ----------------
        left_w = QWidget()
        left_w.setFixedWidth(216)
        lv = QVBoxLayout(left_w)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(6)
        t = QLabel(T("排序 / 筛选"))
        t.setStyleSheet("font-weight:bold;color:" + theme.V("text") + ";")
        lv.addWidget(t)

        lv.addWidget(QLabel(T("排序")))
        self.cb_sort = QComboBox()
        for label, _key in self.SORTS:
            self.cb_sort.addItem(label)
        self.cb_sort.currentIndexChanged.connect(lambda _i: self.refresh())
        lv.addWidget(self.cb_sort)

        lv.addWidget(QLabel(T("BPM（按节拍网格）")))
        self.cb_bpm = QComboBox()
        for label, _v in self.BPM_FILTERS:
            self.cb_bpm.addItem(label)
        self.cb_bpm.currentIndexChanged.connect(lambda _i: self.refresh())
        lv.addWidget(self.cb_bpm)

        lv.addWidget(QLabel(T("时长")))
        self.cb_dur = QComboBox()
        for label, _v in self.DUR_FILTERS:
            self.cb_dur.addItem(label)
        self.cb_dur.currentIndexChanged.connect(lambda _i: self.refresh())
        lv.addWidget(self.cb_dur)

        self.chk_missing = QCheckBox(T("只看文件缺失"))
        self.chk_missing.setToolTip(T("换盘 / 改名之后找不到文件的曲目（修复清单）"))
        self.chk_missing.toggled.connect(lambda _s: self.refresh())
        lv.addWidget(self.chk_missing)

        b_reset = QPushButton(T("重置筛选"))
        b_reset.clicked.connect(self.reset_filters)
        lv.addWidget(b_reset)

        hint = QLabel(T("BPM / 时长来自曲库扫描时建的节拍网格；「无节拍网格」的曲目\n"
                        "八拍乐句对齐用不了，是需要处理的清单。"))
        hint.setWordWrap(True)
        hint.setStyleSheet("color:" + theme.V("muted") + ";font-size:11px;")
        lv.addWidget(hint)
        lv.addStretch(1)
        self.lbl_filter = QLabel("")
        self.lbl_filter.setStyleSheet("color:" + theme.V("muted") + ";font-size:11px;")
        self.lbl_filter.setWordWrap(True)
        lv.addWidget(self.lbl_filter)
        lay.addWidget(left_w, 0)

        # ---------------- 右侧：工具栏 + 列表 ----------------
        right = QVBoxLayout()
        right.setSpacing(6)
        row = QHBoxLayout()
        b_dir = QPushButton(T("导入音乐文件夹"))
        b_file = QPushButton(T("导入音乐文件"))
        self.btn_scan = QPushButton(T("扫描分析"))
        self.btn_scan.setToolTip(T("逐首建指纹 + 查曲风（后台、增量）"))
        b_rescan = QPushButton(T("全量重扫"))
        b_rescan.setToolTip(T("忽略已有结果，重新分析全部"))
        b_clear = QPushButton(T("清除分析数据"))
        b_clear.setToolTip(T("删除全部指纹与自动识别的曲风（可选保留手动设置）"))
        b_dir.clicked.connect(self.main.import_music_dir)
        b_file.clicked.connect(self.main.import_music_files)
        self.btn_scan.clicked.connect(lambda: self.main.scan_music(force=False))
        b_rescan.clicked.connect(lambda: self.main.scan_music(force=True))
        b_clear.clicked.connect(self.main.show_clear_music_analysis)
        for b in (b_dir, b_file, self.btn_scan, b_rescan, b_clear):
            row.addWidget(b)
        row.addStretch(1)
        right.addLayout(row)

        self.progress = QProgressBar()
        self.progress.hide()
        right.addWidget(self.progress)

        self.list = QListWidget()
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._ctx_menu)
        right.addWidget(self.list, 1)

        self.lbl = QLabel("")
        self.lbl.setStyleSheet("color:" + theme.V("muted") + ";")
        right.addWidget(self.lbl)
        lay.addLayout(right, 1)

        self.refresh()

    # ---------------- 数据 ----------------
    @staticmethod
    def _info(meta, path):
        """取一首曲目的 (bpm, 时长秒, 文件在不在)。缺的返回 None。"""
        m = meta.get(path) or {}
        g = m.get("grid") if isinstance(m.get("grid"), dict) else {}
        bpm = g.get("bpm") if g else None
        dur = g.get("dur") if g else None
        try:
            bpm = float(bpm) if bpm else None
        except (TypeError, ValueError):
            bpm = None
        try:
            dur = float(dur) if dur else None
        except (TypeError, ValueError):
            dur = None
        try:
            exists = os.path.isfile(path)
        except Exception:                                          # noqa: BLE001
            exists = False
        return bpm, dur, exists

    @staticmethod
    def _in_bucket(val, bucket):
        """bucket 为 None=全部；"none"=缺失；否则 (lo, hi) 区间。"""
        if bucket is None:
            return True
        if bucket == "none":
            return val is None
        if val is None:
            return False
        lo, hi = bucket
        return lo <= val < hi

    def reset_filters(self):
        for cb in (self.cb_sort, self.cb_bpm, self.cb_dur):
            cb.blockSignals(True)
            cb.setCurrentIndex(0)
            cb.blockSignals(False)
        self.chk_missing.setChecked(False)
        self.refresh()

    def _sort_key(self, key, meta):
        def k(path):
            bpm, dur, _e = self._info(meta, path)
            name = os.path.basename(path)
            if key == "title":
                return (name.lower(),)
            if key == "artist":
                return ((meta.get(path) or {}).get("artist") or "").lower()
            if key == "bpm":
                return (bpm is None, bpm or 0.0, name.lower())
            if key == "bpm_d":
                return (bpm is None, -(bpm or 0.0), name.lower())
            if key == "dur":
                return (dur is None, dur or 0.0, name.lower())
            if key == "dur_d":
                return (dur is None, -(dur or 0.0), name.lower())
            return (0,)
        return k

    def refresh(self):
        """按左栏的排序/筛选重建列表（廉价操作，几百首毫秒级）。"""
        try:
            meta = self.main.cfg["music_meta"] or {}
            lib = list(self.main.cfg["music_library"] or [])
            bpm_b = self.BPM_FILTERS[self.cb_bpm.currentIndex()][1]
            dur_b = self.DUR_FILTERS[self.cb_dur.currentIndex()][1]
            only_miss = self.chk_missing.isChecked()
            rows = []
            for p in lib:
                bpm, dur, exists = self._info(meta, p)
                if not self._in_bucket(bpm, bpm_b):
                    continue
                if not self._in_bucket(dur, dur_b):
                    continue
                if only_miss and exists:
                    continue
                rows.append((p, bpm, dur, exists))
            sk = self.SORTS[self.cb_sort.currentIndex()][1]
            if sk != "orig":
                order = {p: i for i, p in enumerate(lib)}
                rows.sort(key=lambda r: (self._sort_key(sk, meta)(r[0]),
                                         order.get(r[0], 0)))
            self.list.clear()
            for p, bpm, dur, exists in rows:
                m = meta.get(p) or {}
                genres = "、".join(m.get("genres") or []) or T("未分析")
                src = m.get("source") or ""
                bits = []
                if bpm:
                    bits.append("BPM %.0f" % bpm)
                else:
                    bits.append(T("无网格"))
                if dur:
                    bits.append("%d:%02d" % (int(dur) // 60, int(dur) % 60))
                if not exists:
                    bits.append(T("文件缺失"))
                txt = "%s    →    %s    (%s)  ｜ %s" % (
                    os.path.basename(p), genres, src, " ｜ ".join(bits))
                it = QListWidgetItem(txt)
                it.setData(Qt.UserRole, p)
                if not exists:
                    it.setForeground(QColor("#e06666"))
                self.list.addItem(it)
            done = sum(1 for p in lib if p in meta)
            self.lbl.setText(Tf("共 {} 首，已分析 {} 首（当前显示 {} 首）",
                                len(lib), done, len(rows)))
            self.lbl_filter.setText(Tf("筛选后 {} / {} 首", len(rows), len(lib)))
        except Exception:                                          # noqa: BLE001
            pass

    def _ctx_menu(self, pos):
        it = self.list.itemAt(pos)
        if it is None:
            return
        path = it.data(Qt.UserRole)
        menu = QMenu(self)
        edit = menu.addAction(T("纠正曲风…"))
        rescan = menu.addAction(T("重新分析扫描此曲目"))
        info = menu.addAction(T("此曲目信息"))
        menu.addSeparator()
        rm = menu.addAction(theme.danger_icon(), T("从曲库移除"))
        i18n_retranslate(menu)      # 临时菜单：弹出前刷语言
        act = menu.exec(self.list.mapToGlobal(pos))
        if act == edit:
            self.main.edit_music_genre(path)
        elif act == rescan:
            self.main.rescan_music(path)
        elif act == info:
            self.main.show_music_info(path)
        elif act == rm:
            self.main.remove_music(path)


class LibraryGrid(MediaGrid):''')

# ============================== ② ui_main：把面板放进曲库页 ==============================
rep("src/ui_main.py",
    """        row2 = QHBoxLayout()
        self.btn_p2_music = QPushButton(T("🎵 音乐曲库…"))
        self.btn_p2_music.setMinimumHeight(34)
        self.btn_p2_music.setToolTip(T("导入音乐、扫描分析曲风（演出前准备）"))
        self.btn_p2_music.clicked.connect(self.open_music_library)
        row2.addWidget(self.btn_p2_music)
        self.btn_p2_gvmap = QPushButton(T("曲风映射…"))""",
    """        # ★ 用户要求：曲库管理**从弹窗改成整页**，左边加排序/筛选（见 MusicLibraryPanel）。
        #   ⚠ 面板的成员名与旧弹窗刻意对齐（refresh/progress/lbl/btn_scan/list）⇒
        #     主窗口里那些「扫描进度 / 刷新列表」的代码直接复用，并把 music_dialog 指过来。
        self.music_panel = MusicLibraryPanel(self)
        self.music_dialog = self.music_panel
        l2.addWidget(self.music_panel, 1)
        row2 = QHBoxLayout()
        self.btn_p2_gvmap = QPushButton(T("曲风映射…"))""")

rep("src/ui_main.py",
    """    def open_music_library(self):
        from panels import MusicLibraryDialog
        self.music_dialog = MusicLibraryDialog(self)
        self.music_dialog.exec()""",
    """    def open_music_library(self):
        \"\"\"打开音乐曲库 —— 它现在是**第 3 页**（曲库·曲风）里的整页面板，不再是弹窗。\"\"\"
        try:
            self.switch_page(2)
            if getattr(self, "music_panel", None) is not None:
                self.music_panel.refresh()
        except Exception:                                          # noqa: BLE001
            pass""")

rep("src/ui_main.py",
    """from panels import (LayerStackPanel, PreviewPanel, AudioSourceDialog, MiniLevel,
                    LibraryGrid, CollapsibleSection, CLIP_MIME, LIB_THUMB_SIZES,
                    NewLayerDialog, I18nDialog)""",
    """from panels import (LayerStackPanel, PreviewPanel, AudioSourceDialog, MiniLevel,
                    LibraryGrid, CollapsibleSection, CLIP_MIME, LIB_THUMB_SIZES,
                    NewLayerDialog, I18nDialog, MusicLibraryPanel)""")


def main():
    files = {}
    for rel, old, new, cnt in PAT:
        s = files.get(rel) or io.open(os.path.join(ROOT, rel), encoding="utf-8").read()
        n = s.count(old)
        if n != cnt:
            print("✗ %s 命中 %d 次（应为 %d）" % (rel, n, cnt))
            print("   锚点首行：", old.splitlines()[0][:80])
            return 1
        files[rel] = s.replace(old, new)
    for rel, s in files.items():
        io.open(os.path.join(ROOT, rel), "w", encoding="utf-8", newline="\n").write(s)
        print("✓", rel)
    import py_compile
    for rel in files:
        py_compile.compile(os.path.join(ROOT, rel), doraise=True)
    print("语法 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
