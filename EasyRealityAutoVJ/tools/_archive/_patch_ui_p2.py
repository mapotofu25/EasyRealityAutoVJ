# -*- coding: utf-8 -*-
"""补丁：素材库回到演出台；第 2 页改成「曲库 · 曲风」；顶部工具栏去掉两个曲库按钮。

用户 2026-09-28 反馈：「主界面的素材库预览保留，不然我怎么手动添加素材」
                   「右上角的曲风 / 曲库可以去掉了？」

用法：venv/Scripts/python.exe -u tools/_patch_ui_p2.py
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")

FAILS = []


def edit(path, pairs, adds=()):
    p = os.path.join(SRC, path)
    t = io.open(p, encoding="utf-8").read()
    orig = t
    for old, new, cnt in pairs:
        got = t.count(old)
        if got != cnt:
            FAILS.append("%s: 期望 %d 处，实得 %d 处\n--- old ---\n%s\n---" % (path, cnt, got, old[:400]))
            continue
        t = t.replace(old, new)
    for anchor, ins in adds:
        got = t.count(anchor)
        if got != 1:
            FAILS.append("%s: 插入锚点出现 %d 次：%s" % (path, got, anchor[:120]))
            continue
        t = t.replace(anchor, ins + anchor, 1)
    if t != orig:
        io.open(p, "w", encoding="utf-8", newline="").write(t)
        print("  已写入 %s（%+d 字节）" % (path, len(t) - len(orig)))
    else:
        print("  未变化 %s" % path)


edit("ui_main.py", [
    # ① 工具栏：拿掉两个曲库按钮
    (
        '        # ---- 右端：工具入口 ----\n'
        '        # ★ 2026-09-27 工具条收纳（用户选）：**主题 / 语言 / 快捷键 / 关于** 收进「⋯」菜单\n'
        '        #   —— 这几个属于"设置时用一次"，不是现场操作；**音乐曲库 / 曲风映射** 留在工具条上。\n'
        '        #   工具条原本 17 个控件混在一起（演出控制 + 设备 + 工具入口 + 杂项），现在按用途分组：\n'
        '        #     [开始|黑场|冻结|暂停自动|下一素材] │ [音源 电平] │ [GPU 解码] │ [音乐曲库|曲风映射|⋯]\n'
        '        self.btn_music = QPushButton("🎵 音乐曲库")\n'
        '        self.btn_music.setMinimumHeight(32)\n'
        '        self.btn_music.setToolTip("导入音乐、扫描分析曲风（演出前准备）")\n'
        '        self.btn_music.clicked.connect(self.open_music_library)\n'
        '        tb.addWidget(self.btn_music)\n'
        '        self.btn_gvmap = QPushButton("曲风映射")\n'
        '        self.btn_gvmap.setMinimumHeight(32)\n'
        '        self.btn_gvmap.setToolTip("编辑曲风→画面标签的绑定关系")\n'
        '        self.btn_gvmap.clicked.connect(self.open_genre_visual_editor)\n'
        '        tb.addWidget(self.btn_gvmap)\n',
        '        # ---- 右端：工具入口 ----\n'
        '        # ★ 2026-09-27 收纳（用户选）：主题 / 语言 / 快捷键 / 关于 →「⋯ 更多」。\n'
        '        # ★ 2026-09-28 再收纳（用户要求）：「🎵 音乐曲库 / 曲风映射」也挪去第 2 个界面\n'
        '        #   「🎵 曲库 · 曲风」—— 它们属于"演出前准备"，不该占着现场工具条。\n'
        '        #   现在工具条 = [开始|黑场|冻结|暂停自动|下一素材] │ [音源 电平] │ [GPU 解码] │ [⋯ 更多]\n',
        1),
    # ② 演出台：素材库回来（和预览并排）
    (
        '        # 演出台 = 图层 + 预览（素材库搬到第 2 页，见 _build_pages）\n'
        '        self.preview_panel = PreviewPanel(self)            # 绿\n'
        '        left_split.addWidget(self.preview_panel)\n'
        '        left_split.setSizes([430, 620])\n',
        '        # 演出台 = 图层 + 预览 + 素材库。\n'
        '        # ★ 2026-09-28 用户要求把素材库**留在主界面**：「不然我怎么手动添加素材」——\n'
        '        #   而且素材库与图层同页，才能把缩略图**直接拖进图层**（跨页拖拽在 Qt 里做不到）。\n'
        '        bottom = QSplitter(Qt.Horizontal)\n'
        '        self.preview_panel = PreviewPanel(self)            # 绿\n'
        '        bottom.addWidget(self.preview_panel)\n'
        '        self.library_panel = self._build_library()         # 蓝\n'
        '        bottom.addWidget(self.library_panel)\n'
        '        bottom.setSizes([560, 520])\n'
        '        left_split.addWidget(bottom)\n'
        '        left_split.setSizes([430, 380])\n',
        1),
    # ③ 页签文案
    (
        '                ("📁 素材 · 曲库 · 曲风", "演出前准备：素材库 / 音乐曲库 / 曲风映射"),\n',
        '                ("🎵 曲库 · 曲风", "演出前准备：音乐曲库（导入音乐 + 扫描分析曲风）/ 曲风映射"),\n',
        1),
    # ④ 第 2 页内容
    (
        '        # ---- 第 2 页：素材 · 曲库 · 曲风（素材库独占整幅宽）----\n'
        '        p2 = QWidget()\n'
        '        l2 = QVBoxLayout(p2)\n'
        '        l2.setContentsMargins(0, 0, 0, 0)\n'
        '        l2.setSpacing(4)\n'
        '        h2 = QHBoxLayout()\n'
        '        t2 = QLabel(T("素材库"))\n'
        '        t2.setStyleSheet("color:" + theme.V("muted") + ";font-weight:bold;")\n'
        '        h2.addWidget(t2)\n'
        '        # ⚠ 素材库搬到这一页之后，就没法跨页拖到图层了（拖拽要求两个控件同时可见）\n'
        '        #   ⇒ 明确写出替代操作，否则老用户会以为"拖不动了 = 坏了"\n'
        '        self.lbl_page2_hint = QLabel(T("双击缩略图 = 立即播放到当前图层；右键可「添加到图层」"))\n'
        '        self.lbl_page2_hint.setStyleSheet("color:" + theme.V("muted") + ";font-size:11px;")\n'
        '        h2.addWidget(self.lbl_page2_hint)\n'
        '        h2.addStretch(1)\n'
        '        self.btn_p2_music = QPushButton(T("🎵 音乐曲库"))\n'
        '        self.btn_p2_music.setMinimumHeight(26)\n'
        '        self.btn_p2_music.setToolTip(T("导入音乐、扫描分析曲风（演出前准备）"))\n'
        '        self.btn_p2_music.clicked.connect(self.open_music_library)\n'
        '        h2.addWidget(self.btn_p2_music)\n'
        '        self.btn_p2_gvmap = QPushButton(T("曲风映射"))\n'
        '        self.btn_p2_gvmap.setMinimumHeight(26)\n'
        '        self.btn_p2_gvmap.setToolTip(T("编辑曲风→画面标签的绑定关系"))\n'
        '        self.btn_p2_gvmap.clicked.connect(self.open_genre_visual_editor)\n'
        '        h2.addWidget(self.btn_p2_gvmap)\n'
        '        l2.addLayout(h2)\n'
        '        self.library_panel = self._build_library()      # 蓝：素材库\n'
        '        l2.addWidget(self.library_panel, 1)\n'
        '        self.pages.addWidget(p2)\n',
        '        # ---- 第 2 页：曲库 · 曲风（演出前准备；素材库在演出台）----\n'
        '        p2 = QWidget()\n'
        '        l2 = QVBoxLayout(p2)\n'
        '        l2.setContentsMargins(12, 12, 12, 12)\n'
        '        l2.setSpacing(8)\n'
        '        t2 = QLabel(T("🎵 曲库 · 曲风"))\n'
        '        t2.setStyleSheet("font-weight:bold;font-size:15px;color:" + theme.V("text") + ";")\n'
        '        l2.addWidget(t2)\n'
        '        self.lbl_p2_desc = QLabel(T(\n'
        '            "演出前的准备工作都在这里：导入音乐、扫描分析曲风（每首歌的曲风 + 节拍网格），"\n'
        '            "以及编辑「曲风 → 画面标签」的绑定关系。\\n"\n'
        '            "素材库在「🎬 演出台」上，演出中随时能取素材、直接拖进图层。")\n'
        '        self.lbl_p2_desc.setWordWrap(True)\n'
        '        self.lbl_p2_desc.setStyleSheet("color:" + theme.V("muted") + ";")\n'
        '        l2.addWidget(self.lbl_p2_desc)\n'
        '        row2 = QHBoxLayout()\n'
        '        self.btn_p2_music = QPushButton(T("🎵 音乐曲库…"))\n'
        '        self.btn_p2_music.setMinimumHeight(34)\n'
        '        self.btn_p2_music.setToolTip(T("导入音乐、扫描分析曲风（演出前准备）"))\n'
        '        self.btn_p2_music.clicked.connect(self.open_music_library)\n'
        '        row2.addWidget(self.btn_p2_music)\n'
        '        self.btn_p2_gvmap = QPushButton(T("曲风映射…"))\n'
        '        self.btn_p2_gvmap.setMinimumHeight(34)\n'
        '        self.btn_p2_gvmap.setToolTip(T("编辑曲风→画面标签的绑定关系"))\n'
        '        self.btn_p2_gvmap.clicked.connect(self.open_genre_visual_editor)\n'
        '        row2.addWidget(self.btn_p2_gvmap)\n'
        '        row2.addStretch(1)\n'
        '        l2.addLayout(row2)\n'
        '        self.lbl_p2_stat = QLabel("")\n'
        '        self.lbl_p2_stat.setStyleSheet("color:" + theme.V("muted") + ";font-size:11px;")\n'
        '        l2.addWidget(self.lbl_p2_stat)\n'
        '        l2.addStretch(1)\n'
        '        self.pages.addWidget(p2)\n'
        '        self._sync_p2_stat()\n',
        1),
], adds=[
    ('    def _ensure_cue_editor(self):\n', '''    def _sync_p2_stat(self):
        """第 2 页的曲库概况（只读一行）：已导入多少首、其中多少首有节拍网格。"""
        try:
            lib = list(self.cfg.get("music_library") or [])
            meta = self.cfg.get("music_meta") or {}
            with_grid = sum(1 for p in lib
                            if isinstance(meta.get(p), dict) and meta[p].get("grid"))
            self.lbl_p2_stat.setText(Tf(
                "曲库：已导入 {} 首；其中 {} 首已有节拍网格（八拍乐句对齐要用它）",
                len(lib), with_grid))
        except Exception:                                         # noqa: BLE001
            pass

'''),
])

if FAILS:
    print("\n!! 补丁未全部应用：")
    for f in FAILS:
        print("---", f)
    sys.exit(1)
print("\n补丁应用完毕。")
