# -*- coding: utf-8 -*-
"""一次性补丁：主区拆成「三界面」（演出台 / 素材·曲库·曲风 / 码表编排）。

改动：
  `src/cue_editor.py`  CueSheetEditor 由 QDialog 改为 QWidget（可嵌页），
                       新增 embedded 参数 + set_active()（不可见时停定时器）
  `src/ui_main.py`     新增页签条 + QStackedWidget；素材库搬到第 2 页；
                       码表编排成为第 3 页（惰性创建）；open_cuesheet 改为切页

用法：venv/Scripts/python.exe -u tools/_patch_ui_pages.py
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")

FAILS = []


def edit(path, pairs, adds=()):
    """pairs: [(old, new, 期望出现次数)]；adds: [(插入点, 插入内容)] —— 均按精确文本。"""
    p = os.path.join(SRC, path)
    t = io.open(p, encoding="utf-8").read()
    orig = t
    for old, new, cnt in pairs:
        got = t.count(old)
        if got != cnt:
            FAILS.append("%s: 期望 %d 处，实得 %d 处\n--- old ---\n%s\n---" % (path, cnt, got, old[:300]))
            continue
        t = t.replace(old, new)
    for anchor, ins in adds:
        got = t.count(anchor)
        if got != 1:
            FAILS.append("%s: 插入锚点出现 %d 次（应为 1）：%s" % (path, got, anchor[:120]))
            continue
        t = t.replace(anchor, ins + anchor, 1)
    if t != orig:
        io.open(p, "w", encoding="utf-8", newline="").write(t)
        print("  已写入 %s（%+d 字节）" % (path, len(t) - len(orig)))
    else:
        print("  未变化 %s" % path)


# ======================================================================
# 一、cue_editor.py：QDialog -> QWidget（可嵌页）
# ======================================================================
edit("cue_editor.py", [
    (
        'class CueSheetEditor(QDialog):\n'
        '    """码表编排窗口（管理 + 编排合一）。"""\n'
        '\n'
        '    def __init__(self, main):\n'
        '        super().__init__(main)\n'
        '        self.main = main\n'
        '        self.setWindowTitle(T("码表编排"))\n'
        '        self.resize(1180, 720)\n',
        'class CueSheetEditor(QWidget):\n'
        '    """码表编排面板（管理 + 编排合一）。\n'
        '\n'
        '    ★ 一份代码两种用法：\n'
        '      · `embedded=True`  —— 嵌进主窗口的「码表编排」界面（第 3 页，默认）；\n'
        '      · `embedded=False` —— 自己当一个独立窗口。\n'
        '    """\n'
        '\n'
        '    def __init__(self, main, embedded=True):\n'
        '        super().__init__(main)\n'
        '        self.main = main\n'
        '        self.embedded = bool(embedded)\n'
        '        if not self.embedded:\n'
        '            self.setWindowTitle(T("码表编排"))\n'
        '            self.resize(1180, 720)\n',
        1),
    (
        '        self._timer = QTimer(self)\n'
        '        self._timer.timeout.connect(self._refresh)\n'
        '        self._timer.start(500)\n'
        '        self._refresh()\n',
        '        self._timer = QTimer(self)\n'
        '        self._timer.timeout.connect(self._refresh)\n'
        '        if not self.embedded:\n'
        '            self._timer.start(500)\n'
        '        self._refresh()\n'
        '\n'
        '    def set_active(self, on):\n'
        '        """★ 只在**看得见的时候**才跑 500ms 刷新（嵌在主窗口里时切走了就该歇着）。\n'
        '\n'
        '        主线程不做无用功是项目铁律（每次启动都会跑的东西必须先问它在不在主线程上）。\n'
        '        """\n'
        '        on = bool(on)\n'
        '        if self.embedded:\n'
        '            if on:\n'
        '                if not self._timer.isActive():\n'
        '                    self._timer.start(500)\n'
        '                self._refresh()\n'
        '            else:\n'
        '                self._timer.stop()\n'
        '        return on\n',
        1),
    (
        '        bb = QDialogButtonBox(QDialogButtonBox.Close)\n'
        '        bb.button(QDialogButtonBox.Close).setText(T("关闭"))\n'
        '        bb.rejected.connect(self.reject)\n'
        '        bb.accepted.connect(self.accept)\n'
        '        v.addWidget(bb)\n',
        '        bb = QDialogButtonBox(QDialogButtonBox.Close)\n'
        '        _bc = bb.button(QDialogButtonBox.Close)\n'
        '        if self.embedded:\n'
        '            # 嵌在主窗口里时「关闭」没有意义 ⇒ 改成回演出台的快捷出口\n'
        '            _bc.setText(T("← 返回演出台"))\n'
        '            bb.rejected.connect(self._back_to_show)\n'
        '            bb.accepted.connect(self._back_to_show)\n'
        '        else:\n'
        '            _bc.setText(T("关闭"))\n'
        '            bb.rejected.connect(self.reject)\n'
        '            bb.accepted.connect(self.accept)\n'
        '        v.addWidget(bb)\n'
        '\n'
        '    def _back_to_show(self):\n'
        '        fn = getattr(self.main, "switch_page", None)\n'
        '        if callable(fn):\n'
        '            fn(0)\n',
        1),
])

# ======================================================================
# 二、ui_main.py
# ======================================================================
edit("ui_main.py", [
    # --- 顶部文档：描述三界面 ---
    (
        '"""主窗口 UI（Arena 风格布局）\n',
        '"""主窗口 UI（Arena 风格布局）\n'
        '\n'
        '★ 2026-09-28 起主区拆成**三界面**（`QStackedWidget`，共用同一个引擎）：\n'
        '  ① 演出台     —— 图层 + 预览 + 设置 + 输出（现场用）\n'
        '  ② 素材·曲库·曲风 —— 素材库独占整幅宽 + 音乐曲库 / 曲风映射入口（演出前准备）\n'
        '  ③ 码表编排   —— `cue_editor.CueSheetEditor`（编曲；惰性创建）\n',
        1),
    # --- 演出台：素材库搬走 ---
    (
        '        bottom = QSplitter(Qt.Horizontal)\n'
        '        self.preview_panel = PreviewPanel(self)            # 绿\n'
        '        bottom.addWidget(self.preview_panel)\n'
        '        bottom.addWidget(self._build_library())            # 蓝\n'
        '        bottom.setSizes([560, 520])\n'
        '        left_split.addWidget(bottom)\n'
        '        left_split.setSizes([430, 380])\n',
        '        # 演出台 = 图层 + 预览（素材库搬到第 2 页，见 _build_pages）\n'
        '        self.preview_panel = PreviewPanel(self)            # 绿\n'
        '        left_split.addWidget(self.preview_panel)\n'
        '        left_split.setSizes([430, 620])\n',
        1),
    (
        '        # ---- 主区 ----\n',
        '        # ---- 主区（三界面容器，见 _build_pages）----\n',
        1),
    (
        '        main_split.setSizes([1150, 360])\n'
        '        mv.addWidget(main_split, 1)\n',
        '        main_split.setSizes([1150, 360])\n'
        '        self._build_pages(main_split)\n'
        '        mv.addWidget(self.pages, 1)\n',
        1),
    # --- 菜单动作：切页而不是弹独立窗口 ---
    (
        '    def open_cuesheet(self):\n'
        '        """打开「码表 / 时间码」窗口：导入 .ercue → 绑定到码表图层 → 选位置源。"""\n'
        '        try:\n'
        '            # ⚠ 本文件是 `from panels import (…)` 逐个导入的，**没有** `panels` 这个模块名\n'
        '            #   （写成 panels.XxxDialog 会 NameError —— 冒烟测不出来，得点开菜单才发现）\n'
        '            #   编排窗口现在在 src/cue_editor.py（管理 + PR 式时间轴合一）\n'
        '            dlg = CueSheetEditor(self)\n'
        '            dlg.exec()\n'
        '        except Exception as e:                                   # noqa: BLE001\n'
        '            try:\n'
        '                QMessageBox.warning(self, T("码表"), "%s: %s" % (type(e).__name__, e))\n'
        '            except Exception:                                    # noqa: BLE001\n'
        '                pass\n',
        '    def open_cuesheet(self):\n'
        '        """切到「码表编排」界面：导入 .ercue → 绑定到码表图层 → 选位置源。\n'
        '\n'
        '        ⚠ 2026-09-28：码表编排**不再是独立对话框**，而是主窗口的第 3 个界面\n'
        '          （用户要求「把码表管理和编排放一起」）。旧写法 `panels.CueSheetDialog`\n'
        '          曾因本文件是 `from panels import (…)`、**没有** `panels` 模块名而 NameError\n'
        '          —— 冒烟测不出来，得点开菜单才发现，所以这里仍然兜一层弹窗提示。\n'
        '        """\n'
        '        try:\n'
        '            self.switch_page(2)\n'
        '        except Exception as e:                                   # noqa: BLE001\n'
        '            try:\n'
        '                QMessageBox.warning(self, T("码表"), "%s: %s" % (type(e).__name__, e))\n'
        '            except Exception:                                    # noqa: BLE001\n'
        '                pass\n',
        1),
    # --- 「⋯ 更多」里那条动作的提示语 ---
    (
        '            self.act_cuesheet.setText(T("码表 / 时间码…"))\n',
        '            self.act_cuesheet.setText(T("码表 / 时间码…"))\n'
        '            self.act_cuesheet.setToolTip(\n'
        '                T("切换到「码表编排」界面：导入 .ercue、绑定码表图层、选位置源"))\n',
        1),
], adds=[
    ('    def _build_library(self):\n', '''    # ================= 三界面（演出台 / 素材·曲库·曲风 / 码表编排）=================
    def _style_page_buttons(self):
        """三界面页签样式（主题感知；注册进 theme，切亮暗时重设）。"""
        v = theme.V
        for b in getattr(self, "_page_btns", []):
            b.setStyleSheet(
                f"QPushButton{{padding:4px 14px;border:1px solid {v('border')};"
                f"background:{v('btn')};color:{v('text')};}}"
                f"QPushButton:checked{{background:{v('sel')};color:#9fdcff;"
                f"border-color:#2f6f9f;font-weight:bold;}}")

    def _build_pages(self, show_split):
        """★ 2026-09-28：主区拆成**三界面**（`QStackedWidget`），共用同一个引擎。

        为什么拆（用户反馈「看起来乱乱的」）：演出控制、素材整理、码表编排是
        **三个完全不同的场合**（现场 / 演出前准备 / 编曲），混在一屏里互相挤。
        拆开后每页都能占满宽度，页签常显、一键来回切。
        ⚠ 第 3 页（码表编排）**惰性创建** —— 自绘时间轴与定时器不进启动路径。
        """
        self.pages = QStackedWidget()

        # ---- 页签条（常显）----
        bar = QHBoxLayout()
        bar.setSpacing(0)
        self._page_group = QButtonGroup(self)
        self._page_btns = []
        for i, (txt, tip) in enumerate((
                ("🎬 演出台", "现场：预览 / 图层 / 效果 / 输出"),
                ("📁 素材 · 曲库 · 曲风", "演出前准备：素材库 / 音乐曲库 / 曲风映射"),
                ("🎞 码表编排", "编曲：把 .ercue 码表对到拍上，绑定码表图层"))):
            b = QPushButton(T(txt))
            b.setCheckable(True)
            b.setMinimumHeight(28)
            b.setToolTip(T(tip))
            b.clicked.connect(lambda _c=False, ii=i: self.switch_page(ii))
            self._page_group.addButton(b, i)
            self._page_btns.append(b)
            bar.addWidget(b)
        bar.addStretch(1)
        self._style_page_buttons()
        theme.register(self, self._style_page_buttons)
        self.main_view.layout().addLayout(bar)

        # ---- 第 1 页：演出台（图层 + 预览 + 设置 + 输出，原样）----
        p1 = QWidget()
        l1 = QVBoxLayout(p1)
        l1.setContentsMargins(0, 0, 0, 0)
        l1.addWidget(show_split)
        self.pages.addWidget(p1)

        # ---- 第 2 页：素材 · 曲库 · 曲风（素材库独占整幅宽）----
        p2 = QWidget()
        l2 = QVBoxLayout(p2)
        l2.setContentsMargins(0, 0, 0, 0)
        l2.setSpacing(4)
        h2 = QHBoxLayout()
        t2 = QLabel(T("素材库"))
        t2.setStyleSheet("color:" + theme.V("muted") + ";font-weight:bold;")
        h2.addWidget(t2)
        h2.addStretch(1)
        self.btn_p2_music = QPushButton(T("🎵 音乐曲库"))
        self.btn_p2_music.setMinimumHeight(26)
        self.btn_p2_music.setToolTip(T("导入音乐、扫描分析曲风（演出前准备）"))
        self.btn_p2_music.clicked.connect(self.open_music_library)
        h2.addWidget(self.btn_p2_music)
        self.btn_p2_gvmap = QPushButton(T("曲风映射"))
        self.btn_p2_gvmap.setMinimumHeight(26)
        self.btn_p2_gvmap.setToolTip(T("编辑曲风→画面标签的绑定关系"))
        self.btn_p2_gvmap.clicked.connect(self.open_genre_visual_editor)
        h2.addWidget(self.btn_p2_gvmap)
        l2.addLayout(h2)
        self.library_panel = self._build_library()      # 蓝：素材库
        l2.addWidget(self.library_panel, 1)
        self.pages.addWidget(p2)

        # ---- 第 3 页：码表编排（先占位，首次切入才建）----
        p3 = QWidget()
        self._page3_lay = QVBoxLayout(p3)
        self._page3_lay.setContentsMargins(0, 0, 0, 0)
        self.pages.addWidget(p3)
        self.cue_editor = None

        try:
            idx = int(self.cfg["ui"].get("page", 0) or 0)
        except Exception:                                         # noqa: BLE001
            idx = 0
        self.switch_page(idx if 0 <= idx <= 2 else 0, save=False)

    def _ensure_cue_editor(self):
        """首次切到第 3 页时才建码表编排面板（自绘时间轴 + 500ms 定时器不进启动路径）。"""
        if self.cue_editor is not None:
            return self.cue_editor
        try:
            self.cue_editor = CueSheetEditor(self, embedded=True)
            self._page3_lay.addWidget(self.cue_editor, 1)
        except Exception:                                         # noqa: BLE001
            self.cue_editor = None
        return self.cue_editor

    def switch_page(self, i, save=True):
        """切到三界面之一（页签条 / 「⋯ 更多」菜单 / 建完码表图层都走这里）。

        ⚠ 码表编排只在**看得见的时候**才跑 500ms 刷新（CueSheetEditor.set_active）。
        """
        try:
            i = max(0, min(2, int(i)))
        except Exception:                                         # noqa: BLE001
            i = 0
        if i == 2:
            ed = self._ensure_cue_editor()
            if ed is not None:
                ed.set_active(True)
        if i != 2 and self.cue_editor is not None:
            self.cue_editor.set_active(False)
        self.pages.setCurrentIndex(i)
        for k, b in enumerate(getattr(self, "_page_btns", [])):
            b.setChecked(k == i)
        if save:
            try:
                if int(self.cfg["ui"].get("page", -1) or -1) != i:
                    self.cfg["ui"]["page"] = i
                    self.cfg.save()
            except Exception:                                     # noqa: BLE001
                pass

'''),
])

if FAILS:
    print("\n!! 补丁未全部应用：")
    for f in FAILS:
        print("---", f)
    sys.exit(1)
print("\n补丁应用完毕。")
