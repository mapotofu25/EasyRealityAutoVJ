# -*- coding: utf-8 -*-
"""UI 第一步：页签扩成三页（演出台 / 素材库 / 曲库·曲风），第 2 页是**全屏素材库副本**。

用户要求（2026-10-01）：
  「把这里搞成 演出台 / 素材库 / 曲库曲风，**演出台的素材库不要动**，
    就是在这里再复制一个出来 —— 相当于有一个可以**全屏操作**的地方。」

设计（避免大规模重构、把回归风险压到最低）：
  · **共享筛选状态**：类型/角色/标签/搜索词仍是**一套**（`kind_filter`/`role_filter`/
    `tag_selected`/`lib_kw`），两页的控件都读写它 ⇒ 两页显示永远一致，不会互相矛盾；
  · **双网格**：`self.grid`（演出台，原样）+ `self.grid_full`（新页）；
  · ⚠ **性能**：库大时填充网格很贵 ⇒ 全屏那份**只在可见时填充**，不可见先标脏
    （`_lib_full_dirty`），切到那一页再补 —— 在演出台打字搜索的延迟完全不变；
  · ⚠ **跨页拖拽在 Qt 里要求两端同时可见** ⇒ 「把素材拖进图层」仍然只能在演出台做
    （这也是原设计「素材库必须留在演出台」的原因）。新页提供：搜索/筛选/导入/双击播放到
    当前图层/右键菜单（加入图层·移除·打标签）。
"""
import io
import os
import sys

ROOT = r"P:\AutoVJ"
UI = os.path.join(ROOT, "src", "ui_main.py")
PAT = []


def rep(old, new, cnt=1):
    PAT.append((old, new, cnt))


# ---------------- ① 页签：加第 2 项「素材库」 ----------------
rep("""        for i, (txt, tip) in enumerate((
                ("🎬 演出台", "现场：预览 / 图层 / 效果 / 输出"),
                ("🎵 曲库 · 曲风", "演出前准备：音乐曲库（导入音乐 + 扫描分析曲风）/ 曲风映射"))):""",
    """        for i, (txt, tip) in enumerate((
                ("🎬 演出台", "现场：预览 / 图层 / 效果 / 输出"),
                ("🗂 素材库", "全屏管理素材：搜索 / 筛选 / 导入 / 打标签（演出台那份原样不动）"),
                ("🎵 曲库 · 曲风", "演出前准备：音乐曲库（导入音乐 + 扫描分析曲风）/ 曲风映射"))):""")

# ---------------- ② 插入第 2 页 ----------------
rep("""        l1.addWidget(show_split)
        self.pages.addWidget(p1)
""",
    """        l1.addWidget(show_split)
        self.pages.addWidget(p1)

        # ---- 第 2 页：素材库（**全屏副本**；演出台那份原样不动）----
        self.pages.addWidget(self._build_library_page())
""")

# ---------------- ③ 页号上界 1 → 2 ----------------
rep("""        # ⚠ 上界是 1（两页）。老配置里可能存着 2（码表编排页，已移除）⇒ 必须钳回来，
        #   否则 setCurrentIndex(2) 会切到不存在的页，界面一片空白。
        self.switch_page(idx if 0 <= idx <= 1 else 0, save=False)""",
    """        # ⚠ 上界是 2（三页：演出台 / 素材库 / 曲库·曲风）。老配置里的值一律钳回合法范围，
        #   否则 setCurrentIndex 会切到不存在的页，界面一片空白。
        self.switch_page(idx if 0 <= idx <= 2 else 0, save=False)""")

rep("""    def switch_page(self, i, save=True):
        \"\"\"切到两界面之一（页签条 / 「⋯ 更多」菜单都走这里）。\"\"\"
        try:
            i = max(0, min(1, int(i)))
        except Exception:                                         # noqa: BLE001
            i = 0
        self.pages.setCurrentIndex(i)""",
    """    def switch_page(self, i, save=True):
        \"\"\"切到三界面之一（页签条 / 「⋯ 更多」菜单都走这里）。\"\"\"
        try:
            i = max(0, min(2, int(i)))
        except Exception:                                         # noqa: BLE001
            i = 0
        self.pages.setCurrentIndex(i)
        # ⚠ 全屏素材库那份是"不可见就不填充、先标脏"的（见 refresh_grid）⇒ 切过来时补上
        if i == 1 and getattr(self, "_lib_full_dirty", False):
            try:
                self._populate_grid(self.grid_full, getattr(self, "lbl_lib_count2", None))
                self._lib_full_dirty = False
            except Exception:                                     # noqa: BLE001
                pass
        if getattr(self, "grid_full", None) is not None:
            self._sync_lib_controls()""")

# ---------------- ④ 页面构建方法（放在 _sync_p2_stat 之前）----------------
rep("""    def _sync_p2_stat(self):""",
    """    def _build_library_page(self):
        \"\"\"第 2 页：**全屏素材库**（演出台那份原样不动，这里是复制出来的一份）。

        ★ 用户要求：页签做成「演出台 / 素材库 / 曲库·曲风」，演出台的素材库不要动，
          在素材库页**再复制一个出来**，相当于有个能全屏操作的地方。

        ⚠ 两页**共用同一套筛选状态**（类型/角色/标签/搜索词）：任何一个控件改了都会
          同步到另一页（`_sync_lib_controls`），否则两页显示不一致会更迷惑。
        ⚠ 跨页拖拽在 Qt 里要求两端同时可见 ⇒ **拖素材进图层仍然只能在演出台做**；
          这一页给的是：搜索 / 筛选 / 导入 / 双击播放到当前图层 / 右键菜单。
        \"\"\"
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)
        t = QLabel(T("🗂 素材库"))
        t.setStyleSheet("font-weight:bold;font-size:15px;color:" + theme.V("text") + ";")
        lay.addWidget(t)
        self.lbl_lib2_desc = QLabel(T(
            "这里是全屏版素材库，和「🎬 演出台」上那份是同一批素材（那份保持原样）。\\n"
            "搜索 / 筛选 / 导入 / 右键打标签都在这里做；双击缩略图 = 播放到当前图层。\\n"
            "⚠ 「拖进图层」只能在演出台做（跨页面拖拽系统不允许）。"))
        self.lbl_lib2_desc.setWordWrap(True)
        self.lbl_lib2_desc.setStyleSheet("color:" + theme.V("muted") + ";")
        lay.addWidget(self.lbl_lib2_desc)

        # ---- 筛选条（与演出台同一套状态）----
        fbar = QHBoxLayout()
        self.kind_group2 = QButtonGroup(self)
        for i, name in enumerate(("全部", "图片", "视频")):
            b = QPushButton(name)
            b.setCheckable(True)
            b.setChecked(i == 0)
            b.setFixedHeight(24)
            b.clicked.connect(lambda _c=False, ii=i: self.set_kind_filter(ii))
            self.kind_group2.addButton(b, i)
            fbar.addWidget(b)
        fbar.addWidget(QLabel("标签"))
        self.btn_tagfilter2 = QPushButton("标签: 全部")
        self.btn_tagfilter2.setToolTip("按标签筛选素材（可多选）")
        self.btn_tagfilter2.clicked.connect(self.open_tag_filter)
        fbar.addWidget(self.btn_tagfilter2, 1)
        fbar.addWidget(QLabel("角色"))
        self.role_combo2 = QComboBox()
        self.role_combo2.addItems(["全部", "前景", "背景"])
        self.role_combo2.currentIndexChanged.connect(self.set_role_filter)
        fbar.addWidget(self.role_combo2, 0)
        fbar.addWidget(QLabel("数量:"))
        self.lbl_lib_count2 = QLabel("0")
        fbar.addWidget(self.lbl_lib_count2)
        lay.addLayout(fbar)

        # ---- 搜索 + 导入 + 打标 ----
        sbar = QHBoxLayout()
        self.search_box2 = QLineEdit()
        self.search_box2.setPlaceholderText("搜索文件名 / 标签")
        self.search_box2.setClearButtonEnabled(True)
        self.search_box2.textChanged.connect(lambda _t: self._on_search_changed())
        sbar.addWidget(self.search_box2, 1)
        b1 = QPushButton(tr("import_folder"))
        b1.clicked.connect(self.import_folder)
        b2 = QPushButton(tr("import_files"))
        b2.clicked.connect(self.import_files)
        b3 = QPushButton("扫描打标")
        b3.setToolTip("用本地视觉模型为素材自动打内容标签（后台、可续扫）")
        b3.clicked.connect(lambda: self.scan_tags())
        for b in (b1, b2, b3):
            sbar.addWidget(b)
        lay.addLayout(sbar)

        # ---- 大网格 ----
        self.grid_full = LibraryGrid(self)
        self.grid_full.remove_from_layer.connect(self.remove_from_all_layers)
        self.grid_full.files_dropped.connect(self._import_paths)
        self.grid_full.apply_thumb_size(int(self.cfg["ui"].get("thumb_size", 1)))
        lay.addWidget(self.grid_full, 1)
        self._lib_full_dirty = True
        self._style_lib_full()
        theme.register(self, self._style_lib_full)
        return w

    def _style_lib_full(self):
        \"\"\"全屏素材库网格的样式（主题感知）。\"\"\"
        try:
            self.grid_full.setStyleSheet(
                f"QListWidget{{background:{theme.V('panel2')};"
                f"border:1px solid {theme.V('border')};}}")
        except Exception:                                         # noqa: BLE001
            pass

    def _on_search_changed(self):
        \"\"\"任一页的搜索框改动 ⇒ 写进共享状态、同步另一页、刷新两份网格。\"\"\"
        for ed in (getattr(self, "search_box", None), getattr(self, "search_box2", None)):
            if ed is not None and ed.hasFocus():
                self.lib_kw = ed.text() or ""
                break
        self.refresh_grid()

    def _sync_lib_controls(self):
        \"\"\"把共享的筛选状态镜像到**两页**的控件上（避免两页显示不一致）。\"\"\"
        try:
            kf = int(self.kind_filter)
            rf = int(self.role_filter)
            for grp in (getattr(self, "kind_group", None), getattr(self, "kind_group2", None)):
                if grp is None:
                    continue
                for i, b in enumerate(grp.buttons()):
                    b.blockSignals(True)
                    b.setChecked(i == kf)
                    b.blockSignals(False)
            for cb in (getattr(self, "role_combo", None), getattr(self, "role_combo2", None)):
                if cb is None:
                    continue
                cb.blockSignals(True)
                cb.setCurrentIndex(max(0, min(2, rf)))
                cb.blockSignals(False)
            txt = (f"标签: 已选 {len(self.tag_selected)}" if self.tag_selected
                   else "标签: 全部")
            for b in (getattr(self, "btn_tagfilter", None),
                      getattr(self, "btn_tagfilter2", None)):
                if b is not None:
                    b.setText(txt)
            kw = getattr(self, "lib_kw", "")
            for ed in (getattr(self, "search_box", None), getattr(self, "search_box2", None)):
                if ed is None or ed.text() == kw:
                    continue
                ed.blockSignals(True)
                ed.setText(kw)
                ed.blockSignals(False)
        except Exception:                                         # noqa: BLE001
            pass

    def _sync_p2_stat(self):""")

# ---------------- ⑤ refresh_grid 参数化 + 两页共用 ----------------
rep("""    def refresh_grid(self, icons_only=False):
        sz = self.grid.iconSize()
        if not icons_only:
            self.grid.clear()
            sel = self.tag_selected
            kw = (self.search_box.text() or "").strip().lower()""",
    """    def refresh_grid(self, icons_only=False):
        \"\"\"刷新素材库网格（**两页共用**：演出台那份 + 全屏素材库那份）。\"\"\"
        self._populate_grid(self.grid, self.lbl_lib_count, icons_only)
        g2 = getattr(self, "grid_full", None)
        if g2 is None:
            return
        # ⚠ 全屏那份**只在可见时填充**（库大时填充很贵，每次搜索都填两遍会明显变卡）：
        #   不可见就先标脏，切到那一页时再补（见 switch_page）。
        if self.pages.currentIndex() == 1:
            self._populate_grid(g2, getattr(self, "lbl_lib_count2", None), icons_only)
            self._lib_full_dirty = False
        else:
            self._lib_full_dirty = True
        self._sync_lib_controls()

    def _populate_grid(self, grid, count_lbl, icons_only=False):
        \"\"\"把筛选后的素材填进 `grid`（演出台那份与全屏那份共用这段逻辑）。\"\"\"
        sz = grid.iconSize()
        if not icons_only:
            grid.clear()
            sel = self.tag_selected
            kw = (getattr(self, "lib_kw", "") or "").strip().lower()""")

rep("""                li = QListWidgetItem(it.icon_cached(sz.width(), sz.height()), self._item_text(it))
                li.setData(Qt.UserRole, it)
                li.setToolTip(self._item_tooltip(it))
                self._tint_role(li, it)
                self.grid.addItem(li)
        else:
            for i in range(self.grid.count()):
                li = self.grid.item(i)
                it = li.data(Qt.UserRole)
                li.setIcon(it.icon_cached(sz.width(), sz.height()))
                li.setText(self._item_text(it))
                li.setToolTip(self._item_tooltip(it))
                self._tint_role(li, it)
        self.lbl_lib_count.setText(f"{self.grid.count()}/{len(self.library)}")""",
    """                li = QListWidgetItem(it.icon_cached(sz.width(), sz.height()), self._item_text(it))
                li.setData(Qt.UserRole, it)
                li.setToolTip(self._item_tooltip(it))
                self._tint_role(li, it)
                grid.addItem(li)
        else:
            for i in range(grid.count()):
                li = grid.item(i)
                it = li.data(Qt.UserRole)
                li.setIcon(it.icon_cached(sz.width(), sz.height()))
                li.setText(self._item_text(it))
                li.setToolTip(self._item_tooltip(it))
                self._tint_role(li, it)
        if count_lbl is not None:
            count_lbl.setText(f"{grid.count()}/{len(self.library)}")""")

# ---------------- ⑥ 搜索框改走共享状态 ----------------
rep("""        self.search_box.textChanged.connect(lambda _t: self.refresh_grid())""",
    """        # ⚠ 两页共用一个搜索词（`lib_kw`）；同步逻辑见 _on_search_changed/_sync_lib_controls
        self.search_box.textChanged.connect(lambda _t: self._on_search_changed())""")

# ---------------- ⑦ 共享状态初始化 ----------------
rep("""        self.kind_filter = 0   # 0 全部 / 1 图片 / 2 视频
        self.role_filter = 0   # 0 全部 / 1 前景 / 2 背景
        bar = QHBoxLayout()""",
    """        self.kind_filter = 0   # 0 全部 / 1 图片 / 2 视频
        self.role_filter = 0   # 0 全部 / 1 前景 / 2 背景
        self.lib_kw = ""       # 搜索词（两页共用，见 _build_library_page）
        bar = QHBoxLayout()""")

# ---------------- ⑧ 筛选/标签改动后同步两页 ----------------
rep("""    def set_kind_filter(self, i):
        self.kind_filter = i
        self.refresh_grid()

    def set_role_filter(self, i):
        self.role_filter = i
        self.refresh_grid()""",
    """    def set_kind_filter(self, i):
        self.kind_filter = i
        self._sync_lib_controls()
        self.refresh_grid()

    def set_role_filter(self, i):
        self.role_filter = i
        self._sync_lib_controls()
        self.refresh_grid()""")

rep("""    def _fill_tag_combo(self):
        \"\"\"标签集合变化后刷新筛选按钮文案（兼容旧调用点）\"\"\"
        self.btn_tagfilter.setText(
            f"标签: 已选 {len(self.tag_selected)}" if self.tag_selected else "标签: 全部")""",
    """    def _fill_tag_combo(self):
        \"\"\"标签集合变化后刷新筛选按钮文案（**两页都要**，兼容旧调用点）\"\"\"
        self._sync_lib_controls()""")

# ---------------- ⑨ 类型按钮样式覆盖两页 ----------------
rep("""    def _style_kind_buttons(self):
        \"\"\"主题感知的筛选按钮样式（注册进 theme，切换亮暗时重设）\"\"\"
        v = theme.V
        for b in self.kind_group.buttons():""",
    """    def _style_kind_buttons(self):
        \"\"\"主题感知的筛选按钮样式（注册进 theme，切换亮暗时重设；**两页都覆盖**）\"\"\"
        v = theme.V
        btns = list(self.kind_group.buttons())
        g2 = getattr(self, "kind_group2", None)
        if g2 is not None:
            btns += list(g2.buttons())
        for b in btns:"""),
rep("""    def _style_grid(self):
        self.grid.setStyleSheet(""",
    """    def _style_grid(self):
        \"\"\"演出台那份素材库网格的样式（全屏那份见 `_style_lib_full`）。\"\"\"
        self.grid.setStyleSheet(""")


def main():
    s = io.open(UI, encoding="utf-8").read()
    for i, (old, new, cnt) in enumerate(PAT, 1):
        n = s.count(old)
        if n != cnt:
            print("✗ 第 %d 处命中 %d 次（应为 %d）" % (i, n, cnt))
            print("   锚点首行：", old.splitlines()[0][:80])
            return 1
    for old, new, cnt in PAT:
        s = s.replace(old, new)
    io.open(UI, "w", encoding="utf-8", newline="\n").write(s)
    print("✓ ui_main.py 已加「素材库」全屏页（%d 处改动）" % len(PAT))
    import py_compile
    py_compile.compile(UI, doraise=True)
    print("语法 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
