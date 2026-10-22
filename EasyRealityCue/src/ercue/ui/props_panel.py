# -*- coding: utf-8 -*-
"""片段属性面板（④ 属性）。

★★ 这里补上了旧实现**从未做出的功能：素材入点 / 出点**（`src_in_sec` / `src_out_sec`）。
旧实现因为它所有播放器都没有 `seek()` 而无法预览、于是干脆没做 UI（规格书 §7.1/§7.2）；
本项目 `media.frames.CueFrameSource.frame_exact()` 能精确取帧，所以能真正支持。

★ UI 显示秒、存储秒：本项目模型内部坐标**本来就是秒**（架构方案 §3.2），
  所以本面板**不做任何拍↔秒换算**（这正是本项目相对旧实现的简化，旧实现要到处 ×/÷ beat_len）。

★ 需求 7（变速改为拖动横轴 + 保留手动输入框）：
  "变速"行 = 水平 `QSlider` + `QDoubleSpinBox` **并排、双向同步**；滑条范围**严格对照**
  现有输入框的 min/max（`SPEED_MIN=0.1` ~ `SPEED_MAX=3.0`，×100 映射为 10~300）。
  纯 UI 控制，变速的底层行为（`CueItem.speed`）不变。

★ 需求 9（改名 + 语义一起改，见 `PropsPanel` docstring）：
  混合 / 不透明度两项从"本层"**下沉到"本素材（片段）"级**；标签随**选中态**切换。

★ 第五轮（实测反馈 ④⑤）：
  ④ "大小"也改成 **滑条 + 输入框双向同步**（照 `sp_speed` 的模式；范围 0.05~8.0 不变）；
  ⑤ **去掉"继承本层"的 UI 入口**（混合下拉不再有「（继承本层）」项、不透明度去掉勾选框）——
    只删 UI，**底层数据语义不动**：`item.blend==""` / `item.opacity is None` 仍是"继承本层"
    （别的模块仍依赖该契约）。打开面板时按 `CompLayer.effective_blend/effective_opacity` 的
    **实际生效值**显示；**只有用户真正动了该项**才写显式值（改别的字段**不**物化继承 ⇒ 老数据不改坏）。

纪律（架构方案 §4-B）：
  B1 重填下拉框（素材）前 blockSignals + 填完 findData 还原。
  B2 数据→控件回填走 `_loading` 守卫，**置位在动控件之前**，**≥2 处独立复位**；
     写回（commit）在守卫置位期间直接 return，绝不自激。
  B3 禁止裸 except 静默吞异常。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QSlider, QVBoxLayout, QWidget,
)

from . import theme

log = logging.getLogger("ercue.ui.props")

TRANS_LABELS = (("fade", "淡入"), ("cut", "硬切"), ("slide", "滑动"),
                ("zoom", "缩放"), ("glitch", "故障"))
TRANS_LABELS_OUT = (("fade", "淡出"), ("cut", "硬切"), ("slide", "滑动"),
                    ("zoom", "缩放"), ("glitch", "故障"))
BLEND_LABELS = (("normal", "常规"), ("add", "添加"), ("screen", "屏幕"),
                ("multiply", "多层"), ("lighten", "变亮"), ("darken", "变暗"),
                ("subtract", "减少"))

# ★ 需求 7：变速滑条的取值范围**对照现有输入框 `sp_speed` 的 min/max**（`_dspin(0.1, 3.0, ...)`），
#   不凭空发明。模型侧 `CueItem.normalized()` 亦把 speed 钳到 0.1~3.0（`cuesheet.py`）——两处一致。
SPEED_MIN = 0.1                 # 与 sp_speed 的最小值一致
SPEED_MAX = 3.0                 # 与 sp_speed 的最大值一致
SPEED_SLIDER_SCALE = 100        # 滑条整数 = round(倍率 × 100)，即 0.10~3.00 → 10~300

# ★ 第五轮④：大小滑条的取值范围**对照现有输入框 `sp_scale` 的 min/max**（`_dspin(0.05, 8.0, ...)`），
#   不凭空发明。数字框精度 2 位小数 ⇒ 滑条整数 = round(scale × 100)，即 0.05~8.00 → 5~800。
SCALE_MIN = 0.05                # 与 sp_scale 的最小值一致
SCALE_MAX = 8.0                 # 与 sp_scale 的最大值一致
SCALE_SLIDER_SCALE = 100

# ★ 需求 9（改名 + 语义）：混合 / 不透明度两项的标签随"选中态"切换。
LBL_BLEND_ITEM = "本素材混合"           # 选中片段 ⇒ 读写该片段
LBL_BLEND_LAYER = "本层混合"            # 未选中 ⇒ 读写当前层（回退语义）
LBL_OP_ITEM = "本素材不透明度"
LBL_OP_LAYER = "本层不透明度"


def _dspin(lo, hi, dec=2, step=0.05, suffix="", prefix="", special=""):
    sb = QDoubleSpinBox()
    sb.setRange(lo, hi)
    sb.setDecimals(dec)
    sb.setSingleStep(step)
    if suffix:
        sb.setSuffix(suffix)
    if prefix:
        sb.setPrefix(prefix)
    if special:
        sb.setSpecialValueText(special)
    return sb


class PropsPanel(QWidget):
    """片段属性编辑（④ 片段属性）。

    ★ 需求 9（选中态语义）：**混合 / 不透明度**两项的读写目标随选中态切换 ——
      · **选中了片段** ⇒ 读写该片段的 `item.blend` / `item.opacity`（标签「本素材混合/不透明度」）；
        片段**没设过**时按 `effective_blend/effective_opacity` 的**实际生效值**显示。
      · **未选中片段** ⇒ 读写当前**层** `layer.blend` / `layer.opacity`（标签「本层混合/不透明度」）。
      ⚠ 第五轮⑤：**已去掉"继承本层 / 恢复继承"的 UI 入口**；底层 `""`/`None` = 继承本层 的
        **数据契约不变**（别的模块仍依赖）。只有用户**真正动了**混合/不透明度才写显式值 ——
        改其它字段**不会**把继承态物化（老码表不被改坏）。

    ★ 需求 7 / 第五轮④：变速、大小均为**滑条 + 输入框双向同步**（纯 UI，底层语义不变）。
    """

    changed = Signal()                 # 用户改了任一控件（非回填）→ 主窗口落模型

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loading = False
        self.layer = None
        self.item = None
        self._media_ids = []
        # ★ 第五轮⑤：混合 / 不透明度"用户是否真的动过"标记 —— 只有动过才写显式值，
        #   否则保留 `""` / `None`（继承本层）⇒ 改别的字段**不会**把继承态物化（老数据不改坏）。
        self._blend_dirty = False
        self._op_dirty = False
        lv = QVBoxLayout(self)
        lv.setContentsMargins(6, 6, 6, 6)
        lv.setSpacing(4)

        self.lbl_sel = QLabel("未选中")
        self.lbl_sel.setStyleSheet("font-size:11px;color:%s;" % theme.c("fg_dim"))
        lv.addWidget(self.lbl_sel)

        g = QGridLayout()
        g.setVerticalSpacing(4)
        r = 0

        def add(label, wdg):
            nonlocal r
            g.addWidget(QLabel(label), r, 0)
            g.addWidget(wdg, r, 1)
            r += 1

        self.cb_clip = QComboBox()
        self.cb_clip.currentIndexChanged.connect(self._on_edit)
        add("素材", self.cb_clip)

        self.sp_at = _dspin(0, 100000, 2, 0.1, " 秒")
        self.sp_at.valueChanged.connect(self._on_edit)
        add("起点", self.sp_at)

        self.sp_len = _dspin(0, 100000, 2, 0.1, " 秒", special="到下一段")
        self.sp_len.valueChanged.connect(self._on_edit)
        add("时长", self.sp_len)

        # ★★ 入点 / 出点（旧实现从未做出的功能）
        self.sp_src_in = _dspin(0, 100000, 2, 0.1, " 秒")
        self.sp_src_in.setToolTip("素材从第几秒开始用（旧实现从未做出的功能）")
        self.sp_src_in.valueChanged.connect(self._on_edit)
        add("入点", self.sp_src_in)

        self.sp_src_out = _dspin(0, 100000, 2, 0.1, " 秒", special="到素材末尾")
        self.sp_src_out.setToolTip("用到素材第几秒为止（0 = 到素材末尾）")
        self.sp_src_out.valueChanged.connect(self._on_edit)
        add("出点", self.sp_src_out)

        self.cb_tin = QComboBox()
        for k, lbl in TRANS_LABELS:
            self.cb_tin.addItem(lbl, k)
        self.cb_tin.currentIndexChanged.connect(self._on_edit)
        add("入场", self.cb_tin)

        self.sp_tin = _dspin(0, 64, 2, 0.1, " 秒")
        self.sp_tin.valueChanged.connect(self._on_edit)
        add("入场时长", self.sp_tin)

        self.cb_tout = QComboBox()
        for k, lbl in TRANS_LABELS_OUT:
            self.cb_tout.addItem(lbl, k)
        self.cb_tout.currentIndexChanged.connect(self._on_edit)
        add("出场", self.cb_tout)

        self.sp_tout = _dspin(0, 64, 2, 0.1, " 秒")
        self.sp_tout.valueChanged.connect(self._on_edit)
        add("出场时长", self.sp_tout)

        # ★ 需求 7：变速 = 滑条 + 输入框并排、双向同步（范围严格对照 sp_speed 的 min/max）。
        self.sp_speed = _dspin(SPEED_MIN, SPEED_MAX, 2, 0.05, prefix="×")
        self.sp_speed.setValue(1.0)                  # 初值 ×1.00（连接信号**之前**设，避免伪信号）
        self.sld_speed = QSlider(Qt.Horizontal)
        self.sld_speed.setRange(int(round(SPEED_MIN * SPEED_SLIDER_SCALE)),
                                int(round(SPEED_MAX * SPEED_SLIDER_SCALE)))   # 10 .. 300
        self.sld_speed.setSingleStep(int(round(0.05 * SPEED_SLIDER_SCALE)))   # 0.05
        self.sld_speed.setPageStep(int(round(0.25 * SPEED_SLIDER_SCALE)))
        self.sld_speed.setValue(int(round(1.0 * SPEED_SLIDER_SCALE)))         # 初值 100
        self.sld_speed.setToolTip("拖动改变速（%g× ~ %g×），与右侧输入框双向同步"
                                  % (SPEED_MIN, SPEED_MAX))
        speedw = QWidget()
        sl = QHBoxLayout(speedw)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(4)
        sl.addWidget(self.sld_speed, 1)
        sl.addWidget(self.sp_speed)
        # 双向同步 + 防自激：两处写入都包在 `_loading` 守卫内（置位在动控件之前）；
        # 提交走 spinbox 的 `valueChanged → _on_edit`（滑条侧由 `_on_speed_slider` 显式 emit）。
        self.sld_speed.valueChanged.connect(self._on_speed_slider)
        self.sp_speed.valueChanged.connect(self._on_speed_spin)
        self.sp_speed.valueChanged.connect(self._on_edit)
        add("变速", speedw)

        self.chk_crop = QCheckBox("裁切")
        self.chk_crop.toggled.connect(self._on_edit)
        add("画面裁切", self.chk_crop)
        cropw = QWidget()
        cg = QHBoxLayout(cropw)
        cg.setContentsMargins(0, 0, 0, 0)
        self.sp_crop = []
        for nm in ("X", "Y", "W", "H"):
            sb = _dspin(0, 1, 2, 0.02)
            sb.setToolTip(nm)
            sb.valueChanged.connect(self._on_edit)
            cg.addWidget(sb)
            self.sp_crop.append(sb)
        add("", cropw)

        # ★ 第五轮④：大小 = 滑条 + 输入框并排、双向同步（照 sp_speed 的模式；范围对照 sp_scale 的 min/max）。
        self.sp_scale = _dspin(SCALE_MIN, SCALE_MAX, 2, 0.05)
        self.sp_scale.setValue(1.0)                  # 初值 1.00（连接信号**之前**设，避免伪信号）
        self.sld_scale = QSlider(Qt.Horizontal)
        self.sld_scale.setRange(int(round(SCALE_MIN * SCALE_SLIDER_SCALE)),
                                int(round(SCALE_MAX * SCALE_SLIDER_SCALE)))   # 5 .. 800
        self.sld_scale.setSingleStep(int(round(0.05 * SCALE_SLIDER_SCALE)))   # 0.05
        self.sld_scale.setPageStep(int(round(0.25 * SCALE_SLIDER_SCALE)))     # 0.25
        self.sld_scale.setValue(int(round(1.0 * SCALE_SLIDER_SCALE)))         # 初值 100
        self.sld_scale.setToolTip("拖动改大小（%g ~ %g），与右侧输入框双向同步"
                                  % (SCALE_MIN, SCALE_MAX))
        scalew = QWidget()
        sh = QHBoxLayout(scalew)
        sh.setContentsMargins(0, 0, 0, 0)
        sh.setSpacing(4)
        sh.addWidget(self.sld_scale, 1)
        sh.addWidget(self.sp_scale)
        # 双向同步 + 防自激：与 sp_speed **同一套 `_loading` 守卫**（置位在动控件之前）。
        self.sld_scale.valueChanged.connect(self._on_scale_slider)
        self.sp_scale.valueChanged.connect(self._on_scale_spin)
        self.sp_scale.valueChanged.connect(self._on_edit)
        add("大小", scalew)

        posw = QWidget()
        pg = QHBoxLayout(posw)
        pg.setContentsMargins(0, 0, 0, 0)
        self.sp_px = _dspin(-100, 100, 1, 1.0, "%")
        self.sp_py = _dspin(-100, 100, 1, 1.0, "%")
        for sb in (self.sp_px, self.sp_py):
            sb.valueChanged.connect(self._on_edit)
            pg.addWidget(sb)
        add("位移", posw)

        # ★ 第五轮⑤：本素材混合 —— 下拉**只有具体混合模式**（已去掉「（继承本层）」入口）。
        #   片段"没设过"时按 `effective_blend` 的**实际生效值**显示（见 `refresh`）。
        self.lbl_blend = QLabel(LBL_BLEND_ITEM)
        g.addWidget(self.lbl_blend, r, 0)
        self.cb_blend = QComboBox()
        for k, lbl in BLEND_LABELS:
            self.cb_blend.addItem(lbl, k)
        self.cb_blend.currentIndexChanged.connect(self._on_blend_changed)
        g.addWidget(self.cb_blend, r, 1)
        r += 1

        # ★ 第五轮⑤：本素材不透明度 —— **只有数值输入框**（已去掉「继承本层」勾选框）。
        #   片段"没设过"时按 `effective_opacity` 的实际生效值显示（见 `refresh`）。
        self.lbl_op = QLabel(LBL_OP_ITEM)
        g.addWidget(self.lbl_op, r, 0)
        self.sp_op = _dspin(0, 100, 0, 1.0, "%")
        self.sp_op.valueChanged.connect(self._on_op_changed)
        g.addWidget(self.sp_op, r, 1)
        r += 1

        self.le_note = QLineEdit()
        self.le_note.editingFinished.connect(self._on_edit)
        add("备注", self.le_note)

        lv.addLayout(g)
        lv.addStretch(1)

        # 计数（B2 哨兵用：统计 commit 被真正执行的次数）
        self.commit_calls = 0

    # ==================================================================
    # 数据 → 控件（B2）
    # ==================================================================
    def set_media_list(self, sheet):
        """B1：重填素材下拉框前 blockSignals，填完按 clip_id 还原选中项。"""
        self._media_ids = [m.clip_id for m in sheet.media] if sheet is not None else []
        cur = self.cb_clip.currentData() or ""
        self.cb_clip.blockSignals(True)
        try:
            self.cb_clip.clear()
            self.cb_clip.addItem("（无）", "")
            for m in (sheet.media if sheet is not None else []):
                self.cb_clip.addItem(m.name or m.clip_id, m.clip_id)
            j = self.cb_clip.findData(cur)
            self.cb_clip.setCurrentIndex(j if j >= 0 else 0)
        finally:
            self.cb_clip.blockSignals(False)

    def set_selection(self, layer, item):
        self.layer = layer
        self.item = item
        self.refresh()

    def refresh(self):
        it, ly = self.item, self.layer
        on = it is not None
        for w in (self.cb_clip, self.sp_at, self.sp_len, self.sp_src_in, self.sp_src_out,
                  self.cb_tin, self.sp_tin, self.cb_tout, self.sp_tout,
                  self.sld_speed, self.sp_speed,
                  self.chk_crop, self.sld_scale, self.sp_scale, self.sp_px, self.sp_py,
                  self.le_note):
            w.setEnabled(on)
        for sb in self.sp_crop:
            sb.setEnabled(on and self.chk_crop.isChecked())
        self.lbl_sel.setText("未选中" if not on else "选中：%s / %s"
                             % ((ly.name or ly.lyr_id) if ly is not None else "?", it.item_id))

        self._blend_dirty = False                            # ★ 第五轮⑤：重新回填 = 用户尚未改动
        self._op_dirty = False
        self._loading = True                                 # ★ 置位在动控件之前（B2）
        try:
            if not on:
                # ★ 2026-10-22 用户要求：**去掉「本层混合」**（保留「本素材混合」）。
                #   未选中片段时，混合这一行整体隐藏（不显示、也不写 layer.blend）；
                #   不透明度仍按「本层不透明度」编辑当前层。
                self.lbl_blend.setVisible(False)
                self.cb_blend.setVisible(False)
                # ★ 2026-10-22 追加：**本层不透明度也去掉** —— 未选中片段时整组层属性都隐藏，
                #   只保留"选中素材才出现"的属性；不写 layer.opacity。
                self.lbl_op.setVisible(False)
                self.sp_op.setVisible(False)
                self.lbl_op.setText(LBL_OP_LAYER)
                if ly is not None:
                    self._set_combo(self.cb_blend, ly.blend)
                    self.sp_op.setValue(round(float(ly.opacity) * 100))
                    self.cb_blend.setEnabled(True)
                    self.sp_op.setEnabled(True)
                else:
                    self.cb_blend.setEnabled(False)
                    self.sp_op.setEnabled(False)
            else:
                # 选中片段 ⇒ 混合/不透明度编辑**该片段**（标签用「本素材…」）
                self.lbl_blend.setVisible(True)
                self.cb_blend.setVisible(True)
                self.lbl_op.setVisible(True)
                self.sp_op.setVisible(True)
                self.lbl_blend.setText(LBL_BLEND_ITEM)
                self.lbl_op.setText(LBL_OP_ITEM)
                self._set_combo(self.cb_clip, it.clip_id)
                self.sp_at.setValue(float(it.at_sec))
                self.sp_len.setValue(float(it.end_sec) - float(it.at_sec)
                                     if it.end_sec is not None else 0.0)
                self.sp_src_in.setValue(float(it.src_in_sec or 0.0))
                self.sp_src_out.setValue(float(it.src_out_sec or 0.0))
                self._set_combo(self.cb_tin, it.trans_in)
                self.sp_tin.setValue(float(it.trans_in_sec or 0.0))
                self._set_combo(self.cb_tout, it.trans_out)
                self.sp_tout.setValue(float(it.trans_out_sec or 0.0))
                # ★ 需求 7：滑条与输入框**同时**回填（同一权威值；守卫内不乱发信号）
                spd = float(it.speed or 1.0)
                self.sp_speed.setValue(spd)
                self.sld_speed.setValue(int(round(spd * SPEED_SLIDER_SCALE)))
                self.chk_crop.setChecked(bool(it.crop))
                c = it.crop if it.crop else [0.0, 0.0, 1.0, 1.0]
                for sb, v in zip(self.sp_crop, c):
                    sb.setValue(float(v))
                x = it.xform or {}
                # ★ 第五轮④：大小滑条与输入框**同时**回填（同一权威值；守卫内不乱发信号）
                sc = float(x.get("scale", 1.0) or 1.0)
                self.sp_scale.setValue(sc)
                self.sld_scale.setValue(int(round(sc * SCALE_SLIDER_SCALE)))
                self.sp_px.setValue(float(x.get("x", 0.0) or 0.0))
                self.sp_py.setValue(float(x.get("y", 0.0) or 0.0))
                self.le_note.setText(it.note or "")
                # ★ 第五轮⑤：去掉"继承"入口后，片段没设过 ⇒ 按**实际生效值**显示（读 effective_*）；
                #   用户没动过该项则 commit 不写（保留 ""/None 继承，老数据不改坏）。
                self._set_combo(self.cb_blend,
                                (ly.effective_blend(it) if ly is not None else (it.blend or "normal")))
                eff_o = (ly.effective_opacity(it) if ly is not None
                         else (1.0 if it.opacity is None else float(it.opacity)))
                self.sp_op.setValue(round(float(eff_o) * 100))
                self.sp_op.setEnabled(True)
                self.cb_blend.setEnabled(True)
        finally:
            self._loading = False                            # 复位之一
        self._loading = False                                # 复位之二（防御）

    @staticmethod
    def _set_combo(cb, value):
        j = cb.findData(value)
        cb.setCurrentIndex(j if j >= 0 else 0)

    # ==================================================================
    # 滑条 ↔ 输入框 双向同步（需求 7 变速 / 第五轮④ 大小）
    #   防自激 = **同一套** `_loading` 守卫，**置位在动控件之前**；各 handler 各自 finally 复位。
    # ==================================================================
    def _on_speed_slider(self, v):
        """变速滑条 → 输入框。改输入框时置守卫（阻断其 `_on_edit` 回写滑条），随后显式 emit 提交。"""
        if self._loading:
            return
        self._loading = True                                 # ★ 置位在动控件之前
        try:
            self.sp_speed.setValue(v / float(SPEED_SLIDER_SCALE))
        finally:
            self._loading = False                            # 复位
        self.changed.emit()                                  # 用户操作 → 提交（不依赖 spin 信号）

    def _on_speed_spin(self, v):
        """变速输入框 → 滑条。改滑条时置守卫，避免滑条再回写输入框造成自激。"""
        if self._loading:
            return
        self._loading = True                                 # ★ 置位在动控件之前
        try:
            self.sld_speed.setValue(int(round(float(v) * SPEED_SLIDER_SCALE)))
        finally:
            self._loading = False                            # 复位
        # 提交由同一信号上的 `_on_edit` 负责（本槽返回后守卫已复位）

    def _on_scale_slider(self, v):
        """大小滑条 → 输入框（与 `_on_speed_slider` **同一套守卫模式**）。"""
        if self._loading:
            return
        self._loading = True                                 # ★ 置位在动控件之前
        try:
            self.sp_scale.setValue(v / float(SCALE_SLIDER_SCALE))
        finally:
            self._loading = False                            # 复位
        self.changed.emit()                                  # 用户操作 → 提交

    def _on_scale_spin(self, v):
        """大小输入框 → 滑条（与 `_on_speed_spin` **同一套守卫模式**）。"""
        if self._loading:
            return
        self._loading = True                                 # ★ 置位在动控件之前
        try:
            self.sld_scale.setValue(int(round(float(v) * SCALE_SLIDER_SCALE)))
        finally:
            self._loading = False                            # 复位
        # 提交由同一信号上的 `_on_edit` 负责（本槽返回后守卫已复位）

    # ---- 第五轮⑤：混合 / 不透明度"用户是否动过"标记（决定 commit 是否写显式值）----
    def _on_blend_changed(self, *_):
        """用户改了混合下拉 ⇒ 打标记（commit 才写显式值；否则保留 `""` = 继承本层）。"""
        if self._loading:
            return
        self._blend_dirty = True
        self.changed.emit()

    def _on_op_changed(self, *_):
        """用户改了不透明度输入框 ⇒ 打标记（commit 才写显式值；否则保留 `None` = 继承本层）。"""
        if self._loading:
            return
        self._op_dirty = True
        self.changed.emit()

    # ==================================================================
    # 控件 → 数据（由主窗口在用户改动时调用）
    # ==================================================================
    def _on_edit(self, *_):
        if self._loading:                                    # B2：回填期间不写回
            return
        self.changed.emit()

    def commit(self) -> bool:
        """把面板值写回模型。返回是否真的写了（未选中且无层 → False）。

        ★ 需求 9③ 选中态语义：**选中片段** ⇒ 混合/不透明度写该片段；**未选中片段** ⇒ 写当前**层**。
          （底层 `""`/`None` = 继承本层 的契约不变。）
        ★ 第五轮⑤：片段级混合/不透明度**只有用户动过**（`_blend_dirty`/`_op_dirty`）才写显式值，
          否则保留 `""`/`None` 继承 ⇒ 改别的字段不物化继承（老码表不被改坏）。
        """
        if self._loading:                                    # B2：守卫期间拒绝写回
            return False
        it, ly = self.item, self.layer
        if it is None and ly is None:
            return False
        self.commit_calls += 1
        if it is not None:
            # ---- 选中片段：写该片段（含素材级混合/不透明度）----
            it.clip_id = self.cb_clip.currentData() or ""
            it.at_sec = max(0.0, float(self.sp_at.value()))
            ln = float(self.sp_len.value())
            it.end_sec = (it.at_sec + ln) if ln > 0 else None
            it.src_in_sec = max(0.0, float(self.sp_src_in.value()))
            so = float(self.sp_src_out.value())
            # ★★ 2026-10-19 审计修复：出点必须晚于入点。旧实现直接存，
            #   随后 `reconcile_item_timing` 里 `used = max(0, so-si) = 0`
            #   ⇒ 片段被压成 3px 竖条、数据里留下 end==at 的退化片段（用户以为片段丢了）。
            if so > 0.0 and so <= it.src_in_sec:
                so = it.src_in_sec + 0.01
                try:
                    self.sp_src_out.setValue(so)
                except Exception:                                # noqa: BLE001
                    pass
            it.src_out_sec = so if so > 0 else None
            it.trans_in = self.cb_tin.currentData() or "fade"
            it.trans_in_sec = float(self.sp_tin.value())
            it.trans_out = self.cb_tout.currentData() or "fade"
            it.trans_out_sec = float(self.sp_tout.value())
            it.speed = float(self.sp_speed.value())
            it.crop = ([float(sb.value()) for sb in self.sp_crop]
                       if self.chk_crop.isChecked() else None)
            it.xform = {"scale": float(self.sp_scale.value()),
                        "x": float(self.sp_px.value()), "y": float(self.sp_py.value())}
            # ★ 第五轮⑤：混合/不透明度**去掉"继承"UI 入口**后 —— 只有用户**真正动过**该项才写
            #   显式值；否则保留原值（`""`/`None` = 继承本层）⇒ 改别的字段**不会**物化继承
            #   （老码表里 `""`/`None` 的片段不被改坏；数据契约不变）。
            if self._blend_dirty or (it.blend or ""):
                it.blend = str(self.cb_blend.currentData() or "")
            if self._op_dirty or (it.opacity is not None):
                it.opacity = max(0.0, min(1.0, float(self.sp_op.value()) / 100.0))
            it.note = self.le_note.text()
            it.normalized()
            if ly is not None:
                ly.items.sort(key=lambda x: (x.at_sec, x.item_id))
        elif ly is not None:
            # ---- 未选中片段：写当前层 ----
            ly.blend = self.cb_blend.currentData() or "normal"
            ly.opacity = max(0.0, min(1.0, float(self.sp_op.value()) / 100.0))
        return True
