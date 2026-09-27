# -*- coding: utf-8 -*-
"""主界面新布局的面板控件（Arena 风格）

- LayerStackPanel：左"图层头"（可见/Solo/名称/不透明度/混合模式，右键菜单） +
  右"素材格"（一行一个图层，行内横向列出该图层素材，播放高亮、右键菜单、拖动排序、
  接收素材库拖入），两侧共用同一个滚动区，保证行对齐。
- PreviewPanel：输出实时预览 + HUD 浮层（模式/倒计时/BPM/能量）+ 下一个素材预看。
- AudioSourceDialog：音源详细设置（类型/设备/采样率/单声道/重扫/电平表）。
- MiniLevel：工具条上的迷你电平指示灯。
"""
import os

from tags_def import (TAG_CATEGORIES, DYNAMIC_LOW, DYNAMIC_MID, DYNAMIC_HIGH,
                      DYNAMIC_FLICKER)
from PySide6.QtCore import Qt, QSize, Signal, QMimeData, QTimer, QPoint
from PySide6.QtGui import QColor, QPixmap, QImage, QDrag, QAction, QPainter, QPen
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QSlider,
    QComboBox, QScrollArea, QListWidget, QListWidgetItem, QListView, QFrame,
    QAbstractItemView, QMenu, QDialog, QCheckBox, QProgressBar, QDialogButtonBox,
    QSizePolicy, QApplication, QDoubleSpinBox, QSpinBox, QGroupBox, QToolBox,
    QInputDialog, QLineEdit, QRadioButton, QButtonGroup, QSplitter,
)

from engine import BLEND_MODES, BLEND_LABELS, MODE_LABELS
from media_manager import MediaGrid, MediaItem
from i18n import T, Tf, retranslate as i18n_retranslate
import theme

CLIP_MIME = "application/x-autovj-media"
HEADER_W = 456          # 图层头列宽
LIB_THUMB_SIZES = [(96, 54, 112, 82), (160, 90, 178, 138), (240, 135, 262, 194)]


class I18nDialog(QDialog):
    """对话框基类：每次显示前把全部文本刷成当前语言。

    对话框是短命窗口（打开 → 关闭），不必参与「运行中切换语言」的控件树遍历；
    只要保证「打开时是当前语言」即可。放在 showEvent 里做，天然覆盖所有子控件
    （含下拉项、表头、提示），子类一行代码都不用加。
    """

    def showEvent(self, ev):
        try:
            i18n_retranslate(self)
        except Exception:
            pass
        super().showEvent(ev)


class StayOpenMenu(QMenu):
    """checkable 动作点击后不关闭菜单（用于角色多选等场景）"""
    def mouseReleaseEvent(self, e):
        a = self.actionAt(e.pos())
        if a is not None and a.isCheckable():
            # 必须用 trigger()：它会翻转勾选并发出 triggered 信号；
            # toggle() 只翻状态不发信号，connected 的槽永远不会执行（角色切换曾因此失效）
            a.trigger()
            e.accept()
            return
        super().mouseReleaseEvent(e)


class NewLayerDialog(I18nDialog):
    """新建图层对话框：名称 + 类型(手动/自动) + 角色(前景/背景)。自动图层按曲风匹配画面。"""

    KV_DEFAULT_NAME = "Kv 主视觉"

    def __init__(self, parent=None, default_name="图层 1"):
        super().__init__(parent)
        self.setWindowTitle("新建图层")
        self.setMinimumWidth(360)
        self._default_name = default_name
        self._name_touched = False     # 用户是否手动改过名字（改过就不自动覆盖）
        v = QVBoxLayout(self)
        v.setSpacing(8)

        v.addWidget(QLabel("图层名称"))
        self.name_edit = QLineEdit(default_name)
        self.name_edit.selectAll()
        self.name_edit.textEdited.connect(lambda *_: setattr(self, "_name_touched", True))
        v.addWidget(self.name_edit)

        v.addWidget(QLabel("图层类型"))
        self.rb_manual = QRadioButton("手动（自己拖素材）")
        self.rb_auto = QRadioButton("自动（按曲风匹配画面）")
        self.rb_kv = QRadioButton("Kv 主视觉图层（待机层：无声音时切入）")
        self.rb_manual.setChecked(True)
        # 必须显式分组：同一 parent 下的 QRadioButton 会自动互斥成一组，
        # 否则点"前景"会把"自动"取消掉（类型和角色串组）
        self._grp_type = QButtonGroup(self)
        self._grp_type.addButton(self.rb_manual)
        self._grp_type.addButton(self.rb_auto)
        self._grp_type.addButton(self.rb_kv)
        tv = QVBoxLayout()
        tv.setSpacing(4)
        tv.addWidget(self.rb_manual)
        tv.addWidget(self.rb_auto)
        tv.addWidget(self.rb_kv)
        v.addLayout(tv)

        # 角色区（手动/Kv 类型时整体隐藏；仅自动图层需要角色）
        self._role_box = QWidget()
        rv = QVBoxLayout(self._role_box)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(8)
        rv.addWidget(QLabel("图层角色（自动匹配按此挑素材）"))
        self.rb_fg = QRadioButton("前景（特效/覆盖在上层）")
        self.rb_bg = QRadioButton("背景（铺满画面打底）")
        self.rb_bg.setChecked(True)
        self._grp_role = QButtonGroup(self)
        self._grp_role.addButton(self.rb_fg)
        self._grp_role.addButton(self.rb_bg)
        rrow = QHBoxLayout()
        rrow.addWidget(self.rb_fg)
        rrow.addWidget(self.rb_bg)
        rv.addLayout(rrow)
        v.addWidget(self._role_box)

        self._hint = QLabel("")
        self._hint.setStyleSheet("color:#888;font-size:11px;")
        self._hint.setWordWrap(True)
        v.addWidget(self._hint)

        for rb in (self.rb_manual, self.rb_auto, self.rb_kv):
            rb.toggled.connect(self._on_type)
        self._on_type()

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("确定")
        bb.button(QDialogButtonBox.Cancel).setText("取消")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _on_type(self, *_):
        auto = self.rb_auto.isChecked()
        self._role_box.setVisible(auto)
        if auto:
            self._hint.setText("自动图层会随曲风实时替换画面，手动拖入的素材不生效")
        elif self.rb_kv.isChecked():
            self._hint.setText("Kv 主视觉图层：没有声音时把待机画面带进来、有声音时带出去。\n"
                               "全局只能有一个，添加后自动置顶；参数在右侧设置区顶部单独配置。")
        else:
            self._hint.setText("")
        # 名字跟着类型走：选 Kv 时默认名换成「Kv 主视觉」（不再叫「图层 N」），
        # 切回普通图层再换回来。用户手动改过名字就一律不覆盖。
        if not self._name_touched:
            if self.rb_kv.isChecked():
                self.name_edit.setText(self.KV_DEFAULT_NAME)
            else:
                self.name_edit.setText(self._default_name)

    def result_data(self):
        name = self.name_edit.text().strip()
        auto = self.rb_auto.isChecked()
        is_kv = self.rb_kv.isChecked()
        role = "fg" if self.rb_fg.isChecked() else "bg"
        return name, auto, role, is_kv


class TagEditDialog(I18nDialog):
    """分类打勾式标签编辑器：
    - 分类=折叠分区（手动展开/收起，与设置面板一致），整体放得下就滚轮
    - 自动标签也显示在网格里，取消勾选=排除（重扫不复活）
    - 额外标签行：不在词表里的标签（自动=蓝底可取消，手动=可删除）+ 自由输入
    """

    @property
    def STYLE_BASE(self):
        return ("QPushButton{border:1px solid " + theme.V("border") + ";background:" +
                theme.V("btn") + ";color:" + theme.V("muted") +
                ";border-radius:4px;padding:2px 8px;}"
                "QPushButton:checked{background:#1f5f3f;color:#b8ffd9;border-color:#2f9f6f;}")

    @property
    def STYLE_AUTO(self):
        return ("QPushButton{border:1px solid #2f6f9f;background:#1d2c38;color:#8fc7ff;"
                "border-radius:4px;padding:2px 8px;}"
                "QPushButton:checked{border:1px solid #2f9f6f;background:#1f5f3f;color:#b8ffd9;}")

    def __init__(self, main, media):
        super().__init__(main)
        self.main = main
        self.media = media
        self.setWindowTitle(T("编辑标签 - ") + media.name[:24])
        self.setProperty("_i18nDynamic", True)   # 标题含文件名，动态
        self.resize(620, 640)
        lay = QVBoxLayout(self)

        # 最终生效标签集合（保存时据此拆分为 disabled / manual）
        self._state = set(media.all_tags())
        self._vocab = set()
        for _cat, items in TAG_CATEGORIES:
            for zh, _en in items:
                self._vocab.add(zh)

        # 自动行（只读展示）
        if media.tags:
            row = QHBoxLayout()
            row.addWidget(QLabel("自动:"))
            lab = QLabel("·".join(T(t) for t in media.tags[:10]))
            lab.setWordWrap(True)
            lab.setStyleSheet("color:#8fc7ff;")
            lab.setProperty("_i18nDynamic", True)   # 标签展示，语言切换时由 _fill 重建
            row.addWidget(lab, 1)
            lay.addLayout(row)

        # 动态标签行：三档互斥 + 频闪独立（动态词由帧差检测产出，不进内容分类，这里手动可改）
        dyn_row = QHBoxLayout()
        dyn_row.addWidget(QLabel("动态:"))
        self._dyn_btns = {}
        for zh in (DYNAMIC_LOW, DYNAMIC_MID, DYNAMIC_HIGH):
            b = QPushButton(zh)
            b.setCheckable(True)
            b.setFixedHeight(26)
            b.setChecked(zh in self._state)
            b.setStyleSheet(self.STYLE_AUTO if zh in media.tags else self.STYLE_BASE)
            b.setToolTip("三选一（可全不选）；自动识别的会标蓝，改选后以手动为准")
            b.clicked.connect(lambda _c=False, z=zh: self._on_dyn(z))
            dyn_row.addWidget(b)
            self._dyn_btns[zh] = b
        fb = QPushButton(DYNAMIC_FLICKER)
        fb.setCheckable(True)
        fb.setFixedHeight(26)
        fb.setChecked(DYNAMIC_FLICKER in self._state)
        fb.setStyleSheet(self.STYLE_AUTO if DYNAMIC_FLICKER in media.tags else self.STYLE_BASE)
        fb.setToolTip("频闪（亮暗交替）素材；缓和段选材会避开它")
        fb.clicked.connect(lambda _c=False: self._on_dyn(DYNAMIC_FLICKER))
        dyn_row.addWidget(fb)
        self._dyn_btns[DYNAMIC_FLICKER] = fb
        dyn_row.addStretch(1)
        lay.addLayout(dyn_row)

        # 额外标签行：实时汇总当前勾选的全部标签（点下面分类，这里同步增减）
        self.cat_btns = {}      # zh -> 分类网格按钮（额外行按钮不进此字典）
        self._order = list(media.all_tags())   # 稳定显示顺序
        self._extra_next = 0
        ex_box = QVBoxLayout()
        ex_head = QHBoxLayout()
        ex_head.addWidget(QLabel("额外:"))
        self.extra_grid = QGridLayout()
        ex_head.addLayout(self.extra_grid, 1)
        ex_box.addLayout(ex_head)
        add_row = QHBoxLayout()
        self.extra_input = QLineEdit()
        self.extra_input.setPlaceholderText("输入自定义标签，回车或点添加")
        self.extra_input.returnPressed.connect(self._add_extra)
        b_add = QPushButton("添加")
        b_add.clicked.connect(self._add_extra)
        add_row.addWidget(self.extra_input, 1)
        add_row.addWidget(b_add)
        ex_box.addLayout(add_row)
        lay.addLayout(ex_box)
        self._sync_extra()

        # 折叠分类（手动展开/收起，与设置面板一致）
        self.custom = dict(main.cfg["custom_tags"] or {})
        self._cat_grids = {}
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        v = QVBoxLayout(inner)
        v.setContentsMargins(0, 0, 2, 0)
        for cat, items in TAG_CATEGORIES:
            names = [zh for zh, _en in items] + [t for t in self.custom.get(cat, []) if isinstance(t, str)]
            sec = CollapsibleSection(cat, expanded=False, count=len(names))
            page = QWidget()
            g = QGridLayout(page)
            for i, zh in enumerate(names):
                g.addWidget(self._make_btn(cat, zh), i // 4, i % 4)
            nxt = (len(names) + 3) // 4
            add = QPushButton("+ 新建词条")
            add.clicked.connect(lambda _c=False, c=cat: self._add_custom(c))
            g.addWidget(add, nxt, 0, 1, 2)
            self._cat_grids[cat] = (g, nxt + 1)
            sec.set_content(page)
            v.addWidget(sec)
        v.addStretch(1)
        scroll.setWidget(inner)
        lay.addWidget(scroll, 1)

        btns = QHBoxLayout()
        ok = QPushButton("保存")
        ok.clicked.connect(self._save)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(ok)
        btns.addWidget(cancel)
        lay.addLayout(btns)

    # ---- 按钮 ----
    def _make_btn(self, cat, zh):
        b = QPushButton(zh)
        b.setCheckable(True)
        b.setChecked(zh in self._state)
        b.setFixedHeight(26)
        is_auto = zh in self.media.tags
        b.setStyleSheet(self.STYLE_AUTO if is_auto else self.STYLE_BASE)
        if is_auto:
            b.setToolTip("自动识别——取消勾选即排除，重扫不会恢复")
        b.clicked.connect(lambda _c=False, z=zh: self._on_toggle(z, b.isChecked()))
        self.cat_btns[zh] = b
        return b

    def _on_toggle(self, zh, checked):
        if checked:
            self._state.add(zh)
            if zh not in self._order:
                self._order.append(zh)
        else:
            self._state.discard(zh)
        self._sync_extra()

    def _on_dyn(self, zh):
        """动态标签互斥：三档单选（可全不选），频闪独立开关。状态走 _state，保存复用 _save 的
        disabled/manual 拆分——取消自动识别的动态词会进 disabled（重扫不复活），改选的进 manual。"""
        b = self._dyn_btns[zh]
        if zh == DYNAMIC_FLICKER:
            if b.isChecked():
                self._state.add(zh)
            else:
                self._state.discard(zh)
        elif b.isChecked():
            for other in (DYNAMIC_LOW, DYNAMIC_MID, DYNAMIC_HIGH):
                if other != zh:
                    ob = self._dyn_btns[other]
                    ob.blockSignals(True)
                    ob.setChecked(False)
                    ob.blockSignals(False)
                self._state.discard(other)
            self._state.add(zh)
        else:
            self._state.discard(zh)
        self._sync_extra()

    def _extra_add_btn(self, zh):
        b = QPushButton(zh)
        b.setCheckable(True)
        b.setChecked(True)
        b.setFixedHeight(26)
        b.setStyleSheet(self.STYLE_BASE)
        b.setToolTip("已勾选——点一下取消")
        b.clicked.connect(lambda _c=False, z=zh: self._remove_extra(z))
        self.extra_grid.addWidget(b, self._extra_next // 4, self._extra_next % 4)
        self._extra_next += 1

    def _remove_extra(self, zh):
        """点额外行的标签 = 取消勾选（同步分类网格里的按钮状态）"""
        self._state.discard(zh)
        b = self.cat_btns.get(zh)
        if b is not None:
            b.blockSignals(True)
            b.setChecked(False)
            b.blockSignals(False)
        self._sync_extra()

    def _sync_extra(self):
        """额外行实时重建 = 当前勾选的全部标签（动态词除外——上面「动态」行已单独展示）"""
        while self.extra_grid.count():
            it = self.extra_grid.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        self._extra_next = 0
        for zh in self._order:
            if zh in self._state and zh not in (DYNAMIC_LOW, DYNAMIC_MID,
                                                DYNAMIC_HIGH, DYNAMIC_FLICKER):
                self._extra_add_btn(zh)

    def _add_extra(self):
        t = (self.extra_input.text() or "").strip()
        if not t:
            return
        self.extra_input.clear()
        if t in self._state:
            return
        self._state.add(t)
        if t not in self._order:
            self._order.append(t)
        # 词表里已有的词 → 同步分类网格按钮
        b = self.cat_btns.get(t)
        if b is not None:
            b.blockSignals(True)
            b.setChecked(True)
            b.blockSignals(False)
        self._sync_extra()

    def _add_custom(self, cat):
        text, ok = QInputDialog.getText(self, T("新建词条"),
                                        Tf("在「{}」分类下新建标签：", T(cat)))
        if not ok:
            return
        t = text.strip()
        if not t:
            return
        if t in self.cat_btns:
            self.cat_btns[t].setChecked(True)
            self._state.add(t)
            if t not in self._order:
                self._order.append(t)
            self._sync_extra()
            return
        lst = self.custom.setdefault(cat, [])
        lst.append(t)
        self.main.cfg["custom_tags"] = self.custom
        self.main.cfg.save()
        g, nxt = self._cat_grids[cat]
        g.addWidget(self._make_btn(cat, t), nxt // 4, nxt % 4)
        self._cat_grids[cat] = (g, nxt + 1)
        self._state.add(t)
        if t not in self._order:
            self._order.append(t)
        self._sync_extra()

    def _save(self):
        m = self.media
        m.disabled_tags = [t for t in m.tags if t not in self._state]
        m.manual_tags = [t for t in self._state if t not in set(m.tags)]
        self.main.cfg["clip_tags_disabled"][m.path] = list(m.disabled_tags)
        self.main.cfg["clip_tags_manual"][m.path] = list(m.manual_tags)
        self.main.cfg.save()
        self.main._fill_tag_combo()
        self.main.refresh_grid()
        self.accept()


class TagFilterDialog(I18nDialog):
    """素材库标签筛选：按**分类**分组多选（和「编辑标签」「曲风映射」同一套大类折叠交互）+ 全选/清空"""

    STYLE = property(lambda self: ("QPushButton{border:1px solid " + theme.V("border") + ";background:" + theme.V("btn") + ";color:" + theme.V("text") + ";"
             "border-radius:4px;padding:2px 8px;}"
             "QPushButton:checked{background:#1f4f6f;color:#a8d8ff;border-color:#2f8fcf;}"))

    def __init__(self, main, selected):
        super().__init__(main)
        self.setWindowTitle("标签筛选（可多选）")
        self.resize(620, 560)
        self.selected = set(selected or [])
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("勾选要显示的标签（素材含任一选中标签即显示；不选=全部）："))

        # 素材库里实际出现过的标签
        tags = set()
        for m in main.library.values():
            for t in m.all_tags():
                if t:
                    tags.add(t)
        # 按大分类归组；词表外的标签（自动标签/自定义）进「其他」
        vocab = {zh for _cat, items in TAG_CATEGORIES for zh, _en in items}
        self.btns = {}
        inner = QWidget()
        v = QVBoxLayout(inner)
        v.setContentsMargins(0, 0, 2, 0)
        for cat, items in TAG_CATEGORIES:
            names = [zh for zh, _en in items if zh in tags]
            if not names:
                continue
            sec = CollapsibleSection(cat, expanded=False, count=len(names))
            page = QWidget()
            g = QGridLayout(page)
            for i, t in enumerate(names):
                g.addWidget(self._mk_btn(t), i // 4, i % 4)
            sec.set_content(page)
            v.addWidget(sec)
        others = sorted(t for t in tags if t not in vocab)
        if others:
            sec = CollapsibleSection("其他", expanded=True, count=len(others))
            page = QWidget()
            g = QGridLayout(page)
            for i, t in enumerate(others):
                g.addWidget(self._mk_btn(t), i // 4, i % 4)
            sec.set_content(page)
            v.addWidget(sec)
        v.addStretch(1)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        sc.setWidget(inner)
        lay.addWidget(sc, 1)

        row = QHBoxLayout()
        b_all = QPushButton("全选")
        b_all.clicked.connect(lambda: self._set_all(True))
        b_none = QPushButton("清空")
        b_none.clicked.connect(lambda: self._set_all(False))
        row.addWidget(b_all)
        row.addWidget(b_none)
        row.addStretch(1)
        ok = QPushButton("确定")
        ok.clicked.connect(self._save)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        row.addWidget(ok)
        row.addWidget(cancel)
        lay.addLayout(row)

    def _mk_btn(self, tag):
        b = QPushButton(T(tag))
        b.setCheckable(True)
        b.setChecked(tag in self.selected)
        b.setFixedHeight(26)
        b.setStyleSheet(self.STYLE)
        self.btns[tag] = b
        return b

    def _set_all(self, on):
        for b in self.btns.values():
            b.setChecked(on)

    def _save(self):
        self.selected = {t for t, b in self.btns.items() if b.isChecked()}
        self.accept()


class GenreEditDialog(I18nDialog):
    """勾选式曲风编辑（与素材打标同款交互：网格点选）"""

    STYLE = property(lambda self: ("QPushButton{border:1px solid " + theme.V("border") + ";background:" + theme.V("btn") + ";color:" + theme.V("text") + ";"
             "border-radius:4px;padding:2px 8px;}"
             "QPushButton:checked{background:#1f5f3f;color:#b8ffd9;border-color:#2f9f6f;}"))

    def __init__(self, main, path, current):
        super().__init__(main)
        self.main = main
        self.path = path
        self.setWindowTitle(T("纠正曲风 - ") + os.path.basename(path)[:28])
        self.resize(600, 560)
        lay = QVBoxLayout(self)
        hint = QLabel("勾选该曲的曲风（可多选；手动指定优先于自动识别）。点大类展开细分：")
        lay.addWidget(hint)

        # 大类分组（内置 + 用户在「曲风映射」里新建的自定义曲风，后者也在这里可选）
        from genre_keywords import grouped_genres
        groups = grouped_genres(main.cfg["custom_genres"] or {})
        self.btns = {}
        cur = set(current or [])
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        v = QVBoxLayout(inner)
        v.setContentsMargins(0, 0, 2, 0)
        for group_name, names in groups:
            # 已勾选的词所在分组默认展开，方便直接看到
            expand = any(n in cur for n in names)
            sec = CollapsibleSection(group_name, expanded=expand, count=len(names))
            page = QWidget()
            g = QGridLayout(page)
            for i, name in enumerate(names):
                b = QPushButton(name)
                b.setCheckable(True)
                b.setChecked(name in cur)
                b.setFixedHeight(26)
                b.setStyleSheet(self.STYLE)
                g.addWidget(b, i // 4, i % 4)
                self.btns[name] = b
            sec.set_content(page)
            v.addWidget(sec)
        v.addStretch(1)
        sc.setWidget(inner)
        lay.addWidget(sc, 1)

        btns = QHBoxLayout()
        ok = QPushButton("保存")
        ok.clicked.connect(self._save)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(ok)
        btns.addWidget(cancel)
        lay.addLayout(btns)

    def _save(self):
        zh = [z for z, b in self.btns.items() if b.isChecked()]
        m = self.main.cfg["music_meta"].get(self.path) or {}
        m["genres"] = zh
        m["source"] = "manual"
        self.main.cfg["music_meta"][self.path] = m
        self.main.cfg.save()
        dlg = getattr(self.main, "music_dialog", None)
        if dlg:
            dlg.refresh()
        self.accept()


class GenreVisualEditDialog(I18nDialog):
    """曲风→画面标签 映射编辑器。

    左侧按**大类**分组列出曲风（每个大类末尾有「+ 新建曲风」，新建的曲风会存进
    cfg["custom_genres"]，因此「音乐曲库 → 纠正曲风」里也能选到它）；
    右侧顶部是当前曲风**已选的词条**（点一下即取消），下面是按分类勾选的画面标签。
    """

    STYLE = property(lambda self: ("QPushButton{border:1px solid " + theme.V("border") + ";background:" + theme.V("btn") + ";color:" + theme.V("text") + ";"
             "border-radius:4px;padding:2px 8px;}"
             "QPushButton:checked{background:#1f5f3f;color:#b8ffd9;border-color:#2f9f6f;}"))

    @property
    def SEL_STYLE(self):
        """左侧曲风选择按钮（单选态）"""
        return ("QPushButton{border:1px solid " + theme.V("border") + ";background:" +
                theme.V("btn") + ";color:" + theme.V("text") +
                ";border-radius:4px;padding:2px 8px;text-align:left;}"
                "QPushButton:checked{background:#2f4f7f;color:#dcf1ff;border-color:#4a9fe0;}")

    @property
    def CHIP_STYLE(self):
        """已选词条小按钮（点击取消）"""
        return ("QPushButton{border:1px solid #2f9f6f;background:#1f5f3f;color:#b8ffd9;"
                "border-radius:9px;padding:1px 8px;}"
                "QPushButton:hover{background:#2a6f4f;}")

    def __init__(self, main):
        super().__init__(main)
        self.main = main
        from match_engine import get_genre_visual_map
        self.map = {g: set(t) for g, t in get_genre_visual_map().items()}
        self._orig_keys = set(self.map)     # 原有键：保存时即使标签为空也保留
        self.custom = {k: list(v) for k, v in (main.cfg["custom_genres"] or {}).items()}
        self.cur_genre = None
        self.setWindowTitle("曲风 → 画面标签映射")
        self.resize(840, 620)
        lay = QVBoxLayout(self)
        hint = QLabel("左侧按大类选一个曲风（每个大类末尾有「+ 新建曲风」），"
                      "在右侧勾选它对应的画面标签。自动匹配时曲风先映射成这些标签，"
                      "再和素材内容标签求交集挑素材。")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        split = QSplitter(Qt.Horizontal)

        # ---------- 左：曲风（按大类折叠） ----------
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.genre_scroll = QScrollArea()
        self.genre_scroll.setWidgetResizable(True)
        self.genre_scroll.setFrameShape(QFrame.NoFrame)
        lv.addWidget(self.genre_scroll, 1)
        lrow = QHBoxLayout()
        b_del = QPushButton("删除曲风")
        b_del.setToolTip("删除当前曲风在映射表里的条目；自定义曲风同时从大类里移除")
        b_del.clicked.connect(self._del_genre)
        b_reset = QPushButton("恢复默认")
        b_reset.setToolTip("丢弃自定义映射，恢复软件内置的默认曲风→画面标签表")
        b_reset.clicked.connect(self._reset_defaults)
        lrow.addWidget(b_del)
        lrow.addWidget(b_reset)
        lrow.addStretch(1)
        lv.addLayout(lrow)
        split.addWidget(left)

        # ---------- 右：已选（置顶）+ 标签分类 ----------
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        sel_row = QHBoxLayout()
        self.lbl_sel = QLabel("已选：")
        sel_row.addWidget(self.lbl_sel, 0, Qt.AlignTop)
        self.sel_wrap = QWidget()
        self.sel_grid = QGridLayout(self.sel_wrap)
        self.sel_grid.setContentsMargins(0, 0, 0, 0)
        self.sel_grid.setSpacing(4)
        sel_row.addWidget(self.sel_wrap, 1)
        rv.addLayout(sel_row)

        self.btns = {}
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        iv = QVBoxLayout(inner)
        iv.setContentsMargins(0, 0, 2, 0)
        for cat, items in TAG_CATEGORIES:
            sec = CollapsibleSection(cat, expanded=False, count=len(items))
            page = QWidget()
            g = QGridLayout(page)
            for i, (zh, _en) in enumerate(items):
                b = QPushButton(zh)
                b.setCheckable(True)
                b.setFixedHeight(26)
                b.setStyleSheet(self.STYLE)
                b.toggled.connect(lambda _c, name=zh: self._on_tag(name))
                g.addWidget(b, i // 4, i % 4)
                self.btns[zh] = b
            sec.set_content(page)
            iv.addWidget(sec)
        iv.addStretch(1)
        sc.setWidget(inner)
        rv.addWidget(sc, 1)
        split.addWidget(right)
        split.setSizes([320, 520])
        lay.addWidget(split, 1)

        btnrow = QHBoxLayout()
        ok = QPushButton("保存")
        ok.clicked.connect(self._save)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btnrow.addStretch(1)
        btnrow.addWidget(ok)
        btnrow.addWidget(cancel)
        lay.addLayout(btnrow)

        self._rebuild_genres()

    # ---------- 左侧：按大类重建曲风列表 ----------
    def _rebuild_genres(self, expand_group=None):
        """重建左侧大类树。保留当前选中的曲风。"""
        from genre_keywords import grouped_genres
        prev = self.cur_genre
        groups = grouped_genres(self.custom)
        inner = QWidget()
        v = QVBoxLayout(inner)
        v.setContentsMargins(0, 0, 2, 0)
        v.setSpacing(4)
        self._sel_grp = QButtonGroup(self)
        self._sel_grp.setExclusive(True)
        self.genre_btns = {}
        first = None
        for gname, names in groups:
            # 含当前选中项的组默认展开；否则只有第一个有内容的组展开
            expand = bool(prev and prev in names) or (expand_group == gname) or \
                     (expand_group is None and first is None and names)
            sec = CollapsibleSection(gname, expanded=bool(expand), count=len(names))
            page = QWidget()
            g = QGridLayout(page)
            for i, name in enumerate(names):
                b = QPushButton(name)
                b.setCheckable(True)
                b.setFixedHeight(26)
                b.setStyleSheet(self.SEL_STYLE)
                b.clicked.connect(lambda _c=False, n=name: self._select_genre(n))
                self._sel_grp.addButton(b)
                g.addWidget(b, i // 2, i % 2)
                self.genre_btns[name] = b
                if first is None:
                    first = name
            nxt = (len(names) + 1) // 2
            add = QPushButton("+ 新建曲风")
            add.setToolTip(Tf("在「{}」里新建一个曲风（会出现在纠正曲风里）", gname))
            add.clicked.connect(lambda _c=False, g=gname: self._add_genre(g))
            g.addWidget(add, nxt, 0, 1, 2)
            sec.set_content(page)
            v.addWidget(sec)
        v.addStretch(1)
        self.genre_scroll.setWidget(inner)

        target = prev if (prev and prev in self.genre_btns) else first
        if target:
            self.genre_btns[target].setChecked(True)
            self._select_genre(target)

    def _select_genre(self, name):
        self.cur_genre = name
        b = self.genre_btns.get(name)
        if b is not None and not b.isChecked():
            b.setChecked(True)
        tags = self.map.get(name, set())
        for tname, tb in self.btns.items():
            tb.blockSignals(True)
            tb.setChecked(tname in tags)
            tb.blockSignals(False)
        self._sync_sel_row()

    def _sync_sel_row(self):
        """顶部「已选」：当前曲风已勾选的词条，点一下即取消"""
        while self.sel_grid.count():
            it = self.sel_grid.takeAt(0)
            wdg = it.widget()
            if wdg is not None:
                # 必须先脱离父级再 deleteLater：只 deleteLater 的话控件在被真正销毁前
                # 仍挂在界面上（旧的一批「已选」小按钮会残留在原位）
                wdg.setParent(None)
                wdg.deleteLater()
        names = [n for n in self.btns if n in self.map.get(self.cur_genre or "", set())]
        if not names:
            hint = QLabel("（未选，下面勾选画面标签）")
            hint.setStyleSheet("color:#888;")
            self.sel_grid.addWidget(hint, 0, 0)
            return
        for i, n in enumerate(names):
            chip = QPushButton(T(n) + "  ×")
            chip.setStyleSheet(self.CHIP_STYLE)
            chip.setFixedHeight(22)
            chip.setToolTip("点击取消该标签")
            chip.clicked.connect(lambda _c=False, name=n: self._remove_tag(name))
            self.sel_grid.addWidget(chip, i // 4, i % 4)

    def _remove_tag(self, name):
        if not self.cur_genre:
            return
        self.map.setdefault(self.cur_genre, set()).discard(name)
        b = self.btns.get(name)
        if b is not None:
            b.blockSignals(True)
            b.setChecked(False)
            b.blockSignals(False)
        self._sync_sel_row()

    def _on_tag(self, name):
        if not self.cur_genre:
            return
        if self.btns[name].isChecked():
            self.map.setdefault(self.cur_genre, set()).add(name)
        else:
            self.map.setdefault(self.cur_genre, set()).discard(name)
        self._sync_sel_row()

    # ---------- 新建 / 删除 / 恢复 ----------
    def _add_genre(self, group):
        name, ok = QInputDialog.getText(
            self, "新建曲风", Tf("在「{}」里新建曲风的英文名（如 Frenchcore）：", group))
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in self.genre_btns:
            return
        self.custom.setdefault(group, []).append(name)
        self.map.setdefault(name, set())
        self._rebuild_genres(expand_group=group)
        self._select_genre(name)

    def _del_genre(self):
        if not self.cur_genre:
            return
        name = self.cur_genre
        self.map.pop(name, None)
        self._orig_keys.discard(name)
        for g in list(self.custom.keys()):
            self.custom[g] = [c for c in self.custom[g] if c != name]
            if not self.custom[g]:
                self.custom.pop(g, None)
        self.cur_genre = None
        self._rebuild_genres()

    def _reset_defaults(self):
        from PySide6.QtWidgets import QMessageBox
        ret = QMessageBox.question(
            self, "恢复默认",
            "确定恢复内置的默认曲风→画面标签映射吗？\n你保存过的自定义映射将被清除。",
            QMessageBox.Yes | QMessageBox.Cancel)
        if ret != QMessageBox.Yes:
            return
        from match_engine import GENRE_TO_VISUAL
        self.map = {g: set(t) for g, t in GENRE_TO_VISUAL.items()}
        self._orig_keys = set(self.map)
        self._rebuild_genres()

    def _save(self):
        # 只保存「有标签的」+「原本就存在的」+「本次新建的曲风」三类，
        # 避免把大类里那一百多个没配过的曲风全写进映射表（会把默认兜底也顶掉）
        custom_names = {c for names in self.custom.values() for c in names}
        out = {}
        for g, tags in self.map.items():
            if tags or g in self._orig_keys or g in custom_names:
                out[g] = sorted(tags)
        self.main.cfg["genre_visual_map"] = out
        self.main.cfg["custom_genres"] = {k: list(v) for k, v in self.custom.items() if v}
        from match_engine import set_custom_visual_map
        set_custom_visual_map(out)
        self.main.cfg.save()
        self.main.engine._last_genres = None
        self.main.engine._auto_refresh_t = 0.0
        self.accept()


class MusicLibraryDialog(I18nDialog):
    """音乐曲库：导入音乐 → 扫描分析（建指纹+查曲风）→ 列表展示/纠正曲风"""

    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("音乐曲库")
        self.resize(660, 540)
        lay = QVBoxLayout(self)

        row = QHBoxLayout()
        b_dir = QPushButton("导入音乐文件夹")
        b_file = QPushButton("导入音乐文件")
        self.btn_scan = QPushButton("扫描分析")
        self.btn_scan.setToolTip("逐首建指纹 + 查曲风（后台、增量）")
        b_rescan = QPushButton("全量重扫")
        b_rescan.setToolTip("忽略已有结果，重新分析全部")
        b_clear = QPushButton("清除分析数据")
        b_clear.setToolTip("删除全部指纹与自动识别的曲风（可选保留手动设置）")
        b_dir.clicked.connect(self.main.import_music_dir)
        b_file.clicked.connect(self.main.import_music_files)
        self.btn_scan.clicked.connect(lambda: self.main.scan_music(force=False))
        b_rescan.clicked.connect(lambda: self.main.scan_music(force=True))
        b_clear.clicked.connect(self.main.show_clear_music_analysis)
        row.addWidget(b_dir)
        row.addWidget(b_file)
        row.addWidget(self.btn_scan)
        row.addWidget(b_rescan)
        row.addWidget(b_clear)
        row.addStretch(1)
        lay.addLayout(row)

        self.progress = QProgressBar()
        self.progress.hide()
        lay.addWidget(self.progress)

        self.list = QListWidget()
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._ctx_menu)
        lay.addWidget(self.list, 1)

        self.lbl = QLabel("")
        self.lbl.setStyleSheet("color:#7ab;")
        lay.addWidget(self.lbl)

        self.refresh()

    def refresh(self):
        self.list.clear()
        meta = self.main.cfg["music_meta"] or {}
        n = 0
        for p in self.main.cfg["music_library"]:
            m = meta.get(p) or {}
            genres = "、".join(m.get("genres") or []) or "未分析"
            src = m.get("source") or ""
            txt = f"{os.path.basename(p)}    →    {genres}    ({src})"
            it = QListWidgetItem(txt)
            it.setData(Qt.UserRole, p)
            self.list.addItem(it)
            n += 1
        done = sum(1 for p in self.main.cfg["music_library"] if p in meta)
        self.lbl.setText(Tf("共 {} 首，已分析 {} 首", n, done))

    def _ctx_menu(self, pos):
        it = self.list.itemAt(pos)
        if it is None:
            return
        path = it.data(Qt.UserRole)
        menu = QMenu(self)
        edit = menu.addAction("纠正曲风…")
        rescan = menu.addAction("重新分析扫描此曲目")
        info = menu.addAction("此曲目信息")
        menu.addSeparator()
        rm = menu.addAction(theme.danger_icon(), "从曲库移除")
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


class LibraryGrid(MediaGrid):
    """素材库网格：支持拖出到图层行 + 从资源管理器拖入导入 + 自定义右键菜单"""
    files_dropped = Signal(list)   # 外部拖入的文件/文件夹路径列表

    def __init__(self, main, parent=None):
        super().__init__(parent)
        self.main = main
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.CopyAction)

    # ---- 外部拖入（资源管理器 → 素材库） ----
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e):
        if e.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in e.mimeData().urls() if u.toLocalFile()]
            if paths:
                self.files_dropped.emit(paths)
            e.acceptProposedAction()
        else:
            super().dropEvent(e)

    def mousePressEvent(self, e):
        super().mousePressEvent(e)
        if e.button() == Qt.LeftButton:
            self._press_pos = e.position().toPoint()
            self._press_item = self.itemAt(self._press_pos)

    def mouseMoveEvent(self, e):
        # 自己启动拖动：不依赖 QAbstractItemView 内部条件，保证一定能拖
        if (self._press_item is not None and (e.buttons() & Qt.LeftButton)
                and (e.position().toPoint() - self._press_pos).manhattanLength()
                >= QApplication.startDragDistance()):
            it = self._press_item
            self._press_item = None
            self._begin_drag(it)
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._press_item = None
        super().mouseReleaseEvent(e)

    def _begin_drag(self, item):
        m = item.data(Qt.UserRole)
        if m is None:
            return
        self._drag_paths = [m.path]
        md = QMimeData()
        md.setData(CLIP_MIME, m.path.encode("utf-8"))
        md.setText(m.path)
        drag = QDrag(self)
        drag.setMimeData(md)
        pm = m.pixmap(96, 54)
        if pm is not None and not pm.isNull():
            drag.setPixmap(pm)
            drag.setHotSpot(QPoint(pm.width() // 2, pm.height() // 2))
        try:
            drag.exec(Qt.MoveAction)
        finally:
            self._drag_paths = []

    def startDrag(self, actions):
        self._drag_paths = [it.data(Qt.UserRole).path for it in self.selectedItems()]
        if not self._drag_paths:
            return
        super().startDrag(actions)
        self._drag_paths = []

    def mimeData(self, items):
        md = QMimeData()
        paths = [it.data(Qt.UserRole).path for it in items]
        md.setData(CLIP_MIME, ";".join(paths).encode("utf-8"))
        md.setText("\n".join(paths))
        return md

    def apply_thumb_size(self, idx):
        idx = max(0, min(2, int(idx)))
        iw, ih, gw, gh = LIB_THUMB_SIZES[idx]
        self.setIconSize(QSize(iw, ih))
        self.setGridSize(QSize(gw, gh))
        self.main.cfg["ui"]["thumb_size"] = idx
        self.main.cfg.save()
        # 按新尺寸重新生成图标（否则只有间距变化、图标还是旧的）
        self.main.refresh_grid(icons_only=True)

    def _menu(self, pos):
        sel = [it.data(Qt.UserRole) for it in self.selectedItems()]
        menu = QMenu(self)
        menu.setToolTipsVisible(True)   # ★ 长条目拆短后，说明挪进悬停提示

        add_menu = menu.addMenu("添加到图层")
        for i, lay in enumerate(self.main.engine.layers):
            auto = getattr(lay, "auto_mode", False)
            a = add_menu.addAction(lay.name + (T("  [自动]") if auto else ""))
            a.setEnabled(not auto)   # 自动图层素材由曲风匹配管理，不接受手动添加
            a.triggered.connect(lambda _c=False, ii=i, ms=list(sel): self.main.add_to_layer(ii, ms))

        play_menu = menu.addMenu("播放到图层")
        play_menu.setToolTip(T("立即切换，不等下一拍"))
        for i, lay in enumerate(self.main.engine.layers):
            auto = getattr(lay, "auto_mode", False)
            a = play_menu.addAction(lay.name + (T("  [自动]") if auto else ""))
            a.setEnabled(not auto)
            a.triggered.connect(lambda _c=False, ii=i, ms=list(sel): self.main.play_to_layer(ii, ms))

        menu.addSeparator()
        role_menu = StayOpenMenu(T("角色"))
        menu.addMenu(role_menu)
        for key, name in (("fg", "前景"), ("bg", "背景")):
            a = role_menu.addAction(T(name))
            a.setCheckable(True)
            if sel:
                # 单选：勾选 = 当前角色是它（二选一）
                a.setChecked(all(bool(m.roles.get(key)) and not m.roles.get("bg" if key == "fg" else "fg")
                                 for m in sel))
            a.triggered.connect(lambda _c=False, k=key, ms=list(sel): self.main.toggle_role(k, ms))
        a_excl = menu.addAction("排除自动打标/匹配")
        a_excl.setToolTip(T("logo 等固定素材：不参与自动打标与匹配"))
        a_excl.setCheckable(True)
        if sel:
            a_excl.setChecked(all(bool(getattr(m, "excluded", False)) for m in sel))
        a_excl.setEnabled(bool(sel))
        a_excl.triggered.connect(lambda _c=False, ms=list(sel): self.main.toggle_exclude(ms))
        menu.addSeparator()          # 分组：打标类
        scan_one = menu.addAction("扫描打标此素材")
        scan_one.setEnabled(bool(sel))
        edit_tags = menu.addAction("编辑标签…")
        edit_tags.setEnabled(bool(sel))
        rescan_all = menu.addAction("全量重扫打标")
        rescan_all.setToolTip(T("含已打标的一起重扫"))

        menu.addSeparator()          # 分组：危险操作（红点标记）
        rm_layer = menu.addAction(theme.danger_icon(), "从所有图层移除")
        rm_layer.setEnabled(bool(sel))
        rm_lib = menu.addAction(theme.danger_icon(), "从素材库移除")
        rm_lib.setEnabled(bool(sel))

        menu.addSeparator()
        size_menu = menu.addMenu("预览大小")
        acts = []
        cur = int(self.main.cfg["ui"].get("thumb_size", 1))
        for i, name in enumerate(("小", "中", "大")):
            a = size_menu.addAction(T(name))
            a.setCheckable(True)
            a.setChecked(i == cur)
            a.triggered.connect(lambda _c=False, ii=i: self.apply_thumb_size(ii))
            acts.append(a)

        menu.addSeparator()
        cache_menu = menu.addMenu("缩略图缓存")
        a_size = cache_menu.addAction("查看占用…")
        a_clear = cache_menu.addAction("清理缩略图缓存")
        imp = menu.addAction("导入文件夹…")
        impf = menu.addAction("导入文件…")
        imp_set = menu.addAction("导入素材设置…")
        imp_set.setToolTip("修改导入方式（复制/移动/仅引用），即使之前勾选了「记住我的选择」")
        setdir = menu.addAction("设置素材库目录…")

        # 右键菜单是临时构造的短命对象：弹出前刷一次语言（词表机制）
        i18n_retranslate(menu)
        act = menu.exec(self.mapToGlobal(pos))
        if act is None:
            return
        if act == rm_layer:
            for m in sel:
                self.main.remove_from_all_layers(m)
        elif act == scan_one:
            self.main.scan_tags(paths=[m.path for m in sel])
        elif act == edit_tags:
            if sel:
                self.main.edit_tags(sel[0])
        elif act == rescan_all:
            self.main.scan_tags(force=True)
        elif act == setdir:
            self.main.set_library_dir()
        elif act == rm_lib:
            self.main.confirm_remove_media(list(sel))
        elif act == imp_set:
            self.main.open_import_settings()
        elif act == a_size:
            self.main.show_cache_info()
        elif act == a_clear:
            self.main.clear_thumb_cache()
        elif act == imp:
            self.main.import_folder()
        elif act == impf:
            self.main.import_files()
ROW_H = 84              # 每行高度
CELL_W, CELL_H = 104, 58


class _DropViewport(QWidget):
    """自定义 viewport：直接接管拖放事件（不依赖事件过滤器）"""

    def __init__(self, row, parent=None):
        super().__init__(parent)
        self.row = row
        self.setAutoFillBackground(True)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, e):
        self.row._vp_enter(e)

    def dragMoveEvent(self, e):
        self.row._vp_enter(e)

    def dragLeaveEvent(self, e):
        e.ignore()

    def dropEvent(self, e):
        self.row._vp_drop(e)


class ClipRow(QListWidget):
    """一个图层的素材行：横向排列、内部拖动排序、可接收素材库拖入"""

    dropped = Signal(int, list)      # layer_idx, [path...]（外部拖入）
    reordered = Signal(int, list)    # layer_idx, [MediaItem...]（新顺序）
    cross_moved = Signal(int, int, list)   # from_layer, to_layer, [path...]（图层间互拖）
    reorder_requested = Signal(int, list, int)   # layer_idx, [path...], 目标插入位置
    menu_requested = Signal(int, object, QPoint)   # layer_idx, MediaItem/None, global pos

    def __init__(self, layer_idx, parent=None):
        super().__init__(parent)
        self.layer_idx = layer_idx
        self.setViewMode(QListView.IconMode)
        self.setFlow(QListView.LeftToRight)
        self.setWrapping(False)
        self.setResizeMode(QListView.Adjust)
        self.setMovement(QListView.Static)
        self.setIconSize(QSize(CELL_W - 8, CELL_H - 10))
        self.setGridSize(QSize(CELL_W, CELL_H))
        self.setFixedHeight(CELL_H + 14)
        # 单选：ExtendedSelection 会让按住左键变成"拉框多选"，导致拖不动素材
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)   # 内部排序 + 接收外部/其他图层拖入
        self.setDefaultDropAction(Qt.MoveAction)
        self.setAcceptDrops(True)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_menu)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setObjectName("clipRow")
        # 注意：不能写 ::item 规则！一旦用样式表绘制列表项，setBackground() 的高亮会失效
        self._apply_theme()
        theme.register(self, self._apply_theme)   # 主题切换时重设（弱引用，销毁后自动摘除）
        # 拖放事件由自定义 viewport 直接接管（事件过滤器在真机上收不到拖入）
        self._press_pos = None
        self._press_item = None
        self.setViewport(_DropViewport(self))
        self.viewport().installEventFilter(self)   # 双保险
        self._drag_paths = []

    def _apply_theme(self):
        self.setStyleSheet(
            f"#clipRow{{background:{theme.V('row')};border:1px solid {theme.V('border')};}}")

    def _on_menu(self, pos):
        it = self.itemAt(pos)
        m = it.data(Qt.UserRole) if it else None
        self.menu_requested.emit(self.layer_idx, m, self.mapToGlobal(pos))

    # ---- viewport 拖放回调 ----
    def _vp_enter(self, e):
        if e.source() is self:
            e.setDropAction(Qt.MoveAction)
            e.acceptProposedAction()
            return
        md = e.mimeData()
        if md.hasFormat(CLIP_MIME) or md.hasFormat("text/uri-list") or md.hasText():
            e.setDropAction(Qt.CopyAction)
            e.acceptProposedAction()
        else:
            e.ignore()

    def _vp_drop(self, e):
        if self._handle_drop(e):
            return
        e.ignore()

    # ---- 手动接管拖动（保证左键拖素材一定起效果）----
    def mousePressEvent(self, e):
        self._press_pos = None
        self._press_item = None
        if e.button() == Qt.LeftButton:
            pt = e.position().toPoint() if hasattr(e, "position") else e.pos()
            it = self.itemAt(pt)
            if it is not None:
                self._press_item = it
                self._press_pos = pt
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if not (e.buttons() & Qt.LeftButton) or self._press_item is None or self._press_pos is None:
            super().mouseMoveEvent(e)
            return
        pt = e.position().toPoint() if hasattr(e, "position") else e.pos()
        if (pt - self._press_pos).manhattanLength() < QApplication.startDragDistance():
            super().mouseMoveEvent(e)
            return
        # 自行发起拖动
        m = self._press_item.data(Qt.UserRole)
        self._drag_paths = [m.path]
        md = QMimeData()
        md.setData(CLIP_MIME, m.path.encode("utf-8"))
        md.setText(m.path)
        drag = QDrag(self)
        drag.setMimeData(md)
        pm = m.pixmap(CELL_W - 8, CELL_H - 10)
        if not pm.isNull():
            drag.setPixmap(pm)
            drag.setHotSpot(QPoint(pm.width() // 2, pm.height() // 2))
        drag.exec(Qt.CopyAction | Qt.MoveAction, Qt.CopyAction)
        self._drag_paths = []
        self._press_item = None
        self._press_pos = None
        e.accept()

    # ---- 拖放 ----
    def eventFilter(self, obj, ev):
        """拦截 viewport 上的拖放事件（Qt 把拖放投递给 viewport，不装过滤器收不到）"""
        from PySide6.QtCore import QEvent
        if obj is self.viewport():
            t = ev.type()
            if t in (QEvent.DragEnter, QEvent.DragMove):
                if self._accept_external(ev):
                    return True
            elif t == QEvent.Drop:
                if self._handle_drop(ev):
                    return True
        return super().eventFilter(obj, ev)

    def mousePressEvent(self, e):
        super().mousePressEvent(e)
        if e.button() == Qt.LeftButton:
            self._press_pos = e.position().toPoint()
            self._press_item = self.itemAt(self._press_pos)

    def mouseMoveEvent(self, e):
        # 自己启动拖动：不依赖 QAbstractItemView 内部条件，保证一定能拖
        if (self._press_item is not None and (e.buttons() & Qt.LeftButton)
                and (e.position().toPoint() - self._press_pos).manhattanLength()
                >= QApplication.startDragDistance()):
            it = self._press_item
            self._press_item = None
            self._begin_drag(it)
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._press_item = None
        super().mouseReleaseEvent(e)

    def _begin_drag(self, item):
        m = item.data(Qt.UserRole)
        if m is None:
            return
        self._drag_paths = [m.path]
        md = QMimeData()
        md.setData(CLIP_MIME, m.path.encode("utf-8"))
        md.setText(m.path)
        drag = QDrag(self)
        drag.setMimeData(md)
        pm = m.pixmap(96, 54)
        if pm is not None and not pm.isNull():
            drag.setPixmap(pm)
            drag.setHotSpot(QPoint(pm.width() // 2, pm.height() // 2))
        try:
            drag.exec(Qt.MoveAction)
        finally:
            self._drag_paths = []

    def startDrag(self, actions):
        self._drag_paths = [it.data(Qt.UserRole).path for it in self.selectedItems()]
        if not self._drag_paths:
            return
        super().startDrag(actions)
        self._drag_paths = []

    def mimeData(self, items):
        """必须同时保留 Qt 默认格式：行内排序/移动依赖它，只放自己的格式会导致拖动失败丢素材"""
        md = super().mimeData(items)
        paths = [it.data(Qt.UserRole).path for it in items]
        md.setData(CLIP_MIME, ";".join(paths).encode("utf-8"))
        md.setText("\n".join(paths))
        return md

    def _accept_external(self, e):
        """外部拖入（素材库/资源管理器）：接受；内部拖动交给 Qt 排序"""
        if e.source() is self:
            return False
        md = e.mimeData()
        if md.hasFormat(CLIP_MIME) or md.hasFormat("text/uri-list") or md.hasText():
            e.acceptProposedAction()
            return True
        return False

    def dragEnterEvent(self, e):
        if not self._accept_external(e):
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if not self._accept_external(e):
            super().dragMoveEvent(e)

    def dropEvent(self, e):
        if self._handle_drop(e):
            return
        super().dropEvent(e)
        order = [self.item(i).data(Qt.UserRole) for i in range(self.count())]
        self.reordered.emit(self.layer_idx, order)

    def _handle_drop(self, e) -> bool:
        """返回 True 表示已处理（行内排序 / 外部拖入 / 其他图层拖来）"""
        src = e.source()
        md = e.mimeData()
        # 行内拖动排序：自己处理，保证一定能排序（不依赖 Qt 内部移动）
        if src is self:
            paths = list(self._drag_paths) or [it.data(Qt.UserRole).path for it in self.selectedItems()]
            if not paths:
                return False
            pos = e.position().toPoint() if hasattr(e, "position") else e.pos()
            row = self.indexAt(pos).row()
            if row < 0:
                row = self.count()          # 落在空白处 → 移到末尾
            self.reorder_requested.emit(self.layer_idx, paths, row)
            e.acceptProposedAction()
            return True
        # 其他图层拖来的素材 → 图层间互拖（移动）
        if isinstance(src, ClipRow) and src is not self:
            paths = list(getattr(src, "_drag_paths", []) or [])
            if not paths:
                paths = [it.data(Qt.UserRole).path for it in src.selectedItems()]
            if not paths:
                paths = bytes(md.data(CLIP_MIME)).decode("utf-8", "ignore").split(";")
            paths = [p for p in paths if p]
            if paths:
                self.cross_moved.emit(src.layer_idx, self.layer_idx, paths)
                e.setDropAction(Qt.MoveAction)
                e.accept()
                return True
        # 素材库/资源管理器拖入
        if src is not self:
            paths = []
            if md.hasFormat(CLIP_MIME):
                paths = bytes(md.data(CLIP_MIME)).decode("utf-8", "ignore").split(";")
            elif md.hasFormat("text/uri-list") or md.hasText():
                paths = _paths_from_mime(md)
            paths = [p for p in paths if p and os.path.isabs(p)]
            if paths:
                self.dropped.emit(self.layer_idx, paths)
                e.acceptProposedAction()
                return True
        return False


def _paths_from_mime(md: QMimeData):
    out = []
    if md.hasFormat("text/uri-list"):
        for u in bytes(md.data("text/uri-list")).decode("utf-8", "ignore").splitlines():
            u = u.strip()
            if u.startswith("file:///"):
                out.append(u[8:].replace("/", os.sep))
    if not out and md.hasText():
        for line in md.text().splitlines():
            line = line.strip()
            if line and os.path.isabs(line):
                out.append(line)
    return out


class LayerStackPanel(QWidget):
    """图层栈：左图层头 + 右素材格，共用滚动"""

    layer_pick = Signal(int)
    header_changed = Signal()                 # 任一图层属性被改（由外部保存配置）

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.rows = []            # [(header_frame, ClipRow)]
        self._sig_guard = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)

        bar = QHBoxLayout()
        self.title = QLabel("图层（越靠上显示越靠上）")
        self.title.setStyleSheet("color:" + theme.V("muted") + ";font-weight:bold;")
        bar.addWidget(self.title)
        bar.addStretch(1)
        self.btn_add = QPushButton("+ 图层")
        self.btn_add.clicked.connect(main.layer_add)
        bar.addWidget(self.btn_add)
        lay.addLayout(bar)
        theme.register(self, self.apply_theme)   # 主题切换：重设各图层头样式

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.content = QWidget()
        self.vbox = QVBoxLayout(self.content)
        self.vbox.setContentsMargins(0, 0, 0, 0)
        self.vbox.setSpacing(2)
        self.vbox.addStretch(1)
        self.scroll.setWidget(self.content)
        lay.addWidget(self.scroll, 1)
        # 整个图层栈区域（含空白处/滚动区）都能接收拖放，按落点算最近的图层行
        for w in (self, self.scroll, self.scroll.viewport(), self.content):
            w.setAcceptDrops(True)
            w.installEventFilter(self)

    # ---- 图层头小按钮样式（主题感知；apply_theme 时对已有行重设） ----
    @staticmethod
    def _btn_vis_style():
        return ("#btnVis{padding:0;border:1px solid " + theme.V("border") + ";border-radius:3px;font-size:12px;"
                "background:" + theme.V("panel2") + ";color:" + theme.V("text") + ";}"
                "#btnVis:checked{background:#12405c;color:#7fdcff;border-color:#2f7fa8;}")

    @staticmethod
    def _btn_solo_style():
        return ("#btnSolo{padding:0;border:1px solid " + theme.V("border") + ";border-radius:3px;font-weight:bold;"
                "background:" + theme.V("panel2") + ";color:" + theme.V("text") + ";}"
                "#btnSolo:checked{background:#5a4a12;color:#ffd54f;border-color:#a8862f;}")

    @staticmethod
    def _name_style():
        return ("QPushButton{border:none;text-align:left;color:" + theme.V("text") +
                ";font-weight:bold;background:" + theme.V("panel") + ";}")

    @staticmethod
    def _btn_alpha_style():
        return ("#btnAlpha{padding:0;border:1px solid " + theme.V("border") + ";border-radius:3px;font-weight:bold;"
                "background:" + theme.V("panel2") + ";color:" + theme.V("text") + ";}"
                "#btnAlpha:checked{background:#123f2c;color:#7ef0b0;border-color:#2f8a63;}")

    @staticmethod
    def _btn_lock_style():
        return ("#btnLock{padding:0;border:1px solid " + theme.V("border") + ";border-radius:3px;font-size:11px;"
                "background:" + theme.V("panel2") + ";color:" + theme.V("text") + ";}"
                "#btnLock:checked{background:#4a2a12;color:#ffb74d;border-color:#a86a2f;}")

    def apply_theme(self):
        """主题切换：重设所有图层头样式（ClipRow 的样式由各自注册的回调刷新）"""
        self.title.setStyleSheet("color:" + theme.V("muted") + ";font-weight:bold;")
        for hdr, _row in self.rows:
            lay = getattr(hdr, "_lay", None)
            if lay is None:
                continue
            hdr.setStyleSheet(self._header_style(lay))
            if getattr(hdr, "_btn_vis", None) is not None:
                hdr._btn_vis.setStyleSheet(self._btn_vis_style())
                hdr._btn_solo.setStyleSheet(self._btn_solo_style())
                hdr._name.setStyleSheet(self._name_style())
                hdr._btn_alpha.setStyleSheet(self._btn_alpha_style())
                hdr._btn_lock.setStyleSheet(self._btn_lock_style())
                role = getattr(hdr, "_btn_role", None)
                if role is not None:
                    role.setStyleSheet(self._role_style(lay.role))

    def _container_drop(self, idx, src, md):
        """行容器/图层头落点：其它图层拖来=移动（源层移除），素材库拖来=添加"""
        paths = list(getattr(src, "_drag_paths", []) or []) if isinstance(src, ClipRow) else []
        if not paths and md.hasFormat(CLIP_MIME):
            paths = bytes(md.data(CLIP_MIME)).decode("utf-8", "ignore").split(";")
        elif not paths and (md.hasFormat("text/uri-list") or md.hasText()):
            paths = _paths_from_mime(md)
        paths = [x for x in paths if x and os.path.isabs(x)]
        if not paths:
            return False
        if isinstance(src, ClipRow):
            if src.layer_idx != idx:
                self.main.clips_cross_moved(src.layer_idx, idx, paths)
        else:
            self.main.clips_dropped_to_layer(idx, paths)
        return True

    def _row_index_at(self, y_global_pos):
        """按落点 y 找最近的图层行（拖到空白处也能落到最近的行）"""
        best, best_d = None, None
        for i, (hdr, _grid) in enumerate(self.rows):
            top = hdr.mapToGlobal(QPoint(0, 0)).y()
            h = hdr.height()
            if top <= y_global_pos <= top + h:
                return i
            d = min(abs(y_global_pos - top), abs(y_global_pos - (top + h)))
            if best_d is None or d < best_d:
                best, best_d = i, d
        return best

    def eventFilter(self, obj, ev):
        from PySide6.QtCore import QEvent
        t = ev.type()
        # 整栈区域拖放（含空白处）：按落点找最近图层行
        if obj in (self, self.scroll, self.content) or obj is self.scroll.viewport():
            if t in (QEvent.DragEnter, QEvent.DragMove):
                md = ev.mimeData()
                if md.hasFormat(CLIP_MIME) or md.hasFormat("text/uri-list") or md.hasText():
                    ev.acceptProposedAction()
                    return True
            elif t == QEvent.Drop:
                gp = ev.position().toPoint() if hasattr(ev, "position") else ev.pos()
                if obj is not self:
                    gp = obj.mapToGlobal(gp)
                idx = self._row_index_at(gp.y())
                if idx is None and self.rows:
                    idx = len(self.rows) - 1
                if idx is not None and self._container_drop(idx, ev.source(), ev.mimeData()):
                    ev.acceptProposedAction()
                    return True
                return False
        # 图层名双击 → 重命名
        if t == QEvent.MouseButtonDblClick and getattr(obj, "_is_name", False):
            self.main._cur_layer = obj._layer_idx
            self.main.layer_rename()
            return True
        # 拖放到行容器/图层头上 → 加入到该图层
        idx = getattr(obj, "_layer_idx", None)
        if idx is not None and not getattr(obj, "_is_name", False):
            if t in (QEvent.DragEnter, QEvent.DragMove):
                md = ev.mimeData()
                if (md.hasFormat(CLIP_MIME) or md.hasFormat("text/uri-list") or md.hasText()):
                    ev.setDropAction(Qt.CopyAction)
                    ev.acceptProposedAction()
                    return True
            elif t == QEvent.Drop:
                if self._container_drop(idx, ev.source(), ev.mimeData()):
                    ev.acceptProposedAction()
                    return True
        return super().eventFilter(obj, ev)

    # ---- 重建 ----
    def rebuild(self):
        while self.vbox.count() > 1:
            w = self.vbox.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.rows = []
        for i, lay in enumerate(self.main.engine.layers):
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(6)
            hdr = self._build_header(i, lay)
            hdr._layer_idx = i
            hdr.setAcceptDrops(True)
            hdr.installEventFilter(self)
            row._layer_idx = i
            row.setAcceptDrops(True)
            row.installEventFilter(self)
            rl.addWidget(hdr)
            grid = ClipRow(i)
            grid.dropped.connect(self.main.clips_dropped_to_layer)
            grid.reordered.connect(self.main.clips_reordered)
            grid.cross_moved.connect(self.main.clips_cross_moved)
            grid.menu_requested.connect(self.main.clip_menu)
            rl.addWidget(grid, 1)
            self.vbox.insertWidget(self.vbox.count() - 1, row)
            self.rows.append((hdr, grid))
            self._fill_row(i, lay)
        self.refresh_playing()

    @staticmethod
    def _role_style(role):
        if role == "fg":
            return ("#btnRole{padding:0;border:1px solid #a8682f;border-radius:3px;font-size:11px;"
                    "background:#4a2a12;color:#ffb74d;}"
                    "#btnRole:hover{background:#5a3418;}")
        return ("#btnRole{padding:0;border:1px solid #2f7fa8;border-radius:3px;font-size:11px;"
                "background:#123f5c;color:#7fdcff;}"
                "#btnRole:hover{background:#164a70;}")

    @staticmethod
    def _header_style(lay):
        """图层头样式：普通层中性边框；自动层角色色（前景橙/背景蓝）；
        Kv 主视觉图层用专属紫金配色（与前景/背景同款结构，一眼区分）。"""
        if getattr(lay, "is_kv", False):
            border, bg = "#a06cf0", "#221430"
        elif getattr(lay, "auto_mode", False) and getattr(lay, "role", "bg") == "fg":
            border, bg = "#a8682f", "#241c14"
        elif getattr(lay, "auto_mode", False):
            border, bg = "#2f7fa8", "#13202a"
        else:
            border, bg = theme.V("border"), theme.V("panel2")
        return ("#layerHeader{background:" + bg + ";border:1px solid " + border +
                ";border-radius:3px;}"
                "#layerHeader QLabel{color:" + theme.V("text") + ";}"
                "#layerHeader QComboBox{background:" + theme.V("btn") + ";color:" +
                theme.V("text") + ";border:1px solid " + theme.V("border") + ";}"
                "#layerHeader QComboBox QAbstractItemView{background:" + theme.V("panel") +
                ";color:" + theme.V("text") +
                ";selection-background-color:#4a6cf7;selection-color:#fff;}")

    def _build_header(self, idx, lay):
        f = QFrame()
        f.setFixedWidth(HEADER_W)
        f.setObjectName("layerHeader")
        f._lay = lay   # apply_theme 重设样式时取回图层属性
        # 注意：必须用 #id 作用域，否则会串到下拉弹出列表导致文字看不清；
        # 也不能用 #layerHeader QPushButton 这类带 ID 的兜底规则，否则会盖掉按钮自身 :checked 状态色
        f.setStyleSheet(self._header_style(lay))
        f.setContextMenuPolicy(Qt.CustomContextMenu)
        f.customContextMenuRequested.connect(
            lambda pos, i=idx: self.main.layer_menu(i, f.mapToGlobal(pos)))
        h = QHBoxLayout(f)
        h.setContentsMargins(4, 2, 4, 2)
        h.setSpacing(4)

        btn_vis = QPushButton("👁" if lay.visible else "◌")
        btn_vis.setCheckable(True)
        btn_vis.setChecked(lay.visible)
        btn_vis.setFixedSize(26, 22)
        btn_vis.setToolTip("显示/隐藏（点亮=显示中）")
        btn_vis.setObjectName("btnVis")
        btn_vis.setStyleSheet(self._btn_vis_style())
        h.addWidget(btn_vis)

        btn_solo = QPushButton("S")
        btn_solo.setCheckable(True)
        btn_solo.setChecked(bool(getattr(lay, "solo", False)))
        btn_solo.setFixedSize(24, 22)
        btn_solo.setToolTip("Solo：只显示本图层")
        btn_solo.setObjectName("btnSolo")
        btn_solo.setStyleSheet(self._btn_solo_style())
        h.addWidget(btn_solo)

        name = QPushButton(lay.name)
        name.setFixedWidth(86)
        # 文本是图层名（用户数据），tooltip 含图层名 → 整体标为动态，
        # 由 refresh_header 用 T() 重建，绝不能被 i18n 当静态文案翻译
        name.setProperty("_i18nDynamic", True)
        name.setToolTip(lay.name + T("\n双击重命名"))
        name.setStyleSheet(self._name_style())
        name.clicked.connect(lambda _c=False, i=idx: self.main.on_layer_pick(i))
        name.installEventFilter(self)  # 双击重命名由 MainWindow 处理
        name._layer_idx = idx
        name._is_name = True
        h.addWidget(name)

        op = QSlider(Qt.Horizontal)
        op.setRange(0, 100)
        op.setValue(int(lay.opacity * 100))
        op.setFixedWidth(64)
        op.setProperty("_i18nDynamic", True)   # tooltip 含数值，动态重建
        op.setToolTip(Tf("不透明度 {}%", int(lay.opacity * 100)))
        h.addWidget(op)

        btn_alpha = QPushButton("α")
        btn_alpha.setCheckable(True)
        btn_alpha.setChecked(bool(getattr(lay, "use_alpha", True)))
        btn_alpha.setFixedSize(24, 22)
        btn_alpha.setObjectName("btnAlpha")
        btn_alpha.setToolTip("保留素材自带透明通道（点亮=透明区域透出下层；关闭=透明区域填黑）")
        btn_alpha.setStyleSheet(self._btn_alpha_style())
        h.addWidget(btn_alpha)
        btn_alpha.toggled.connect(lambda st, i=idx: self._set(i, "use_alpha", bool(st)))

        btn_lock = QPushButton("🔒")
        btn_lock.setCheckable(True)
        btn_lock.setChecked(bool(getattr(lay, "lock_motion", False)))
        btn_lock.setFixedSize(24, 22)
        btn_lock.setObjectName("btnLock")
        btn_lock.setToolTip("锁定画面（logo/字幕用）：不随能量脉冲缩放、不随氛围漂移")
        btn_lock.setStyleSheet(self._btn_lock_style())
        h.addWidget(btn_lock)
        btn_lock.toggled.connect(lambda st, i=idx: self._set(i, "lock_motion", bool(st)))

        blend = QComboBox()
        blend.setFixedWidth(74)
        blend.addItems([BLEND_LABELS[b] for b in BLEND_MODES])
        try:
            blend.setCurrentIndex(BLEND_MODES.index(getattr(lay, "blend", "normal")))
        except ValueError:
            blend.setCurrentIndex(0)
        blend.setToolTip("混合模式")
        h.addWidget(blend)

        # 自动图层专属：角色按钮（前景/背景，点击切换，自动匹配按此挑素材）
        btn_role = None
        if getattr(lay, "auto_mode", False):
            btn_role = QPushButton(T("前景") if lay.role == "fg" else T("背景"))
            btn_role.setFixedSize(46, 22)   # 与其他按钮同高对齐
            btn_role.setObjectName("btnRole")
            btn_role.setToolTip("图层角色（自动匹配按此挑素材）：点击在前景/背景间切换")
            btn_role.setStyleSheet(self._role_style(lay.role))
            btn_role.clicked.connect(lambda _c=False, i=idx: self.main.toggle_layer_role(i))
            h.addWidget(btn_role)

        # 事件
        btn_vis.toggled.connect(lambda s, i=idx, b=btn_vis: self._set_visible(i, s, b))
        btn_solo.toggled.connect(lambda s, i=idx: self._set(i, "solo", bool(s)))
        op.valueChanged.connect(lambda v, i=idx, b=btn_vis: self._set_opacity(i, v))
        blend.currentIndexChanged.connect(
            lambda j, i=idx: self._set_blend(i, BLEND_MODES[j]))
        # 记住控件，便于刷新
        f._btn_vis, f._btn_solo, f._name, f._op, f._blend, f._btn_alpha, f._btn_lock = (
            btn_vis, btn_solo, name, op, blend, btn_alpha, btn_lock)
        f._btn_role = btn_role
        return f

    def _set_visible(self, idx, visible, btn):
        btn.setText("👁" if visible else "◌")
        self._set(idx, "visible", bool(visible))

    def _set_speed(self, idx, s):
        if not (0 <= idx < len(self.main.engine.layers)):
            return
        self.main.engine.layers[idx].speed = float(s)
        self.main.save_layers()

    def _set(self, idx, attr, val):
        if not (0 <= idx < len(self.main.engine.layers)):
            return
        if attr == "solo":
            self.main.set_solo(idx, bool(val))     # 独奏互斥：只能有一个图层 Solo
            return
        setattr(self.main.engine.layers[idx], attr, val)
        self.main.save_layers()

    def _set_opacity(self, idx, v):
        if not (0 <= idx < len(self.main.engine.layers)):
            return
        self.main.engine.layers[idx].opacity = v / 100.0
        self.main.save_layers()

    def _set_blend(self, idx, blend):
        if not (0 <= idx < len(self.main.engine.layers)):
            return
        self.main.engine.layers[idx].blend = blend
        self.main.save_layers()

    def _fill_row(self, idx, lay):
        grid = self.rows[idx][1]
        grid.clear()
        for m in lay.clips:
            icon = m.icon(CELL_W - 8, CELL_H - 10)   # 走缩略图缓存，避免重复读盘/缩放
            it = QListWidgetItem(icon, "")
            it.setData(Qt.UserRole, m)
            it.setToolTip(m.name)
            it.setSizeHint(QSize(CELL_W, CELL_H))
            grid.addItem(it)

    def sync_layer(self, idx):
        """仅刷新指定行的素材格（不重建控件）"""
        if 0 <= idx < len(self.main.engine.layers) and idx < len(self.rows):
            self._fill_row(idx, self.main.engine.layers[idx])

    def refresh_header(self, idx=None):
        """刷新图层头控件显示（可见/Solo/不透明度/混合模式/名称）"""
        rng = range(len(self.rows)) if idx is None else [idx]
        for i in rng:
            if not (0 <= i < len(self.rows)):
                continue
            f, _ = self.rows[i]
            lay = self.main.engine.layers[i]
            for w in (f._btn_vis, f._btn_solo, f._op, f._blend, f._btn_alpha, f._btn_lock):
                w.blockSignals(True)
            f._btn_vis.setChecked(lay.visible)
            f._btn_vis.setText("👁" if lay.visible else "◌")
            f._btn_solo.setChecked(bool(getattr(lay, "solo", False)))
            f._op.setValue(int(lay.opacity * 100))
            f._op.setToolTip(Tf("不透明度 {}%", int(lay.opacity * 100)))
            try:
                f._blend.setCurrentIndex(BLEND_MODES.index(getattr(lay, "blend", "normal")))
            except ValueError:
                f._blend.setCurrentIndex(0)
            f._btn_alpha.setChecked(bool(getattr(lay, "use_alpha", True)))
            f._btn_lock.setChecked(bool(getattr(lay, "lock_motion", False)))
            f._name.setText(lay.name)
            f._name.setToolTip(lay.name + T("\n双击重命名"))
            for w in (f._btn_vis, f._btn_solo, f._op, f._blend, f._btn_alpha, f._btn_lock):
                w.blockSignals(False)
            # 角色按钮（自动图层专属）
            btn_role = getattr(f, "_btn_role", None)
            if btn_role is not None:
                btn_role.setText(T("前景") if lay.role == "fg" else T("背景"))
                btn_role.setStyleSheet(self._role_style(lay.role))
            # 自动层边框/底色跟随类型与角色变化
            f._lay = lay
            f.setStyleSheet(self._header_style(lay))

    # ---- 播放高亮：所有正在播放的素材（含过渡中的上一个）都高亮 ----
    def refresh_playing(self):
        for i, (_, grid) in enumerate(self.rows):
            if i >= len(self.main.engine.layers):
                continue
            lay = self.main.engine.layers[i]
            playing = set()
            if 0 <= lay.cur < len(lay.clips):
                playing.add(lay.clips[lay.cur])
            if lay.trans_active and 0 <= lay.prev_idx < len(lay.clips):
                playing.add(lay.clips[lay.prev_idx])
            for k in range(grid.count()):
                it = grid.item(k)
                m = it.data(Qt.UserRole)
                if m in playing:
                    it.setBackground(QColor(255, 205, 0, 235))   # 正在播放：金黄打底
                    it.setForeground(QColor(25, 25, 25))
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)
                else:
                    it.setBackground(QColor(0, 0, 0, 0))
                    it.setForeground(QColor(230, 230, 230))
                    f = it.font()
                    f.setBold(False)
                    it.setFont(f)


class CollapsibleSection(QWidget):
    """可独立展开/收起的设置分区（互不影响，可同时展开多个）"""

    def __init__(self, title, expanded=True, parent=None, count=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.btn = QPushButton()
        self.btn.setCheckable(True)
        self.btn.setChecked(bool(expanded))
        self.btn.setObjectName("secHead")
        # 标题带 ▼/▶ 展开符号，属动态文本：标记后由 retranslate() 统一重建，
        # 避免被 i18n 当成静态文案记录（否则切换语言后标题会串味）
        self.btn.setProperty("_i18nDynamic", True)
        self.btn.clicked.connect(self._toggle)
        self._sync_theme()
        theme.register(self, self._sync_theme)   # 主题切换时重设分区头样式
        v.addWidget(self.btn)
        self.body = QWidget()
        self.body.setVisible(bool(expanded))
        v.addWidget(self.body)
        self._title = title
        self._count = count          # 可选计数后缀（如「颜色 (15)」），计数不参与翻译
        self._sync_text()

    def _sync_theme(self):
        self.btn.setStyleSheet(
            "#secHead{text-align:left;font-weight:bold;padding:6px 8px;"
            "background:" + theme.V("head") + ";color:" + theme.V("text") +
            ";border:1px solid " + theme.V("border") + ";border-radius:3px;}"
            "#secHead:hover{background:" + theme.V("btn_hov") + ";}")

    def set_content(self, widget_or_layout):
        if isinstance(widget_or_layout, QWidget):
            l = QVBoxLayout(self.body)
            l.setContentsMargins(2, 4, 2, 6)
            l.addWidget(widget_or_layout)
        else:
            self.body.setLayout(widget_or_layout)

    def _sync_text(self):
        txt = T(self._title)
        if self._count is not None:
            txt = f"{txt} ({self._count})"
        self.btn.setText(("▼ " if self.btn.isChecked() else "▶ ") + txt)

    def retranslate(self):
        """语言切换后重建标题（▼/▶ 符号 + 译文）"""
        self._sync_text()

    def _toggle(self):
        self.body.setVisible(self.btn.isChecked())
        self._sync_text()
        cb = getattr(self, "on_toggle", None)
        if cb:
            cb(self._title, self.btn.isChecked())


class MiniLevel(QWidget):
    """工具条迷你电平：3 段小灯（绿/黄/红）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(58, 16)
        self.level = 0.0
        self.setToolTip("输入电平")

    def set_level(self, v):
        v = max(0.0, min(1.0, v))
        if abs(v - self.level) > 0.01:
            self.level = v
            self.update()

    def paintEvent(self, _e):
        from PySide6.QtGui import QPainter
        p = QPainter(self)
        n = 12
        for i in range(n):
            frac = (i + 1) / n
            on = self.level >= frac * 0.95
            if frac > 0.9:
                c = QColor(230, 70, 70) if on else QColor(60, 26, 26)
            elif frac > 0.7:
                c = QColor(230, 200, 60) if on else QColor(58, 52, 24)
            else:
                c = QColor(80, 210, 110) if on else QColor(24, 50, 30)
            p.fillRect(i * 5, 3, 4, 10, c)
        p.end()


class BeatGridOverlay(QWidget):
    """预览里的「节拍网格线」浮层（用户 2026-09-27 要求）。

    以**当前拍**为最左一格，向右画 2 组栅格（网格模式 8 拍/组 = 16 拍；普通模式 4 拍/组 = 8 拍）：
      · 普通拍       —— 短刻线（暗）
      · 小节头(4 拍) —— 长刻线（亮）
      · 乐句头(8 拍) —— 最长刻线 + 琥珀色，**这才是切换真正对齐的那条线**
      · 当前拍       —— 最左的白色游标，随时间从左扫到右，扫到头就换下一组

    ★ 相位直接来自 `engine.beat_grid_view()`，而它内部走的是与切换对齐**同一套** `_align_width/_bar_off`
      ⇒ 这条线就是"切换实际踩的那条线"，不会画出一条好看但和实际不符的网格。
    ★ 只画在**预览浮层**里，不进合成画面 ⇒ 输出窗口 / Spout / NDI 都不会带上它。
    """

    CELL = 15          # 每拍像素宽
    HH = 22            # 浮层高度

    def __init__(self, parent):
        super().__init__(parent)
        self._idx = None       # 当前拍在栅格里的位置 0..w-1
        self._w = 4            # 栅格宽度（8=八拍乐句，4=小节）
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.hide()

    def has_grid(self):
        return self._idx is not None

    def set_grid(self, idx, w):
        """idx=None / w 非法 ⇒ 视为无数据（隐藏）。"""
        if idx is None or not w or int(w) <= 0:
            if self._idx is None:
                return
            self._idx = None
            self.update()
            return
        key = (int(idx), int(w))
        if key == (self._idx, self._w):
            return                      # 同格内不重画（HUD 每帧都会喂）
        self._idx, self._w = key
        self.setFixedSize(int(self.CELL * self._w * 2) + 2, self.HH)
        self.update()

    def paintEvent(self, _e):
        if self._idx is None:
            return
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        p.drawRoundedRect(0, 0, w - 1, h - 1, 3, 3)
        n = self._w * 2
        for j in range(n):
            gp = (self._idx + j) % self._w      # 该格在本组里的位置
            x = j * self.CELL + 2
            if gp == 0:                          # 乐句头（八拍头 / 普通模式下即小节头）
                p.setPen(QPen(QColor(255, 213, 79), 2))
                p.drawLine(x, 3, x, h - 4)
            elif gp % 4 == 0:                    # 小节头（网格模式下 = 每 4 拍）
                p.setPen(QPen(QColor(255, 255, 255, 150), 1))
                p.drawLine(x, 6, x, h - 4)
            else:                                # 普通拍
                p.setPen(QPen(QColor(255, 255, 255, 70), 1))
                p.drawLine(x, h - 8, x, h - 4)
        p.setPen(QPen(QColor(255, 255, 255, 225), 2))   # 当前拍游标
        p.drawLine(2, 2, 2, h - 2)


class PreviewPanel(QWidget):
    """输出预览 + HUD + 下一个素材预看"""

    def __init__(self, main):
        super().__init__()
        self.main = main
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(3)

        bar = QHBoxLayout()
        t = QLabel("输出预览")
        t.setStyleSheet("color:" + theme.V("muted") + ";font-weight:bold;")
        bar.addWidget(t)
        bar.addStretch(1)
        self.btn_pic = QPushButton("画面设置")
        self.btn_pic.setToolTip("预览显示设置：开关 / 缩放模式 / 刷新率")
        self.btn_pic.clicked.connect(self.open_pic_settings)
        bar.addWidget(self.btn_pic)
        self.chk_preview = QCheckBox("预览")
        self.chk_preview.setToolTip("关掉主界面预览画面。实测省的 CPU 很少（约 0.05 个核，整机不到 1%）——\n"
                                    "它属于「微调」，解决不了卡顿；输出窗口 / Spout / NDI 完全不受影响")
        self.chk_preview.toggled.connect(self._set_preview_on)
        bar.addWidget(self.chk_preview)
        self.chk_hud = QCheckBox("HUD")
        self.chk_hud.setChecked(True)
        self.chk_hud.setToolTip("显示模式/倒计时/BPM/能量浮层")
        bar.addWidget(self.chk_hud)
        self.chk_next = QCheckBox("下一个素材")
        self.chk_next.setChecked(True)
        self.chk_next.setToolTip("角落预看即将切入的素材")
        bar.addWidget(self.chk_next)
        lay.addLayout(bar)

        self.view = QLabel()
        self.view.setMinimumSize(360, 203)
        self.view.setAlignment(Qt.AlignCenter)
        self.view.setStyleSheet("background:#000;border:1px solid #2a2a2a;")
        self.view.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        lay.addWidget(self.view, 1)

        # 子浮层
        self.hud = QLabel(self.view)
        self.hud.setStyleSheet("background:rgba(0,0,0,150);color:#9fe;padding:3px 6px;"
                               "border-radius:3px;font-size:11px;")
        self.hud.move(8, 8)
        self.hud.hide()

        # 「节拍网格线」浮层：贴在 HUD 下方（用户 2026-09-27 要求）
        self.beatbar = BeatGridOverlay(self.view)

        self.next_box = QLabel(self.view)
        self.next_box.setStyleSheet("background:rgba(0,0,0,150);color:#ffd54f;"
                                    "padding:2px 4px;border-radius:3px;font-size:10px;")
        self.next_box.setAlignment(Qt.AlignCenter)
        self.next_box.hide()

        self._next_path = None
        # 每图层一个"下一个素材"缩略图（动态增减；无素材/隐藏的图层不显示）
        self.layer_thumbs = []
        self.chk_hud.toggled.connect(self._sync_overlays)
        self.chk_next.toggled.connect(self._sync_overlays)
        self.apply_preview_state()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._place_overlays()
        self._rescale()

    def _place_overlays(self):
        w, h = self.view.width(), self.view.height()
        self.hud.adjustSize()
        self.hud.move(8, 8)
        self.beatbar.move(8, 8 + self.hud.height() + 4)
        self._place_thumbs()

    def _place_thumbs(self):
        x, y = 8, self.view.height() - 54 - 8
        for lbl in self.layer_thumbs:
            if lbl.isVisible():
                lbl.move(x, max(8, y))
                x += 96 + 6

    def _sync_overlays(self, *_):
        on = self.preview_on()
        self.hud.setVisible(on and self.chk_hud.isChecked())
        self.beatbar.setVisible(on and self.chk_hud.isChecked() and self.beatbar.has_grid())
        if not on or not self.chk_next.isChecked():
            for lbl in self.layer_thumbs:
                lbl.hide()

    def update_grid(self, idx, w):
        """预览「节拍网格线」浮层：idx=当前拍在栅格里的位置(0..w-1)，w=栅格宽度(8=八拍乐句/4=小节)。

        `idx=None` ⇒ 无数据（未开始 / 无拍钟）⇒ 隐藏。相位由 engine.beat_grid_view() 提供，
        与切换对齐共用同一套 `_align_width/_bar_off`，所以画出来的就是切换实际踩的那条线。
        每帧调用一次即可（同格内不重画，开销可忽略）。
        """
        self.beatbar.set_grid(idx, w)
        self.beatbar.setVisible(self.preview_on() and self.chk_hud.isChecked()
                                and self.beatbar.has_grid())
        self.beatbar.move(8, 8 + self.hud.height() + 4)

    # ---------------- 预览开关（用户 2026-09-25 要求：预览画面设置里可关闭预览） ----------------
    def preview_on(self):
        return bool(self.pic_cfg().get("on", True))

    def _set_preview_on(self, on):
        """工具栏勾选框：写配置并立即生效"""
        self.pic_cfg()["on"] = bool(on)
        try:
            self.main.cfg.save()
        except Exception:
            pass
        self.apply_preview_state()

    def apply_preview_state(self):
        """把配置里的 on 状态同步到界面（勾选框 / 占位文字 / 浮层）

        ⚠ 只影响**主界面预览**：引擎照常合成，输出窗口、Spout、NDI 都照常发。
        """
        on = self.preview_on()
        if self.chk_preview.isChecked() != on:
            self.chk_preview.blockSignals(True)
            self.chk_preview.setChecked(on)
            self.chk_preview.blockSignals(False)
        if on:
            self.view.setStyleSheet("background:#000;border:1px solid #2a2a2a;")
            self.view.setText("")
            if self._img is not None:
                self._rescale()
        else:
            self.view.setPixmap(QPixmap())
            self.view.setStyleSheet("background:#111;border:1px solid #2a2a2a;color:#666;")
            self.view.setProperty("_i18nDynamic", True)
            self.view.setText(T("预览已关闭\n（关掉它省的 CPU 很少，约 0.05 个核；可在「画面设置」里重新打开）"))
            self._img = None
        self._sync_overlays()

    def _rescale(self):
        if self._img is None or not self.preview_on():
            return
        p = self.pic_cfg()
        fit = p.get("fit", "keep")
        pm = QPixmap.fromImage(self._img)
        res = float(p.get("res", 1.0))
        if res < 0.999:
            # 预览分辨率降采样：先缩到较小尺寸再适配，省 CPU（只影响预览）
            pm = pm.scaled(max(1, int(pm.width() * res)), max(1, int(pm.height() * res)),
                           Qt.KeepAspectRatio, Qt.SmoothTransformation)
        tgt = self.view.size()
        if fit == "stretch":
            pm = pm.scaled(tgt, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        elif fit == "cover":
            pm = pm.scaled(tgt, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            x = max(0, (pm.width() - tgt.width()) // 2)
            y = max(0, (pm.height() - tgt.height()) // 2)
            pm = pm.copy(x, y, min(tgt.width(), pm.width()), min(tgt.height(), pm.height()))
        else:
            pm = pm.scaled(tgt, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.view.setPixmap(pm)

    _img = None

    def pic_cfg(self):
        p = self.main.cfg["ui"].setdefault("preview", {})
        p.setdefault("res", 1.0)         # 预览分辨率：1.0 / 0.5 / 0.25（性能）
        p.setdefault("fit", "keep")      # keep / cover / stretch
        p.setdefault("fps", 60)
        return p

    def open_pic_settings(self):
        PreviewSettingsDialog(self.main, self).exec()

    def set_frame(self, img):
        import time as _t
        if not self.preview_on():
            return                        # 预览已关闭：跳过 fromImage + 缩放（省 CPU）
        self._img = img
        p = self.pic_cfg()
        fps = max(1, int(p.get("fps", 60)))
        now = _t.perf_counter()
        if fps < 60 and (now - getattr(self, "_last_t", 0.0)) < 1.0 / fps:
            return                        # 按设定帧率节流（省 CPU）
        self._last_t = now
        self._rescale()

    def update_hud(self, text):
        if not self.preview_on():
            return
        if text != self.hud.text():
            self.hud.setText(text)
            self.hud.adjustSize()

    def update_next(self, media):
        """兼容保留：旧的单一"下一个素材"小窗已由 update_layer_thumbs 取代"""
        pass

    def update_layer_thumbs(self, layers):
        """输出预览底部显示每个图层的下一个素材缩略图。
        多个图层显示多个；图层无素材或被关闭显示时不显示。"""
        infos = []
        if self.chk_next.isChecked() and self.preview_on():
            for lay in layers:
                if not getattr(lay, "visible", False) or not lay.clips:
                    continue
                if getattr(lay, "is_kv", False):
                    # Kv 待机层不自动切换：显示当前素材（而非"下一个"）
                    cur = lay.clips[lay.cur] if 0 <= lay.cur < len(lay.clips) else lay.clips[0]
                    infos.append((lay.name, cur))
                    continue
                nxt = self.main.engine.next_clip_for(lay)
                if nxt:
                    infos.append((lay.name, nxt))
        while len(self.layer_thumbs) < len(infos):
            lbl = QLabel(self.view)
            lbl.setFixedSize(96, 54)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet("background:#000;border:1px solid #2f6f9f;")
            lbl.hide()
            self.layer_thumbs.append(lbl)
        for i, lbl in enumerate(self.layer_thumbs):
            if i < len(infos):
                lname, m = infos[i]
                lbl.setPixmap(m.pixmap(94, 52))
                lbl.setToolTip(f"[{lname}]  下一个：{m.name}")
                if not lbl.isVisible():
                    lbl.show()
            elif lbl.isVisible():
                lbl.hide()
        self._place_thumbs()


class AudioSourceDialog(I18nDialog):
    """音源详细设置：类型/设备/采样率/单声道/增益/重扫/电平表"""

    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("音源设置")
        self.setMinimumWidth(460)
        self._devs = {"system": [], "mic": []}
        self._scanner = None

        lay = QVBoxLayout(self)
        g = QGridLayout()
        g.addWidget(QLabel("音源类型"), 0, 0)
        self.source = QComboBox()
        self.source.addItems(["系统声音（WASAPI Loopback）", "麦克风", "线路输入/声卡"])
        g.addWidget(self.source, 0, 1, 1, 2)

        g.addWidget(QLabel("设备"), 1, 0)
        self.device = QComboBox()
        self.device.setMinimumWidth(260)
        g.addWidget(self.device, 1, 1)
        self.btn_rescan = QPushButton("重新扫描")
        self.btn_rescan.clicked.connect(self.scan_devices)
        g.addWidget(self.btn_rescan, 1, 2)

        g.addWidget(QLabel("采样率"), 2, 0)
        self.rate = QComboBox()
        self.rate.addItems(["自动（跟随系统）", "44100", "48000", "96000"])
        self.rate.setToolTip("自动=跟随设备原生采样率（避免重采样、能量更准）；手动可强制指定")
        g.addWidget(self.rate, 2, 1)

        self.chk_mono = QCheckBox("单声道分析")
        self.chk_mono.setToolTip("只取左声道做分析。不省 CPU，作用是在左右声道反相时避免低频被平均抵消")
        g.addWidget(self.chk_mono, 3, 0, 1, 3)
        lay.addLayout(g)

        # 电平表（黑底电平槽在亮色下也保持深底——电平色条对比度需要）
        meter_box = QFrame()
        meter_box.setObjectName("meterBox")
        meter_box.setStyleSheet(
            "#meterBox{background:#111;border:1px solid " + theme.V("border") + ";}"
            "#meterBox QLabel{color:" + theme.V("text") + ";}")
        mv = QVBoxLayout(meter_box)
        self.bars = {}
        for key, name, color in (("level", "总电平", "#4fc3f7"), ("bass", "低频", "#7ddf7d"),
                                 ("mid", "中频", "#ffd54f"), ("high", "高频", "#ff8a65")):
            row = QHBoxLayout()
            lb = QLabel(T(name))
            lb.setFixedWidth(48)
            row.addWidget(lb)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setTextVisible(False)
            bar.setFixedHeight(10)
            bar.setStyleSheet(
                f"QProgressBar{{background:{theme.V('panel2')};border:none;}}"
                f"QProgressBar::chunk{{background:{color};}}")
            row.addWidget(bar)
            mv.addLayout(row)
            self.bars[key] = bar
        lay.addWidget(meter_box)

        self.lbl_dev = QLabel(T("当前采集设备: ") + "-")
        self.lbl_dev.setProperty("_i18nDynamic", True)   # 后面会拼接设备名，动态重建
        self.lbl_dev.setStyleSheet("color:#888;")
        self.lbl_dev.setWordWrap(True)
        lay.addWidget(self.lbl_dev)
        self.lbl_hint = QLabel("")
        self.lbl_hint.setWordWrap(True)
        self.lbl_hint.setStyleSheet("color:#e0a030;font-size:11px;")
        self.lbl_hint.setProperty("_i18nDynamic", True)   # 含设备名，动态重建
        lay.addWidget(self.lbl_hint)

        btns = QDialogButtonBox(QDialogButtonBox.Apply | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Apply).setText("应用")
        btns.button(QDialogButtonBox.Cancel).setText("取消")
        btns.clicked.connect(self._on_btn)
        self._btnbox = btns
        lay.addWidget(btns)

        # 切换音源类型：先用缓存立刻填充，再后台重新扫描一次设备
        # （设备可能刚插上/拔掉，光用缓存会显示过期列表）
        self.source.currentIndexChanged.connect(self._on_source_changed)
        self._load_from_cfg()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._update_meters)
        self.timer.start(120)
        self.scan_devices()

    # ---- 配置载入/应用 ----
    SOURCE_KEYS = ("system", "mic", "mic")   # 下拉索引 → 设备类别（线路输入并入 mic）

    def _src_key(self):
        return self.SOURCE_KEYS[max(0, min(2, self.source.currentIndex()))]

    def _remembered_dev(self):
        """本音源类型上次使用的设备 → (设备名, 是否已被「应用」确认过)。

        配置里按类型分开存（`audio.device_by_source`）。旧配置只有单一的
        `device_name`，它到底属于哪个类型无从判断 —— 此时返回「未确认」，
        匹配不到也**不报警**。否则会出现「切到麦克风、却提示扬声器已拔出」
        这种张冠李戴（设备名跟类型是绑定的，混着比必然误判）。
        """
        a = self.main.cfg["audio"]
        kind = self._src_key()
        dbs = a.get("device_by_source") or {}
        if kind in dbs:
            return (dbs.get(kind) or ""), True
        legacy = a.get("device_name") or ""
        if not legacy:
            return "", False
        cfg_idx = {"system": 0, "mic": 1, "linein": 2}.get(a.get("source_type", "system"), 0)
        if self.SOURCE_KEYS[cfg_idx] == kind:
            return legacy, False     # 可能是本类型，但没确认过 → 静默处理
        return "", False

    def _load_from_cfg(self):
        a = self.main.cfg["audio"]
        idx = {"system": 0, "mic": 1, "linein": 2}.get(a.get("source_type", "system"), 0)
        self.source.blockSignals(True)      # 载入配置不触发「类型变化 → 重扫」
        self.source.setCurrentIndex(idx)
        self.source.blockSignals(False)
        sr_map = {"0": 0, "44100": 1, "48000": 2, "96000": 3}
        self.rate.setCurrentIndex(sr_map.get(str(int(a.get("sample_rate", 0) or 0)), 0))
        # ⚠ 「增益」已移除（2026-09-25）：它从来没接进分析链路（死代码），
        #   而且算法两层自适应归一化天生对整体增益免疫（实测 0.5~4x 能量完全相同）。
        #   配置键 audio.gain 保留只为兼容旧配置，不再读取。
        self.chk_mono.setChecked(bool(a.get("mono", True)))
        self._want_dev = ""      # 由 _fill_devices 按当前音源类型去配置里取

    def scan_devices(self):
        from PySide6.QtCore import QThread
        if self._scanner and self._scanner.isRunning():
            return
        self.device.clear()
        self.device.addItem("正在扫描…")

        class _Scanner(QThread):
            from PySide6.QtCore import Signal as _S
            ready = _S(dict)

            def __init__(self, audio, parent=None):
                super().__init__(parent)
                self.audio = audio

            def run(self):
                try:
                    self.ready.emit(self.audio.list_devices())
                except Exception as e:
                    self.ready.emit({"system": [], "mic": [], "error": str(e)})

        self._scanner = _Scanner(self.main.audio, self)
        self._scanner.ready.connect(self._fill_devices)
        self._scanner.start()

    def _actual_dev(self):
        """当前**实际在用**的采集设备名（设备被拔掉后用它兜底选中）"""
        try:
            return self.main.audio.state.snapshot().get("device_desc") or ""
        except Exception:
            return ""

    def _fill_devices(self, devs=None):
        if devs is not None:
            self._devs = devs if "error" not in devs else {"system": [], "mic": []}
        kind = self._src_key()
        items = self._devs.get(kind, [])
        self.device.blockSignals(True)
        self.device.clear()
        self.device.addItems(items or [T("（未找到设备）")])

        want, confirmed = self._remembered_dev()

        def _match(w):
            if not w:
                return False
            for i in range(self.device.count()):
                t = self.device.itemText(i)
                if w in t or t in w:
                    self.device.setCurrentIndex(i)
                    return True
            return False

        matched = _match(want)
        if not matched:
            # 本类型记忆的设备不在了：退回「当前实际在用」的设备（同类型才有意义），
            # 再不行就选第一项 —— 绝不让下拉停在一个不存在的项上
            if not _match(self._actual_dev()):
                self.device.setCurrentIndex(0)
        self.device.blockSignals(False)

        # 提示只在「这台设备确实属于本类型、且用户确认过」时才给，
        # 否则就会出现「切到麦克风却提示扬声器掉线」的误报。
        if not items:
            self.lbl_hint.setText(T("没有扫描到可用设备：请检查连接后点「重新扫描」。"))
        elif confirmed and want and not matched:
            self.lbl_hint.setText(
                Tf("本类型上次使用的设备「{}」已不可用（可能被拔出），请重新选择后点「应用」。",
                   want))
        else:
            self.lbl_hint.setText("")

    def _on_btn(self, btn):
        """按**标准按钮角色**分派（不能用按钮文本判断）。

        曾经的写法是 `if btn.text() == "应用"` —— 一旦把界面切成英文，
        重译机制会把按钮文案变成「Apply」，判断落空 → 点「应用」被当成
        「取消」，音源设备永远保存不进去（重启也没用，因为语言设置是持久的）。
        """
        try:
            std = self._btnbox.standardButton(btn)
        except Exception:
            std = None
        if std == QDialogButtonBox.Apply:
            self.apply()
        else:
            self.reject()

    def _on_source_changed(self, _i=0):
        """切换音源类型：立刻按缓存填充，同时后台重新扫描设备列表。"""
        self._fill_devices()
        self.scan_devices()

    def apply(self):
        t = self.SOURCE_KEYS[max(0, min(2, self.source.currentIndex()))]
        dev = (self.device.currentText() or "").strip()
        # 占位文本（扫描中／未找到设备）不能当设备名保存
        if dev.startswith(("（", "(")) or dev in ("正在扫描…", T("正在扫描…"), "Scanning…"):
            dev = ""
        sr = [0, 44100, 48000, 96000][max(0, self.rate.currentIndex())]
        mono = self.chk_mono.isChecked()
        # 按音源类型分别记住设备：切回该类型时能自动选中，
        # 且「设备掉线」提示不会把别的类型的设备名拿来误报。
        a = self.main.cfg["audio"]
        dbs = dict(a.get("device_by_source") or {})
        dbs[t] = dev
        a["device_by_source"] = dbs
        a["device_name"] = dev          # 与 source_type 配对，引擎启动时读它
        self.main.apply_audio_settings(t, dev, sr, mono)
        self._want_dev = dev
        self.accept()

    def _update_meters(self):
        snap = self.main.audio.state.snapshot()
        for k, b in self.bars.items():
            b.setValue(int(max(0.0, min(1.0, snap.get(k, 0.0))) * 100))
        self.lbl_dev.setText(T("当前采集设备: ") + (snap.get("device_desc") or "-"))


class ImageAdjustDialog(I18nDialog):
    """素材大小与位置调整（单独作用于当前素材，图片摆位）：缩放 / 水平 / 垂直"""

    def __init__(self, main, media):
        super().__init__(main)
        self.main = main
        self.media = media
        self.setWindowTitle(T("素材大小与位置 - ") + media.name)
        self.setProperty("_i18nDynamic", True)   # 标题含文件名，动态
        self.setMinimumWidth(420)
        outer = QVBoxLayout(self)
        grid = QGridLayout()

        def row(r, name, lo, hi, val, suffix=""):
            grid.addWidget(QLabel(T(name)), r, 0)
            sl = QSlider(Qt.Horizontal)
            sl.setRange(lo, hi)
            sl.setValue(int(val))
            sp = QSpinBox()
            sp.setRange(lo, hi)
            sp.setValue(int(val))
            sp.setSuffix(suffix)
            sp.setFixedWidth(86)
            grid.addWidget(sl, r, 1)
            grid.addWidget(sp, r, 2)
            sl.valueChanged.connect(sp.setValue)
            sp.valueChanged.connect(sl.setValue)
            sl.valueChanged.connect(self._apply)
            return sl

        self.sl_scale = row(0, "大小", 5, 400, round(media.img_scale * 100), " %")
        self.sl_x = row(1, "水平位置", -100, 100, round(media.img_x), " %")
        self.sl_y = row(2, "垂直位置", -100, 100, round(media.img_y), " %")
        self.sl_rot = row(3, "旋转", 0, 360, round(getattr(media, "img_rot", 0.0)), " °")
        outer.addLayout(grid)

        hint = QLabel(T("100% = 完整显示（保持比例）；位置为相对画面中心的百分比。\n"
                        "只作用于当前素材，其它素材不受影响。"))
        hint.setStyleSheet("color:#888;")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        btns = QHBoxLayout()
        b_reset = QPushButton("重置")
        b_reset.clicked.connect(self._reset)
        btns.addWidget(b_reset)
        btns.addStretch(1)
        b_close = QPushButton("关闭")
        b_close.clicked.connect(self.accept)
        btns.addWidget(b_close)
        outer.addLayout(btns)

    def _apply(self, *_):
        self.media.img_scale = self.sl_scale.value() / 100.0
        self.media.img_x = float(self.sl_x.value())
        self.media.img_y = float(self.sl_y.value())
        self.media.img_rot = float(self.sl_rot.value())
        adj = dict(self.main.cfg["clip_adj"].get(self.media.path, {}))
        adj.update({"scale": self.media.img_scale, "x": self.media.img_x,
                    "y": self.media.img_y, "rot": self.media.img_rot})
        self.main.cfg["clip_adj"][self.media.path] = adj
        self.main.cfg.save()

    def _reset(self):
        self.sl_scale.setValue(100)
        self.sl_x.setValue(0)
        self.sl_y.setValue(0)
        self.sl_rot.setValue(0)



class PreviewSettingsDialog(I18nDialog):
    """预览画面设置（缩放模式 / 刷新率 / 重置），仅影响主界面预览窗口"""

    def __init__(self, main, panel):
        super().__init__(main)
        self.main = main
        self.panel = panel
        self.setWindowTitle("预览画面设置")
        self.setMinimumWidth(430)
        p = panel.pic_cfg()
        outer = QVBoxLayout(self)
        grid = QGridLayout()

        grid.addWidget(QLabel("预览分辨率"), 1, 0)
        self.res = QComboBox()
        self.res.addItems(["原始（最清晰）", "1/2（省性能）", "1/4（最省性能）"])
        try:
            self.res.setCurrentIndex([1.0, 0.5, 0.25].index(float(p.get("res", 1.0))))
        except ValueError:
            self.res.setCurrentIndex(0)
        grid.addWidget(self.res, 1, 1)

        grid.addWidget(QLabel("缩放模式"), 2, 0)
        self.fit = QComboBox()
        self.fit.addItems(["等比（完整显示）", "铺满（裁切填满）", "拉伸（变形填满）"])
        self.fit.setCurrentIndex({"keep": 0, "cover": 1, "stretch": 2}.get(p.get("fit", "keep"), 0))
        grid.addWidget(self.fit, 2, 1)

        grid.addWidget(QLabel("预览刷新率"), 3, 0)
        self.fps = QComboBox()
        self.fps.addItems(["60 fps（最流畅）", "30 fps（省性能）", "15 fps（最省）"])
        try:
            self.fps.setCurrentIndex([60, 30, 15].index(int(p.get("fps", 60))))
        except ValueError:
            self.fps.setCurrentIndex(0)
        grid.addWidget(self.fps, 3, 1)

        # 预览总开关（⚠ 实测省的 CPU 很少，约 0.05 核 —— 别把它当省 CPU 的主要手段；
        #   真正吃 CPU 的是解码与后处理特效，见「更新内容」的说明）
        self.on = QCheckBox("显示预览画面")
        self.on.setToolTip("关掉后主界面不再渲染预览。实测省的 CPU 很少（约 0.05 个核）；\n"
                           "输出窗口 / Spout / NDI 完全不受影响。它解决不了卡顿")
        self.on.setChecked(bool(p.get("on", True)))
        grid.addWidget(self.on, 0, 0, 1, 2)

        def slider_row(r, name, key, lo=-100, hi=100):
            grid.addWidget(QLabel(name), r, 0)
            sl = QSlider(Qt.Horizontal)
            sl.setRange(lo, hi)
            sl.setValue(int(p.get(key, 0)))
            sp = QSpinBox()
            sp.setRange(lo, hi)
            sp.setValue(int(p.get(key, 0)))
            sp.setFixedWidth(80)
            sl.valueChanged.connect(sp.setValue)
            sp.valueChanged.connect(sl.setValue)
            sl.valueChanged.connect(self._apply)
            wrap = QHBoxLayout()
            wrap.addWidget(sl, 1)
            wrap.addWidget(sp)
            grid.addLayout(wrap, r, 1)
            return sl

        outer.addLayout(grid)

        hint = QLabel(T("「预览刷新率」和「预览分辨率」对 CPU 的影响都很小（60→30 大约省 0.05 个核）；\n"
                        "关掉上面的「显示预览画面」也就省这么多 —— 它们都是微调，解决不了卡顿。\n"
                        "真正吃 CPU 的是解码（用「GPU 解码」解决）和后处理特效。\n"
                        "预览设置只影响主界面预览窗口，不改变输出画面。"))
        hint.setStyleSheet("color:#888;")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        btns = QHBoxLayout()
        b_reset = QPushButton("重置")
        b_reset.clicked.connect(self._reset)
        btns.addWidget(b_reset)
        btns.addStretch(1)
        b_close = QPushButton("关闭")
        b_close.clicked.connect(self.accept)
        btns.addWidget(b_close)
        outer.addLayout(btns)

        self.on.toggled.connect(self._apply)
        self.res.currentIndexChanged.connect(self._apply)
        self.fit.currentIndexChanged.connect(self._apply)
        self.fps.currentIndexChanged.connect(self._apply)
        for wid in (self.res, self.fit, self.fps):
            wid.setEnabled(self.on.isChecked())

    def _apply(self, *_):
        p = self.panel.pic_cfg()
        p["on"] = bool(self.on.isChecked())
        p["res"] = [1.0, 0.5, 0.25][self.res.currentIndex()]
        p["fit"] = ["keep", "cover", "stretch"][self.fit.currentIndex()]
        p["fps"] = [60, 30, 15][self.fps.currentIndex()]
        for wid in (self.res, self.fit, self.fps):
            wid.setEnabled(self.on.isChecked())
        try:
            self.main.cfg.save()
        except Exception:
            pass
        self.panel._last_t = 0.0
        self.panel.apply_preview_state()

    def _reset(self):
        self.on.setChecked(True)
        self.res.setCurrentIndex(0)
        self.fit.setCurrentIndex(0)
        self.fps.setCurrentIndex(0)
        self._apply()

