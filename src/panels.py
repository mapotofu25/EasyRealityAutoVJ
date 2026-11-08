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

from tags_def import (DYNAMIC_LOW, DYNAMIC_MID, DYNAMIC_HIGH, DYNAMIC_FLICKER,
                      COVERAGE_FULL, COVERAGE_PART)
from PySide6.QtCore import (Qt, QSize, Signal, QMimeData, QTimer, QPoint, QRectF,
                            QThread, QPointF, QEvent)
from PySide6.QtGui import (QColor, QPixmap, QImage, QDrag, QAction, QPainter, QPen,
                           QIcon, QFont, QPolygonF)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QSlider,
    QComboBox, QScrollArea, QListWidget, QListWidgetItem, QListView, QFrame,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QMenu, QDialog, QCheckBox, QProgressBar, QDialogButtonBox,
    QSizePolicy, QApplication, QDoubleSpinBox, QSpinBox, QGroupBox, QToolBox,
    QToolButton, QInputDialog, QLineEdit, QRadioButton, QButtonGroup, QSplitter,
    QFileDialog, QMessageBox, QPlainTextEdit, QStyledItemDelegate, QStyle,
)

from engine import BLEND_MODES, BLEND_LABELS, MODE_LABELS
from media_manager import MediaGrid, MediaItem, ThumbBackfillWorker, pixmap_file
from i18n import T, Tf, retranslate as i18n_retranslate
import theme

CLIP_MIME = "application/x-autovj-media"
HEADER_W = 456          # 图层头列宽（**默认值**；用户可拖分隔条改，见 ui.layer_header_w）
HEADER_W_MIN = 220      # 左列最窄（再窄名称/码表下拉就挤没了）
HEADER_W_MAX = 760      # 左列最宽（别把素材格挤到看不见）
#: 图层头的最小可能高度 = 行高滑轨的**下限**。
#  ⚠ 行高比它小的时候，行高**根本压不下去**（整行被图层头撑住 ⇒ 素材上下各留一条黑边，
#    用户 2026-11-06 报「行高缩小了，素材间距也应该缩小」）。
#  ⚠ 图层头有**两个尺寸档**（见 `LayerStackPanel._apply_row_metrics`）：
#      · 标准档（行高 ≥ `HEADER_COMPACT_H`）：2×2 按钮 20 高 + 边距/边框 ⇒ 实测头高 55
#      · 紧凑档（行高 <  `HEADER_COMPACT_H`）：按钮 16 高 + 边距收窄 ⇒ 实测头高 37
#    ⇒ 下限取 **38**（紧凑档实测 37，留 1px 余量给不同 DPI 取整）。
#    用户 2026-11-06：「行高最小想再小一点，38 那么小」。
HEADER_MIN_H = 38
#: 行高 **小于** 该值 ⇒ 图层头切「紧凑档」（否则标准档 55 高会把整行撑住、2×2 被裁）。
#  取 56 是为了让"标准档的最小可行行高"（55）仍然落在标准档里 ⇒ 默认观感逐像素不变。
HEADER_COMPACT_H = 56
LIB_THUMB_SIZES = [(96, 54, 112, 82), (160, 90, 178, 138), (240, 135, 262, 194)]
COVER_ICON = 64         # 曲库列表每行左侧的歌曲封面边长默认值（px）
# ★ 2026-10-17（用户要求「用不一样的颜色」）：曲库里**码表带来**的派生行用**青绿**
#   （与码表 UI 的主色一致）。蓝 `#7aa2f7` 已被「导入曲库分析包」占用、
#   红 `#e06666` 已被「文件缺失」占用 —— 三者语义必须能一眼分开。
CUE_ROW_COLOR = "#4ec9b0"
# ★ 2026-10-30（用户要求）：**只有正在演出中使用的码表**（引擎当前装载的那张）
#   才用青绿；其他码表的曲目一律**灰色** —— 否则曲库里躺着七八张表的歌，
#   看着像"它们都在参与演出"，现场会误判。
#   灰不能太暗（要能读），也不能抢色（要明显退到背景）：取中间偏暗的中性灰。
CUE_ROW_DIM_COLOR = "#6b6b6b"
# 封面尺寸档位（右键「封面大小」里选）。⚠ 值要在下面的 COVER_SIZES 里唯一，
#   因为它同时是配置里存的值 —— 改档位就是要让老的配置能对上（对不上回退默认）。
COVER_SIZES = [("小", 40), ("中", 64), ("大", 96), ("特大", 128)]


def _disp_w(s):
    """粗略的**显示宽度**（单位 ≈ 半个汉字宽）：CJK 记 2，其余记 1。

    只用来判断「这条路径塞不塞得进 210px 的来源列」，不需要精确 —— 真正的
    像素精度由 Qt 的省略号兜底，这里只要别让 CJK 路径把列撑爆。
    """
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in str(s))


def _clip(s, budget):
    """按**显示宽度**把 `s` 截到 `budget` 以内（超出时末尾补一个 …）。"""
    if _disp_w(s) <= budget:
        return s
    out, w = [], 0
    for ch in s:
        cw = 2 if ord(ch) > 0x2E7F else 1
        if w + cw > budget - 1:
            break
        out.append(ch)
        w += cw
    return "".join(out) + "…"


def _dir_short(path, maxw=28, measure=None):
    """歌曲**所在文件夹**（曲库「来源」列用）。

    ★ 2026-10-30 用户：「本地的歌来源写歌曲文件硬盘位置」——曲风砍掉以后
      `source` 字段不再产出，本机曲目的来源列一直是空的。

    ★★ 2026-10-31（用户「这些怎么不显示后面的」→「我说的是来源」）：
      `measure` 给了就按**像素**裁（精度最高，和 Qt 自己的省略口径一致）。
      ⚠ 之前只按 `_disp_w`（半角=1 / 全角=2）估算，比例字体下差得很远：
        中文实际宽度远大于 2×ASCII ⇒ 估算"放得下"时 Qt 还要再截一刀，
        用户看到的就是一串 `…` 顶在末尾。

    省略策略（两种度量一致）：
      · 整条目录**放得下就原样显示**；
      · 放不下就从**左边**逐级丢（盘符 + `…` + 末尾几级目录）—— 绝不靠 Qt 的右侧省略，
        那会把最有用的"最后一级目录名"截掉；
      · 连"盘符 + 末一级"都放不下时，把末一级本身截断（只留前几个字 + `…`），
        **绝不只剩盘符 + `…`**（那等于什么都没说）。
    """

    def _w(s):
        if measure is not None:
            try:
                return float(measure(s))
            except Exception:                                      # noqa: BLE001
                pass
        return float(_disp_w(s))

    def _cut(s, budget):
        """把 `s` 裁到 `budget`（按当前度量）。

        像素模式用**中间省略**（`2025…0126]`）—— 目录名的两头都带信息
        （前面是日期、后面是编号），只砍尾巴会把最关键的编号砍掉。
        半角单位模式保持原来的尾部省略（没有度量函数可用）。
        """
        if _w(s) <= budget:
            return s
        if measure is None:
            return _clip(s, int(budget))
        try:
            best, lo, hi = "…", 1, len(s) - 1
            while lo <= hi:
                mid = (lo + hi) // 2
                keep_h = (mid + 1) // 2
                keep_t = mid - keep_h
                cand = s[:keep_h] + "…" + (s[-keep_t:] if keep_t > 0 else "")
                if measure(cand) <= budget:
                    best = cand
                    lo = mid + 1
                else:
                    hi = mid - 1
            return best
        except Exception:                                          # noqa: BLE001
            return s

    try:
        d = os.path.dirname(str(path))
    except Exception:                                          # noqa: BLE001
        return ""
    if not d:
        return ""
    parts = [x for x in d.replace("/", os.sep).split(os.sep) if x]
    if len(parts) <= 1:                                        # 盘根 `<盘符>:\`
        return d.rstrip("\\/") or d
    norm = os.sep.join(parts)
    if d[:1] in ("/", "\\"):                                   # POSIX 绝对路径
        norm = os.sep + norm
    # 逐级丢最外层，直到放得下；`k` = 保留末尾几级
    for k in range(len(parts), 0, -1):
        cand = (norm if k == len(parts) else
                parts[0] + os.sep + "…" + os.sep + os.sep.join(parts[-k:]))
        if _w(cand) <= maxw:
            return cand
    # 退到底：盘符 + … + **截断的末一级**（只留 `<盘符>:\…` 没有信息量）
    head = parts[0] + os.sep + "…" + os.sep
    _bud = maxw - _w(head)
    # ⚠ 列被压到很窄（实测窄窗下来源列只剩 ~60px）时，光"盘符 + … + 分隔符"就把预算
    #   吃光了，再拼什么都放不下 ⇒ Qt 会把它裁回 `<盘符>:\…\`（等于什么都没说）。
    #   这种情况**丢掉盘符**，只留末一级的前几个字 —— 至少能看出是哪一层目录。
    if measure is not None and _bud < 30:
        return _cut(parts[-1], max(30.0, maxw))
    _floor = 50.0 if measure is not None else 4.0
    return head + _cut(parts[-1], max(_floor, _bud))


# 「来源」列的**原始文件路径**存这两个 role 上，列宽一变就据此重算省略
# （见 `MusicLibraryPanel._restyle_src_col`）。选 +5/+6 是为了避开
# UserRole（= 行主键）和波形 delegate 用的 UserRole。
_SRC_ROLE = int(Qt.ItemDataRole.UserRole) + 5
_SRC_TAIL_ROLE = int(Qt.ItemDataRole.UserRole) + 6


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


class I18nWidget(QWidget):
    """内嵌面板基类：首次显示与运行中切语言时都刷新文案（与 I18nDialog 同思路）。"""

    def showEvent(self, ev):
        try:
            i18n_retranslate(self)
        except Exception:                                        # noqa: BLE001
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
    """新建图层对话框：名称 + 类型(手动 / 自动 / Kv 主视觉 / **码表**) + 角色(前景/背景)。

    自动图层按动态档匹配画面；Kv 主视觉是唯一的待机层；
    **码表图层**是"按码表接管画面"的容器层（选中后由 `ui_main.layer_add`
    立刻弹「选一张表」——本对话框只负责把类型带出去）。
    """

    KV_DEFAULT_NAME = "Kv 主视觉"
    CUE_DEFAULT_NAME = "码表图层"

    def __init__(self, parent=None, default_name="图层 1"):
        super().__init__(parent)
        self.setWindowTitle( T("新建图层"))
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
        self.rb_auto = QRadioButton("自动（按动态档匹配画面）")
        self.rb_cue = QRadioButton("码表图层（按码表接管画面）")
        self.rb_kv = QRadioButton("Kv 主视觉图层（待机层：无声音时切入）")
        self.rb_manual.setChecked(True)
        # 必须显式分组：同一 parent 下的 QRadioButton 会自动互斥成一组，
        # 否则点"前景"会把"自动"取消掉（类型和角色串组）
        self._grp_type = QButtonGroup(self)
        self._grp_type.addButton(self.rb_manual)
        self._grp_type.addButton(self.rb_auto)
        self._grp_type.addButton(self.rb_cue)
        self._grp_type.addButton(self.rb_kv)
        tv = QVBoxLayout()
        tv.setSpacing(4)
        tv.addWidget(self.rb_manual)
        tv.addWidget(self.rb_auto)
        tv.addWidget(self.rb_cue)
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

        for rb in (self.rb_manual, self.rb_auto, self.rb_cue, self.rb_kv):
            rb.toggled.connect(self._on_type)
        self._on_type()

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText( T("确定"))
        bb.button(QDialogButtonBox.Cancel).setText( T("取消"))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _on_type(self, *_):
        auto = self.rb_auto.isChecked()
        self._role_box.setVisible(auto)
        if auto:
            self._hint.setText( T("自动图层会按「低/中/高动态」实时替换画面，手动拖入的素材不生效"))
        elif self.rb_cue.isChecked():
            self._hint.setText(T("码表图层：画面由码表接管（片段入点/出场都按码表走）。\n"
                                 "确定后会让你选择「应用哪一张码表」——还没导入码表的话，\n"
                                 "请先到「码表」页签点「添加…」。"))
        elif self.rb_kv.isChecked():
            self._hint.setText(T("Kv 主视觉图层：没有声音时把待机画面带进来、有声音时带出去。\n"
                                 "全局只能有一个，添加后自动置顶；参数在右侧设置区顶部单独配置。"))
        else:
            self._hint.setText("")
        # 名字跟着类型走：选 Kv 时默认名换成「Kv 主视觉」、选码表时换成「码表图层」
        # （不再叫「图层 N」），切回普通图层再换回来。用户手动改过名字就一律不覆盖。
        if not self._name_touched:
            if self.rb_kv.isChecked():
                self.name_edit.setText(self.KV_DEFAULT_NAME)
            elif self.rb_cue.isChecked():
                self.name_edit.setText(self.CUE_DEFAULT_NAME)
            else:
                self.name_edit.setText(self._default_name)

    def result_data(self):
        """返回 (name, auto, role, is_kv, is_cue)。唯一调用点：ui_main.layer_add。

        ⚠ 改返回值元数必须 grep 全部调用点（项目铁律）。"""
        name = self.name_edit.text().strip()
        auto = self.rb_auto.isChecked()
        is_kv = self.rb_kv.isChecked()
        is_cue = self.rb_cue.isChecked()
        role = "fg" if self.rb_fg.isChecked() else "bg"
        return name, auto, role, is_kv, is_cue


# ★ 2026-10-30 用户「大砍刀」：CLIP 视觉打标 / 亮度分档 / 曲风识别整条删除后，
#   这里原本的四个对话框一并移除：
#     · `TagEditDialog`        —— 编辑素材内容标签（颜色/元素/场景/人物/风格）
#     · `TagFilterDialog`      —— 按标签筛选素材
#     · `GenreEditDialog`      —— 纠正曲风
#     · `GenreVisualEditDialog`—— 曲风 → 画面标签映射编辑器
#   素材现在只有「低/中/高动态 + 频闪 + 铺满/局部」这几个自动算出来的标签，
#   在「扫描素材」里产出于 `tagger`，不需要手工编辑词表。


# ★ 2026-10-30：`MusicLibraryDialog`（旧的弹窗版曲库，660×540）早已被整页的
#   `MusicLibraryPanel` 取代，本次「大砍刀」一并删除，避免它继续引用已移除的曲风接口。


_cover_placeholder_cache = {}


def cover_placeholder(size):
    """没有封面时的占位图（圆角方块 + 音符），按尺寸缓存。

    ⚠ 只画一次：几百上千行列表如果每行都现画 QPixmap，滚动会明显卡。
    """
    key = int(size)
    hit = _cover_placeholder_cache.get(key)
    if hit is not None:
        return hit
    pm = QPixmap(key, key)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    try:
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.V("btn")))
        p.drawRoundedRect(0, 0, key, key, 6, 6)
        f = QFont()
        f.setPointSize(max(9, int(key * 0.40)))
        p.setFont(f)
        p.setPen(QPen(QColor(theme.V("muted"))))
        p.drawText(pm.rect(), Qt.AlignCenter, "♪")
    finally:
        p.end()
    _cover_placeholder_cache[key] = pm
    return pm


def load_cover_pixmap(path, size):
    """读封面缓存文件 → 缩放到 size（带缩放缓存）。失败返回 None。

    ⚠ **只读缓存**（`music_cover.cached()` 的产物），绝不在主线程提取。
    """
    if not path:
        return None
    key = (path, int(size))
    hit = _cover_pix_cache.get(key)
    if hit is not None:
        return hit if hit is not False else None
    pm = None
    try:
        if os.path.isfile(path):
            raw = QPixmap()
            if raw.load(path):
                pm = raw.scaled(int(size), int(size), Qt.KeepAspectRatio,
                                Qt.SmoothTransformation)
    except Exception:                                          # noqa: BLE001
        pm = None
    if len(_cover_pix_cache) > 4000:                            # 别无限涨
        _cover_pix_cache.clear()
    _cover_pix_cache[key] = pm if pm is not None else False
    return pm


_cover_pix_cache = {}


def cover_cached(path):
    """曲目封面缓存文件（**只查盘，不提取**，主线程安全）。没有则 None。"""
    try:
        import music_cover
        return music_cover.cached(path)
    except Exception:                                          # noqa: BLE001
        return None


def cover_icon_for(meta_rec, path, size, need_out=None):
    """取一行的封面图标：`meta["cover"]` → 缓存查询 → 占位图。

    need_out：传 list 时，把「需要后台补封面」的路径塞进去（调用方据此起补图线程）。
    """
    cp = meta_rec.get("cover") if isinstance(meta_rec, dict) else None
    if not (isinstance(cp, str) and os.path.isfile(cp)):
        cp = cover_cached(path)
    pm = load_cover_pixmap(cp, size) if cp else None
    if pm is None:
        if need_out is not None and os.path.isfile(path):
            need_out.append(path)
        pm = cover_placeholder(size)
    return QIcon(pm)


class CoverBackfillWorker(QThread):
    """后台补封面：把还缺封面的曲目挨个提取一遍，完成后发信号让列表刷新。

    ⚠ 与项目其它缩略图一致：**主线程只读缓存**，提取一律在后台。
    ⚠ 串行、可中断、自己吞异常 —— 补图是锦上添花，绝不能影响演出。
    """

    done = Signal(int)

    def __init__(self, paths, parent=None):
        super().__init__(parent)
        self.paths = list(paths or [])
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        n = 0
        try:
            import music_cover
        except Exception:                                      # noqa: BLE001
            self.done.emit(0)
            return
        for p in self.paths:
            if self._stop:
                break
            try:
                if music_cover.extract(p):
                    n += 1
            except Exception:                                  # noqa: BLE001
                pass
        self.done.emit(n)


#: 波形列的固定列数：整曲包络先压成这么多列，重绘时再按像素拉伸。
#: 96 已经能看出段落对比，256 在 240px 宽里是浪费（但保底给 160，缩放时更平滑）。
_WAVE_COLS = 160

#: 段落档位 → 色带颜色（与详情页 `waveform_mark._tc` 同一套：低=蓝 / 中=黄 / 高=红）
_WAVE_TIER_RGB = {"low": (74, 132, 214), "mid": (222, 190, 90), "high": (232, 96, 76)}


class WaveColumnDelegate(QStyledItemDelegate):
    """曲库「波形」列：整曲包络 + 能量段落**半透明色块**。

    ★ 2026-10-30 用户：「时长后面再加个波形图，一整个的波形图，然后依据分析切的
      段落用不同颜色的半透明色块覆盖渲染」。

    ⚠ 主线程**只读缓存**：波形从 `waveform.waveform_load`（.wj）读；缺的话由
      `WaveBackfillWorker` 在后台补（`_start_wave_backfill`）。绝不在 paint 里解码音频
      —— 一次卡 40ms 就是整屏冻结。
    ⚠ 色带画在**波形下面**（alpha 120）：盖在上面会把波峰糊掉，看不出"哪段有多炸"。
    """

    def __init__(self, panel, parent=None):
        super().__init__(parent or panel)
        self.panel = panel

    def paint(self, p, opt, idx):                                  # noqa: N802
        p.save()
        try:
            r = opt.rect
            if opt.state & QStyle.StateFlag.State_Selected:
                p.fillRect(r, QColor(58, 92, 140, 130))
            path = idx.data(Qt.ItemDataRole.UserRole)
            box = r.adjusted(4, 4, -4, -4)
            w, h = box.width(), box.height()
            if not path or w < 10 or h < 6:
                return
            got = self.panel._wave_for(path)
            if not got:
                # 还没算波形：画一条很淡的中线 + 提示色，别留空白（看着像坏了）
                p.setPen(QPen(QColor(120, 130, 145, 90), 1))
                p.drawLine(QPointF(box.left(), box.center().y()),
                           QPointF(box.right(), box.center().y()))
                return
            amp, dur = got
            n = int(amp.size)
            if n < 1 or dur <= 0:
                return
            mid = float(box.top()) + h / 2.0
            hh = max(1.0, h / 2.0 - 1.0)
            left = float(box.left())
            # ---- ① 波形（先画，镜像填充）----
            pts = [QPointF(left + (i / max(1, n - 1)) * w,
                           mid - float(amp[i]) * hh) for i in range(n)]
            pts += [QPointF(left + (i / max(1, n - 1)) * w,
                            mid + float(amp[i]) * hh) for i in range(n - 1, -1, -1)]
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            p.setPen(QPen(QColor(176, 214, 236, 235), 0.8))
            p.setBrush(QColor(198, 226, 242, 205))
            p.drawPolygon(QPolygonF(pts))
            p.setBrush(Qt.BrushStyle.NoBrush)
            # ---- ② 能量段落色带（**盖在波形上**的半透明"滤镜"）----
            #   用户原话：「用不同颜色的半透明色块或者滤镜覆盖渲染」。
            #   alpha 只用 78：再高就把波峰糊掉，看不出"哪段有多炸"。
            for (t0, t1, tier) in self.panel._wave_bands(path):
                x0 = left + (max(0.0, t0) / dur) * w
                x1 = left + (min(dur, t1) / dur) * w
                if x1 <= x0 + 0.5:
                    continue
                c = _WAVE_TIER_RGB.get(str(tier or "mid"), (150, 150, 150))
                p.fillRect(QRectF(x0, float(box.top()), max(1.0, x1 - x0), float(h)),
                           QColor(c[0], c[1], c[2], 78))
        except Exception:                                          # noqa: BLE001
            pass
        finally:
            p.restore()


def _decode_mono(path):
    """整曲解码成单声道 `(sig, sr)`；两条路都失败时抛异常。

    优先 `erlib.scanner.decode`（与详情页/建库同口径）；它不可用时退回 soundfile。
    """
    try:
        from erlib.scanner import decode
        sig, sr, _dur = decode(path)
        return sig, sr
    except Exception:                                          # noqa: BLE001
        import numpy as _np
        import soundfile as _sf
        data, sr = _sf.read(path, dtype="float32", always_2d=True)
        return _np.mean(data, axis=1), sr


class WaveBackfillWorker(QThread):
    """后台补波形缓存（`.wj`）：串行、可中断、自己吞异常。

    ⚠ 只在**没在演出**时启动（解码要读整曲，抢 IO/CPU 会影响渲染）。
    ⚠ 每补完一首 `one(path)` 通知界面**只重绘视口**，不重建整张表。
    """

    one = Signal(str)
    done = Signal(int)

    def __init__(self, paths, main=None, parent=None):
        super().__init__(parent)
        self.paths = list(paths or [])
        self.main = main
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        n = 0
        try:
            import waveform as _wf
        except Exception:                                          # noqa: BLE001
            self.done.emit(0)
            return
        for p in self.paths:
            if self._stop:
                break
            # 演出中立即收手（下一次打开曲库页会重新排）
            try:
                if self.main is not None and self.main.engine.running:
                    break
            except Exception:                                      # noqa: BLE001
                pass
            try:
                if _wf.waveform_load(p):
                    continue
                sig, sr = _decode_mono(p)
                wf = _wf.waveform_from_signal(sig, sr)
                if wf:
                    _wf.waveform_save(p, wf)
                    n += 1
                    self.one.emit(p)
            except Exception:                                      # noqa: BLE001
                continue
        self.done.emit(n)


# 曲库表格单列的最小宽度（`_fit_cols` 与 `_apply_drag` 必须用同一个值，
# 否则一套算出来的宽度会被另一套顶回去 ⇒ 拖动后总宽撑破 viewport）。
MIN_COL_W = 60


class MusicLibraryPanel(QWidget):
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
    # ★ 2026-10-28（用户要求「默认顺序改成歌曲的导入顺序」）：
    #   第一项即默认项。`import` = 按 `music_library` 的加入顺序**倒序**（新的在前）；
    #   `orig` 是物理顺序（扫描时是字母序，看着不像"导入顺序"）。
    SORTS = [("导入顺序（新的在前）", "import"), ("原顺序", "orig"),
             ("曲名", "title"), ("艺人", "artist"),
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

        # ★ 2026-10-09（用户要求②）：按**来源**筛选 —— 本机 / 具体导入包。
        #   选项在每次 refresh 前重建（用户可能刚导入新包）⇒ 程序化重填必须
        #   blockSignals（否则 setCurrentIndex 会触发 cascade refresh）。
        lv.addWidget(QLabel(T("来源")))
        self.cb_src = QComboBox()
        self.cb_src.addItem(T("全部"))
        self.cb_src.currentIndexChanged.connect(lambda _i: self.refresh())
        lv.addWidget(self.cb_src)

        self.chk_missing = QCheckBox(T("只看文件缺失"))
        self.chk_missing.setToolTip(T("换盘 / 改名之后找不到文件的曲目（修复清单）"))
        self.chk_missing.toggled.connect(lambda _s: self.refresh())
        lv.addWidget(self.chk_missing)

        b_reset = QPushButton(T("重置筛选"))
        b_reset.clicked.connect(self.reset_filters)
        lv.addWidget(b_reset)

        # ★ 2026-10-09（用户要求①）：复选框多选的「全选 / 全不选」——
        #   作用于**当前显示**的行（被筛选隐藏的不受影响）。
        row_sel = QHBoxLayout()
        row_sel.setSpacing(6)
        b_all = QPushButton(T("全选"))
        b_none = QPushButton(T("全不选"))
        b_all.clicked.connect(self.select_all)
        b_none.clicked.connect(self.clear_all)
        row_sel.addWidget(b_all)
        row_sel.addWidget(b_none)
        lv.addLayout(row_sel)

        hint = QLabel(T("BPM / 时长来自曲库扫描时建的节拍网格；「无节拍网格」的曲目"
                        "八拍乐句对齐用不了，是需要处理的清单。"))
        hint.setWordWrap(True)
        hint.setStyleSheet("color:" + theme.V("muted") + ";font-size:11px;")
        lv.addWidget(hint)
        # ★★ 2026-10-30（用户要求「增加文字，文字提示」）：曲库行现在有 4 种颜色语义，
        #   不写图例只能靠猜 —— 尤其新加的「绿=演出中的码表 / 灰=其他码表」这一对，
        #   两者**都是码表**，差别只在"有没有装载在图层里"，必须说清楚。
        self.lbl_cue_legend = QLabel(
            '<span style="color:%s">●</span> 演出中的码表　'
            '<span style="color:%s">●</span> 其他码表　'
            '<span style="color:#7aa2f7">●</span> 导入分析包　'
            '<span style="color:#e06666">●</span> 文件缺失'
            % (CUE_ROW_COLOR, CUE_ROW_DIM_COLOR))
        self.lbl_cue_legend.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_cue_legend.setWordWrap(True)
        self.lbl_cue_legend.setStyleSheet("font-size:11px;")
        self.lbl_cue_legend.setToolTip(T(
            "码表曲目的颜色只表示「是否正在参与演出」：\n"
            "· 青绿 = 该表已装载（绑定到图层），演出中会跟着它切素材 / 换位置\n"
            "· 灰　 = 该表只在曲库里参考，当前没装载，不影响演出\n"
            "「来源」列会写明表名，并标「演出中」或「未装载」。"))
        lv.addWidget(self.lbl_cue_legend)
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
        self.btn_scan.setToolTip(T("逐首建指纹 + 算节拍网格 + 能量段落（后台、增量）"))
        b_rescan = QPushButton(T("全量重扫"))
        b_rescan.setToolTip(T("忽略已有结果，重新分析全部"))
        b_clear = QPushButton(T("清除分析数据"))
        b_clear.setToolTip(T("删除全部指纹、节拍网格与自动段落（保留手动标注与码表）"))
        # ★★ 2026-10-08（erlib 第二步）：导入 DJ 那边扫好的曲库分析包。
        #   包 = 封面 + 指纹 + 离线节拍网格（**不含音频**）。
        #   导入后：DJ 放歌时本机能认出是哪首，自动套用八拍网格。
        b_imp = QPushButton(T("导入曲库分析包"))
        b_imp.setToolTip(T("导入 erlib 导出的 .eravjlib（DJ 机扫好的封面/指纹/节拍网格）"))
        b_dir.clicked.connect(self.main.import_music_dir)
        b_file.clicked.connect(self.main.import_music_files)
        self.btn_scan.clicked.connect(lambda: self.main.scan_music(force=False))
        b_rescan.clicked.connect(lambda: self.main.scan_music(force=True))
        b_clear.clicked.connect(self.main.show_clear_music_analysis)
        b_imp.clicked.connect(self.main.import_erlib_package)
        for b in (b_dir, b_file, self.btn_scan, b_rescan, b_clear, b_imp):
            row.addWidget(b)
        row.addStretch(1)
        right.addLayout(row)

        self.progress = QProgressBar()
        self.progress.hide()
        right.addWidget(self.progress)

        # ★★ 2026-10-28（用户要求「改成表格显示」）：QListWidget → QTableWidget。
        #   4 列：曲名（含封面/复选框）｜ 来源 ｜ BPM ｜ 时长。
        #   ★ 2026-10-30 用户「大砍刀」：曲风识别删除 ⇒ 去掉「曲风」列。
        #   ⚠ 语义全部挂在**第 0 列**（`Qt.UserRole`=路径、checkState、icon），
        #     其余列只做显示 ⇒ 遍历统一 `item(i, 0)`。
        #   ⚠ 不启用表头点击排序：开了就会按列 0 重排、破坏「导入顺序」；
        #     排序仍走左侧 `cb_sort` 下拉。
        # ★ 2026-10-30（用户）：**时长后面再加一列波形**（整曲包络 + 段落半透明色带）
        self.list = QTableWidget(0, 5)
        self.list.setHorizontalHeaderLabels(
            [T("曲目"), T("来源"), "BPM", T("时长"), T("波形")])
        self.list.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.setShowGrid(False)
        self.list.setWordWrap(False)
        self.list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list.verticalHeader().setVisible(False)
        self.list.verticalHeader().setDefaultSectionSize(30)
        _hv = self.list.horizontalHeader()
        _hv.setSectionsMovable(False)
        _hv.setHighlightSections(False)
        # ⚠ 列 0 不用 Stretch：嵌套布局里控件还没拿到真实宽度时它就算了一次，
        #   实测被挤到 75px 且之后不重算 ⇒ 改成 Interactive + `_fit_cols()` 按
        #   viewport 宽度显式分配（在 resizeEvent / refresh 里调）。
        # ★ 2026-10-30（用户「这些点是什么意思」）：来源列原来 210px，放了目录就放不下
        #   状态标记（无网格/未入库…）⇒ 加宽到 248px，并把"路径"和"状态"的宽度预算分开算。
        self._COL_FIXED = ((1, 248), (2, 72), (3, 66), (4, 240))
        # 各列默认宽度（列 0 = 曲目的初始值；其它列与 `_COL_FIXED` 同源）
        self._COL_DEF = {0: 360}
        self._COL_DEF.update(dict(self._COL_FIXED))
        # ★★ 2026-10-30（用户「曲目也得拖啊」）：**所有列都能拖**，但"吃掉剩余
        #   宽度"的**弹性列只能有一个** —— 否则总宽不可能同时等于窗口又听用户的。
        #   规则（`_on_section_resized`）：
        #     拖列 0（曲目）⇒ 弹性列换成最后一列（波形）
        #     拖 1~4 任意列 ⇒ 弹性列换回列 0（曲目）
        #   效果：拖曲目 → 波形吸收差额；拖波形/来源 → 曲目吸收差额。
        #   永远铺满，且"我拖的那一列停在我放的位置"。
        for _c in range(5):
            _hv.setSectionResizeMode(_c, QHeaderView.ResizeMode.Interactive)
        for _c, _w in self._COL_FIXED:
            self.list.setColumnWidth(_c, _w)
        self.list.setColumnWidth(0, 360)
        # ★ 2026-10-28（用户要求「可以左右拖动调整列宽」）：
        #   Interactive 本来就能拖，但自动分配会把用户拖的宽度盖掉。
        #   这里记住「用户动过列宽」，之后不再自动分配（`_fitting` 屏蔽程序自身设宽触发）。
        self._user_w = {}          # 用户亲手拖过的列宽 {col: px}
        self._elastic = 0          # 当前吃剩余宽度的那一列（默认曲目列）
        self._fitting = False
        self._hdr_down = False     # 鼠标正按在分隔线上（拖拽中）—— 期间**绝不**改列宽
        # 受控拖拽状态（见 `_drag_begin/_drag_to/_drag_end`）
        self._dg_col = None        # 正在拖的分隔线左侧列
        self._dg_x0 = 0.0          # 按下时的 x（表头 viewport 坐标）
        self._dg_base = None       # 按下时的列宽快照
        self._dg_moved = False     # 这次拖动有没有真的改变宽度
        self._dg_grab = False      # 是否已 grabMouse
        _hv.sectionResized.connect(self._on_section_resized)
        # ★★ 2026-10-31（用户「现在还是带着右边整个在拖」）：Qt 的 `Interactive` 拖拽
        #   是它自己在 mouseMove 里改被拖列宽度、右边所有列跟着平移。所有"松手后再纠正"
        #   的做法都改不了**拖动中**的观感 ⇒ 改成按下就接管（`eventFilter` 吞事件 +
        #   `_drag_to` 自己算两列）。这个计时器只是"Release 没送到"的兜底收尾。
        self._fit_timer = QTimer(self)         # 兜底：重新分配列宽（单发）
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._fit_after_drag)
        # ⚠ 表头的鼠标事件投递目标是 **header 的 viewport**（不是 header 本身）：
        #   装错对象就永远收不到 Press/Move/Release，接管不了拖拽。
        self._hdr_viewport = _hv.viewport()
        self._hdr_viewport.installEventFilter(self)
        # 表头右键：恢复默认列宽 / 单列按内容自适应
        _hv.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        _hv.customContextMenuRequested.connect(self._hdr_menu)
        # ★★ 2026-10-30（用户「列宽没铺满」的**真因**）：父控件的 `resizeEvent` 里
        #   子控件**还没被布局**，读到的 `viewport().width()` 是控件初始值（实测
        #   638 —— 比表格实际宽度 414 还大）⇒ `max(160, 638-628)` 把曲目列钉死在
        #   最小 160px，右边留一大块空白，而且之后没有第二次 resize 就永远不自愈。
        #   viewport 自己的 Resize 事件是**唯一**能拿到真实可用宽度的时机 ⇒ 听它。
        self.list.viewport().installEventFilter(self)
        # ★ 2026-10-30（用户「显示的文字对齐」）：表头对齐方式跟**内容**一致 ——
        #   曲目/来源左对齐，BPM/时长右对齐。原来全是居中，数字右对齐后
        #   表头居中就显得"偏了"（用户截图里 BPM 那格就是这么错开的）。
        for _c, _al in ((0, Qt.AlignmentFlag.AlignLeft),
                        (1, Qt.AlignmentFlag.AlignLeft),
                        (2, Qt.AlignmentFlag.AlignRight),
                        (3, Qt.AlignmentFlag.AlignRight),
                        (4, Qt.AlignmentFlag.AlignCenter)):
            _hi = self.list.horizontalHeaderItem(_c)
            if _hi is not None:
                _hi.setTextAlignment(int(_al | Qt.AlignmentFlag.AlignVCenter))
        # 波形列用自绘 delegate（整曲包络 + 段落色带）
        self._wave_delegate = WaveColumnDelegate(self, self.list)
        self.list.setItemDelegateForColumn(4, self._wave_delegate)
        self._wave_cache = {}      # path -> (amp, dur) / False(=没有)
        self._wave_sec_map = {}    # path -> [(t0, t1, tier), ...]
        self._wave_job = None
        # ★★ 2026-10-31（用户「eravj 的导入怎么没有波形」）：导入歌的波形/段落
        #   **本机没有 `.wj` 文件**（行标识是伪路径 `@imported:<sid>`）⇒ 之前的
        #   `_wave_for()` 一律读空 ⇒ 曲库「波形」列 21 行全空白。
        #   数据其实就在 `imported_meta.db` 的 `waveforms` / `songs.sections` 里
        #   （erlib 扫描时算好、导入时落库）。这里按库文件的 (mtime, size) 缓存，
        #   避免每次 refresh（勾选一下就会触发）都把 BLOB 全解一遍。
        self._imp_wave = {}        # "@imported:<sid>" -> (amp, dur)
        self._imp_secs = {}        # "@imported:<sid>" -> [(t0, t1, tier), ...]
        self._imp_key = None       # (mtime, size) —— 变了才重读
        # ★ 2026-10-09（用户要求）：支持**多选**批量删除（Ctrl/Shift 点选）。
        #   ⚠ 这里可以安全用 ExtendedSelection：曲库面板的列表**没有拖拽**
        #     （素材库那边不行 —— 拖拽会被拉框多选抢掉点击，见 LibraryGrid）。
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # ★ 歌曲封面（用户要求 2026-10-01）：每行左侧显示封面，且给足尺寸。
        #   ⚠ 主线程只读缓存（`cover_cached`），缺图的交给后台 `CoverBackfillWorker`。
        # 封面尺寸：从配置恢复（老配置里没有/值不合法都回退默认档）
        self.cover_size = self._load_cover_size()
        self.list.setIconSize(QSize(self.cover_size, self.cover_size))
        self._cover_job = None
        # ★ 2026-10-09（用户要求①）：复选框多选的状态位。
        #   `_building`：refresh 重建列表期间置 True —— 逐行 setCheckState /
        #   itemChanged 会刷屏，重建时忽略它们，结束后统一更新计数。
        #   `_stat`：底部计数的基数（在 refresh 里填），供 _update_lbl 组装。
        self._building = False
        self._stat = {}
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._ctx_menu)
        # 勾选状态变化 → 刷新底部「已勾选 N 首」（不整表重建）
        self.list.itemChanged.connect(self._on_item_changed)
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
        # 来源下拉是动态重建的 ⇒ 重置只需设回第 0 项（「全部」），同样 blockSignals
        self.cb_src.blockSignals(True)
        self.cb_src.setCurrentIndex(0)
        self.cb_src.blockSignals(False)
        self.chk_missing.setChecked(False)
        self.refresh()

    def _sort_key(self, key, meta):
        def k(path):
            rec = meta.get(path) or {}
            if key == "import":
                # 新的在前；派生行（导入包/码表）不在 music_library 里 ⇒ 排最后
                return (-self._order_index.get(path, -1),)
            bpm, dur, _e = self._info(meta, path)
            # ⚠ 码表派生行的 key 是伪路径（`@cue:<表路径>#…`），basename 没有意义 ⇒
            #   按曲名排序。本机行保持原行为（按文件名），避免改变既有排序结果。
            if rec.get("_cue"):
                name = (rec.get("title") or rec.get("file_name") or "").lower()
            else:
                name = os.path.basename(path).lower()
            if key == "title":
                return (name,)
            if key == "artist":
                return (rec.get("artist") or "").lower()
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
        """按左栏的排序/筛选重建列表（廉价操作，几百首毫秒级）。

        ★ 2026-10-09（用户要求①）：每行带**复选框**；列表重建时按「键」恢复勾选
          （键：本机=文件路径，导入=`@imported:<sid>`）—— 切排序/筛选/导入后刷新
          都不会丢用户的勾选。
        ★ 2026-10-09（用户要求②）：按**来源**（本机 / 具体导入包）筛选。
        ★ 2026-10-09（用户要求③）：导入歌行尾显示**包名**而非笼统的「导入包」。
        ★ 2026-10-17（用户要求）：**码表里的曲目（含指纹）也进曲库列表** ——
          青绿行、可筛「仅码表 / 某张表」、行尾显示「未入库 + 指纹」。
          ⚠ 它们是**派生视图**（每次现算，不落盘）⇒ 码表删了它们自动消失。
        """
        try:
            # ① 先收集**重建前**的勾选键（列表即将 clear，必须先记下来）
            checked_keys = self._checked_keys()
            meta = self.main.cfg["music_meta"] or {}
            lib = list(self.main.cfg["music_library"] or [])
            # ★★ 2026-10-08（erlib 第二步）：把「导入的曲库分析包」也列进来。
            #   这些歌**本机没有音频文件**，用伪路径 `@imported:<song_id>` 作行标识；
            #   数据合并进一份**视图 meta**（不动 `cfg["music_meta"]`，
            #   避免污染"按文件路径索引"的曲库数据，也避免被扫描逻辑当成曲目）。
            imp_lib = self._imported_lib()
            # ★★ 2026-10-31（用户「导入了怎么 识别不到 …… 而且 导入重复的歌也没提醒」）：
            #   导入库里可能有"本机其实已经有"的歌（同一文件、路径写法不同 ⇒ song_id 不同，
            #   见 `erlib.dupdetect` 的说明）。它们在列表里各占一行 ⇒ 用户看到重复。
            #   这里把这种导入行**隐去**（数据不动、只在展示层合并），并记下合并了几首，
            #   在底部状态栏说明 —— 否则用户会以为"导入的歌怎么少了"。
            #   ⚠ 只在"确实同 sid / 同名同长"时才隐（宁少勿多：误隐 = 用户丢歌）。
            imp_lib, _imp_merged = self._dedupe_imported(imp_lib, meta)
            # ★★ 2026-10-17（用户要求）：把**码表里的曲目（含指纹）**也列进曲库。
            #   ⚠ 这些行是**派生视图**：每次 refresh 现算，**绝不写进
            #     `cfg["music_library"]` / `cfg["music_meta"]`**。
            #     所以「码表被删 / 移除 ⇒ 曲库里对应条目一起消失」天然成立 ——
            #     派生行没有实体，表没了下次 refresh 就再也算不出来。
            #     ⇒ 不要在这里补"级联删除"：`music_library` 是用户曲库唯一真源，
            #       级联删错删的是用户本机的真歌。详见 cue_index.library_rows()。
            cue_lib = self._cue_lib_rows()
            # ★ 2026-10-30（用户「怎么来源里没有码表的选项了」）：还要拿"码表里有、
            #   本机也有"的那些（`covered_paths`）。它们**不新建行**（同一首歌不列两遍），
            #   但「来源」列要标出码表名、「来源」下拉里也要能筛出来。
            #   此前这套可见性**完全依赖 `music_meta` 残缺**：清空时 4 首都判"未覆盖"
            #   ⇒ 派生行冒出来 ⇒ 下拉里有码表选项；恢复后派生行归零 ⇒ 选项消失，
            #   用户以为码表数据又丢了。数据一直在，只是没有入口可看。
            cue_cov = self._cue_covered()
            # ★ 2026-10-30（用户要求）：只有**正在演出中使用的**那张码表上青绿，
            #   其他表的曲目灰显。`cue_act` 是"在用表"的曲目身份集合。
            cue_act = self._active_cue_ids()
            view_meta = meta
            if imp_lib or cue_lib:
                view_meta = dict(meta)
                # ★ 2026-10-31：导入歌的波形/段落来自 `imported_meta.db`（见方法注释）
                self._refresh_imp_extras(imp_lib)
                for _fp, _r in imp_lib.items():
                    view_meta[_fp] = {
                        "grid": {"bpm": _r["bpm"], "dur": _r["dur"]},
                        "artist": _r["artist"],
                        "title": _r["title"], "source": "", "cover": _r["cover"],
                        "song_id": _r["sid"], "_imported": True,
                        # ★ 包名（用于来源筛选 + 行尾显示）——空串 = 早期导入
                        "pack": _r.get("pack", ""),
                        # ★★ 2026-10-31：段落色带（包内带过来的，没导入过的旧包为空）
                        "sections": self._imp_secs.get(_fp) or [],
                    }
                for _fp, _r in cue_lib.items():
                    # 码表派生行：BPM/时长来自码表曲目身份（不读本机文件）
                    view_meta[_fp] = {
                        "grid": {"bpm": _r["bpm"], "dur": _r["duration_sec"]},
                        "artist": _r["artist"], "title": _r["title"],
                        "source": "", "cover": "", "song_id": "",
                        "_cue": True,
                        # 表名/表路径：来源筛选（稳定键用绝对路径）+ 行尾显示
                        "sheet": _r["sheet_name"], "sheet_path": _r["sheet_path"],
                        "fp_id": _r["fp_id"], "file_md5": _r["file_md5"],
                        "meta_id": _r["meta_id"], "file_name": _r["file_name"],
                    }
                lib = lib + list(imp_lib) + list(cue_lib)
            # ★ 来源下拉在**过滤前**重建（用户可能刚导入新包 / 刚加码表 ⇒ 选项要实时更新）；
            #   程序化重填内部 blockSignals，绝不触发 cascade refresh。
            self._refresh_source_combo(imp_lib, cue_lib, cue_cov)
            src_key = self.cb_src.currentData(Qt.UserRole) or "__all__"
            bpm_b = self.BPM_FILTERS[self.cb_bpm.currentIndex()][1]
            dur_b = self.DUR_FILTERS[self.cb_dur.currentIndex()][1]
            only_miss = self.chk_missing.isChecked()
            rows = []
            for p in lib:
                imp = p in imp_lib
                cue = p in cue_lib
                # ★ 来源筛选（②）：全部 / 仅本机 / 某导入包 / 码表内曲目 / 某张码表
                if not self._src_ok(p, imp, imp_lib, src_key, cue, cue_lib, cue_cov):
                    continue
                bpm, dur, exists = self._info(view_meta, p)
                if not self._in_bucket(bpm, bpm_b):
                    continue
                if not self._in_bucket(dur, dur_b):
                    continue
                # ⚠ 导入的歌 / 码表派生行**都不算**"文件缺失"：它们本来就没有本机文件，
                #   混进来会让"只看文件缺失"（那是"换盘/改名后找不到文件"的
                #   修复清单）语义混乱。
                if only_miss and (exists or imp or cue):
                    continue
                rows.append((p, bpm, dur, exists))
            # ★★ 2026-10-28：「导入顺序」= `music_library` 的加入顺序倒序（新的在前）。
            #   ⚠ 必须用**原始 music_library**，不能用上面被拼过的 `lib`：
            #     `lib = 本机 + 导入包 + 码表` ⇒ 派生行索引最大 ⇒ key 最负
            #     ⇒ 码表/导入包全排到本机歌前面（实测第一行是码表里的歌）。
            #   派生行不在 music_library 里 ⇒ `get(..., -1)` ⇒ key=+1 ⇒ 排最后。
            self._order_index = {p: i for i, p in enumerate(
                self.main.cfg['music_library'] or [])}
            sk = self.SORTS[self.cb_sort.currentIndex()][1]
            if sk != "orig":
                order = {p: i for i, p in enumerate(lib)}
                rows.sort(key=lambda r: (self._sort_key(sk, view_meta)(r[0]),
                                         order.get(r[0], 0)))
            self._building = True
            try:
                self.list.clearContents()
                self.list.setRowCount(0)
                need_cover = []
                need_wave = []          # 缺波形缓存的（后台补，见 `_start_wave_backfill`）
                self._wave_sec_map = {}  # 每次重建都重算（旧条目可能已不在库里）
                for p, bpm, dur, exists in rows:
                    m = view_meta.get(p) or {}
                    imp = bool(m.get("_imported"))
                    cue = bool(m.get("_cue"))
                    # ★ 2026-10-30：本机也有、但属于某张码表的那一行（不新建行的那些）。
                    #   提到这里取，是因为**颜色也要用**（原来只在下面的 else 分支里取，
                    #   结果"本机已有的码表歌"完全不上色，用户看不出它来自码表）。
                    _cv = None if (imp or cue) else (
                        (cue_cov or {}).get(p) if cue_cov else None)
                    # 是否属于**正在演出中使用的**那张表（其他表灰显）
                    _act = self._cue_row_active(m if cue else _cv, cue_act)
                    src = m.get("source") or ""
                    bits = []
                    # 只有"本机文件行"才有原始路径 ⇒ 列宽变化时据此重算省略
                    _src_p = None
                    _src_tail = ""
                    # ★★ 2026-10-30（用户：「来源不要显示 bpm 和时长」）：
                    #   BPM / 时长**各自已经有独立列**，来源列再拼一遍只是把列撑长、
                    #   把真正有用的标记（未入库 / 指纹 / 文件缺失）挤到看不见。
                    #   「无网格」**保留** —— 它是"缺数据"的状态提示，不是数值复述
                    #   （左侧筛选里也有同名项，用户要靠它找需要补扫的歌）。
                    if not bpm:
                        bits.append(T("无网格"))
                    if imp:
                        name = (m.get("title") or m.get("file_name")
                                or ("#%s" % m.get("song_id")))
                        artist = m.get("artist") or ""
                        disp = ("%s - %s" % (artist, name)) if artist else name
                        disp = "📦 " + disp
                        # ★（③）行尾显示**包名**；空串 = 加列之前的旧数据
                        src = (m.get("pack") or "") or T("早期导入")
                    elif cue:
                        # ★ 码表带来的曲目：行尾显示**表名** + 「未入库」+ 指纹前 8 位
                        #   （指纹整串 16 hex 太长，会把行撑出去）
                        name = m.get("title") or m.get("file_name") or ""
                        artist = m.get("artist") or ""
                        disp = ("%s - %s" % (artist, name)) if artist else name
                        disp = "🀄 " + (disp or T("未命名"))
                        src = Tf("码表：{}", m.get("sheet")
                                 or os.path.basename(str(m.get("sheet_path") or "")))
                        src += " · " + (T("演出中") if _act else T("未装载"))
                        bits.append(T("未入库"))
                        fp = str(m.get("fp_id") or "")
                        if fp:
                            bits.append(Tf("指纹 {}",
                                           fp if len(fp) <= 12 else fp[:12] + "…"))
                        else:
                            md = str(m.get("file_md5") or "")
                            if md:
                                bits.append("md5 " + md[:8] + "…")
                    else:
                        disp = os.path.basename(p)
                        if not exists:
                            bits.append(T("文件缺失"))
                        # ★ 本机也有、但它同时在某张码表里 ⇒ 「来源」标出码表名。
                        #   为什么要替换 `source`（通常是 localai）而不是并列：
                        #   「来源」列就一个词的位置，用户问的是"这首歌从哪来"，
                        #   码表这条信息比"曲风是谁猜的"有用得多；曲风来源在曲风列里有。
                        #   （`_cv` 已在上面的循环头取过一次，这里不再重复取。）
                        if _cv:
                            src = Tf("码表：{}", _cv.get("sheet_name") or "")
                            src += " · " + (T("演出中") if _act else T("未装载"))
                        else:
                            # ★ 2026-10-30 用户：「本地的歌来源写歌曲文件硬盘位置」
                            #   （曲风砍掉后 `source` 不再产出 ⇒ 这里原来一直是空的）
                            # ★★ 2026-10-31（用户「这些怎么不显示后面的」→「我说的是来源」）：
                            #   预算改成**按来源列当前宽度**算（原来写死 17/28），
                            #   列宽一变由 `_restyle_src_col` 重排。
                            _fm = self.list.fontMetrics()
                            _mw = self._src_budget()
                            if bits:
                                _mw -= _fm.horizontalAdvance(" · " + " · ".join(bits))
                            src = _dir_short(p, maxw=max(40, _mw),
                                             measure=_fm.horizontalAdvance)
                            _src_p = p
                            _src_tail = " · ".join(bits)
                    # ★★ 2026-10-28：一行拆成 5 列（原来是一串拼接文本）。
                    #   `bits`（未入库/指纹/文件缺失）并进「来源」列，避免列太多。
                    _src_txt = src or ""
                    if bits:
                        _src_txt = (_src_txt + " · " + " · ".join(bits)).strip(" ·")
                    # ★ 来源列讲的是「这首歌在哪」⇒ 本机曲目的悬浮给**完整文件路径**
                    #   （列里那串目录是缩过的，长路径看不了全貌）。
                    #   导入包 / 纯码表行没有本机文件，保持文字本身的提示。
                    _src_tip = _src_txt
                    if not imp and not cue:
                        _src_tip = ("%s\n%s" % (_src_txt, p)) if _src_txt else p
                    _bpm_txt = ("%.0f" % bpm) if bpm else "—"
                    _dur_txt = ("%d:%02d" % (int(dur) // 60, int(dur) % 60)) if dur else "—"
                    _row = self.list.rowCount()
                    self.list.insertRow(_row)
                    it = QTableWidgetItem(
                        cover_icon_for(m, p, self.cover_size, need_cover), disp)
                    it.setData(Qt.UserRole, p)
                    # ★ 2026-10-30（用户要求「增加文字，文字提示」）：
                    #   码表行给**整行 tooltip**，把"这张表到底在不在演出里"讲明白。
                    #   只靠颜色不够 —— 绿和灰都是码表，现场没空分辨色差。
                    _cvrow = m if cue else _cv
                    if _cvrow:
                        _sn = str(_cvrow.get("sheet_name") or os.path.basename(
                            str(_cvrow.get("sheet_path") or "")))
                        if _act:
                            it.setToolTip("%s\n\n%s\n%s" % (
                                disp,
                                Tf("来自码表「{}」——正在演出中使用", _sn),
                                T("该表已装载在图层里：切歌、换位置、素材切换都跟着它。")))
                        else:
                            it.setToolTip("%s\n\n%s\n%s" % (
                                disp,
                                Tf("来自码表「{}」——当前未装载", _sn),
                                T("只作为曲库参考，不参与演出切换。")))
                    else:
                        it.setToolTip(disp)
                    # ★ 复选框（①）：默认未勾选；按重建前的勾选键恢复勾选
                    it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    it.setCheckState(
                        Qt.CheckState.Checked if p in checked_keys
                        else Qt.CheckState.Unchecked)
                    if imp:
                        # 来自导入包：用**蓝**区分（红色是"文件缺失"，别混用语义）
                        it.setForeground(QColor("#7aa2f7"))
                    elif cue or _cv:
                        # ★★ 2026-10-30（用户要求）：码表的曲目**分两档**
                        #   · 属于正在演出中使用的码表 ⇒ 青绿（醒目，要能一眼找到）
                        #   · 其他码表                    ⇒ 灰（存在但"不参与演出"）
                        #   为什么必须分：曲库里躺着好几张表的歌，如果全青绿，
                        #   现场会误判"这些都在跟着演出切"。
                        it.setForeground(QColor(
                            CUE_ROW_COLOR if _act else CUE_ROW_DIM_COLOR))
                    elif not exists:
                        it.setForeground(QColor("#e06666"))
                    self.list.setItem(_row, 0, it)
                    # ★ 2026-10-30：波形列的段落色带数据（档位 + 区间），供 delegate 画
                    _secs = [x for x in ((view_meta.get(p) or {}).get("sections") or [])
                             if isinstance(x, dict)]
                    self._wave_sec_map[p] = [
                        (float(x.get("t0") or 0.0), float(x.get("t1") or 0.0),
                         str(x.get("tier") or "mid")) for x in _secs]
                    for _c, _txt in ((1, _src_txt), (2, _bpm_txt), (3, _dur_txt)):
                        _ci = QTableWidgetItem(_txt)
                        _ci.setToolTip(_src_tip if _c == 1 else _txt)
                        if _c >= 2:
                            _ci.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                                 | Qt.AlignmentFlag.AlignVCenter)
                        _ci.setForeground(it.foreground())
                        if _c == 1 and _src_p:
                            # 记下原始路径 + 状态尾巴：列宽一变 `_restyle_src_col`
                            # 直接用它们重算（不必再走一遍整表 refresh）
                            _ci.setData(_SRC_ROLE, _src_p)
                            _ci.setData(_SRC_TAIL_ROLE, _src_tail)
                        self.list.setItem(_row, _c, _ci)
                    # 波形列：数据挂在 UserRole（delegate 靠它取包络/段落）
                    _wi = QTableWidgetItem("")
                    _wi.setData(Qt.UserRole, p)
                    if _secs:
                        _wi.setToolTip(Tf("{} 段能量段落（蓝=低 / 黄=中 / 红=高）",
                                          str(len(_secs))))
                    else:
                        _wi.setToolTip(T("还没有分析段落"))
                    self.list.setItem(_row, 4, _wi)
                    # ⚠ 导入行/码表行的"没有波形"是**正常状态**（本机根本没文件）：
                    #   塞进补算队列只会让后台 worker 去解码 `@imported:123` 这种
                    #   伪路径、白白占掉本机歌的补算名额（2026-10-31）。
                    if not self._wave_for(p) and not imp and not cue:
                        need_wave.append(p)
                    self.list.setRowHeight(_row, int(self.cover_size) + 6)
            finally:
                self._building = False
            # 刚按当前列宽排过一遍来源列 ⇒ 记下预算，`_restyle_src_col` 不会白跑
            self._src_budget_cache = self._src_budget()
            # ⚠ 「已分析」只数本机/导入库的曲目：码表派生行没有任何本机分析数据，
            #   算进去会虚报（它们连音频文件都不在本机）。
            done = sum(1 for p in lib if p in view_meta and p not in cue_lib)
            # 底部统计：勾选数由 _update_lbl 追加（勾选变化时不必整表重建）
            # ★ 2026-10-31：`imp` 是**导入库里实际有的首数**（去重前），
            #   `imp_merged` 是"与本机重复、展示时被合并掉"的首数 —— 两者一起写进状态栏，
            #   用户才不会以为导入的歌少了（见 `_dedupe_imported`）。
            _imp_all = len(imp_lib) + int(_imp_merged or 0)
            self._stat = {"lib": len(lib), "done": done,
                          "rows": len(rows), "imp": _imp_all,
                          "imp_merged": int(_imp_merged or 0),
                          # 码表内曲目总数 = 未入库派生行 + 本机已有那些
                          "cue": len(cue_lib) + len(cue_cov)}
            self._update_lbl()
            self._schedule_fit()   # 此刻表格可能还没被布局 ⇒ 必须延后一轮再算列宽
            self.lbl_filter.setText(Tf("筛选后 {} / {} 首", len(rows), len(lib)))
            # 缺封面 ⇒ 起一次后台补图，补完再刷一遍（不阻塞、不重复起）
            # ★ 记下"这一次渲染用的在用表身份集合"，供 `showEvent` 比对
            #   （见那边的说明：切回曲库页时只在它变了才重建）。
            self._act_snap = cue_act
            self._start_cover_backfill(need_cover)
            self._start_wave_backfill(need_wave)
        except Exception:                                          # noqa: BLE001
            pass

    def _src_budget(self):
        """「来源」列当前能用的**像素**宽度（已扣掉单元格左右内边距）。

        ★★ 2026-10-31（用户「这些怎么不显示 后面的」→「我说的是来源」）：
          预算原来是**写死**的半角单位 `28`（有状态标记时 `17`），和列宽彻底脱钩：
          · 列被拖宽 → 文字仍按老预算截断，右边一片空白；
          · 列被拖窄 → 文字超出列宽，被 Qt 再从右边截一刀，末尾只剩 `...`。
          现在一律按像素算（与 `QFontMetrics` 同口径，不再有估算误差）。
        """
        try:
            w = int(self.list.columnWidth(1))
        except Exception:                                          # noqa: BLE001
            w = 248
        return max(40, w - 14)

    def _restyle_src_col(self):
        """列宽变了 ⇒ 重算「来源」列每一行的省略程度（能放多宽就写多宽）。

        只重排**本机文件**行（`_SRC_ROLE` 有值）；导入包 / 码表行的来源是表名，
        本来就是短文本，交给 Qt 自己省略即可。
        """
        try:
            bud = self._src_budget()
            if bud == getattr(self, "_src_budget_cache", None):
                return                      # 宽度没变，别白跑（拖动中会被反复调用）
            self._src_budget_cache = bud
            n = int(self.list.rowCount())
            if n <= 0:
                return
            fm = self.list.fontMetrics()
            _b = getattr(self, "_building", False)
            self._building = True           # 屏蔽 itemChanged → 别为每行刷一次底部计数
            try:
                for r in range(n):
                    it = self.list.item(r, 1)
                    if it is None:
                        continue
                    p = it.data(_SRC_ROLE)
                    if not p:
                        continue
                    tail = str(it.data(_SRC_TAIL_ROLE) or "")
                    m2 = bud
                    if tail:
                        m2 -= fm.horizontalAdvance(" · " + tail)
                    txt = _dir_short(p, maxw=max(40, m2),
                                     measure=fm.horizontalAdvance)
                    if tail:
                        txt = (txt + " · " + tail).strip(" ·")
                    if it.text() != txt:
                        it.setText(txt)
                    it.setToolTip(("%s\n%s" % (txt, p)) if txt else str(p))
            finally:
                self._building = _b
        except Exception:                                          # noqa: BLE001
            pass

    def _fit_col_to_content(self, c):
        """双击分隔线 ⇒ 该列贴合内容宽（Excel 同款）。

        复用受控拖拽那条已验证的路径（`_drag_begin/_drag_to/_drag_end`），
        所以"只动相邻两列、总宽不变"的语义自动成立。
        """
        try:
            n = int(self.list.columnCount())
            if not (0 <= c < n - 1):
                return                      # 最后一列右边没有邻列可让位
            if c == 4:
                return                      # 波形列是自绘 delegate，没有内容宽
            w = int(self.list.sizeHintForColumn(c)) + 12
            w = max(MIN_COL_W, min(w, 900))
            d = w - int(self.list.columnWidth(c))
            if d == 0:
                return
            self._drag_begin(c, 0.0)
            self._drag_to(float(d))
            self._drag_end()
        except Exception:                                          # noqa: BLE001
            pass

    def _reset_cols(self):
        """恢复默认列宽（表头右键）。用户拖乱了之后的一条退路。"""
        self._user_w = {}
        self._elastic = 0
        self._src_budget_cache = None       # 强制重排来源列（预算会变）
        self._fit_cols()

    def _hdr_menu(self, pos):
        """表头右键菜单：恢复默认列宽 / 这一列按内容自适应。"""
        try:
            hv = self.list.horizontalHeader()
            _c = int(hv.logicalIndexAt(int(pos.x())))
        except Exception:                                          # noqa: BLE001
            _c = -1
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        a_reset = menu.addAction(T("恢复默认列宽"))
        a_reset.setToolTip(T("把所有列宽恢复成默认比例，并重新铺满窗口"))
        a_fit = None
        try:
            _hi = self.list.horizontalHeaderItem(_c) if _c >= 0 else None
            if _hi is not None and 0 <= _c <= 3:
                a_fit = menu.addAction(Tf("「{}」列宽按内容自适应", _hi.text()))
                a_fit.setToolTip(T("等同于直接双击这条分隔线"))
        except Exception:                                          # noqa: BLE001
            a_fit = None
        try:
            act = menu.exec(hv.mapToGlobal(pos))
        except Exception:                                          # noqa: BLE001
            act = None
        if act is a_reset:
            self._reset_cols()
        elif a_fit is not None and act is a_fit:
            self._fit_col_to_content(_c)

    def _on_section_resized(self, idx, _old, _new):
        """兜底：**非用户拖拽**（程序/样式引起）的列宽变化，记成固定宽度。

        ★ 2026-10-30 用户：「列的宽度实时调整…整个表格的宽度对齐窗口大小」。
        ⚠ 用户拖分隔线**不再经过这里** —— 见 `_drag_begin` / `_drag_to` / `_drag_end`，
          那条路径被 `eventFilter` 完全接管（否则 Qt 会"带着右边整个在拖"）。
        """
        if getattr(self, "_fitting", False):
            return
        try:
            i = int(idx)
        except (TypeError, ValueError):
            return
        if 0 <= i < int(self.list.columnCount()):
            self._user_w[i] = int(_new)

    def _arm_fit(self, delay=0):
        """安排一次"重新分配列宽"（重复调用会重置计时）。"""
        t = getattr(self, "_fit_timer", None)
        if t is None:
            return
        t.start(int(delay))

    def _fit_after_drag(self):
        """兜底刷新：拖拽状态若因故残留（Release 没送到），先收尾再重新分配列宽。"""
        if getattr(self, "_hdr_down", False):
            self._arm_fit(300)          # 还按着 ⇒ 等松手
            return
        self._dg_col = None
        self._dg_base = None
        self._dg_moved = False
        try:
            self._fit_cols()
        except Exception:                                          # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # 表头列宽拖拽：Excel 语义（只改分隔线左右两列）
    # ------------------------------------------------------------------
    # ★★ 2026-10-31（用户：「改成像 excel 那样 只影响左右两列」→「不对 现在还是带着
    #   右边整个在拖」）：**必须在按下时接管**，不能只在松手后纠正。
    #   `ResizeMode.Interactive` 是 QHeaderView 自己在 `mouseMoveEvent` 里改被拖列宽度、
    #   右边所有列跟着平移（总宽变化 ⇒ 横滚条）。事后修正救不了拖动**过程中**的观感，
    #   用户看到的就是"带着右边整个一起拖"。
    #   ⇒ 命中分隔线时把 Press/Move/Release **全部吞掉**，自己算两列宽度。
    HANDLE_TOL = 5            # 分隔线命中半径（Qt 默认 handle 宽度约 5px）

    def _hdr_handle_col(self, x):
        """`x`（表头 viewport 坐标）命中哪条列分隔线？返回**左侧那列**的索引。

        ⚠ 只扫前 n-1 条：最后一列的右边界在表格外沿（竖向滚动条/边框上），抓不到。
        """
        try:
            hv = self.list.horizontalHeader()
            n = int(self.list.columnCount())
            for c in range(n - 1):
                _edge = int(hv.sectionViewportPosition(c)) + int(hv.sectionSize(c))
                if abs(float(x) - float(_edge)) <= self.HANDLE_TOL:
                    return c
        except Exception:                                          # noqa: BLE001
            pass
        return None

    def _drag_begin(self, col, x):
        """按下分隔线：拍列宽快照，进入受控拖拽（**不**让 QHeaderView 自己拖）。"""
        try:
            self._dg_col = int(col)
            self._dg_x0 = float(x)
            self._dg_base = [int(self.list.columnWidth(c))
                             for c in range(int(self.list.columnCount()))]
            self._dg_moved = False
            self._hdr_down = True
        except Exception:                                          # noqa: BLE001
            self._dg_col = None

    def _drag_to(self, x):
        """拖动中：只改「被拖列」和它右边那一列，两列之和恒定 ⇒ 表格总宽不动。

        返回实际生效的位移（被最小宽 `MIN_COL_W` 截断后的值）。
        """
        i = getattr(self, "_dg_col", None)
        base = getattr(self, "_dg_base", None)
        if i is None or not base:
            return 0
        n = int(self.list.columnCount())
        j = i + 1
        if j >= n or j < 0:
            return 0
        d = int(round(float(x) - float(getattr(self, "_dg_x0", 0.0))))
        wi = max(MIN_COL_W, base[i] + d)
        d = wi - base[i]
        wj = max(MIN_COL_W, base[j] - d)
        d = base[j] - wj            # 邻居到底了就截断，绝不把总宽撑开
        if d == 0:
            return 0
        wi = base[i] + d
        _prev = self._fitting
        self._fitting = True
        try:
            self.list.setColumnWidth(i, wi)
            self.list.setColumnWidth(j, wj)
        finally:
            self._fitting = _prev
        self._dg_moved = True
        return d

    def _drag_end(self):
        """松手：记成"用户宽度"，弹性列换到被压缩的那一列，再让总宽对齐 viewport。"""
        i = getattr(self, "_dg_col", None)
        moved = bool(getattr(self, "_dg_moved", False))
        self._dg_col = None
        self._dg_base = None
        self._dg_moved = False
        self._hdr_down = False
        try:
            n = int(self.list.columnCount())
            if i is not None and moved and 0 <= i < n - 1:
                j = i + 1
                # ⚠ 其余列必须把**当前实际宽度**记成固定值：它们之前可能不是固定列
                #   （比如原来吃剩余的那一列），不记的话 `_fit_cols` 会把它们打回默认宽度
                #   ⇒ 用户会看到"我只拖了一条线，结果最左边那列整个变小了"。
                for _c in range(n):
                    if _c != j:
                        self._user_w[_c] = int(self.list.columnWidth(_c))
                self._user_w.pop(j, None)   # j 变成弹性列，宽度由 `_fit_cols` 算
                self._elastic = j
            self._fit_cols()
        except Exception:                                          # noqa: BLE001
            pass

    def _fit_cols(self):
        """按 viewport 实际宽度分配列宽：**弹性列**吃掉剩余，其余用固定/用户宽度。

        ★ 2026-10-30（用户「整个表格的宽度对齐窗口大小」+「曲目也得拖啊」）：
          各列优先用**用户拖过的宽度**，没有就用默认；弹性列（`_elastic`，
          默认 0=曲目）永远吃掉剩余 ⇒ 表格总宽**始终等于窗口可用宽**，
          不会右边留白，同时每一列都拖得动。

        ⚠ 调用时机：**只能**在 viewport 已经拿到真实尺寸之后调（见
          `eventFilter(viewPort Resize)`）。父控件 `resizeEvent` 里读
          `viewport().width()` 得到的是控件初始值（638）⇒ 弹性列被钉在最小宽度。
        ⚠ 鼠标按在表头上（拖拽中）**一律跳过** —— 此时改任何列宽都会打乱
          QHeaderView 的拖拽基准，表现成"往右拖，边界往左跑"。
        """
        if not hasattr(self, 'list') or self.list is None:
            return                      # resizeEvent 可能在 self.list 建好之前就来
        if getattr(self, "_hdr_down", False):
            return                      # 拖拽中：松手后 `_fit_after_drag` 会补上
        _prev = getattr(self, "_fitting", False)
        self._fitting = True
        try:
            try:
                vw = int(self.list.viewport().width())
            except Exception:                                      # noqa: BLE001
                vw = 0
            if vw < 200:
                vw = max(360, int(self.list.width()) - 4)
            _els = int(getattr(self, "_elastic", 0) or 0)
            _user = getattr(self, "_user_w", {}) or {}
            _def = getattr(self, "_COL_DEF", {})
            _n = int(self.list.columnCount())
            _avail = max(120, vw - 2)
            _cols = [c for c in range(_n) if c != _els]
            _want = {c: int(_user.get(c, _def.get(c, 160))) for c in _cols}
            # ★★ 2026-10-31（用户截图：表格里只剩「曲目 / 来源」两列，BPM/时长/波形不见了）：
            #   窗口一旦变窄，`_fixed + MIN_COL_W` 就超过可用宽 ⇒ 出现横向滚动条，
            #   **后面的列直接被推到视口外**（实测窗口 717 时列总宽 1234 vs 视口 542）。
            #   ⇒ 超了就把固定列**按比例压缩**（不低于 MIN_COL_W），保证永远不冒滚动条。
            #   ⚠ 压缩只作用于**本次计算**，绝不写回 `_user_w` —— 否则窗口拉大之后
            #     用户原先拖好的宽度就永久丢了（只能靠「恢复默认列宽」找回）。
            _fixed = sum(_want.values())
            if _fixed + MIN_COL_W > _avail and _fixed > 0:
                _budget = max(MIN_COL_W * len(_cols), _avail - MIN_COL_W)
                for _ in range(8):                     # 几轮就收敛，留足余量
                    _s = sum(_want.values())
                    if _s <= _budget:
                        break
                    _flex = [c for c in _cols if _want[c] > MIN_COL_W]
                    _cap = sum(_want[c] - MIN_COL_W for c in _flex)
                    if _cap <= 0:
                        break
                    _take = min(_s - _budget, _cap)
                    for _c in _flex:
                        _cut = int(round((_want[_c] - MIN_COL_W) * _take / float(_cap)))
                        _want[_c] = max(MIN_COL_W, _want[_c] - _cut)
            _fixed = 0
            for _c in _cols:
                _w = int(_want[_c])
                if int(self.list.columnWidth(_c)) != _w:
                    self.list.setColumnWidth(_c, _w)
                _fixed += _w
            # -2：给 header 边框留 2px 余量，避免"差 1px 就冒出横向滚动条"
            # ⚠ 最小宽必须与 `_drag_to` 里的 MIN_COL_W **一致**：
            #   这里原来是 120，拖动那边是 60 ⇒ 拖窄邻居列到 60 后，`_fit_cols`
            #   又把它顶回 120，总宽直接撑破 viewport（实测 1234 → 1294），
            #   用户看到的就是"拖一条线，右边那列反而变宽了 + 冒出横向滚动条"。
            _we = max(MIN_COL_W, _avail - _fixed)
            if int(self.list.columnWidth(_els)) != _we:
                self.list.setColumnWidth(_els, _we)
        finally:
            # ⚠ 必须 finally：中途异常若是把 `_fitting` 留在 True，
            #   之后用户所有的拖拽都会被当"程序设宽"忽略，而且再也不会自愈。
            self._fitting = _prev
        # 来源列的省略预算跟着新列宽走（见 `_restyle_src_col`）
        self._restyle_src_col()

    def _schedule_fit(self):
        """把 `_fit_cols()` 推到**下一轮事件循环**（列表重建后需要）。

        父控件 `resizeEvent` 触发时布局还没激活 ⇒ 当场读 viewport 是旧值。
        延迟一轮后布局已完成，读到的才是真实可用宽度。
        """
        self._arm_fit(0)

    def eventFilter(self, obj, ev):
        """表头：**接管**列宽拖拽（Excel 语义）；viewport：尺寸变化时重分配列宽。

        ⚠ 表头的鼠标事件投递目标是 **header 的 viewport**（不是 QHeaderView 本身）。
        ★ 命中分隔线时必须 `return True` 把事件吞掉 —— 只要让 QHeaderView 收到 Press，
          它就会自己进入拖动状态（改被拖列宽度、右边整体平移），我们的逻辑白做。
        """
        try:
            et = ev.type()
            if obj is getattr(self, "_hdr_viewport", None):
                if et == QEvent.Type.MouseButtonPress \
                        and ev.button() == Qt.MouseButton.LeftButton:
                    _c = self._hdr_handle_col(ev.position().x())
                    if _c is not None:
                        self._drag_begin(_c, ev.position().x())
                        # 抢鼠标：拖到表头外面（上下移出）也要继续收到 Move/Release
                        try:
                            self._hdr_viewport.grabMouse()
                            self._dg_grab = True
                        except Exception:                          # noqa: BLE001
                            self._dg_grab = False
                        return True                     # ★ 吞掉：不许 Qt 自己拖
                elif et == QEvent.Type.MouseMove \
                        and getattr(self, "_dg_col", None) is not None:
                    self._drag_to(ev.position().x())
                    return True
                elif et == QEvent.Type.MouseButtonRelease \
                        and getattr(self, "_dg_col", None) is not None:
                    if getattr(self, "_dg_grab", False):
                        self._dg_grab = False
                        try:
                            self._hdr_viewport.releaseMouse()
                        except Exception:                      # noqa: BLE001
                            pass
                    self._drag_end()
                    return True
                elif et == QEvent.Type.MouseButtonDblClick \
                        and ev.button() == Qt.MouseButton.LeftButton:
                    # 双击分隔线 = 该列按内容自适应（Excel 同款）
                    _c = self._hdr_handle_col(ev.position().x())
                    if _c is not None:
                        self._fit_col_to_content(_c)
                        return True
                elif et == QEvent.Type.UngrabMouse \
                        and getattr(self, "_dg_col", None) is not None:
                    # 鼠标被系统/别处抢走（切窗口、Esc）⇒ 收尾，别让状态卡住
                    self._dg_grab = False
                    self._drag_end()
                    return True
            elif obj is self.list.viewport() and et == QEvent.Type.Resize:
                if not getattr(self, "_fitting", False) \
                        and not getattr(self, "_hdr_down", False):
                    self._fit_cols()
        except Exception:                                          # noqa: BLE001
            pass
        return super().eventFilter(obj, ev)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        # 布局还没激活 ⇒ 延迟一轮（主路径仍是 viewport 自己的 Resize）
        self._arm_fit(0)

    def _selected_paths(self):
        """当前**选中的行**对应的路径（表格化后一行有 5 个单元格，必须按行取）。"""
        out = []
        try:
            for idx in self.list.selectionModel().selectedRows():
                _it = self.list.item(idx.row(), 0)
                if _it is not None:
                    out.append(_it.data(Qt.UserRole))
        except Exception:                                          # noqa: BLE001
            pass
        return out

    # ---------------- 复选框多选（用户要求①）----------------
    def _checked_keys(self):
        """当前列表里**已勾选**行的键集合（本机=路径，导入=`@imported:<sid>`）。"""
        out = set()
        try:
            for i in range(self.list.rowCount()):
                it = self.list.item(i, 0)
                if it is not None and it.checkState() == Qt.CheckState.Checked:
                    out.add(it.data(Qt.UserRole))
        except Exception:                                          # noqa: BLE001
            pass
        return out

    def _checked_items(self):
        """当前列表里已勾选的 QListWidgetItem 列表（顺序 = 列表顺序）。"""
        out = []
        try:
            for i in range(self.list.rowCount()):
                it = self.list.item(i, 0)
                if it is not None and it.checkState() == Qt.CheckState.Checked:
                    out.append(it)
        except Exception:                                          # noqa: BLE001
            pass
        return out

    def _set_all_checked(self, on):
        """把**当前显示**的所有行设为勾选/未勾选（被筛选隐藏的不受影响）。

        ⚠ 期间置 `_building` ⇒ 逐行 setCheckState 触发的 itemChanged 被忽略，
          结束后统一 `_update_lbl()`（避免 N 次重算 + 无谓刷新）。
        """
        st = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        self._building = True
        try:
            for i in range(self.list.rowCount()):
                _it0 = self.list.item(i, 0)
                if _it0 is not None:
                    _it0.setCheckState(st)
        finally:
            self._building = False
        self._update_lbl()

    def select_all(self):
        self._set_all_checked(True)

    def clear_all(self):
        self._set_all_checked(False)

    def _on_item_changed(self, _it):
        """勾选状态变化 → 只更新底部计数（不整表重建，保留滚动/焦点）。"""
        if getattr(self, "_building", False):
            return
        self._update_lbl()

    def _update_lbl(self):
        """重建底部计数标签（含「已勾选 N 首」，仅 N>0 时显示）。"""
        s = getattr(self, "_stat", None)
        if not s:
            return
        lbl = Tf("共 {} 首，已分析 {} 首（当前显示 {} 首）",
                 s["lib"], s["done"], s["rows"])
        if s["imp"]:
            lbl += Tf("   ｜ 含导入包 {} 首", s["imp"])
            # ★ 2026-10-31：说明"其中 X 首与本机重复、已合并显示" ——
            #   否则用户看到导入 21 首却只多出 19 行，会以为歌丢了。
            n_mg = int(s.get("imp_merged") or 0)
            if n_mg > 0:
                lbl += Tf("（与本机合并 {} 首）", n_mg)
        n_cue = int(s.get("cue") or 0)
        if n_cue > 0:
            lbl += Tf("   ｜ 含码表 {} 首", n_cue)
        n = len(self._checked_items())
        if n > 0:
            lbl += Tf("   ｜ 已勾选 {} 首", n)
        self.lbl.setText(lbl)

    # ---------------- 来源筛选（用户要求②）----------------
    def _refresh_source_combo(self, imp_lib, cue_lib=None, cue_cov=None):
        """重建「来源」下拉：全部 / 仅本机 / <包名>… / 码表内曲目（N 首）/ <码表名>…

        选项值（`Qt.UserRole` 数据）用**稳定键**而非显示文本：
          `"__all__"` / `"__local__"` / `"pack:<包名>"`（包名可为空串=早期导入）/
          `"__cue__"`（仅码表派生行）/ `"__cue_any__"`（码表内曲目，**含本机已有**）/
          `"cue:<码表**绝对路径**>"`。
        这样切换语言后文本变了也不会选错；包名/表名是**用户数据**，不走词表翻译
        （只 `i18n_retranslate` 会把固定的「全部/仅本机/早期导入」翻成当前语言）。

        ★ 2026-10-30：`sheets` 现在**同时**从 `cue_cov` 收集。原因——码表曲目可能
          全部被本机曲库覆盖（`music_meta` 完整的正常状态），那时 `cue_lib` 是空的，
          下拉里就一个码表选项都没有，用户以为码表没了。码表本身是"用户导入的东西"，
          它的入口**不该随本机分析数据的状态出现/消失**。

        ⚠ 表名会重名（两张表同名、或同名不同目录）⇒ **稳定键用绝对路径**，
          显示文本才用 basename。

        ⚠ 程序化重填**必须** blockSignals —— 否则 clear/addItem/setCurrentIndex
          会一路触发 currentIndexChanged → cascade refresh（项目铁律）。
        """
        cb = self.cb_src
        cue_lib = cue_lib or {}
        cue_cov = cue_cov or {}
        try:
            cur = cb.currentData(Qt.UserRole)
        except Exception:                                          # noqa: BLE001
            cur = None
        # 收集包名（去重 + 排序；空串=早期导入，单独排在最后）
        packs = sorted({(r.get("pack") or "") for r in imp_lib.values()})
        named = [p for p in packs if p]
        has_early = "" in packs
        # 收集码表（绝对路径 → 显示用表名）；按表名排序，用户好在列表里找
        sheets = {}
        for r in list(cue_lib.values()) + list(cue_cov.values()):
            sp = str(r.get("sheet_path") or "")
            if sp:
                sheets[sp] = str(r.get("sheet_name") or "") or os.path.basename(sp)
        cb.blockSignals(True)
        try:
            cb.clear()
            cb.addItem("全部", "__all__")
            cb.addItem("仅本机", "__local__")
            for pk in named:
                cb.addItem(pk, "pack:" + pk)
            if has_early:
                cb.addItem("早期导入", "pack:")
            if sheets:
                # 「码表内曲目」= 派生行 + 本机已有的那些（后者才是常态）
                cb.addItem(Tf("码表内曲目（{} 首）", len(cue_lib) + len(cue_cov)),
                           "__cue_any__")
                if cue_lib:
                    cb.addItem("仅码表 · 未入库", "__cue__")
                for sp, sn in sorted(sheets.items(), key=lambda kv: kv[1].lower()):
                    cb.addItem(sn, "cue:" + sp)
            # 按稳定键恢复之前的选择；找不到（如那个包/那张表刚被删）就回「全部」
            idx = cb.findData(cur, Qt.UserRole) if cur else -1
            cb.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            cb.blockSignals(False)
        # 把「全部/仅本机/早期导入/仅码表」翻成当前语言（包名/表名不在词表 ⇒ 原样保留）
        try:
            i18n_retranslate(cb)
        except Exception:                                          # noqa: BLE001
            pass

    @staticmethod
    def _src_ok(path, imp, imp_lib, key, cue=False, cue_lib=None, cue_cov=None):
        """来源筛选判据：`全部` → 恒真；`仅本机` → 排除导入包与码表派生行；
        `pack:X` → 只要该包；`__cue__` → 只要码表派生行；
        `__cue_any__` → 只要**码表内曲目**（含本机已有那些）；
        `cue:<表路径>` → 只要该表的曲目（同样含本机已有那些）。"""
        if key in (None, "", "__all__"):
            return True
        if key == "__local__":
            return not imp and not cue
        cov = (cue_cov or {}).get(path) or {}
        if key == "__cue__":
            return bool(cue)
        if key == "__cue_any__":
            return bool(cue) or bool(cov)
        if isinstance(key, str) and key.startswith("cue:"):
            want = key[4:]
            if not (cue or cov):
                return False
            sp = ((cue_lib or {}).get(path) or {}).get("sheet_path", "") if cue \
                else str(cov.get("sheet_path") or "")
            return sp == want
        if isinstance(key, str) and key.startswith("pack:"):
            if not imp:
                return False
            return (imp_lib.get(path) or {}).get("pack", "") == key[5:]
        return True

    def _dedupe_imported(self, imp_lib, meta):
        """隐去「本机其实已经有」的导入行。返回 `(过滤后的 imp_lib, 合并掉几条)`。

        ## 为什么要在展示层做
        `song_id = md5(完整路径)` 对**路径写法**敏感：本机存的是
        `<曲库目录>/...\\02-....mp3`，ERLIB 扫进包的是 `<曲库目录>\\...\\02-....mp3`
        ⇒ 同一个文件两套 id ⇒ 本机行 + `@imported:<sid>` 行各列一遍（用户看到的重复）。
        真正的修复在**导入时**（`ui_main._ask_import_duplicates` 让用户选跳过），
        这里只是兜住**本次修复之前已经导进来的**那些（用户明确要求不删既有数据）。

        ## 判据（与 `erlib.dupdetect.find_duplicates` 同口径，宁少勿多）
        1. 导入歌 `sid` == 本机某行的 `music_meta[path]["song_id"]`（严格）；
        2. 文件名相同（忽略大小写）**且** 两边时长都有值、差 ≤ `DUR_TOL`(0.5s)。

        ⚠ 缺少时长时**不隐藏**：把"同名不同版（radio edit / extended）"藏掉 = 用户丢歌，
          比多看到一行重复严重得多。
        """
        if not imp_lib:
            return imp_lib, 0
        tol = 0.5
        norm = None
        try:
            from erlib.dupdetect import norm_name as _nn, DUR_TOL as _tol
            norm, tol = _nn, _tol
        except Exception:                                        # noqa: BLE001
            # erlib 不可用（极罕见）⇒ 用同口径的本地实现，**绝不因此整列消失**
            def norm(s):
                s = str(s or "").strip().replace("\\", "/").rstrip("/")
                return s.rsplit("/", 1)[-1].strip().lower()
        local_sids = set()
        local_names = {}          # 归一化文件名 -> [时长, ...]（时长 0.0 = 未知）
        try:
            # ★ 2026-11-01 18:20：`self.main.cfg` 是 `config.Config`（**没有 `.get()`**）。
            #   这里原来写 `cfg.get("music_library")` ⇒ AttributeError 被下面 except 吞掉
            #   ⇒ `lib` 恒为空 ⇒ 展示层去重只能靠 `music_meta`，曲库里"已从列表移除、
            #   只剩分析数据"的那批歌判不出来（同一类静默失效，见 `dupdetect.cfg_get`）。
            from erlib.dupdetect import cfg_get as _cfg_get
            lib = list(_cfg_get(self.main.cfg, "music_library") or [])
        except Exception:                                        # noqa: BLE001
            lib = []
        try:
            meta_keys = list((meta or {}).keys())
        except Exception:                                        # noqa: BLE001
            meta_keys = []
        seen = set()
        for p in list(lib) + [k for k in meta_keys if k not in lib]:
            ps = str(p or "")
            if not ps or ps in seen:
                continue
            seen.add(ps)
            rec = (meta or {}).get(ps)
            if not isinstance(rec, dict):
                rec = {}
            try:
                sid = int(rec.get("song_id") or 0)
            except (TypeError, ValueError):
                sid = 0
            if sid:
                local_sids.add(sid)
            g = rec.get("grid") if isinstance(rec.get("grid"), dict) else {}
            dur = 0.0
            for v in (g.get("dur"), rec.get("duration")):
                try:
                    dur = float(v or 0.0)
                except (TypeError, ValueError):
                    dur = 0.0
                if dur > 0.0:
                    break
            key = norm(os.path.basename(ps.replace("\\", "/")))
            if key:
                local_names.setdefault(key, []).append(max(0.0, dur))
        out = {}
        merged = 0
        for fp, r in imp_lib.items():
            try:
                sid = int(r.get("sid") or 0)
            except (TypeError, ValueError):
                sid = 0
            try:
                du = float(r.get("dur") or 0.0)
            except (TypeError, ValueError):
                du = 0.0
            dup = bool(sid and sid in local_sids)
            if not dup and du > 0.0:
                for ld in local_names.get(norm(r.get("file_name") or ""), ()):
                    if ld > 0.0 and abs(du - ld) <= tol:
                        dup = True
                        break
            if dup:
                merged += 1
                continue
            out[fp] = r
        return out, merged

    def _dup_imported_items(self):
        """导入库里**与本机重复**的那些：返回 `(imp_lib, items)`。

        ★★ 2026-10-31（主理人实测 6236:6236）：数据源必须是 `imported_meta.db` **全体**
          （`_imported_lib()` 读的就是整表），**不能**用"当前可见行" ——
          重复行已被 `_dedupe_imported` 隐去，只看可见行会永远一条都清不掉。
        判据复用 `erlib.dupdetect.find_duplicates`（与导入时**同一套**，别复制一份）。
        """
        imp_lib = self._imported_lib()
        if not imp_lib:
            return {}, []
        try:
            from erlib.dupdetect import (find_duplicates, local_tracks,
                                         norm_name, to_dur)
        except Exception:                                        # noqa: BLE001
            return imp_lib, []
        tracks = []
        for r in imp_lib.values():
            tracks.append({"sid": int(r.get("sid") or 0),
                           "file_name": r.get("file_name") or "",
                           "name": norm_name(r.get("file_name") or ""),
                           "dur": to_dur(r.get("dur"))})
        _sids, items = find_duplicates(tracks, local_tracks(self.main.cfg))
        return imp_lib, items

    def _purge_imported_dups(self):
        """**清理与本机重复的导入副本**（独立、可确认的一步；返回删掉的歌数）。

        为什么独立成一个动作（而**不**在导入流程里自动删）：
          · 用户要能自己决定"删不删"，导入是导入、清理是清理；
          · 删了要能重来（重新导入包即可重建），所以只打印清单、备份元数据库。

        安全边界见 `imported_lib.purge_sids()`：本机自己的 song_id **只删导入副本的
        元数据，绝不删它的指纹**（那是本机那首歌的命）。
        """
        try:
            _imp, items = self._dup_imported_items()
        except Exception as e:                                   # noqa: BLE001
            QMessageBox.warning(self, T("清理重复导入曲目"),
                                Tf("检查重复失败：{}", e))
            return 0
        if not items:
            # 幂等：再点一次就是这句话，不报错、不删东西
            QMessageBox.information(
                self, T("清理重复导入曲目"),
                T("没有重复：导入库里没有与本机同名同长的曲目。"))
            return 0
        names = "\n".join("· " + (it.get("file_name") or "") for it in items[:5])
        if len(items) > 5:
            names += Tf("\n…等共 {} 首", len(items))
        ret = QMessageBox.question(
            self, T("清理重复导入曲目"),
            Tf("要清理这 {} 首导入副本吗？\n\n{}\n\n"
               "这些歌本机已有同名同长的文件，删掉导入副本即可让识别只认本机那份。\n"
               "删的是**导入库里的副本，不会动你的音乐文件**。\n\n"
               "会先备份 imported_meta.db；指纹按 song_id 精确删除（不备份指纹库）。",
               len(items), names),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return 0
        sids = [int(it["sid"]) for it in items if int(it.get("sid") or 0)]
        protect = set()
        try:
            from erlib.dupdetect import local_tracks
            protect = {int(t["sid"]) for t in local_tracks(self.main.cfg)
                       if t.get("sid")}
        except Exception as e:                                   # noqa: BLE001
            # ★ 2026-10-31（QA 提的 fail-open）：原来这里 `protect = set()` 就往下走 ——
            #   保护集为空时，万一"导入 sid 与本机 sid 相同"，删指纹就等于把本机那首歌
            #   从识别里抹掉。**拿不到本机 sid 就拒绝清理**（fail-closed）。
            print("[曲库] 读取本机曲库失败，已取消清理：%s" % e)
            QMessageBox.warning(
                self, T("清理重复导入曲目"),
                T("读取本机曲库失败，已取消清理（避免误删本机指纹）。"))
            return 0
        if not protect:
            # 本机一首都没扫过 ⇒ 不存在"与本机重复"，不该有清理动作；安全起见也拒绝。
            print("[曲库] 本机曲库为空，已取消清理（不确认保护集不删指纹）")
            QMessageBox.warning(
                self, T("清理重复导入曲目"),
                T("本机曲库为空，已取消清理（避免误删本机指纹）。"))
            return 0
        try:
            from config import app_base_dir
            import imported_lib as _IL
            st = _IL.purge_sids(app_base_dir(), sids, protect_sids=protect)
        except Exception as e:                                   # noqa: BLE001
            print("[曲库] 清理重复导入曲目失败：%s" % e)
            QMessageBox.warning(self, T("清理重复导入曲目"),
                                Tf("清理失败：{}", e))
            return 0
        # ★ 刷新：面板 + **引擎的导入网格/段落缓存**
        #   （复用导入完成那次注入的同一入口，不另写一套；否则界面/引擎还是旧数据）
        for _fn in ("_inject_song_grids", "_init_recognizer"):
            _f = getattr(self.main, _fn, None)
            if callable(_f):
                try:
                    _f()
                except Exception as _e:                          # noqa: BLE001
                    print("[曲库] 清理后 %s 失败：%s" % (_fn, _e))
        try:
            self.refresh()
        except Exception:                                        # noqa: BLE001
            pass
        print("[曲库] 清理重复导入：歌 %d 首 / 指纹 %d 条 / 封面 %d 张 / 备份 %s"
              % (st.get("songs", 0), st.get("fingerprints", 0),
                 st.get("covers", 0), os.path.basename(st.get("backup") or "") or "—"))
        _pt = st.get("protected") or []
        QMessageBox.information(
            self, T("清理重复导入曲目"),
            Tf("已清理 {} 首导入副本：\n"
               "· 删除指纹 {} 条（只按 song_id）\n"
               "· 删除封面 {} 张\n"
               "· 元数据已备份：{}\n\n"
               "本机的音乐文件没有动；下次导入同一个包还会重新出现（到时选「跳过这些」即可）。",
               st.get("songs", 0), st.get("fingerprints", 0), st.get("covers", 0),
               os.path.basename(st.get("backup") or "") or "—")
            + (("\n\n⚠ 这几首的 sid 与本机相同，指纹**保留**（那是本机那首歌的）：\n"
                + ", ".join(str(x) for x in _pt)) if _pt else ""))
        return int(st.get("songs") or 0)

    def _imported_lib(self):
        """导入包里的歌：`{"@imported:<sid>": 行数据}`。读不到时 `{}`（绝不抛）。

        ⚠ 每次 refresh 都读一次 `imported_meta.db`（几十行，毫秒级）。
          大库（几千首手工导入）也不值得为它加缓存 —— 加了反而要考虑失效。
        """
        try:
            from config import app_base_dir
            import imported_lib as _IL
            out = {}
            for r in _IL.display_rows(app_base_dir()):
                out["@imported:%d" % int(r["sid"])] = r
            return out
        except Exception:                                          # noqa: BLE001
            return {}

    def _cue_lib_rows(self):
        """码表带来的曲库**派生行**：`{"@cue:<表路径>#<track_key>": 行数据}`。

        ⚠ 每次 `refresh()` 现算 —— `CueIndex().load_cfg(cfg)` 是**纯内存、零 IO**；
          包是否还在只做一次 `os.path.isfile`（几次 stat，可忽略）。
        ⚠ **不要**在这里 `rescan()`：那是读包（几百 ms 的 IO），而 refresh 会被
          切排序/切筛选/勾选后恢复频繁调用。
        ⚠ **绝不**把结果写进 `cfg["music_library"]` / `cfg["music_meta"]` ——
          「码表删了 ⇒ 曲库里的一起删」靠的就是**没有实体可删**
          （详见 `cue_index.library_rows()` 的说明；别补级联删除）。
        读不到/异常 ⇒ `{}`（曲库面板绝不能被码表问题拖挂）。
        """
        try:
            import cue_index as _CI
            idx = _CI.CueIndex().load_cfg(self.main.cfg)
            return {r["key"]: r for r in _CI.library_rows(self.main.cfg, idx)}
        except Exception:                                          # noqa: BLE001
            return {}

    def _active_cue_ids(self):
        """**正在演出中使用的码表**里所有曲目的身份（`fp_id`/`file_md5`/`meta_id`）。

        判据：`engine._cue_sheet` —— 码表页「装载 / 绑定曲目」时由
        `engine.set_cue_sheet()` 设的那个 `CueSheet` 对象。它是**此刻唯一**
        参与演出的表；曲库里其他表的歌只是参考 ⇒ 灰显（用户 2026-10-30 要求）。

        ⚠ 为什么用"曲目身份"而不是"表路径"判定：
          `cuesheet.CueSheet` 是纯内容 dataclass，**没有 path 字段**
          （路径只存在于 `CueIndex` 的索引条目里，装载时没回填到对象上）。
          身份判定同样精确到"哪几行属于在用表"，而且不用改 `set_cue_sheet()`
          的签名（它有 8 处调用，改签名要全线回归）。
        ⚠ 返回空集 = 当前没装载任何码表 ⇒ 所有码表行都灰。
          这是**正确语义**：没装表就没有"演出中的码表"。
        ⚠ 一律 `.lower()` 归一：`identity.file_md5` 可能大写（不同工具产物），
          而 `music_meta.file_md5` 经 `_as_md5()` 统一小写。
        """
        ids = set()
        try:
            sheet = getattr(self.main.engine, "_cue_sheet", None)
        except Exception:                                        # noqa: BLE001
            sheet = None
        for t in (getattr(sheet, "tracks", None) or []):
            idn = getattr(t, "identity", None)
            if idn is None:
                continue
            for v in (getattr(idn, "fp_id", ""),
                      getattr(idn, "file_md5", ""),
                      getattr(idn, "meta_id", "")):
                s = str(v or "").strip().lower()
                if s:
                    ids.add(s)
        return ids

    @staticmethod
    def _cue_row_active(row, active_ids):
        """这一行（码表派生行 / 本机覆盖行）是否属于**正在使用**的那张码表。"""
        if not active_ids or not isinstance(row, dict):
            return False
        for k in ("fp_id", "file_md5", "meta_id"):
            v = str(row.get(k) or "").strip().lower()
            if v and v in active_ids:
                return True
        return False

    def _cue_covered(self):
        """码表曲目里**本机曲库也有**的那些：`{本机路径: 行数据}`。

        与 `_cue_lib_rows()` 互补且**互斥**（两边判据都在
        `cue_index._LocalIndex.find()`，不会同一首歌既"未覆盖"又"已覆盖"）：
          · `_cue_lib_rows()` → 新建行（青绿色，标"未入库 + 指纹"）
          · 本函数            → 不新建行，只让「来源」列显示码表名、并能被筛选

        为什么需要（用户 2026-10-30「怎么来源里没有码表的选项了」）：正常状态下
        码表曲目本机都有 ⇒ 派生行为 0 ⇒ 码表在曲库面板上"看不见了"。数据没丢，
        缺的是入口。

        ⚠ 每次 `refresh()` 现算，但很便宜：`load_cfg` 纯内存 + 每张表一次 `isfile`。
        ⚠ 异常 ⇒ `{}`：曲库面板绝不能因为码表问题变空列表。
        """
        try:
            import cue_index as _CI
            idx = _CI.CueIndex().load_cfg(self.main.cfg)
            return _CI.covered_paths(self.main.cfg, idx)
        except Exception:                                          # noqa: BLE001
            return {}

    def _start_cover_backfill(self, paths):
        """后台补封面（串行、可中断）。⚠ 已经在跑就不重复起。"""
        try:
            if not paths:
                return
            job = self._cover_job
            if job is not None and job.isRunning():
                return
            w = CoverBackfillWorker(paths, self)
            w.done.connect(self._on_cover_done)
            self._cover_job = w
            w.start()
        except Exception:                                          # noqa: BLE001
            pass

    # ---------------- 波形列（2026-10-30 用户要求） ----------------
    def _wave_preview(self, amp_list, dur):
        """整曲包络 → 缩略包络 `(amp[_WAVE_COLS], dur)`；没数据返回 None。

        ★ 2026-10-31：从 `_wave_for()` 里抽出来 —— 本机歌（`.wj`）与导入歌
          （`imported_meta.db`）两条数据源必须走**同一套**降采样/对比度处理，
          否则同一首歌在两种来源下行数、起伏都不一样。
        """
        try:
            import numpy as _np
            amp = _np.asarray(amp_list, dtype=_np.float32)
            if amp.size == 0:
                return None
            if amp.size > _WAVE_COLS:
                edges = _np.linspace(0, amp.size, _WAVE_COLS + 1).astype(_np.int64)
                edges[-1] = amp.size
                cnt = _np.maximum(1, _np.diff(edges)).astype(_np.float32)
                # ★ 只取 max 会让"母带很响"的曲子整块顶满（实测列最大值均值 0.86
                #   ⇒ 缩略图变成一整面实心墙，看不出任何形状）。取 0.75*均值 + 0.25*峰值，
                #   既保留瞬态尖峰又看得见包络起伏 —— 与详情页 `_paint_waveform` 同口径。
                amp = (0.75 * (_np.add.reduceat(amp, edges[:-1]) / cnt)
                       + 0.25 * _np.maximum.reduceat(amp, edges[:-1]))
            # ★ 再压一次对比度：母带压过的曲子绝大多数列都落在 0.6~1.0，
            #   46px 高的缩略图上只差 6~7 像素 ⇒ 看着还是一块实心。
            #   `a**1.6` 把 0.69 拉到 0.58、0.97 保持 0.99 ⇒ 起伏立刻显形。
            #   （详情页不用这么干：那里有 100+ 像素高 + 按振幅渐变配色）
            amp = _np.clip(amp, 0.0, 1.0) ** 1.6
            return (amp, float(dur or 0.0))
        except Exception:                                          # noqa: BLE001
            return None

    def _wave_for(self, path):
        """取波形包络：`(amp[160], dur)`；没有缓存返回 None。

        ⚠ **只读 `.wj` 缓存**（`waveform.waveform_load`），绝不在这里解码音频。
          缺的由 `_start_wave_backfill` 后台补。结果进 `_wave_cache`（含"确实没有"
          的否定缓存 `False`，免得每帧重试一次读盘）。
        ★★ 2026-10-31（用户「eravj 的导入怎么没有波形」）：导入歌是伪路径
          `@imported:<sid>`（本机没有文件、更没有 `.wj`）⇒ 走 `self._imp_wave`
          （从 `imported_meta.db` 的 `waveforms` 表读出来的）。
        """
        if str(path).startswith("@imported:"):
            return self._imp_wave.get(path) or None
        hit = self._wave_cache.get(path)
        if hit is not None:
            return hit or None
        got = None
        try:
            import waveform as _wf
            w = _wf.waveform_load(path)
            if w and (w.get("amp") or []):
                got = self._wave_preview(w["amp"], w.get("dur") or 0.0)
        except Exception:                                          # noqa: BLE001
            got = None
        self._wave_cache[path] = got if got else False
        while len(self._wave_cache) > 200:                          # FIFO 淘汰
            self._wave_cache.pop(next(iter(self._wave_cache)))
        return got

    def _wave_bands(self, path):
        """该曲目的能量段落色带 `[(t0, t1, tier), ...]`（refresh 时已备好）。"""
        return self._wave_sec_map.get(path) or ()

    def _refresh_imp_extras(self, imp_lib):
        """读导入歌的**波形 + 段落**（供「波形」列画包络/色带）。

        ★★ 2026-10-31（用户「eravj 的导入怎么没有波形」）：导入歌本机没有音频文件，
          `.wj` 永远读不到 ⇒ 波形列 21 行全空白。数据其实在 `imported_meta.db`：
            · `waveforms` 表     —— 包络（erlib 扫描时算好、导入时落库）；
            · `songs.sections`   —— 能量段落（画下方色带）。
          ⚠ **按库文件的 (mtime, size) 缓存**：refresh 会被"勾选一下"触发，每次都解
            21 条 zlib BLOB 是白费（且会卡勾选的响应）。库一变（新导入/删除）自动重读。
        """
        if not imp_lib:
            self._imp_wave, self._imp_secs, self._imp_key = {}, {}, None
            return
        base, key = None, None
        try:
            from config import app_base_dir
            import imported_lib as _IL
            base = app_base_dir()
            p = _IL.meta_db_path(base)
            key = (os.path.getmtime(p), os.path.getsize(p))
        except Exception:                                          # noqa: BLE001
            base = base if base else None
        if key is not None and key == self._imp_key:
            return
        self._imp_key = key
        wf_map, sec_map = {}, {}
        try:
            if base:
                import imported_lib as _IL
                for sid, w in _IL.waveforms_all(base).items():
                    got = self._wave_preview(w.get("amp") or [],
                                             w.get("dur") or 0.0)
                    if got:
                        wf_map["@imported:%d" % int(sid)] = got
                for sid, secs in _IL.sections_display(base).items():
                    sec_map["@imported:%d" % int(sid)] = [
                        {"t0": float(x.get("t0") or 0.0),
                         "t1": float(x.get("t1") or 0.0),
                         "tier": str(x.get("tier") or "mid")} for x in secs]
        except Exception:                                          # noqa: BLE001
            pass
        self._imp_wave, self._imp_secs = wf_map, sec_map

    def _start_wave_backfill(self, paths):
        """后台补波形缓存（串行、可中断）。⚠ 已经在跑就不重复起。"""
        try:
            if not paths:
                return
            job = getattr(self, "_wave_job", None)
            if job is not None and job.isRunning():
                return
            try:
                if self.main.engine.running:
                    return          # 演出中不补（解码要读整曲，抢 IO）
            except Exception:                                      # noqa: BLE001
                pass
            w = WaveBackfillWorker(paths, self.main, self)
            w.one.connect(self._on_wave_one)
            w.done.connect(self._on_wave_done)
            self._wave_job = w
            w.start()
        except Exception:                                          # noqa: BLE001
            pass

    def _on_wave_one(self, path):
        """补好一首 ⇒ 丢掉否定缓存，只重绘视口（不重建整张表）。"""
        try:
            self._wave_cache.pop(path, None)
            self.list.viewport().update()
        except Exception:                                          # noqa: BLE001
            pass

    def _on_wave_done(self, _n):
        try:
            self.list.viewport().update()
        except Exception:                                          # noqa: BLE001
            pass

    def _on_cover_done(self, n):
        """补图完成 → 刷新列表（封面从缓存里读，主线程不做提取）。"""
        try:
            if n > 0:
                self.refresh()
        except Exception:                                          # noqa: BLE001
            pass

    def showEvent(self, ev):
        """切回曲库页时，**若"演出中的码表"变了**就重建列表。

        ★★ 2026-10-30（用户：「加上了怎么曲库还是灰的」）：
          码表曲目的青绿/灰是由 `_active_cue_ids()`（引擎当前装载的那张表）决定的，
          但它**只在 `refresh()` 里读一次**。用户在码表页把表装载/卸载之后切回曲库，
          列表还是上一次的渲染 ⇒ 明明是"演出中"的表，歌却还是灰的，看着像没生效。
          现场日志对得上：`cue_health` 里 `sheet=1`（表确实装上了）而曲库仍旧灰。

        ⚠ **不能无条件 refresh**：它会 `clearContents()` 整表重建，滚动位置、键盘焦点、
          多选状态全丢 —— 用户只是切个页签，列表跳回顶部会很烦。
          所以只在"在用表身份集合"真的变了才重建（比对 `refresh()` 时留的快照）。
        """
        super().showEvent(ev)
        try:
            cur = self._active_cue_ids()
        except Exception:                                        # noqa: BLE001
            return
        prev = getattr(self, "_act_snap", None)
        if prev is not None and prev == cur:
            return
        self._act_snap = cur
        try:
            self.refresh()
        except Exception:                                        # noqa: BLE001
            pass

    def closeEvent(self, ev):
        try:
            if self._cover_job is not None and self._cover_job.isRunning():
                self._cover_job.stop()
                self._cover_job.wait(1500)
        except Exception:                                          # noqa: BLE001
            pass
        super().closeEvent(ev)

    # ---------------- 封面大小（右键菜单，用户 2026-10-01 要求）----------------
    def _load_cover_size(self):
        """从配置读封面尺寸；非法/缺失一律回退默认档。"""
        try:
            v = int(self.main.cfg["ui"].get("music_cover_size", COVER_ICON) or COVER_ICON)
        except Exception:                                          # noqa: BLE001
            v = COVER_ICON
        allowed = [px for _lbl, px in COVER_SIZES]
        return v if v in allowed else COVER_ICON

    def set_cover_size(self, px):
        """设置封面尺寸：立即生效 + 记住。⚠ 缩略图缓存按尺寸分文件（见 music_cover），
        所以换尺寸只是换读哪一批缓存，**不会重新解码**（列表先显示占位图，
        后台补图线程会按新尺寸补出来）。"""
        try:
            px = int(px)
        except Exception:                                          # noqa: BLE001
            return
        if px == self.cover_size:
            return
        self.cover_size = px
        try:
            self.list.setIconSize(QSize(px, px))
            self.list.verticalHeader().setDefaultSectionSize(int(px) + 6)
        except Exception:                                          # noqa: BLE001
            pass
        try:
            self.main.cfg["ui"]["music_cover_size"] = px
            self.main.cfg.save()
        except Exception:                                          # noqa: BLE001
            pass
        self.refresh()

    @staticmethod
    def _resolve_targets(path, checked, selected):
        """决定右键**批量操作对象**（纯函数，便于单测）。

        规则（用户要求①）：
          · **有勾选** ⇒ 一律以**勾选集**为操作对象（`checked_mode=True`）；
          · 无勾选 ⇒ 退回旧行为：右键项在选中集里就用整个选中集，否则只用它自己。
        返回 `(loc, imp, checked_mode)`：loc=本机路径、imp=导入伪路径。

        ⚠ **码表派生行（`@cue:`）不进这两组**：它们没有实体，
          删了下次 refresh 又回来（"删掉"只是自欺）。要让它消失，
          正确做法是到「码表」页删除那张表 —— 派生行会跟着消失。
        """
        if checked:
            src = list(checked)
            mode = True
        else:
            src = list(selected or ())
            if path not in src:
                src = [path]
            mode = False
        loc = [p for p in src
               if not str(p).startswith("@imported:") and not str(p).startswith("@cue:")]
        imp = [p for p in src if str(p).startswith("@imported:")]
        return loc, imp, mode

    def _ctx_menu(self, pos):
        _hit = self.list.itemAt(pos)
        if _hit is None:
            return
        it = self.list.item(_hit.row(), 0)      # 语义只在第 0 列
        if it is None:
            return
        path = it.data(Qt.UserRole)
        # ★ 2026-10-09：右键项若**在选中集里** ⇒ 对整个选中集操作（支持批量删除）；
        #   否则只对它自己（并让它成为唯一选中，行为与旧版一致）。
        # ★ 2026-10-09（用户要求①）：**只要有勾选**，删除一律针对勾选集。
        checked = [x.data(Qt.UserRole) for x in self._checked_items()]
        selected = self._selected_paths()
        loc, imp, checked_mode = self._resolve_targets(path, checked, selected)
        # ★ 码表派生行：**不可移除**（没有实体，删了下次 refresh 又回来）。
        #   这里单独取一份，只提供「查看信息 + 跟随码表」的说明。
        _pool = list(checked) if checked_mode else list(selected or ())
        if not checked_mode and path not in _pool:
            _pool = [path]
        cue = [p for p in _pool if str(p).startswith("@cue:")]
        if not checked_mode and path not in selected:
            self.list.setCurrentCell(int(self.list.row(it)), 0)
        solo_loc = (not checked_mode) and (len(loc) == 1 and not imp)  # 单首本机歌=旧版完整菜单
        menu = QMenu(self)
        edit = rescan = info = None
        if solo_loc:
            # ★ 2026-10-30 用户「大砍刀」：曲风识别删除 ⇒「纠正曲风…」菜单项删除
            edit = None
            rescan = menu.addAction(T("重新分析扫描此曲目"))
            # ★ 2026-10-28（用户要求）：把「此曲目信息」与「歌曲详情…」**合并成一个入口**，
            #   曲目信息作为只读块显示在详情页顶部（同一份数据，不再两个弹窗）。
            info = menu.addAction(T("曲目信息与详情…"))
            menu.addSeparator()
            # ★ 封面大小（用户要求）：放子菜单里，当前档位打勾。
            #   为什么不做成"拖拽缩放"：曲库是**演出前整理**用的，这里要的是
            #   "一次调好、记住"，不需要连续微调；子菜单也最不容易误触。
            sub = menu.addMenu(T("封面大小"))
            cur = self.cover_size
            for lbl, px in COVER_SIZES:
                act = sub.addAction("%s (%d px)" % (T(lbl), px))
                act.setCheckable(True)
                act.setChecked(px == cur)
                act.triggered.connect(lambda _c=False, v=px: self.set_cover_size(v))
        elif cue and not loc and not imp:
            # 码表派生行：本机没有文件 ⇒ 只能看信息（以及被告知"跟随码表"）
            info = menu.addAction(T("查看信息"))
        elif len(imp) == 1 and not loc and str(path).startswith("@imported:"):
            # 导入包的歌：本机没有文件 ⇒ 只给「查看信息」与删除
            # （⚠ 必须是**右键的那一项本身**是导入歌，否则信息框会拿本机路径去解析 sid）
            info = menu.addAction(T("查看信息"))
        rm_loc = rm_imp = None
        if loc:
            if solo_loc:
                menu.addSeparator()
            if checked_mode:
                rm_loc = menu.addAction(
                    theme.danger_icon(),
                    Tf("从曲库移除勾选的 {} 首", len(loc)))
            else:
                rm_loc = menu.addAction(
                    theme.danger_icon(),
                    T("从曲库移除") if len(loc) == 1
                    else Tf("从曲库移除（{} 首）", len(loc)))
        if imp:
            if checked_mode:
                rm_imp = menu.addAction(
                    theme.danger_icon(),
                    Tf("从导入库移除勾选的 {} 首", len(imp)))
            else:
                rm_imp = menu.addAction(
                    theme.danger_icon(),
                    T("从导入库移除") if len(imp) == 1
                    else Tf("从导入库移除（{} 首）", len(imp)))
        if cue:
            # 明确告诉用户为什么这里没有"移除"（而不是让他以为菜单坏了）
            menu.addSeparator()
            _note = menu.addAction(T("码表曲目跟随码表：请到「码表」页删除该表"))
            _note.setEnabled(False)
        # ★★ 2026-10-31（用户「识别不到」的真正机理）：**清理与本机重复的导入副本**。
        #   为什么必须有这个入口：重复行已被 `_dedupe_imported` 隐去 ⇒ 用户**右键点不到**
        #   那几行，没法用「从导入库移除」自救；而库里那两套指纹还在，`match()` 分票
        #   50/50（现场实测 6236:6236）⇒ 这两首永远认不出来。
        #   ⚠ 判据/数据源都取自 `imported_meta.db` **全体**（不是可见行）。
        clean_dup = None
        try:
            _ndup = len(self._dup_imported_items()[1])
        except Exception:                                        # noqa: BLE001
            _ndup = 0
        if _ndup > 0:
            menu.addSeparator()
            clean_dup = menu.addAction(
                theme.danger_icon(),
                Tf("清理与本机重复的导入曲目…（{} 首）", _ndup))
        i18n_retranslate(menu)      # 临时菜单：弹出前刷语言
        act = menu.exec(self.list.mapToGlobal(pos))
        if act is None:
            return
        if clean_dup is not None and act == clean_dup:
            self._purge_imported_dups()
            return
        if act == rescan:
            self.main.rescan_music(path)
        elif act == info:
            if solo_loc:
                self._open_music_detail(path, with_info=True)
            elif str(path).startswith("@cue:"):
                self._show_cue_info(path)
            else:
                try:
                    self._show_imported_info(int(str(path).split(":", 1)[1]))
                except Exception:                                  # noqa: BLE001
                    pass
        elif act == rm_loc:
            self._remove_local(loc)
        elif act == rm_imp:
            self._remove_imported(imp)

    def _open_music_detail(self, path, with_info=False):
        """打开演出端「歌曲详情」对话框（波形上标 BPM / 八拍起点 / BPM 分段）。

        写入 `cfg["music_meta"][path]["grid_manual"]`；关闭后立即重新注入引擎，演出中
        改标注当场生效（引擎下一帧 `_grid_observe` 就走新的 `effective_grid`）。
        """
        try:
            from music_detail import MusicDetailDialog
        except Exception as e:                                   # noqa: BLE001
            QMessageBox.warning(self, T("歌曲详情…"), Tf("打开歌曲详情失败：{}", e))
            return
        try:
            _info = ""
            if with_info:
                try:
                    from music_detail import info_text as _mt_info
                    _info = _mt_info(self.main.cfg, path)
                except Exception:                                # noqa: BLE001
                    _info = ""
            dlg = MusicDetailDialog(self.main.cfg, path, parent=self, info_text=_info)
            dlg.exec()
            try:
                self.main._inject_song_grids()
            except Exception:                                    # noqa: BLE001
                pass
            self.refresh()
        except Exception as e:                                   # noqa: BLE001
            QMessageBox.warning(self, T("歌曲详情…"), Tf("打开歌曲详情失败：{}", e))

    def _remove_local(self, paths):
        """从本机曲库移除（**二次确认**；批量 = 一次 save + 一次 refresh）。"""
        paths = [p for p in (paths or []) if p]
        if not paths:
            return
        names = "\n".join(os.path.basename(str(p)) for p in paths[:6])
        if len(paths) > 6:
            names += Tf("\n…等共 {} 首", len(paths))
        ret = QMessageBox.question(
            self, T( T("从曲库移除")),
            Tf("从曲库移除这 {} 首？\n\n{}\n\n"
               "只从曲库列表移除（连同曲风/网格等分析数据）；"
               "音频文件本身不会被删除。", len(paths), names),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        try:
            self.main.remove_music_many(paths)
        except Exception as e:                                   # noqa: BLE001
            # ⚠ 2026-10-10（审计 P2）：移除失败必须让用户看见 —— 旧实现静默 pass，
            #   用户点了"移除"却什么都没发生、无从排查。失败时不 refresh（避免掩盖）。
            print("[曲库] 从曲库移除失败：%s" % e)
            QMessageBox.warning(
                self, T( T("从曲库移除")),
                Tf("移除失败，这 {} 首未从曲库移除：\n{}", len(paths), e))

    def _remove_imported(self, fake_paths):
        """从导入库移除（**二次确认**；曲风/封面/指纹一起删 —— 删完就不再认得这些歌）。"""
        sids = []
        for p in (fake_paths or []):
            try:
                sids.append(int(str(p).split(":", 1)[1]))
            except Exception:                                    # noqa: BLE001
                pass
        if not sids:
            return
        ret = QMessageBox.question(
            self, T( T("从导入库移除")),
            Tf("从导入库移除这 {} 首？\n\n"
               "它们的曲风、封面与指纹会一起删除 —— "
               "删除后就不再认得这些歌；本机自己扫描的曲目不受影响。", len(sids)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        try:
            from config import app_base_dir
            import imported_lib as _IL
            _IL.remove_songs(app_base_dir(), sids)
        except Exception as e:                                   # noqa: BLE001
            # ⚠ 2026-10-10（审计 P2）：同 _remove_local —— 失败要提示且**不 refresh**
            #   （旧实现失败也照样 refresh，让"没删掉"看起来像"删掉了"）。
            print("[曲库] 从导入库移除失败：%s" % e)
            QMessageBox.warning(
                self, T( T("从导入库移除")),
                Tf("移除失败，这 {} 首未从导入库移除：\n{}", len(sids), e))
            return
        self.refresh()

    def _show_imported_info(self, sid):
        """导入歌的信息框（本机没有该音频文件，只能看分析数据）。"""
        r = self._imported_lib().get("@imported:%d" % int(sid))
        if not r:
            return
        lines = [
            "%s: %s" % (T("曲名"), r["title"] or r["file_name"] or "-"),
            "%s: %s" % (T("艺人"), r["artist"] or "-"),
        ]
        if r["bpm"]:
            lines.append("BPM: %.0f" % r["bpm"])
        if r["dur"]:
            lines.append("%d:%02d" % (int(r["dur"]) // 60, int(r["dur"]) % 60))
        lines.append("")
        lines.append(T("来自导入曲库分析包（本机没有该音频文件，靠指纹识别）"))
        QMessageBox.information(self, T( T("导入包曲目")), "\n".join(lines))

    def _show_cue_info(self, fake_path):
        """**码表派生行**的信息框（本机没有该文件，数据全部来自码表包）。

        ⚠ 重新从 `cue_index.library_rows()` 取一遍，而不是把行数据塞进
          `QListWidgetItem`：派生行的数据必须**永远来自当前码表**（表改了指纹，
          这里要显示新指纹），缓存反而会显示过期信息。
        """
        r = self._cue_lib_rows().get(str(fake_path))
        if not r:
            return
        lines = [
            "%s: %s" % (T("曲名"), r.get("title") or r.get("file_name") or "-"),
            "%s: %s" % (T("艺人"), r.get("artist") or "-"),
            "%s: %s" % (T("码表"), r.get("sheet_name") or "-"),
        ]
        if r.get("bpm"):
            lines.append("BPM: %.0f" % r["bpm"])
        dur = r.get("duration_sec") or 0.0
        if dur:
            lines.append("%d:%02d" % (int(dur) // 60, int(dur) % 60))
        if r.get("fp_id"):
            lines.append("%s: %s" % (T("指纹"), r["fp_id"]))
        if r.get("file_md5"):
            lines.append("md5: %s" % r["file_md5"])
        lines.append("")
        lines.append(T("来自码表（本机曲库未收录；码表删除后这行自动消失）"))
        QMessageBox.information(self, T( T("码表曲目")), "\n".join(lines))


class LibraryGrid(MediaGrid):
    """素材库网格：支持拖出到图层行 + 从资源管理器拖入导入 + 自定义右键菜单"""
    files_dropped = Signal(list)   # 外部拖入的文件/文件夹路径列表

    def __init__(self, main, parent=None):
        super().__init__(parent)
        self.main = main
        # ★ 2026-11-08：拖拽状态必须先初始化。以前只在 mousePressEvent 里创建，
        #   某些鼠标进入/重排后直接收到 mouseMoveEvent（没有 press）⇒
        #   `AttributeError: 'LibraryGrid' object has no attribute '_press_item'`。
        self._press_pos = None
        self._press_item = None
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.CopyAction)
        # ★ 2026-09-28：双击缩略图 = **立即播放到当前图层**（等价于右键 →「播放到图层」→ 选中那层）。
        #   比"拖到图层"更快，演出中一只手就能用；自动图层会被 add_to_layer 里的
        #   auto_mode 判断挡掉，不会污染自动匹配。
        self.itemDoubleClicked.connect(self._on_double_click)

    def _on_double_click(self, item):
        """双击素材 = 播放到当前图层（快捷方式；拖拽依然可用）。"""
        try:
            m = item.data(Qt.UserRole)
            if m is None:
                return
            idx = int(getattr(self.main, "_cur_layer", 0) or 0)
            self.main.play_to_layer(idx, [m])
        except Exception:                                        # noqa: BLE001
            pass

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
        if (getattr(self, "_press_item", None) is not None
                and (e.buttons() & Qt.LeftButton)
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
        a_excl = menu.addAction("排除自动匹配")
        a_excl.setToolTip(T("logo 等固定素材：不参与自动匹配"))
        a_excl.setCheckable(True)
        if sel:
            a_excl.setChecked(all(bool(getattr(m, "excluded", False)) for m in sel))
        a_excl.setEnabled(bool(sel))
        a_excl.triggered.connect(lambda _c=False, ms=list(sel): self.main.toggle_exclude(ms))
        menu.addSeparator()
        # ★ 2026-10-30 用户：「素材右键菜单加一个打开该素材本地位置的选项」
        #   用资源管理器的 `/select,` → **打开所在文件夹并选中该文件**（比只开目录好用，
        #   素材库动辄几百个文件，开个目录还得自己找）。多选时只跳第一个。
        a_reveal = menu.addAction("打开文件所在位置")
        a_reveal.setToolTip(T("在资源管理器里打开并选中该文件"))
        a_reveal.setEnabled(bool(sel))

        menu.addSeparator()          # 分组：扫描类（★ 2026-10-30「大砍刀」：手动标签编辑已删）
        scan_one = menu.addAction("重扫此素材的动态/铺满度")
        scan_one.setEnabled(bool(sel))
        rescan_all = menu.addAction("全量重扫素材")
        rescan_all.setToolTip(T("含已扫过的一起重扫（动态三档 / 频闪 / 铺满度 + 角色）"))

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
        elif act == a_reveal:
            self.main.reveal_in_explorer(sel[0] if sel else "")
        elif act == scan_one:
            self.main.scan_tags(paths=[m.path for m in sel])
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


# "这个格子正在播"的标记 role（给 _PlayBorderDelegate 画金边用）
PLAY_ROLE = Qt.UserRole + 7


class _PlayBorderDelegate(QStyledItemDelegate):
    """在"正在播放"的素材格上，**压在缩略图之上**再画一道金边。

    为什么光靠 `item.setBackground` 不够（用户 2026-09-30 说"没修好"）：
    缩略图几乎铺满整格，金黄底只在四周露出 ~4px，一屏深色素材里根本看不见。
    金边画在最后 ⇒ 无论素材多亮多花，都能一眼认出哪一格在播。
    """

    def paint(self, painter, opt, idx):
        super().paint(painter, opt, idx)
        if not idx.data(PLAY_ROLE):
            return
        painter.save()
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            r = QRectF(opt.rect).adjusted(1.0, 1.0, -1.5, -1.5)
            pen = QPen(QColor(255, 205, 0, 255))
            pen.setWidth(3)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(r, 5, 5)
        finally:
            painter.restore()


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
        # ★ 2026-11-06（用户：「图层做一个可以增加高度，就是修改每一行图层高度的选项」）：
        #   行高从模块常量改成**实例值**（`set_cell_height`），由 `LayerStackPanel` 统一调
        #   （默认值仍是 `CELL_H`，`cfg["ui"]["layer_row_h"]` 记住用户调过的高度）。
        self.cell_w = CELL_W
        self.cell_h = CELL_H
        #: 码表图层模式：格子只用来"看"，一律不许拖、不许收拖、不许右键
        self.cue_mode = False
        self.setViewMode(QListView.IconMode)
        self.setFlow(QListView.LeftToRight)
        self.setWrapping(False)
        self.setResizeMode(QListView.Adjust)
        self.setMovement(QListView.Static)
        self.setIconSize(QSize(self.cell_w - 8, self.cell_h - 10))
        self.setGridSize(QSize(self.cell_w, self.cell_h))
        # ★ 2026-11-06（用户：「图层里面这个缩略图把它改成居中的，现在上面距离小下面距离大，
        #   看着不像」）：
        #   原来 `CELL_H + 14` = 72，而格子本身只有 58 高 ⇒ item 从**顶部**排起，
        #   底部凭空多出 14px 空白，视觉上就是"上贴边、下悬空"。
        #   改成 `+ 4`（上下各 2px 呼吸位）⇒ 缩略图在行内视觉居中。
        #   ⚠ 那 14px 原本可能是给横向滚动条留的；真出现滚动条时若压住内容，把这里调回 +8。
        self.setFixedHeight(self.cell_h + 4)
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
        # ★ 播放高亮的金边靠它画（见 _PlayBorderDelegate）
        self.setItemDelegate(_PlayBorderDelegate(self))
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

    def set_cell_height(self, h):
        """改行高（同时改格子/图标尺寸）。⚠ 只改控件尺寸；格子里的 item 要重填
        （`LayerStackPanel.apply_row_height` 会连带 `_fill_row`），否则 sizeHint 还是旧的。

        ★★★ 2026-11-06 下午（用户：「能调行高，怎么不会自动调整这个素材的间距呢？
          还是那么大呀」）：
          原来**格子宽度是死值 `CELL_W=104`**，只有高度跟着行高变 ⇒ 行高调到 38 时
          缩略图只剩 28 高、宽 56，而每格仍占 104 宽 ⇒ 右边空出 48px，看起来"间距没变"。
          现在宽度按 **2:1 + 8px 边距** 从行高推出来（与默认 58→104 完全一致）：
              h=58  → (58-10)*2+8 = 104  （原值，默认观感零变化）
              h=38  → 64
              h=140 → 268
          素材格的间距 = 格子宽 - 图宽 = 8px + 行内 spacing，行高越小间距越小。
        """
        h = max(HEADER_MIN_H, min(140, int(h)))   # ⚠ 下限 = 图层头最小高度（见 HEADER_MIN_H）
        if h == self.cell_h:
            return
        self.cell_h = h
        self.cell_w = max(56, int(round((h - 10) * 2.0)) + 8)   # 见上面的推导
        self.setIconSize(QSize(self.cell_w - 8, max(16, h - 10)))
        self.setGridSize(QSize(self.cell_w, h))
        self.setFixedHeight(h + 4)
        # ★ 顺手把**已有 item** 的 sizeHint 宽度也改掉（很便宜，不动图标）：
        #   拖动行高时 `preview_row_height` 只走这条路，不重建 item ⇒ 若不同步这里，
        #   拖动过程中格子变窄了、item 还是旧宽度，看着"间距没缩"，松手 110ms 后才跳一下。
        try:
            for k in range(self.count()):
                it = self.item(k)
                if it is not None:
                    it.setSizeHint(QSize(self.cell_w, h))
        except Exception:                                          # noqa: BLE001
            pass

    def set_cue_mode(self, on):
        """码表图层：格子**只读**（不拖出、不收拖、不右键）。

        ★★★ 2026-11-06（用户：「码表图层的素材是没有右键菜单的，它这个是按照码表编排的，
          所以说我不能让它去有额外操作」）：码表的片段进出/转场/混合全在 `.ercue` 里，
          在这一行上拖来拖去只会让用户误以为改了编排（实际不会写回码表）。
        """
        on = bool(on)
        if on == self.cue_mode:
            return
        self.cue_mode = on
        if on:
            self.setContextMenuPolicy(Qt.NoContextMenu)
            self.setDragEnabled(False)
            self.setAcceptDrops(False)
            self.setDragDropMode(QAbstractItemView.NoDragDrop)
            self.setSelectionMode(QAbstractItemView.NoSelection)
        else:
            self.setContextMenuPolicy(Qt.CustomContextMenu)
            self.setDragEnabled(True)
            self.setAcceptDrops(True)
            self.setDragDropMode(QAbstractItemView.DragDrop)
            self.setSelectionMode(QAbstractItemView.SingleSelection)

    def _on_menu(self, pos):
        if self.cue_mode:
            return
        it = self.itemAt(pos)
        m = it.data(Qt.UserRole) if it else None
        self.menu_requested.emit(self.layer_idx, m, self.mapToGlobal(pos))

    # ---- viewport 拖放回调 ----
    def _vp_enter(self, e):
        if self.cue_mode:
            e.ignore()
            return
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

    # ---- 拖放 ----
    def eventFilter(self, obj, ev):
        """拦截 viewport 上的拖放事件（Qt 把拖放投递给 viewport，不装过滤器收不到）"""
        from PySide6.QtCore import QEvent
        if obj is self.viewport() and not self.cue_mode:
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
        if (not self.cue_mode and self._press_item is not None
                and (e.buttons() & Qt.LeftButton)
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
        if self.cue_mode:          # 码表图层：不许拖出（编排在 .ercue 里，这里改了也不会写回）
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


class _RowHeightPopup(QWidget):
    """行高滑轨（点工具栏「行高」按钮弹出）。

    ★★★ 2026-11-06（用户：「做一个选项，点了之后会出现一个滑轨，可以拉动调整行高」）：
      布局里塞一个常驻数值框太占宽度（图层栈只有 400~500px），改成**点开才出现**的滑轨：
        · 拖动即时生效（`save=False`，每一格都重排，能直接看到效果）
        · 关闭时落盘一次（`hideEvent` → `save_row_height`）
      `Qt.Popup` ⇒ 点外面自动关，不抢主窗口焦点。
    """

    def __init__(self, panel):
        super().__init__(panel, Qt.Popup)
        self.panel = panel
        self.setObjectName("rowHPop")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)
        self.lbl = QLabel("行高")
        self.sld = QSlider(Qt.Horizontal)
        self.sld.setRange(HEADER_MIN_H, 140)     # 下限 = 图层头最小高度（否则行高压不下去）
        self.sld.setSingleStep(2)
        self.sld.setPageStep(6)
        self.sld.setFixedWidth(170)
        self.sld.setToolTip("拖动调整所有图层行的高度（%d~140）" % HEADER_MIN_H)
        self.val = QLabel(str(int(panel.cell_h)))
        self.val.setFixedWidth(30)
        self.val.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.lbl)
        lay.addWidget(self.sld)
        lay.addWidget(self.val)
        self.sld.valueChanged.connect(self._on_value)
        self.setStyleSheet(
            "#rowHPop{background:%s;border:1px solid %s;border-radius:4px;}"
            "#rowHPop QLabel{color:%s;}" % (theme.V("panel"), theme.V("border"),
                                            theme.V("text")))

    def set_value(self, v):
        v = max(HEADER_MIN_H, min(140, int(v)))
        self.sld.blockSignals(True)      # 回填时别再触发一遍重排（铁律 11）
        self.sld.setValue(v)
        self.sld.blockSignals(False)
        self.val.setText(str(v))

    def _on_value(self, v):
        """拖动中：只 resize（`preview_row_height` 会把整行重建合并到停手后一次）。"""
        self.val.setText(str(int(v)))
        self.panel.preview_row_height(int(v))

    def hideEvent(self, e):                                          # noqa: N802
        try:
            self.panel.end_row_height_drag()    # 关掉就把合并的那次重建补上 + 落盘
        except Exception:                                        # noqa: BLE001
            pass
        super().hideEvent(e)


class _ColDivider(QWidget):
    """图层头与素材格之间的**可拖动空隙**（用户 2026-11-06：「中间那条竖，鼠标放下可以调宽窄」）。

    为什么不是 `QSplitter`：每一行是各自独立的 QWidget（头 + 格子），
    用 N 个 splitter 就得让它们互相同步（拖动时 N 个布局各自重算 = 卡 + 抖）。
    这里只维护**一个全局 `panel.header_w`**，拖动时把它套到所有图层头的 `setFixedWidth`。

    ★★ 2026-11-09（用户第 6 条：「把中间那条竖线去掉，改成按在空隙上就能拖」）：
      **不再画任何东西**（原来画一条 1px 竖线 + hover 变亮）——现在它就是"两列之间的空白"。
      为了让"按在空隙上"真的按得到，这里把**热区加宽到 18px**（原来只有 6px），
      同时把行的 `spacing` 改成 0：`图层头 |←— 18px 空白 —→| 素材格`，
      与改动前的 `6 间距 + 6 热区 + 6 间距` **总宽完全一样** ⇒ 视觉/几何零变化，
      但可拖区域从 6px 变成 18px（鼠标一进这块空白就是左右箭头光标）。
    · 松手才落盘（`ui.layer_header_w`），拖动过程不写盘
    """

    W = 18

    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.setFixedWidth(self.W)
        self.setCursor(Qt.SplitHCursor)
        self.setToolTip("拖动调整左列宽度（图层头 ↔ 素材格）")
        self.setMouseTracking(True)
        self._grab = None

    def paintEvent(self, e):                                     # noqa: N802
        # ★ 用户第 6 条：这里**什么都不画**（以前画的竖线已删除）。
        #   留着空实现是为了不让 Qt 走默认绘制。
        pass

    def mousePressEvent(self, e):                                # noqa: N802
        if e.button() == Qt.LeftButton:
            try:
                gx = e.globalPosition().x()
            except AttributeError:                               # Qt5 兜底
                gx = e.globalPos().x()
            self._grab = (float(gx), int(self.panel.header_w))
            e.accept()

    def mouseMoveEvent(self, e):                                 # noqa: N802
        if self._grab is not None:
            try:
                gx = e.globalPosition().x()
            except AttributeError:
                gx = e.globalPos().x()
            self.panel.set_header_width(self._grab[1] + (float(gx) - self._grab[0]))
            e.accept()

    def mouseReleaseEvent(self, e):                              # noqa: N802
        if self._grab is not None:
            self._grab = None
            self.panel.save_header_width()          # 松手才落盘
            self.update()
            e.accept()


class LayerStackPanel(QWidget):
    """图层栈：左图层头 + 右素材格，共用滚动"""

    layer_pick = Signal(int)
    header_changed = Signal()                 # 任一图层属性被改（由外部保存配置）

    #: 码表下拉宽度。普通图层：右侧还有 不透明度/α/锁/混合 ⇒ 只剩 92px 可用；
    #: 码表图层：那 4 个控件隐藏（不占布局）⇒ 可以宽到 200px，长表名能看清。
    CUE_COMBO_W_COMPACT = 92
    CUE_COMBO_W = 200

    #: ★ 2026-11-06（用户：「行高最小想再小一点，38 那么小」）：
    #:   图层头**两个尺寸档**的参数表（见 `_apply_row_metrics`）。
    #:   · 标准档 = 改动前的像素尺寸（行高 ≥ HEADER_COMPACT_H）⇒ 默认观感逐像素不变
    #:   · 紧凑档 = 行高 < HEADER_COMPACT_H 时用，保证 2×2 田字格不被裁、整行高真的跟着行高走
    #:   ⚠ 这里只放**数值**，QSS 字符串由各 `_xxx_style(compact)` 现场拼（min/max-height 必须
    #:     和这些数值一致，否则全局主题 QSS 的 26/24 会盖掉 setFixedSize）。
    _HDR_STD = {
        "btn_w_vis": 26, "btn_w": 24, "btn_h": 20,   # 2×2 田字格按钮（👁 略宽）
        "name_h": 22, "cmb_h": 22, "sld_h": 16,      # 名称 / 下拉 / 不透明度滑块
        "role_w": 46, "role_h": 22,                  # 自动图层的角色按钮
        "m_lr": 4, "m_tb": 2,                        # 图层头 layout 边距
        "hov_sp": 6,                                 # 田字格 ↔ 右侧两行的间距
        "right_sp": 2, "grid_sp": 2, "row_sp": 4,    # 右侧两行 / 田字格内部 / 行内控件间距
        "tog_w": 56,                                 # 田字格容器宽
    }
    _HDR_CMP = {
        "btn_w_vis": 22, "btn_w": 20, "btn_h": 16,
        "name_h": 16, "cmb_h": 16, "sld_h": 12,
        "role_w": 40, "role_h": 16,
        "m_lr": 3, "m_tb": 1,
        "hov_sp": 4,
        "right_sp": 1, "grid_sp": 1, "row_sp": 2,
        "tog_w": 46,
    }

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
        # ★★★ 2026-11-06（用户：「图层做一个可以增加高度，就是修改每一行图层高度的选项」）：
        #   全局行高（所有图层行一起变，默认 `CELL_H`=58，38~140 可调）。
        #   为什么做全局而不是每行一个：图层头的宽度只有 `HEADER_W`，再塞一个 stepper
        #   会把名称挤没；而用户要的是"看着太小/太大"这种整体观感，逐行调反而麻烦。
        #   落盘在 `cfg["ui"]["layer_row_h"]`，重启保持。
        self.cell_h = CELL_H
        try:
            self.cell_h = int(self.main.cfg["ui"].get("layer_row_h", CELL_H) or CELL_H)
        except Exception:                                          # noqa: BLE001
            self.cell_h = CELL_H
        self.cell_h = max(HEADER_MIN_H, min(140, self.cell_h))
        # ★ 2026-11-06（用户：「左边图层和右边素材中间那条竖，鼠标放下可以调左右宽度」）：
        #   左列宽度 = 全局值（所有图层行共用），落在 `cfg["ui"]["layer_header_w"]`。
        self.header_w = HEADER_W
        try:
            self.header_w = int(self.main.cfg["ui"].get("layer_header_w", HEADER_W) or HEADER_W)
        except Exception:                                          # noqa: BLE001
            self.header_w = HEADER_W
        self.header_w = max(HEADER_W_MIN, min(HEADER_W_MAX, self.header_w))
        # 拖动行高时的**合并重建**定时器（见 `preview_row_height`）
        self._refill_timer = QTimer(self)
        self._refill_timer.setSingleShot(True)
        self._refill_timer.setInterval(110)
        self._refill_timer.timeout.connect(self._refill_rows)
        # ★ 补图失败过的素材（normcase 路径）：一次会话里不再重试，否则
        #   「done → 重建 → 又缺图 → 再起线程」会无限循环（拖行高时会明显卡）。
        self._cue_thumb_failed = set()
        self._cue_thumb_worker = None
        self._cue_thumb_pending = None
        # ★ 点开才出现的滑轨（见 `_RowHeightPopup`）：常驻控件太占宽度
        self.btn_h = QToolButton()
        self.btn_h.setObjectName("btnRowH")
        self.btn_h.setText("行高 %d ▾" % self.cell_h)
        self.btn_h.setToolTip("调整图层行高（所有图层一起变，%d~140）；点一下弹滑轨"
                              % HEADER_MIN_H)
        self.btn_h.setAutoRaise(False)          # ★ 要像"选项"，不能像一行文字
        self.btn_h.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.btn_h.setFixedHeight(22)
        self.btn_h.setStyleSheet(self._btn_row_h_style())
        self.btn_h.clicked.connect(self._open_row_h_popup)
        bar.addWidget(self.btn_h)
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
    # ⚠⚠ 所有样式函数都带 `compact` 参数（默认 False = 标准档）：
    #   全局主题 QSS 里的 `QPushButton{min-height:26px}` / `QComboBox{min-height:24px}`
    #   **会盖过 setFixedSize/setFixedHeight** ⇒ 想让控件变矮，必须在**控件自己的样式表**里
    #   也把 min-height/max-height 写死。标准档的写法与改动前**逐字节一致**（默认观感不变）。
    @staticmethod
    def _hdr_font(compact, std_px):
        """字号片段。`std_px is None` ⇒ 标准档**不写** font-size（保持旧字符串不变）。"""
        if compact:
            return "font-size:%dpx;" % (9 if (std_px or 12) <= 11 else 10)
        return ("font-size:%dpx;" % std_px) if std_px else ""

    @classmethod
    def _hdr_metrics(cls, compact):
        """图层头尺寸表：行高 ≥ `HEADER_COMPACT_H` 用标准档，否则用紧凑档。"""
        return cls._HDR_CMP if compact else cls._HDR_STD

    @staticmethod
    def _btn_vis_style(compact=False):
        h = 16 if compact else 20
        return ("#btnVis{padding:0;border:1px solid " + theme.V("border") + ";border-radius:3px;"
                + LayerStackPanel._hdr_font(compact, 12) +
                "min-height:%dpx;max-height:%dpx;" % (h, h) +
                "background:" + theme.V("panel2") + ";color:" + theme.V("text") + ";}"
                "#btnVis:checked{background:#12405c;color:#7fdcff;border-color:#2f7fa8;}")

    @staticmethod
    def _btn_solo_style(compact=False):
        h = 16 if compact else 20
        return ("#btnSolo{padding:0;border:1px solid " + theme.V("border") + ";border-radius:3px;font-weight:bold;"
                + LayerStackPanel._hdr_font(compact, None) +
                "min-height:%dpx;max-height:%dpx;" % (h, h) +
                "background:" + theme.V("panel2") + ";color:" + theme.V("text") + ";}"
                "#btnSolo:checked{background:#5a4a12;color:#ffd54f;border-color:#a8862f;}")

    @staticmethod
    def _name_style(compact=False):
        h = 16 if compact else 22
        return ("QPushButton{border:none;text-align:left;color:" + theme.V("text") +
                ";font-weight:bold;background:" + theme.V("panel") +
                ";padding:0 6px;" + LayerStackPanel._hdr_font(compact, None) +
                "min-height:%dpx;max-height:%dpx;}" % (h, h))

    @staticmethod
    def _btn_alpha_style(compact=False):
        h = 16 if compact else 20
        return ("#btnAlpha{padding:0;border:1px solid " + theme.V("border") + ";border-radius:3px;font-weight:bold;"
                + LayerStackPanel._hdr_font(compact, None) +
                "min-height:%dpx;max-height:%dpx;" % (h, h) +
                "background:" + theme.V("panel2") + ";color:" + theme.V("text") + ";}"
                "#btnAlpha:checked{background:#123f2c;color:#7ef0b0;border-color:#2f8a63;}")

    @staticmethod
    def _btn_lock_style(compact=False):
        h = 16 if compact else 20
        pad = "0 0 2px 0" if compact else "0 0 3px 0"
        return ("#btnLock{padding:" + pad + ";border:1px solid " + theme.V("border") +
                ";border-radius:3px;" + LayerStackPanel._hdr_font(compact, 11) +
                "min-height:%dpx;max-height:%dpx;" % (h, h) +
                "background:" + theme.V("panel2") + ";color:" + theme.V("text") + ";}"
                "#btnLock:checked{background:#4a2a12;color:#ffb74d;border-color:#a86a2f;}")

    @staticmethod
    def _btn_row_h_style():
        """「行高 N ▾」：做成**有边框的选项**（用户 2026-11-06：光一行字像纯文本）。"""
        return ("#btnRowH{padding:0 8px 0 6px;border:1px solid " + theme.V("border") +
                ";border-radius:3px;background:" + theme.V("btn") + ";color:" +
                theme.V("text") + ";font-size:12px;}"
                "#btnRowH:hover{background:" + theme.V("panel2") + ";}"
                "#btnRowH:pressed{background:" + theme.V("border") + ";}")

    # ---- 行高（全局） ----
    def bump_row_h(self, delta):
        self.apply_row_height(self.cell_h + int(delta))

    def _open_row_h_popup(self):
        """点「行高」按钮：在按钮正下方弹出滑轨。"""
        pop = getattr(self, "_row_h_pop", None)
        if pop is None:
            pop = _RowHeightPopup(self)
            self._row_h_pop = pop
        pop.set_value(self.cell_h)
        b = self.btn_h
        pop.adjustSize()
        pop.move(b.mapToGlobal(QPoint(0, b.height() + 2)))
        pop.show()
        pop.raise_()

    def save_row_height(self):
        """把当前行高写进配置（拖动过程中不写，关弹窗/按钮时才写）。"""
        try:
            self.main.cfg["ui"]["layer_row_h"] = int(self.cell_h)
            self.main.cfg.save()
        except Exception:                                          # noqa: BLE001
            pass

    # ---- 左列宽度（图层头 ↔ 素材格，可拖动） ----
    def set_header_width(self, w):
        """改左列宽度：**所有图层行一起**（它们是同一列）。"""
        try:
            w = int(round(float(w)))
        except (TypeError, ValueError):
            return
        w = max(HEADER_W_MIN, min(HEADER_W_MAX, w))
        if w == self.header_w:
            return
        self.header_w = w
        for hdr, _grid in self.rows:
            try:
                hdr.setFixedWidth(w)
            except Exception:                                      # noqa: BLE001
                pass

    def save_header_width(self):
        try:
            self.main.cfg["ui"]["layer_header_w"] = int(self.header_w)
            self.main.cfg.save()
        except Exception:                                          # noqa: BLE001
            pass

    def preview_row_height(self, h):
        """拖滑轨时用：**只改尺寸**（便宜），整行重建合并成一次（110ms 后）。

        ★★ 2026-11-06（用户：「刚刚在调整行高的时候软件闪退了」）：
          原来滑轨每移动 1px 就把**所有**图层行整批重建（`_fill_row`：清空格子 +
          重建全部 QListWidgetItem + 重新生成图标 + 可能再起补图线程）。
          一次拖动 ≈ 上百次全量重建 ⇒ 栈里疯狂创建/销毁 QWidget/线程，
          Windows 事件日志记到 `shibatic → shiboken6.abi3.dll / 0xc0000005`（访问冲突）。
          现在拖动期间只调 `set_cell_height`（纯 resize，零分配），
          停手 110ms 后才真正重建一次 —— 视觉上仍然是"拖到哪看到哪"。
        """
        h = max(HEADER_MIN_H, min(140, int(h)))
        if h == self.cell_h:
            return
        self.cell_h = h
        btn = getattr(self, "btn_h", None)
        if btn is not None:
            btn.setText("行高 %d ▾" % h)
        for _hdr, grid in self.rows:
            try:
                grid.set_cell_height(h)
            except Exception:                                      # noqa: BLE001
                continue
        # ★ 行高变了 ⇒ 图层头**每一行**都要重选尺寸档（行高 < 56 用紧凑档，
        #   否则标准档的 55 高会把整行撑住、2×2 被裁）。拖动中也要切，不然小档位会被裁。
        for hdr, _grid in self.rows:
            self._apply_row_metrics(hdr, h)
        pop = getattr(self, "_row_h_pop", None)
        if pop is not None:
            pop.set_value(h)
        self._refill_timer.start()      # 合并：一次拖动只重建一次

    def _refill_rows(self):
        """按当前 `cell_h` 重填每一行的素材格（`_fill_row` 会重建 item/图标）。"""
        for i, _row in enumerate(self.rows):
            if i < len(self.main.engine.layers):
                self._fill_row(i, self.main.engine.layers[i])
        self.refresh_playing()

    def end_row_height_drag(self):
        """松手 / 关滑轨：立刻把合并的那次重建补上，并落盘。"""
        try:
            self._refill_timer.stop()
        except Exception:                                          # noqa: BLE001
            pass
        self._refill_rows()
        self.save_row_height()

    def apply_row_height(self, h, save=True):
        """改所有图层行的行高（重排格子；素材格尺寸/图标都跟着变）。

        ⚠ 必须**重填**每一行：`QListWidgetItem` 的 `sizeHint` 和 icon 尺寸在创建时就定了，
          只改 `setGridSize` 会让新高度和旧 item 打架（图标还是旧的、格子拉高但图不动）。
        """
        h = max(HEADER_MIN_H, min(140, int(h)))
        self.cell_h = h
        btn = getattr(self, "btn_h", None)
        if btn is not None:
            btn.setText("行高 %d ▾" % h)
        pop = getattr(self, "_row_h_pop", None)
        if pop is not None:
            pop.set_value(h)          # 内部已 blockSignals
        try:
            self._refill_timer.stop()  # 直接调用时不需要再排队
        except Exception:                                          # noqa: BLE001
            pass
        for _i, (_hdr, grid) in enumerate(self.rows):
            try:
                grid.set_cell_height(h)
            except Exception:                                      # noqa: BLE001
                continue
        # ★ 行高变了 ⇒ 图层头逐行重选尺寸档（见 `_apply_row_metrics`）
        for hdr, _grid in self.rows:
            self._apply_row_metrics(hdr, h)
        self._refill_rows()
        if save:
            try:
                self.main.cfg["ui"]["layer_row_h"] = h
                self.main.cfg.save()
            except Exception:                                      # noqa: BLE001
                pass

    # ---- 码表图层的素材格 ----
    def _cue_item(self, path):
        """码表素材路径 → 复用的 `MediaItem`（避免每次重建都新建对象丢图标缓存）。"""
        cache = getattr(self, "_cue_items", None)
        if cache is None:
            cache = self._cue_items = {}
        k = os.path.normcase(str(path))
        m = cache.get(k)
        if m is None:
            try:
                m = MediaItem(str(path))
            except Exception:                                      # noqa: BLE001
                return None
            cache[k] = m
        return m

    def _ensure_cue_thumbs(self, items):
        """**老码表包**（没有 `thumbs/`）里缺缩略图的素材 ⇒ 后台补生成。

        ★ 2026-11-06：正常情况下这里**一个都不会有** —— `.ercue` 包自带
          `thumbs/<clip_id>.png`，行里直接显示包内小图（见 `_fill_row` 的码表分支）。
          只有 2026-11 之前导出的老包、或导出时缩略图生成失败的素材才走这条路。
        ⚠ 主线程绝不解码（见 `ensure_thumbnail` 的注释），一律交给后台线程，
          生成完再刷新一次这一行。

        ★★ 2026-11-06（拖行高闪退的第二道防线）：
          ① **一次只允许一个补图线程**；正在跑就只记 `_cue_thumb_pending`，
             绝不再 new 一个（QThread 被替换/回收时还在跑 = 必崩）；
          ② 补图**失败过**的素材记进 `_cue_thumb_failed`，本会话不再重试 ——
             否则 `done → 重建 → 仍缺图 → 又起线程` 会无限循环（坏文件/不支持的编码
             就永远解不出缩略图）。
        """
        failed = getattr(self, "_cue_thumb_failed", None)
        if failed is None:
            failed = self._cue_thumb_failed = set()
        need = []
        for m in items:
            try:
                if m.resolve_thumb():
                    continue
                if os.path.normcase(str(getattr(m, "path", ""))) in failed:
                    continue
                need.append(m)
            except Exception:                                      # noqa: BLE001
                continue
        if not need:
            return
        w = getattr(self, "_cue_thumb_worker", None)
        if w is not None:
            try:
                if w.isRunning():
                    self._cue_thumb_pending = need     # 这一批等当前跑完再补
                    return
            except RuntimeError:                       # C++ 侧已销毁（极端情况）
                w = None
                self._cue_thumb_worker = None
        try:
            w = ThumbBackfillWorker(need)
            w.done.connect(self._on_cue_thumbs_done)
            self._cue_thumb_worker = w            # 必须持引用，否则 QThread 被 GC 掉直接崩
            self._cue_thumb_items = list(need)    # 结束时要按这批判"还缺不缺"
            self._cue_thumb_pending = None
            w.start()
        except Exception:                                          # noqa: BLE001
            self._cue_thumb_worker = None

    def _on_cue_thumbs_done(self, *_a):
        """补图线程结束：把**仍然缺图**的记成失败（不再重试），再刷新一次。"""
        failed = getattr(self, "_cue_thumb_failed", None)
        if failed is None:
            failed = self._cue_thumb_failed = set()
        for m in list(getattr(self, "_cue_thumb_items", []) or []):
            try:
                if not m.resolve_thumb():
                    failed.add(os.path.normcase(str(getattr(m, "path", ""))))
            except Exception:                                      # noqa: BLE001
                pass
        pend = getattr(self, "_cue_thumb_pending", None)
        if pend:
            self._cue_thumb_pending = None
            self._ensure_cue_thumbs(pend)
        self._refill_cue_rows()

    def _refill_cue_rows(self):
        for i, lay in enumerate(self.main.engine.layers):
            if getattr(lay, "is_cuesheet", False) and i < len(self.rows):
                self._fill_row(i, lay)
        self.refresh_playing()

    def apply_theme(self):
        """主题切换：重设所有图层头样式（ClipRow 的样式由各自注册的回调刷新）"""
        self.title.setStyleSheet("color:" + theme.V("muted") + ";font-weight:bold;")
        btn_h = getattr(self, "btn_h", None)
        if btn_h is not None:
            btn_h.setStyleSheet(self._btn_row_h_style())
        for hdr, _row in self.rows:
            lay = getattr(hdr, "_lay", None)
            if lay is None:
                continue
            # ★ 主题切换不能把紧凑档的尺寸冲掉 ⇒ 按**当前档位**重设样式
            compact = bool(getattr(hdr, "_compact_hdr", False))
            hdr.setStyleSheet(self._header_style(lay, compact))
            if getattr(hdr, "_btn_vis", None) is not None:
                hdr._btn_vis.setStyleSheet(self._btn_vis_style(compact))
                hdr._btn_solo.setStyleSheet(self._btn_solo_style(compact))
                hdr._name.setStyleSheet(self._name_style(compact))
                hdr._btn_alpha.setStyleSheet(self._btn_alpha_style(compact))
                hdr._btn_lock.setStyleSheet(self._btn_lock_style(compact))
                role = getattr(hdr, "_btn_role", None)
                if role is not None:
                    role.setStyleSheet(self._role_style(lay.role, compact))

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
            # ★ 2026-11-09（用户第 6 条）：原来是 6 ⇒ 两列间总宽 = 6+6(竖条)+6 = 18。
            #   现在竖条改宽 18（热区）且不再画线，间距必须改 0，总宽仍是 18 ⇒ 几何零变化。
            rl.setSpacing(0)
            hdr = self._build_header(i, lay)
            hdr._layer_idx = i
            hdr.setAcceptDrops(True)
            hdr.installEventFilter(self)
            row._layer_idx = i
            row.setAcceptDrops(True)
            row.installEventFilter(self)
            rl.addWidget(hdr)
            # ★ 2026-11-06：两个面板之间那条竖 = 可拖动的列宽分隔条（所有行一起变）
            rl.addWidget(_ColDivider(self))
            grid = ClipRow(i)
            grid.set_cell_height(self.cell_h)          # ★ 用户设定的行高（全局）
            # ★ 码表图层：只读格子（见 `ClipRow.set_cue_mode`）
            grid.set_cue_mode(bool(getattr(lay, "is_cuesheet", False)))
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
    def _role_style(role, compact=False):
        """角色按钮（前景/背景）。

        ⚠ 紧凑档**必须**写 min/max-height：全局主题 QSS 的 `QPushButton{min-height:26px}`
          会盖掉 `setFixedSize` ⇒ 整个图层头被这个按钮撑高（行高就压不下去）。
        ⚠ 标准档刻意**不加**这两条（与改动前逐字节一致，自动图层的默认观感不变）。
        """
        h = 16
        qss_h = "min-height:%dpx;max-height:%dpx;" % (h, h) if compact else ""
        font = LayerStackPanel._hdr_font(compact, 11)
        if not compact:
            font = "font-size:11px;"
        if role == "fg":
            return ("#btnRole{padding:0;border:1px solid #a8682f;border-radius:3px;" + font + qss_h +
                    "background:#4a2a12;color:#ffb74d;}"
                    "#btnRole:hover{background:#5a3418;}")
        return ("#btnRole{padding:0;border:1px solid #2f7fa8;border-radius:3px;" + font + qss_h +
                "background:#123f5c;color:#7fdcff;}"
                "#btnRole:hover{background:#164a70;}")

    @staticmethod
    def _header_style(lay, compact=False):
        """图层头样式：普通层中性边框；自动层角色色（前景橙 / 背景蓝）；
        Kv 主视觉图层专属紫金。

        `compact=True`（行高 < `HEADER_COMPACT_H`）⇒ 下拉也跟着变矮：
        全局主题 QSS 的 `QComboBox{min-height:24px}` 会盖掉 `setFixedHeight`，
        所以**必须**在这里再写一遍 min/max-height，否则整个图层头又被下拉撑回 55+。
        """
        if getattr(lay, "is_kv", False):
            border, bg = "#a06cf0", "#221430"
        elif getattr(lay, "auto_mode", False) and getattr(lay, "role", "bg") == "fg":
            border, bg = "#a8682f", "#241c14"
        elif getattr(lay, "auto_mode", False):
            border, bg = "#2f7fa8", "#13202a"
        else:
            border, bg = theme.V("border"), theme.V("panel2")
        # 下拉的高度约束：标准档沿用原值（20~22，逐字节不变）；紧凑档 14~16
        cmb_h = LayerStackPanel._HDR_CMP["cmb_h"] if compact else 22
        cmb_qss = "min-height:%dpx;max-height:%dpx;" % (cmb_h - 2, cmb_h)
        cmb_font = "font-size:10px;" if compact else ""
        return ("#layerHeader{background:" + bg + ";border:1px solid " + border +
                ";border-radius:3px;}"
                "#layerHeader QLabel{color:" + theme.V("text") + ";}"
                "#layerHeader QComboBox{background:" + theme.V("btn") + ";color:" +
                theme.V("text") + ";border:1px solid " + theme.V("border") +
                # ⚠ 全局 QSS 的 `QComboBox{min-height:24px}` 会盖过 setFixedHeight(22)
                #   ⇒ 这里必须一起钉死，否则整个图层头又被下拉撑高（行高就压不下去）
                ";padding:0 4px;" + cmb_qss + cmb_font + "}"
                "#layerHeader QComboBox QAbstractItemView{background:" + theme.V("panel") +
                ";color:" + theme.V("text") +
                ";selection-background-color:#4a6cf7;selection-color:#fff;}"
                # 码表下拉：青绿（与 Kv 的金、Solo 的黄区分开），一眼看出"这层被码表接管"
                # ⚠ 必须写成 `#layerHeader QComboBox#cmbCue`：CSS 特异性里
                #   `#layerHeader QComboBox`（id+类型）**高于** `#cmbCue`（只有 id），
                #   只写 #cmbCue 会被上面那条基础规则盖掉。
                "#layerHeader QComboBox#cmbCue{border:1px solid #1f8a72;background:#0d3b33;"
                "color:#6ff0cf;font-size:" + ("10px" if compact else "11px") + ";padding:0 2px;}"
                "#layerHeader QComboBox#cmbCue:hover{background:#12564a;}"
                # ⚠ 不要自定义 `::drop-down`：一旦写了该子控件，Qt 就不再画默认箭头
                #   ⇒ 用户会以为那是个标签而不是下拉（实测截图里箭头会消失）。
                "#layerHeader QComboBox#cmbCue QAbstractItemView{background:" +
                theme.V("panel") + ";color:" + theme.V("text") +
                ";selection-background-color:#1f8a72;selection-color:#fff;}")

    def _build_header(self, idx, lay):
        f = QFrame()
        f.setFixedWidth(self.header_w)          # ★ 可拖动分隔条改的全局左列宽
        f.setObjectName("layerHeader")
        f._lay = lay   # apply_theme 重设样式时取回图层属性
        # 注意：必须用 #id 作用域，否则会串到下拉弹出列表导致文字看不清；
        # 也不能用 #layerHeader QPushButton 这类带 ID 的兜底规则，否则会盖掉按钮自身 :checked 状态色
        f.setStyleSheet(self._header_style(lay))
        f.setContextMenuPolicy(Qt.CustomContextMenu)
        f.customContextMenuRequested.connect(
            lambda pos, i=idx: self.main.layer_menu(i, f.mapToGlobal(pos)))
        h = QHBoxLayout(f)
        # ★★ 2026-11-06（用户：「图标下面显示不全了」+「行高缩小了素材间距也该缩」）：
        #   整行的高度 = max(图层头最小高, 格子高)。原来图层头被**里面的控件**撑到 73px
        #   （名称按钮 34、混合模式下拉 30、滑块 24…）⇒ 行高调到 49 时整行仍是 73，
        #   素材上下各多出十几像素黑边，而 2×2 按钮格底部顶到边框（看着像被裁）。
        #   修法：把右侧每个控件的高度**钉死**，让图层头最小高 = HEADER_MIN_H(50)。
        h.setContentsMargins(4, 2, 4, 2)
        h.setSpacing(6)
        # ★★★ 2026-11-06（用户：「图层每一层上面不是有四个开关嘛，把那四个开关 UI 改成田字格，
        #   不要一行一个，改成两行」）：
        #   左侧 2×2：👁 / S（上排）、α / 🔒（下排）；右侧两行：名称（+ 码表下拉 / 角色）、
        #   不透明度 + 混合模式。⚠ 码表图层会把 α / 🔒 / 不透明度 / 混合模式全隐藏 ⇒
        #   田字格只剩上排两个，右侧变成"名称一行 + 码表下拉一行"（用户 2026-11-06 要求）。
        _tog = QWidget()
        _grid = QGridLayout(_tog)
        _grid.setContentsMargins(0, 0, 0, 0)
        _grid.setSpacing(2)
        _tog.setFixedWidth(56)
        # ★ 竖向固定：不然行高拉大时 QGridLayout 会把两排按钮**撑开**（上下分散，很难看）
        _tog.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        h.addWidget(_tog, 0, Qt.AlignVCenter)

        _right = QVBoxLayout()
        _right.setContentsMargins(0, 0, 0, 0)
        _right.setSpacing(2)
        _row1 = QHBoxLayout()
        _row1.setContentsMargins(0, 0, 0, 0)
        _row1.setSpacing(4)
        _row2 = QHBoxLayout()
        _row2.setContentsMargins(0, 0, 0, 0)
        _row2.setSpacing(4)
        _right.addStretch(1)          # 上下留白 ⇒ 两行整体**垂直居中**（不要被拉开）
        _right.addLayout(_row1)
        _right.addLayout(_row2)
        _right.addStretch(1)
        h.addLayout(_right, 1)
        # ★ 2026-11-06：记住布局与容器 ⇒ 行高变档时 `_apply_row_metrics` 要改它们的
        #   contentsMargins / spacing / 田字格宽度（只改控件尺寸不够：间距不缩，头还是高）
        f._hdr_lay, f._tog, f._right = h, _tog, _right
        f._compact_hdr = None          # 未定档 ⇒ 建完 `_apply_row_metrics` 会按当前行高应用

        btn_vis = QPushButton("👁" if lay.visible else "◌")
        btn_vis.setCheckable(True)
        btn_vis.setChecked(lay.visible)
        btn_vis.setFixedSize(26, 20)
        btn_vis.setToolTip("显示/隐藏（点亮=显示中）")
        btn_vis.setObjectName("btnVis")
        btn_vis.setStyleSheet(self._btn_vis_style())
        _grid.addWidget(btn_vis, 0, 0)

        btn_solo = QPushButton("S")
        btn_solo.setCheckable(True)
        btn_solo.setChecked(bool(getattr(lay, "solo", False)))
        btn_solo.setFixedSize(24, 20)
        btn_solo.setToolTip("Solo：只显示本图层")
        btn_solo.setObjectName("btnSolo")
        btn_solo.setStyleSheet(self._btn_solo_style())
        _grid.addWidget(btn_solo, 0, 1)

        name = QPushButton(lay.name)
        name.setFixedWidth(86)
        name.setFixedHeight(22)      # ★ 钉死：默认 QPushButton 会按字体撑到 34px（见 HEADER_MIN_H）
        # 文本是图层名（用户数据），tooltip 含图层名 → 整体标为动态，
        # 由 refresh_header 用 T() 重建，绝不能被 i18n 当静态文案翻译
        name.setProperty("_i18nDynamic", True)
        name.setToolTip(lay.name + T("\n双击重命名"))
        name.setStyleSheet(self._name_style())
        name.clicked.connect(lambda _c=False, i=idx: self.main.on_layer_pick(i))
        name.installEventFilter(self)  # 双击重命名由 MainWindow 处理
        name._layer_idx = idx
        name._is_name = True
        _row1.addWidget(name)

        # ★ 码表选择（用户要求 2026-10）：原来是青绿「码」方徽标，点开弹菜单；
        #   现在换成**一整个码表下拉选项**（未绑显示「（无码表）」/ 已绑显示表名）。
        #   ⚠ **只在 `is_cuesheet` 时显示**（隐藏时 QLayout 不占位 ⇒ 普通图层头部逐像素不变）
        #   ⚠ 宽度见 `_apply_cue_header_mode`：普通图层占位时 92，码表图层把右侧控件
        #     隐藏后可以宽到 200（长表名能看清）
        cmb_cue = QComboBox()
        cmb_cue.setObjectName("cmbCue")
        cmb_cue.setFixedSize(self.CUE_COMBO_W_COMPACT, 22)
        cmb_cue.setProperty("_i18nDynamic", True)   # 文本是表名（用户数据）
        cmb_cue.setVisible(bool(getattr(lay, "is_cuesheet", False)))
        self._fill_cue_combo(cmb_cue, lay)
        cmb_cue.activated.connect(lambda _k, i=idx: self._on_cue_combo(i))
        _row1.addWidget(cmb_cue)

        op = QSlider(Qt.Horizontal)
        op.setRange(0, 100)
        op.setValue(int(lay.opacity * 100))
        op.setFixedWidth(64)
        op.setFixedHeight(16)        # ★ 钉死（默认 24 会把整行撑高）
        op.setProperty("_i18nDynamic", True)   # tooltip 含数值，动态重建
        op.setToolTip(Tf("不透明度 {}%", int(lay.opacity * 100)))
        _row2.addWidget(op, 0, Qt.AlignVCenter)

        btn_alpha = QPushButton("α")
        btn_alpha.setCheckable(True)
        btn_alpha.setChecked(bool(getattr(lay, "use_alpha", True)))
        btn_alpha.setFixedSize(24, 20)
        btn_alpha.setObjectName("btnAlpha")
        btn_alpha.setToolTip("保留素材自带透明通道（点亮=透明区域透出下层；关闭=透明区域填黑）")
        btn_alpha.setStyleSheet(self._btn_alpha_style())
        _grid.addWidget(btn_alpha, 1, 0)
        btn_alpha.toggled.connect(lambda st, i=idx: self._set(i, "use_alpha", bool(st)))

        # ★★ 2026-11-06（用户：「这个锁的图标有点意义不明，别人一看以为是锁定当前素材」）：
        #   语义是「**画面**别跟着音乐动」（`lock_motion`：不随能量脉冲缩放、不随氛围漂移），
        #   不是"锁定这一层的素材选择" ⇒ 换掉 🔒（与 AE/PR 的"锁定图层"撞义），
        #   改成图钉 📌（钉住 = 固定在原地），tooltip 里再点明一次。
        btn_lock = QPushButton("📌")
        btn_lock.setCheckable(True)
        btn_lock.setChecked(bool(getattr(lay, "lock_motion", False)))
        btn_lock.setFixedSize(24, 20)
        btn_lock.setObjectName("btnLock")
        btn_lock.setToolTip("锁定画面：只固定这一层的画面（不随脉冲缩放/漂移），与选素材无关")
        btn_lock.setStyleSheet(self._btn_lock_style())
        _grid.addWidget(btn_lock, 1, 1)
        btn_lock.toggled.connect(lambda st, i=idx: self._set(i, "lock_motion", bool(st)))

        blend = QComboBox()
        blend.setFixedWidth(74)
        blend.setFixedHeight(22)     # ★ 钉死（默认 30 会把整行撑高）
        blend.addItems([BLEND_LABELS[b] for b in BLEND_MODES])
        try:
            blend.setCurrentIndex(BLEND_MODES.index(getattr(lay, "blend", "normal")))
        except ValueError:
            blend.setCurrentIndex(0)
        blend.setToolTip("混合模式：决定这一层怎么和下面的层叠在一起。正常=直接盖住；相加=提亮；相减=抠掉黑底；正片叠底=压暗，适合光效/花纹")
        _row2.addWidget(blend, 0, Qt.AlignVCenter)

        # 自动图层专属：角色按钮（前景/背景，点击切换，自动匹配按此挑素材）
        btn_role = None
        if getattr(lay, "auto_mode", False):
            btn_role = QPushButton(T("前景") if lay.role == "fg" else T("背景"))
            btn_role.setFixedSize(46, 22)   # 与其他按钮同高对齐
            btn_role.setObjectName("btnRole")
            btn_role.setToolTip("图层角色（自动匹配按此挑素材）：点击在前景/背景间切换")
            btn_role.setStyleSheet(self._role_style(lay.role))
            btn_role.clicked.connect(lambda _c=False, i=idx: self.main.toggle_layer_role(i))
            _row1.addWidget(btn_role)

        # 两行都靠左排（stretch 放在末尾），否则控件会被拉到整行宽
        _row1.addStretch(1)
        _row2.addStretch(1)

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
        f._cmb_cue = cmb_cue
        f._row1, f._row2 = _row1, _row2
        f._cue_mode = None            # 未定 ⇒ `_apply_cue_header_mode` 第一次会重排
        # 码表图层 / 普通图层的头部**排布不同** ⇒ 建好就应用一次
        self._apply_cue_header_mode(f, lay)
        # ★ 按**当前**行高选尺寸档（默认 58 ⇒ 标准档，与改动前逐像素一致）
        self._apply_row_metrics(f, self.cell_h)
        return f

    # ---- 码表图层的头部排布（右侧 4 个控件隐藏）----
    def _apply_cue_header_mode(self, f, lay):
        """码表图层：头部只留 `[👁][S]`，右边是**两行**：图层名 / 码表包名。

        用户 2026-10：「一整个啊 不要右边那些东西啊 那些不是在码表里都做好了吗」
        ⇒ **不透明度 / α / 锁 / 混合模式** 是码表里**每个片段/层自己定义的**
          （Cue 端的 `blend` / `opacity`），演出端再给一遍只会跟码表打架。
        用户 2026-11-06：「码表图层这个名字和码表包也改成两行，左边 logo 还是一行」
        ⇒ 名称留在第 1 行，码表下拉**挪到第 2 行**（原来两个挤在同一行，表名被压得只剩几个字）。

        ⚠ **隐藏而不是销毁**：`setVisible(False)` ⇒
          ① 隐藏控件不占布局（QLayout 跳过）⇒ 右侧控件消失后下拉能吃下更多宽度；
          ② **信号接线一律不动** ⇒ 换回普通图层（卸下码表）时 `setVisible(True)`
             立刻恢复原样，不用重建控件。
        ⚠ 普通图层：这里只做"全部可见 + 下拉 92"，与改动前**逐像素一致**。
        ⚠ `refresh_header` 每次都会调这里 ⇒ **只在模式真的变了才搬控件**（`f._cue_mode`），
          否则每刷新一次就 reparent 一次，布局会闪。
        """
        is_cue = bool(getattr(lay, "is_cuesheet", False))
        for w in (getattr(f, "_op", None), getattr(f, "_btn_alpha", None),
                  getattr(f, "_btn_lock", None), getattr(f, "_blend", None)):
            if w is not None:
                w.setVisible(not is_cue)
        cmb = getattr(f, "_cmb_cue", None)
        if cmb is None:
            return
        if getattr(f, "_cue_mode", None) != is_cue:
            r1, r2 = getattr(f, "_row1", None), getattr(f, "_row2", None)
            if r1 is not None and r2 is not None:
                if is_cue:
                    r1.removeWidget(cmb)         # 名称下面一行放码表包名
                    r2.insertWidget(0, cmb)
                else:
                    r2.removeWidget(cmb)         # 普通图层：跟着名称排在同一行
                    r1.insertWidget(1, cmb)
            f._cue_mode = is_cue
        cmb.setVisible(is_cue)
        # 码表图层右侧空了 ⇒ 下拉铺满剩余宽度；普通图层保持 92（隐藏时也不占位）
        cmb.setFixedWidth(self.CUE_COMBO_W if is_cue else self.CUE_COMBO_W_COMPACT)

    # ---- 图层头尺寸档（行高 ≥ HEADER_COMPACT_H ⇒ 标准档；否则紧凑档）----
    def _apply_row_metrics(self, hdr, h):
        """按行高把**一个**图层头切到标准档 / 紧凑档（**幂等**：同档重复调用立即返回）。

        ★ 2026-11-06（用户：「行高最小想再小一点，38 那么小」）：
          行高下限从 56 降到 38 之后，标准档图层头的最小高（实测 55）**仍会把整行撑住**
          ⇒ ① 整行止步于 60，滑轨拖到 38 完全没反应；② 2×2 田字格底部顶到边框（像被裁）。
          所以行高 < `HEADER_COMPACT_H`(56) 时整个头换一套更小的像素参数
          （按钮 22/20×16、名称高 16、下拉高 16、滑块高 12、头边距 3/1、
            右侧 spacing 1、田字格 spacing 1、字号降到 10px）；
          行高 ≥ 56 恢复标准参数 —— **与改动前逐像素一致**，默认观感（行高 58）不变。

        ⚠⚠ 必须**同时**改控件自己样式表里的 `min-height/max-height`：
          全局主题 QSS 的 `QPushButton{min-height:26px}` / `QComboBox{min-height:24px}`
          **优先级高于 `setFixedSize/setFixedHeight`** ⇒ 只改尺寸的话控件根本不缩，
          图层头照旧 55 高（这就是"行高压不下去"的真根因）。
        ⚠ 幂等靠 `hdr._compact_hdr` 记住当前档位：拖动滑轨时每 1px 都会调到这里，
          同档直接返回 ⇒ 不会反复 unpolish/repolish（不闪、不卡）。
        """
        if hdr is None:
            return
        try:
            h = int(h)
        except (TypeError, ValueError):
            return
        compact = h < HEADER_COMPACT_H
        if getattr(hdr, "_compact_hdr", None) == compact:
            return                    # 同档：什么都不用做（拖动时这是绝对多数情况）
        hdr._compact_hdr = compact
        m = self._hdr_metrics(compact)
        lay = getattr(hdr, "_lay", None)

        vis = getattr(hdr, "_btn_vis", None)
        solo = getattr(hdr, "_btn_solo", None)
        alpha = getattr(hdr, "_btn_alpha", None)
        lock = getattr(hdr, "_btn_lock", None)
        name = getattr(hdr, "_name", None)
        role = getattr(hdr, "_btn_role", None)
        op = getattr(hdr, "_op", None)
        blend = getattr(hdr, "_blend", None)
        cmb = getattr(hdr, "_cmb_cue", None)

        # ① 控件自己的样式表（min-height/max-height 只能写在这里，见 docstring 的 ⚠⚠）
        for obj, fn in ((vis, self._btn_vis_style), (solo, self._btn_solo_style),
                        (alpha, self._btn_alpha_style), (lock, self._btn_lock_style),
                        (name, self._name_style)):
            if obj is None:
                continue
            try:
                obj.setStyleSheet(fn(compact))
            except Exception:                                      # noqa: BLE001
                pass
        if role is not None:
            try:
                role.setStyleSheet(self._role_style(getattr(lay, "role", "bg"), compact))
            except Exception:                                      # noqa: BLE001
                pass
        if lay is not None:
            try:
                hdr.setStyleSheet(self._header_style(lay, compact))   # 下拉的高度约束在这里
            except Exception:                                      # noqa: BLE001
                pass

        # ② 控件尺寸（QSS 已同步 ⇒ setFixedSize 这次才真的生效）
        for obj, w in ((vis, m["btn_w_vis"]), (solo, m["btn_w"]),
                       (alpha, m["btn_w"]), (lock, m["btn_w"])):
            if obj is None:
                continue
            try:
                obj.setFixedSize(w, m["btn_h"])
            except Exception:                                      # noqa: BLE001
                pass
        for obj, hh in ((name, m["name_h"]), (op, m["sld_h"]), (blend, m["cmb_h"]),
                        (cmb, m["cmb_h"])):
            if obj is None:
                continue
            try:
                obj.setFixedHeight(hh)     # 宽度由各自逻辑管（如 `_apply_cue_header_mode`）
            except Exception:                                      # noqa: BLE001
                pass
        if role is not None:
            try:
                role.setFixedSize(m["role_w"], m["role_h"])
            except Exception:                                      # noqa: BLE001
                pass

        # ③ 布局边距/间距（只缩控件、不缩间距的话，头还是被间距撑住）
        hl = getattr(hdr, "_hdr_lay", None)
        if hl is not None:
            try:
                hl.setContentsMargins(m["m_lr"], m["m_tb"], m["m_lr"], m["m_tb"])
                hl.setSpacing(m["hov_sp"])
            except Exception:                                      # noqa: BLE001
                pass
        right = getattr(hdr, "_right", None)
        if right is not None:
            try:
                right.setSpacing(m["right_sp"])
            except Exception:                                      # noqa: BLE001
                pass
        for r in (getattr(hdr, "_row1", None), getattr(hdr, "_row2", None)):
            if r is None:
                continue
            try:
                r.setSpacing(m["row_sp"])
            except Exception:                                      # noqa: BLE001
                pass
        tog = getattr(hdr, "_tog", None)
        if tog is not None:
            try:
                tog.setFixedWidth(m["tog_w"])
                gl = tog.layout()
                if gl is not None:
                    gl.setContentsMargins(0, 0, 0, 0)
                    gl.setSpacing(m["grid_sp"])
            except Exception:                                      # noqa: BLE001
                pass
        try:
            hdr.updateGeometry()          # 让父布局立刻按新尺寸重算（切换档位时才有）
        except Exception:                                          # noqa: BLE001
            pass

    # ---- 码表下拉（图层头） ----
    def _fill_cue_combo(self, cmb, lay):
        """填「（无码表） + 管理页里的所有码表 + 导入新码表…」，并选中当前绑定的那张。

        ⚠ 全程 `blockSignals`：这是**程序**在同步，不是用户在选，
          否则每次 refresh_header 都会触发一次换表/卸下。
        """
        cmb.blockSignals(True)
        try:
            cmb.clear()
            cmb.addItem(T("（无码表）"), "")
            entries = []
            label_fn = None
            try:
                # 延迟 import：cue_page 会 import panels（I18nDialog）⇒ 模块级 import 会成环
                import cue_page
                entries = cue_page.sheet_entries(self.main)
                label_fn = cue_page.sheet_label
            except Exception:                                    # noqa: BLE001
                entries = []
            # ⚠ strip：`cuesheet_id` 只有空白 = 脏数据 ⇒ 当成"没绑"（「（无码表）」），
            #   绝不能让空白看起来像"绑了一张名字空白的表"
            cur = str(getattr(lay, "cuesheet_id", "") or "").strip()
            sel = 0
            found = False
            for e in entries:
                p = str(e.get("path") or "")
                label = (label_fn(e) if label_fn is not None
                         else (os.path.basename(p) or p))
                cmb.addItem(label, p)
                # ★ 2026-10-30：名字统一成**文件名**后，包内标题（可能与文件名不同，
                #   实测 `测试151.ercue` 的 title 是 `测试11`）放进 tooltip，信息不丢。
                try:
                    cmb.setItemData(cmb.count() - 1,
                                    cue_page.sheet_tooltip(e), Qt.ToolTipRole)
                except Exception:                                # noqa: BLE001
                    cmb.setItemData(cmb.count() - 1, p, Qt.ToolTipRole)
                if cur and (cur == os.path.basename(p) or cur == label or cur == p):
                    sel = cmb.count() - 1
                    found = True
            # 绑着一张**不在索引里**的表（比如被手动移出列表 / 索引没建）⇒ 也要如实显示，
            # 不能假装"（无码表）"（否则用户看到的绑定状态是假的）
            if cur and not found:
                # ⚠ 表名兜底链：去扩展名 → 原文件名 → 原字符串 →「（已绑定的码表）」
                #   **绝不允许下拉出现空文本**（空文本会被读成"没绑"，与「（无码表）」混淆）
                # ★ 与索引里的项保持**同一口径**：完整文件名（含扩展名）。
                #   原来这里去掉了扩展名（"测试151"），而列表里是"测试151.ercue"
                #   ⇒ 同一张表在"已绑定但索引缺失"时又显示成第三个名字。
                label = (os.path.basename(cur) or cur or "").strip()
                cmb.addItem(label or T("（已绑定的码表）"), "__bound__")
                sel = cmb.count() - 1
            cmb.addItem(T("导入新码表…"), "__import__")
            cmb.setCurrentIndex(sel if 0 <= sel < cmb.count() else 0)
            # 最后一层保险：当前项文本为空/全空白 ⇒ 明确写成「（无码表）」，
            # 绝不让"没绑"和"绑了一张名字空白的表"看起来一样
            if not cmb.currentText().strip():
                cmb.setItemText(cmb.currentIndex(), T("（无码表）"))
            # tooltip：完整表名（下拉只有 92px，长名字会被截断）
            txt = cmb.itemText(cmb.currentIndex())
            cmb.setToolTip(Tf("码表：{}（选择换表 /「（无码表）」卸下）", txt)
                           if sel else T("码表：（无码表）—— 选一张表让本图层由码表接管"))
            if sel and cur:
                cmb.setToolTip(Tf("码表：{}（包：{}）", txt, cur))
        except Exception:                                        # noqa: BLE001
            pass
        finally:
            cmb.blockSignals(False)

    def _on_cue_combo(self, idx):
        """用户在下拉里选了一项：换表 / 卸下 / 导入新码表。"""
        lays = list(getattr(self.main.engine, "layers", None) or [])
        if not (0 <= idx < len(lays)):
            return
        lay = lays[idx]
        cmb = getattr(self.rows[idx][0], "_cmb_cue", None) if 0 <= idx < len(self.rows) else None
        if cmb is None:
            return
        data = str(cmb.currentData() or "")
        import cue_ui
        if data == "":
            # 卸下：复用与「从这个图层卸下码表」**同一段**逻辑
            cue_ui.unbind_layer(self.main, lay)
            self._fill_cue_combo(cmb, lay)
            self.refresh_header(idx)
            self.sync_layer(idx)          # ★ 卸表 ⇒ 素材行清空（见 `_fill_row` 的分支）
            self.refresh_playing()
            self._refresh_cue_page()
            return
        new_path = ""
        if data == "__import__":
            paths, _ = QFileDialog.getOpenFileNames(
                self, T("导入码表"), "", T("码表包 (*.ercue);;所有文件 (*)"))
            if not paths:
                self._fill_cue_combo(cmb, lay)     # 取消 ⇒ 把下拉还原成当前绑定
                return
            new_path = paths[0]
            data = new_path
        # 换表 / 导入并绑定：**唯一实现**在 cue_ui.load_and_bind
        _lv = []
        _ok, msg = cue_ui.load_and_bind(self.main, data, lay, parent=self, out_level=_lv)
        self._fill_cue_combo(cmb, lay)
        # ★ 刚从「导入新码表…」进来的：**自动选中刚导入的那张**（否则用户还得再点一次）
        if new_path:
            want = os.path.abspath(new_path)
            for k in range(cmb.count()):
                if os.path.abspath(str(cmb.itemData(k) or "")) == want:
                    cmb.blockSignals(True)
                    cmb.setCurrentIndex(k)
                    cmb.blockSignals(False)
                    break
        self.refresh_header(idx)
        self.sync_layer(idx)              # ★ 换表/导入 ⇒ 素材行要按新包重建
        self.refresh_playing()
        try:
            self.main.save_layers()
        except Exception:                                        # noqa: BLE001
            pass
        self._refresh_cue_page()
        if msg:
            # ★ 2026-11-02：成功 + 补充说明用信息图标，只有真问题才用警告
            if _lv and _lv[0] == "info":
                QMessageBox.information(self, T(T("码表")), msg)
            else:
                QMessageBox.warning(self, T(T("码表")), msg)

    def _refresh_cue_page(self):
        """绑/换/卸之后：第 4 页签的「已绑图层」列要跟着变。"""
        page = getattr(self.main, "cue_panel", None)
        fn = getattr(page, "refresh", None)
        if callable(fn):
            try:
                fn()
            except Exception:                                    # noqa: BLE001
                pass

    def cue_menu(self, idx):
        """（**已停用**）原「码」徽标的点击菜单。

        2026-10 用户要求：徽标 → 码表下拉（`_cmb_cue`），换表/卸下都走下拉，
        本函数不再被任何地方调用。**函数体保留**是因为它里面的"卸下"两步
        （`clear_cue_sheet` + 清 `is_cuesheet/cuesheet_id/cs_states/cs_active`）
        已完整搬进 `cue_ui.unbind_layer()` —— 留一份可读的对照，不要删了以后
        再有人把它"重新实现"一遍。
        """
        lays = getattr(self.main.engine, "layers", None) or []
        if not (0 <= idx < len(lays)):
            return
        lay = lays[idx]
        m = QMenu(self)
        # 第一行只做信息展示（灰掉）
        st = {}
        try:
            st = self.main.engine.cuesheet_status()
        except Exception:                                        # noqa: BLE001
            st = {}
        bound = "、".join(st.get("bound_layers") or [])
        act_info = m.addAction("码表：%s" % ((st.get("title") or "（未装载）")))
        act_info.setEnabled(False)
        act_info2 = m.addAction("已绑图层：%s ｜ 片段：%d"
                                % (bound or "无", int(st.get("rt_items", 0) or 0)))
        act_info2.setEnabled(False)
        m.addSeparator()
        act_open = m.addAction(T("换表 / 导入码表…"))
        act_off = m.addAction(T("从这个图层卸下码表"))
        act_off.setEnabled(bool(getattr(lay, "is_cuesheet", False)))
        btn = self.rows[idx][0] if 0 <= idx < len(self.rows) else self
        chosen = m.exec(btn.mapToGlobal(btn.rect().bottomLeft()))
        if chosen is act_open:
            fn = getattr(self.main, "open_cuesheet", None)
            if callable(fn):
                fn()
        elif chosen is act_off:
            try:
                import cue_ui
                cue_ui.unbind_layer(self.main, lay)
            except Exception:                                    # noqa: BLE001
                pass
            self.refresh_header(idx)
            self.refresh_playing()

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
        h = self.cell_h
        # ★ 格子宽度跟着行高走（见 `ClipRow.set_cell_height`）——这里必须用**这一行**
        #   的 `cell_w`，否则 item 还是 104 宽、图标被缩到 56，右边照样空一大截。
        cw = int(getattr(grid, "cell_w", CELL_W) or CELL_W)
        iw, ih = cw - 8, max(16, h - 10)
        # ★★★ 2026-11-06（用户：「码表图层在右边把这个码表里面缩略图显示出来」）：
        #   码表图层的素材来自 `.ercue` 包，**不在 `lay.clips` 里** ⇒ 这一行以前永远是空的。
        #   现在按包内 media 列表建格子；只读（拖/右键都被 `set_cue_mode` 关掉）。
        if getattr(lay, "is_cuesheet", False):
            grid.set_cue_mode(True)
            try:
                paths = list(self.main.engine.cue_media_paths() or [])
            except Exception:                                      # noqa: BLE001
                paths = []
            # ★★★ 2026-11-06（用户：「AVJ 素材缩略图不是已经打包在码表导出包里了吗」）：
            #   是。`.ercue` 里就有 `thumbs/<clip_id>.png`（长边 ≤256、带 alpha），装表时
            #   `cuesheet.load()` 已解出。**优先用包内这张图**：与码表编辑器里看到的一模一样，
            #   而且零解码（主线程只读一张小 PNG）。
            #   老包没有 `thumbs/` 才退回"已有缓存 → 否则后台补生成"，绝不在这里解码视频。
            try:
                tmap = self.main.engine.cue_thumb_map() or {}
            except Exception:                                      # noqa: BLE001
                tmap = {}
            items, need = [], []
            for p in paths:
                m = self._cue_item(p)
                if m is None:
                    continue
                items.append(m)
                icon = None
                tp = tmap.get(os.path.normcase(str(p)))
                if tp:
                    pm = pixmap_file(tp, iw, ih)
                    if pm is not None and not pm.isNull():
                        icon = QIcon(pm)
                if icon is None:
                    icon = m.icon_cached(iw, ih)     # 只认已有缓存，主线程绝不解码
                    need.append(m)
                it = QListWidgetItem(icon, "")
                it.setData(Qt.UserRole, m)
                it.setToolTip("%s\n（码表素材：进出/转场由 .ercue 决定，这一行不能编辑）" % m.name)
                it.setSizeHint(QSize(cw, h))
                grid.addItem(it)
            if need:
                self._ensure_cue_thumbs(need)
            return
        grid.set_cue_mode(False)
        for m in lay.clips:
            icon = m.icon(iw, ih)   # 走缩略图缓存，避免重复读盘/缩放
            it = QListWidgetItem(icon, "")
            it.setData(Qt.UserRole, m)
            it.setToolTip(m.name)
            it.setSizeHint(QSize(cw, h))
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
            # 码表下拉 + 头部排布：码表层只留 [👁][S] 名称 [▼码表]，右侧 4 个控件隐藏
            cmb_cue = getattr(f, "_cmb_cue", None)
            if cmb_cue is not None:
                if bool(getattr(lay, "is_cuesheet", False)):
                    # 表名 / 绑定的包可能变了（换表、导入、被移出列表）⇒ 重建选项并选中当前项
                    self._fill_cue_combo(cmb_cue, lay)
            self._apply_cue_header_mode(f, lay)
            for w in (f._btn_vis, f._btn_solo, f._op, f._blend, f._btn_alpha, f._btn_lock):
                w.blockSignals(False)
            # 角色按钮（自动图层专属）
            btn_role = getattr(f, "_btn_role", None)
            if btn_role is not None:
                btn_role.setText(T("前景") if lay.role == "fg" else T("背景"))
                btn_role.setStyleSheet(
                    self._role_style(lay.role, bool(getattr(f, "_compact_hdr", False))))
            # 自动层边框/底色跟随类型与角色变化（⚠ 档位不能被刷新冲掉）
            f._lay = lay
            f.setStyleSheet(self._header_style(lay, bool(getattr(f, "_compact_hdr", False))))

    # ---- 播放高亮：所有正在播放的素材（含过渡中的上一个）都高亮 ----
    def refresh_playing(self):
        """给"正在使用的素材"打金黄底。

        ⚠⚠ **必须按素材路径比对，不能按对象身份比对**（2026-09-29 用户报"底色没了"）：
          自动图层每次重算素材池都会 `lay.clips = list(picked)`（engine._refresh_auto_layers），
          `sync_layer()` 又把行里的项整批重建 ⇒ 行里存的 MediaItem 和 `lay.clips` 里的
          已经不是同一批对象，`m in playing` 永远为假 ⇒ **一个格子都不亮**。
          换成路径比对后，对象怎么重建都能对上。

        ⚠ 再加一层兜底：`cur == -1`（引擎停了 / 正在等切换点）时 `lay.clips[lay.cur]` 取不到，
          但图层播放器还在解那个文件 —— 用播放器的路径高亮。用户要的是"正在使用的素材"
          的底色，而不是"引擎此刻的索引"。
        """
        def _key(p):
            return os.path.normcase(str(p or ""))

        for i, (_, grid) in enumerate(self.rows):
            if i >= len(self.main.engine.layers):
                continue
            lay = self.main.engine.layers[i]
            playing = set()
            # ★★★ 2026-11-06（用户：「在用哪一个素材的时候就把它显示成黄底，就和其他的图层一样」）：
            #   码表图层的"当前素材"在 `cs_states`（引擎每帧算出来的片段状态）里，
            #   `lay.cur` / `lay.player` 对它是无意义的（容器层没有单一播放器）。
            if getattr(lay, "is_cuesheet", False):
                try:
                    playing = set(self.main.engine.cue_active_media() or ())
                except Exception:                                  # noqa: BLE001
                    playing = set()
            # ⚠ `prev_idx` 只在**过渡进行中**才算"正在使用"：引擎过渡结束后**不会**
            #   把它重置成 -1（只在换池/硬切时置 -1）⇒ 无条件带上它就会
            #   **永远有两个金框**（用户 2026-10-01 反馈「不在逐拍为什么有两个框」）。
            idxs = [getattr(lay, "cur", -1)]
            if getattr(lay, "trans_active", False):
                idxs.append(getattr(lay, "prev_idx", -1))
            for idx in idxs:
                try:
                    if isinstance(idx, int) and 0 <= idx < len(lay.clips):
                        playing.add(_key(getattr(lay.clips[idx], "path", "")))
                except Exception:                                  # noqa: BLE001
                    pass
            playing.discard("")
            if not playing:
                # 兜底：直接用播放器当前解的文件（引擎停止/待切换时仍有画面在放）
                for attr in ("player", "prev_player"):
                    p = getattr(getattr(lay, attr, None), "path", "")
                    if p:
                        playing.add(_key(p))
            for k in range(grid.count()):
                it = grid.item(k)
                m = it.data(Qt.UserRole)
                pth = getattr(m, "path", None) or (m if isinstance(m, str) else "")
                if _key(pth) and _key(pth) in playing:
                    it.setBackground(QColor(255, 205, 0, 235))   # 正在播放：金黄打底
                    it.setData(PLAY_ROLE, True)                  # ↑ delegate 再压一道金边
                    it.setForeground(QColor(25, 25, 25))
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)
                else:
                    it.setBackground(QColor(0, 0, 0, 0))
                    it.setData(PLAY_ROLE, False)
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
        try:
            ow = getattr(self.main, "out_win", None)
            self.main.engine.set_frame_consumers(
                bool(on) or bool(ow is not None and ow.isVisible()))
        except Exception:
            pass
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

    def showEvent(self, e):
        super().showEvent(e)
        # 隐藏期间 set_frame 只存最新帧不缩放；重新显示时立刻补画，避免空白到下一帧。
        try:
            if self._img is not None:
                self._rescale()
        except Exception:
            pass

    def _rescale(self):
        if self._img is None or not self.preview_on():
            return
        p = self.pic_cfg()
        fit = p.get("fit", "keep")
        res = float(p.get("res", 1.0))
        tgt = self.view.size()
        if tgt.width() <= 1 or tgt.height() <= 1:
            return
        # ★ 2026-11-08（码表演出掉帧）：先缩到预览尺寸，再 QPixmap.fromImage；
        #   旧顺序是「整张 1080p QImage → QPixmap（实测 3~4ms）→ 再缩两次」，
        #   每帧白烧 5~10ms GUI 线程，和输出窗口抢主线程。
        img = self._img
        if res < 0.999:
            img = img.scaled(max(1, int(img.width() * res)),
                             max(1, int(img.height() * res)),
                             Qt.KeepAspectRatio, Qt.SmoothTransformation)
        if fit == "stretch":
            img = img.scaled(tgt, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        elif fit == "cover":
            img = img.scaled(tgt, Qt.KeepAspectRatioByExpanding,
                             Qt.SmoothTransformation)
            x = max(0, (img.width() - tgt.width()) // 2)
            y = max(0, (img.height() - tgt.height()) // 2)
            img = img.copy(x, y, min(tgt.width(), img.width()),
                           min(tgt.height(), img.height()))
        else:
            img = img.scaled(tgt, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.view.setPixmap(QPixmap.fromImage(img))

    _img = None

    def pic_cfg(self):
        p = self.main.cfg["ui"].setdefault("preview", {})
        p.setdefault("res", 1.0)         # 预览分辨率：1.0 / 0.5 / 0.25（性能）
        p.setdefault("fit", "keep")      # keep / cover / stretch
        p.setdefault("fps", 60)
        return p

    def open_pic_settings(self):
        PreviewSettingsDialog(self.main, self).exec()

    def set_frame(self, img, allow=True):
        import time as _t
        if not self.preview_on():
            return                        # 预览已关闭：跳过 fromImage + 缩放（省 CPU）
        self._img = img
        # ★ 2026-11-08（码表演出掉帧）：预览在别的页签/被隐藏/被全屏输出盖住时
        #   只存最新帧，不做缩放和贴图。旧实现只要配置里 preview.on=True 就每帧
        #   fromImage+两次 Smooth 缩放 —— 白占 GUI 线程 4~9ms/帧。
        if not allow:
            return
        try:
            if not self.isVisible() or not self.view.isVisible():
                return
        except Exception:
            pass
        p = self.pic_cfg()
        fps = max(1, int(p.get("fps", 60)))
        # ★ 2026-11-08 二修：演出中且独立输出窗口在显示时，预览封顶 15fps。
        #   输出窗口要的是 30fps 的稳定；预览只是监看，15fps 足够，能省下
        #   约一半 GUI 线程时间（实测预览缩放 4~9ms/帧，直接抬高引擎 tick）。
        try:
            ow = getattr(self.main, "out_win", None)
            if bool(getattr(self.main.engine, "running", False)) and ow is not None and ow.isVisible():
                fps = min(fps, 15)
        except Exception:
            pass
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
        多个图层显示多个；图层无素材或被关闭显示时不显示。

        ⚠ 2026-10-10（审计 P1）：「下一个素材」改为读引擎的**无锁快照**
          `engine.ui_snapshot()["next_clips"]`（键 = 图层在 engine.layers 里的索引），
          不再调用 `engine.next_clip_for()` —— 后者会抢渲染锁，导致 GUI 卡渲染。
        ⚠ 索引对齐：本方法的 `layers` 就是 `engine.layers`（唯一调用点
          ui_main.py:5391），故 enumerate 的 i 即 engine.layers 下标，与快照键一致。
        ⚠ Kv 层显示"当前素材"的分支不读快照（它本就不调 next_clip_for），保持不变。
        """
        infos = []
        if self.chk_next.isChecked() and self.preview_on():
            # 无锁只读快照：可能为空 {}（引擎未就绪）⇒ 一律当作"没有下一个素材"。
            try:
                _nxt_map = (self.main.engine.ui_snapshot() or {}).get("next_clips") or {}
            except Exception:                                      # noqa: BLE001
                _nxt_map = {}
            for i, lay in enumerate(layers):
                if not getattr(lay, "visible", False) or not lay.clips:
                    continue
                if getattr(lay, "is_kv", False):
                    # Kv 待机层不自动切换：显示当前素材（而非"下一个"）
                    cur = lay.clips[lay.cur] if 0 <= lay.cur < len(lay.clips) else lay.clips[0]
                    infos.append((lay.name, cur))
                    continue
                nxt = _nxt_map.get(i)
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
                lbl.setToolTip(Tf("[{}]  下一个：{}", lname, m.name))
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
        self.setWindowTitle( T("音源设置"))
        self.setMinimumWidth(460)
        self._devs = {"system": [], "mic": [], "asio": []}
        self._scanner = None
        self._scanning = False      # 重入守卫：扫描进行中（结构上防止信号重入 ⇒ 栈溢出）

        lay = QVBoxLayout(self)
        g = QGridLayout()
        g.addWidget(QLabel("音源类型"), 0, 0)
        self.source = QComboBox()
        # ★ 2026-10-22：麦克风 / 线路输入 是**同一套 WASAPI 录音端点**，但端点类型不同。
        #   以前两个条目都列出**全部**端点 ⇒ 内容一模一样（用户指出"这两个该一样吗"）。
        #   现在仍分两项，但**按端点类型过滤**：麦克风只列麦克风类，线路只列线路/混音类。
        self.source.addItems(["系统声音（WASAPI Loopback）", "麦克风", "线路输入/声卡"])
        # ★ ASIO（低延迟）：2026-10-22 起**按驱动可用性开放**。
        #   10-18 曾因"功能未完成"永久禁用；现已用真实声卡（Donner USB）
        #   验证采集可用（48k / 8 帧 / 1.7ms）⇒ 有驱动就允许选。
        #   注意：下拉项的类别靠**索引**取（_src_key() 里 3 = ASIO），
        #   所以条的**位置不能变**。
        self.source.addItem("ASIO（低延迟 · 声卡）")
        self._asio_idx = 3
        _asio_drv = []
        try:
            import asio_engine as _ae
            if _ae.available()[0]:
                _asio_drv = _ae.list_drivers() or []
        except Exception:                                  # noqa: BLE001
            _asio_drv = []
        if _asio_drv:
            self.source.setItemData(self._asio_idx, T(
                "低延迟声卡输入：选本项后，在「设备」里选 ASIO 驱动（如 %s），"
                "再用右侧「输入通道」选第几路输入。ASIO 抓不到别的程序播放的声音，"
                "只录声卡输入。") % _asio_drv[0], Qt.ToolTipRole)
        else:
            try:
                _asio_item = self.source.model().item(self._asio_idx)
                if _asio_item is not None:
                    _asio_item.setEnabled(False)
            except Exception:                              # noqa: BLE001
                pass
            self.source.setItemData(self._asio_idx, T(
                "未检测到 ASIO 驱动：请先安装声卡自带的 ASIO 驱动"
                "（或 ASIO4ALL），再回到本页点「刷新」。"), Qt.ToolTipRole)

        # ★ 局域网音频（自研轻量协议，2026-10-08 新增）：
        #   A 机跑独立采集发送小 exe，B 机选这一项接收。
        #   「设备」下拉里填的是**端口号**（默认 47811，一般不用改）。
        self.source.addItem("局域网音频（另一台电脑）")
        self._lan_idx = 4
        self.source.setItemData(self._lan_idx, T(
            "接收另一台电脑通过局域网发来的音频（需在 A 机运行"
            "「EasyReality 音频发送端」）。零第三方依赖，两机需在同一局域网，"
            "防火墙放行 UDP。下方「设备」填端口（默认 47811），"
            "或直接填 A 机的 IP（如 192.168.1.100）以跳过自动发现。"), Qt.ToolTipRole)
        g.addWidget(self.source, 0, 1, 1, 2)

        self.lbl_device = QLabel("设备")
        g.addWidget(self.lbl_device, 1, 0)
        self.device = QComboBox()
        self.device.setMinimumWidth(260)
        g.addWidget(self.device, 1, 1)
        self.btn_rescan = QPushButton("重新扫描")
        self.btn_rescan.clicked.connect(self.scan_devices)
        g.addWidget(self.btn_rescan, 1, 2)
        # 换驱动 ⇒ 重新取该驱动的输入通道名（只有 ASIO 需要）
        self.device.currentIndexChanged.connect(self._on_device_changed)

        g.addWidget(QLabel("采样率"), 2, 0)
        self.rate = QComboBox()
        self.rate.addItems(["自动（跟随系统）", "44100", "48000", "96000"])
        self.rate.setToolTip("自动=跟随设备原生采样率（避免重采样、能量更准）；手动可强制指定")
        g.addWidget(self.rate, 2, 1)

        self.chk_mono = QCheckBox("单声道分析")
        self.chk_mono.setToolTip("只取左声道做分析。不省 CPU，作用是在左右声道反相时避免低频被平均抵消")
        g.addWidget(self.chk_mono, 3, 0, 1, 3)

        # ★ ASIO 输入通道（用户问「ASIO 能采集指定输出吗」）——
        #   事实：ASIO **没有 loopback 概念**，抓不到别的程序播出来的声音，
        #   它能做的是选**硬件输入通道**。但有些驱动的输入里**自带 Loopback 通道**
        #   （名字里带 Loopback 之类），把通道名列出来，用户才能选中那两路。
        #   驱动被独占/装不上时列不出名字，就退回按**序号**选（照样能用）。
        self.lbl_ch = QLabel("输入通道")
        self.cb_ch0 = QComboBox()
        self.cb_ch1 = QComboBox()
        for cb in (self.cb_ch0, self.cb_ch1):
            cb.setMinimumWidth(120)
        g.addWidget(self.lbl_ch, 4, 0)
        g.addWidget(self.cb_ch0, 4, 1)
        g.addWidget(self.cb_ch1, 4, 2)

        self.lbl_ch_hint = QLabel("")
        self.lbl_ch_hint.setWordWrap(True)
        self.lbl_ch_hint.setStyleSheet("color:" + theme.V("muted") + ";font-size:11px;")
        g.addWidget(self.lbl_ch_hint, 5, 0, 1, 3)
        self._asio_ch = []          # 当前驱动的输入通道列表
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

        # ★ 局域网音频专用状态行（2026-10-08）：显示**对端 IP**、深度、丢包。
        #   用户明确要求"能看见对方的 IP" —— 现场排查时这是第一手信息：
        #   连上了但不知道连的是谁，就无法判断是不是连到了错误的一台机器。
        self.lbl_lan = QLabel("")
        self.lbl_lan.setWordWrap(True)
        self.lbl_lan.setStyleSheet("color:#4fc3f7;font-size:11px;")
        self.lbl_lan.setProperty("_i18nDynamic", True)
        self.lbl_lan.hide()          # 只有音源 = 局域网时才显示
        lay.addWidget(self.lbl_lan)

        self.lbl_hint = QLabel("")
        self.lbl_hint.setWordWrap(True)
        self.lbl_hint.setStyleSheet("color:#e0a030;font-size:11px;")
        self.lbl_hint.setProperty("_i18nDynamic", True)   # 含设备名，动态重建
        lay.addWidget(self.lbl_hint)

        btns = QDialogButtonBox(QDialogButtonBox.Apply | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Apply).setText( T("应用"))
        btns.button(QDialogButtonBox.Cancel).setText( T("取消"))
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
    #                   下拉索引 → 设备类别（线路输入并入 mic；索引 3 = ASIO；索引 4 = 局域网）
    SOURCE_KEYS = ("system", "mic", "mic", "asio", "lan")

    def _mic_category(self):
        """当前是「麦克风」(1) 还是「线路输入/声卡」(2)？两者共用 mic 通道，列表要分开。"""
        i = self.source.currentIndex()
        return "mic" if i == 1 else ("line" if i == 2 else "")

    def _src_key(self):
        return self.SOURCE_KEYS[max(0, min(4, self.source.currentIndex()))]

    @staticmethod
    def _looks_like_lan_target(s):
        """`s` 看起来像「端口 / 对端IP」吗？（用于回填局域网那栏的下拉框）

        接受：
          · 纯数字端口（`47811`）
          · IP（`192.168.1.100`）
          · IP:端口（`192.168.1.100:47811`）
        拒绝：其它任意字符串（尤其是**别的音源的设备名**）——
          可编辑下拉的 `setCurrentText()` 会原样显示任何字符串，
          若把残留的设备名填进去，用户会以为"这栏坏了 / 扫不到网络输入"。
        """
        t = (s or "").strip()
        if not t:
            return False
        if t.isdigit():
            return True
        host, _, tail = t.rpartition(":")
        if host and tail.isdigit():
            t = host
        parts = t.split(".")
        if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            return True
        return False

    # ---------------- ASIO 输入通道 ----------------
    def _ensure_ch_items(self, chs=None, err=""):
        """填充「输入通道」两个下拉。

        chs：`asio_engine.list_channels()` 的结果（可能为空 = 列不出名字）。
        ⚠ 列不出名字**不是错误**（驱动被 VirtualDJ 占着就会这样）——
          这时退回"按序号选"，ASIO 照样能用，只是用户不知道每路是什么。
        """
        chs = list(chs or [])
        self._asio_ch = chs
        cfg0 = int(self.main.cfg["audio"].get("asio_ch0", 0) or 0)
        cfg1 = int(self.main.cfg["audio"].get("asio_ch1", 1) or 1)
        for cb, cur in ((self.cb_ch0, cfg0), (self.cb_ch1, cfg1)):
            cb.blockSignals(True)
            cb.clear()
            if chs:
                for c in chs:
                    label = "%d: %s" % (c["index"] + 1, c["name"] or ("输入 %d" % (c["index"] + 1)))
                    cb.addItem(label, c["index"])
            else:
                for i in range(8):        # 退路：按序号，最多给 8 路
                    cb.addItem("%d" % (i + 1), i)
            idx = cb.findData(cur)
            cb.setCurrentIndex(idx if idx >= 0 else min(cur, cb.count() - 1))
            cb.blockSignals(False)
        if chs:
            names = "、".join(c["name"] or ("输入 %d" % (c["index"] + 1)) for c in chs[:4])
            self.lbl_ch_hint.setText(Tf(
                "该驱动有 {} 路输入：{}。选你要采的那两路。", len(chs), names))
        else:
            why = ("；" + err) if err else ""
            self.lbl_ch_hint.setText(T("读不到通道名（驱动可能正被别的程序占用）"
                                       "，这里按序号选即可。") + why)

    def _sync_ch_visible(self):
        """「输入通道」只在音源 = ASIO 时显示（其它音源固定双声道，没有可选的）。"""
        is_asio = self._src_key() == "asio"
        for w in (self.lbl_ch, self.cb_ch0, self.cb_ch1, self.lbl_ch_hint):
            w.setVisible(is_asio)

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
        cfg_idx = {"system": 0, "mic": 1, "linein": 2, "asio": 3, "lan": 4}.get(
            a.get("source_type", "system"), 0)
        if self.SOURCE_KEYS[cfg_idx] == kind:
            return legacy, False     # 可能是本类型，但没确认过 → 静默处理
        return "", False

    def _load_from_cfg(self):
        a = self.main.cfg["audio"]
        idx = {"system": 0, "mic": 1, "linein": 2, "asio": 3, "lan": 4}.get(
            a.get("source_type", "system"), 0)
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
        self._sync_ch_visible()
        try:
            self._ensure_ch_items(None)     # 先按序号占位；扫描回来后会换成真名字
        except Exception:                                          # noqa: BLE001
            pass

    def scan_devices(self):
        from PySide6.QtCore import QThread
        # ⚠ 重入守卫（结构性防御）：程序化重填下拉列表会在 Qt 里**同步**发信号，
        #   若槽又回头调本方法就会无限递归 ⇒ 栈溢出（2026-10-01 真实阻断项）。
        #   ① blockSignals 只堵住"当前这个"触发路径；这里再加显式标志，二者都要。
        if self._scanning or (self._scanner is not None and self._scanner.isRunning()):
            return
        self._scanning = True
        try:
            # ⚠ 2026-10-10（审计 P2）：ASIO 驱动名必须**在清空下拉之前**取走。
            #   旧实现在 clear()+addItem("正在扫描…") **之后**才读 currentText() ⇒
            #   读到的永远是占位符 ⇒ list_channels() 拿到"正在扫描…" ⇒ 通道名功能从未生效。
            #   占位文本（扫描中／未找到设备）不是真驱动名 ⇒ 传空串，
            #   list_channels("") 会走"取第一个已注册驱动"的兜底（比传占位符正确）。
            #   ⚠ 这里只把「当前选中文本」取进局部变量，判据仍是"是不是占位符"，不拿控件文本判业务分支。
            _cur_drv = (self.device.currentText() or "").strip()
            if (not _cur_drv or _cur_drv.startswith(("（", "("))
                    or _cur_drv in (T("正在扫描…"), "正在扫描…", "Scanning…")):
                _cur_drv = ""
            # ⚠ `clear()` / `addItem()` 会**同步**发 `currentIndexChanged` → 回
            #   `_on_device_changed` → 音源==asio 时又调 `scan_devices()`；而此刻
            #   `self._scanner` 还没重新赋值，旧的 `isRunning()` 守卫必然失效。
            #   按 `_fill_devices` 的做法：列表重填期间 blockSignals。
            _prev_blocked = self.device.blockSignals(True)
            try:
                self.device.clear()
                # ★ 占位文本必须走 T()：否则英文界面下与 `apply()` 的守卫口径不一致，
                #   扫描中误点「应用」会把"正在扫描…"当成设备名存进配置。
                self.device.addItem(T("正在扫描…"))
            finally:
                self.device.blockSignals(_prev_blocked)

            class _Scanner(QThread):
                from PySide6.QtCore import Signal as _S
                ready = _S(dict)

                def __init__(self, audio, parent=None, kind="", driver=""):
                    super().__init__(parent)
                    self.audio = audio
                    self.kind = kind
                    self.driver = driver

                def run(self):
                    try:
                        devs = self.audio.list_devices()
                        # ★ 音源是 ASIO 时**顺带**把该驱动的输入通道名也取回来
                        #   （跑在这条后台线程上，不卡界面；取不到就退回按序号选）
                        if self.kind == "asio":
                            try:
                                import asio_engine
                                chs, cerr = asio_engine.list_channels(self.driver)
                                devs["asio_ch"] = chs
                                devs["asio_ch_err"] = cerr
                            except Exception as e:                     # noqa: BLE001
                                devs["asio_ch"] = []
                                devs["asio_ch_err"] = str(e)
                        self.ready.emit(devs)
                    except Exception as e:
                        self.ready.emit({"system": [], "mic": [], "asio": [], "error": str(e)})

            self._scanner = _Scanner(self.main.audio, self, self._src_key(), _cur_drv)
            self._scanner.ready.connect(self._fill_devices)
            # ⚠ 兜底复位：线程结束就复位，绝不把 `_scanning` 留在 True（那会把之后
            #   所有扫描永久挡死 ⇒ 界面永远停在"正在扫描…"，等于用一个新 bug 换旧的）。
            #   `_fill_devices` 里还有一次复位（成功/失败都会走到）—— 两处独立出口，刻意冗余。
            self._scanner.finished.connect(self._on_scan_finished)
            self._scanner.start()
        except Exception:
            # 中途抛异常（含 start 失败）：复位后原样抛出，不留僵标志
            self._scanning = False
            raise

    def _on_scan_finished(self):
        """扫描线程结束（`finished` 信号）⇒ 兜底复位重入标志（见 `scan_devices`）。"""
        self._scanning = False

    def _actual_dev(self):
        """当前**实际在用**的采集设备名（设备被拔掉后用它兜底选中）"""
        try:
            return self.main.audio.state.snapshot().get("device_desc") or ""
        except Exception:
            return ""

    def _fill_devices(self, devs=None):
        # 扫描结果回来了（成功/失败都会发 ready）⇒ 复位重入标志（`scan_devices` 的第二条出口）
        self._scanning = False
        if devs is not None:
            self._devs = devs if "error" not in devs else {"system": [], "mic": [], "asio": []}
        # ★ 局域网音频：没有"设备"可扫，这里放的是**端口 / 对端 IP**。
        #   用可编辑下拉，用户既能选默认值也能自己敲。
        #   ★★ 2026-10-08 新增「手动输入 IP」（用户要求）：
        #     自动发现依赖广播，在有代理 TUN（Clash 等）/ 跨 VLAN / 交换机隔离的
        #     现场经常收不到 ⇒ 允许直接填 A 机 IP（`1.2.3.4` 或 `1.2.3.4:47811`）。
        if self._src_key() == "lan":
            if not self.device.isEditable():
                self.device.setEditable(True)
            # 标签跟着变，让用户知道这里可以填 IP
            try:
                self.lbl_device.setText(T("端口 / 对端IP"))
            except Exception:                                      # noqa: BLE001
                pass
            self.device.blockSignals(True)
            self.device.clear()
            self.device.addItems(["47811"])
            a = self.main.cfg["audio"]
            want = (a.get("device_by_source") or {}).get("lan") or a.get("device_name") or ""
            # ★★ 2026-10-08 修（用户报「扫不到网络输入了」）：
            #   这里只在 want **看起来像端口或 IP** 时才回填 —— 否则回落到默认值。
            #   原因：可编辑下拉框的 `setCurrentText()` 会**原样显示**任意字符串，
            #   而 `device_by_source["lan"]` / `device_name` 里可能残留**其它音源的
            #   设备名**（切换音源、旧配置迁移都会留下）⇒ 下拉框会显示
            #   「扬声器 (Realtek …)」这种一看就"不是端口"的内容，
            #   用户看到的正是"**扫不到网络输入**"（以为这一栏坏了/空了）。
            #   旧代码是 `want.isdigit()`（只认端口，最保守）；现在放宽到
            #   「数字」或「形如 IP / IP:端口」，其它一律丢弃。
            if self._looks_like_lan_target(want):
                self.device.setCurrentText(want.strip())
            else:
                self.device.setCurrentIndex(0)
            self.device.blockSignals(False)
            # ★ 局域网音频推荐「自动」采样率（跟随对端，避免一次无谓的重采样）。
            #   只在配置里没存过用户选择时才动，不覆盖用户的显式设置。
            try:
                if not int(a.get("sample_rate", 0) or 0):
                    self.rate.setCurrentIndex(0)
            except Exception:                                      # noqa: BLE001
                pass
            self.lbl_hint.setText(T("在 A 机运行「EasyReality 音频发送端」，"
                                    "两台电脑需在同一局域网。\n"
                                    "• 默认自动发现（填端口 47811 即可）\n"
                                    "• 自动发现连不上时，在这里直接填 A 机的 IP，"
                                    "如 192.168.1.100（也可写 192.168.1.100:47811）"))
            return
        if self.device.isEditable():
            self.device.setEditable(False)   # 其它音源：下拉不可编辑
        try:
            self.lbl_device.setText(T("设备"))     # 从「局域网」切回来 ⇒ 标签复位
        except Exception:                                      # noqa: BLE001
            pass
        # ASIO 输入通道（只有 ASIO 音源才有这一步）
        if self._src_key() == "asio" and devs is not None and "asio_ch" in devs:
            self._ensure_ch_items(devs.get("asio_ch"), devs.get("asio_ch_err") or "")
        kind = self._src_key()
        items = self._devs.get(kind, [])
        # ★ 麦克风 / 线路 同为 "mic" 通道，但**列出的端点要分开**：
        #   麦克风类看名字含「麦克风 / Microphone」，其余（线路、立体声混音、采集卡）归线路。
        if kind == "mic":
            _cat = self._mic_category()
            if _cat == "mic":
                _f = [n for n in items if any(k in n.lower() for k in ("麦克风", "microphone", "webcam"))]
            elif _cat == "line":
                _f = [n for n in items if not any(k in n.lower() for k in ("麦克风", "microphone", "webcam"))]
            else:
                _f = items
            items = _f or items          # 过滤后为空 ⇒ 退回全部（不让用户卡死）
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
        # ASIO 的采样率是**驱动全局**的（改了会影响同一驱动的其它客户端，比如 VDJ），
        # 我们始终沿用驱动当前值 ⇒ 选 ASIO 时把采样率下拉灰掉，免得用户白设。
        try:
            self.rate.setEnabled(self._src_key() != "asio")
        except Exception:                                          # noqa: BLE001
            pass
        self._sync_ch_visible()
        self._fill_devices()
        # ★ 局域网音频没有"设备"可扫（下拉里是端口号）⇒ 不触发设备扫描，
        #   省一次无意义的枚举（而且要枚举的是别人电脑上的设备，本就扫不到）。
        if self._src_key() != "lan":
            self.scan_devices()

    def _on_device_changed(self, _i=0):
        """换了设备/驱动 ⇒ ASIO 的话重新取一遍输入通道名。"""
        if self._src_key() != "asio":
            return
        self.scan_devices()

    def apply(self):
        t = self.SOURCE_KEYS[max(0, min(4, self.source.currentIndex()))]
        dev = (self.device.currentText() or "").strip()
        # 占位文本（扫描中／未找到设备）不能当设备名保存
        if dev.startswith(("（", "(")) or dev in ("正在扫描…", T("正在扫描…"), "Scanning…"):
            dev = ""
        sr = [0, 44100, 48000, 96000][max(0, self.rate.currentIndex())]
        if t == "asio":
            # ⚠ ASIO 的采样率是**驱动全局**的，改了会连带影响 VirtualDJ / DAW 那一边，
            #   所以我们永远沿用驱动当前值（原生侧传 sr=0）⇒ 这里强制归零。
            sr = 0
        mono = self.chk_mono.isChecked()
        # 按音源类型分别记住设备：切回该类型时能自动选中，
        # 且「设备掉线」提示不会把别的类型的设备名拿来误报。
        a = self.main.cfg["audio"]
        dbs = dict(a.get("device_by_source") or {})
        dbs[t] = dev
        a["device_by_source"] = dbs
        a["device_name"] = dev          # 与 source_type 配对，引擎启动时读它
        if t == "asio":
            # ASIO 的"用哪两路输入"也存下来（默认 0/1 = 前两路）
            try:
                a["asio_ch0"] = int(self.cb_ch0.currentData() or 0)
                a["asio_ch1"] = int(self.cb_ch1.currentData() or 1)
            except Exception:                                      # noqa: BLE001
                a["asio_ch0"], a["asio_ch1"] = 0, 1
        self.main.apply_audio_settings(t, dev, sr, mono,
                                        int(a.get("asio_ch0", 0) or 0),
                                        int(a.get("asio_ch1", 1) or 1))
        self._want_dev = dev
        self.accept()

    def _update_meters(self):
        snap = self.main.audio.state.snapshot()
        for k, b in self.bars.items():
            b.setValue(int(max(0.0, min(1.0, snap.get(k, 0.0))) * 100))
        self.lbl_dev.setText(T("当前采集设备: ") + (snap.get("device_desc") or "-"))
        # ★★ 2026-10-22：**ASIO 状态必须显式显示**。
        #   旧行为：ASIO 打开失败会静默回退到系统声音，界面没有任何提示
        #   ⇒ 用户只看到"切到 ASIO 没反应/切不过去"。现在把成功/失败原因写在提示行。
        try:
            if self._src_key() == "asio":
                _fail = str(snap.get("asio_failed") or "")
                _info = str(snap.get("asio_info") or "")
                if _fail:
                    self.lbl_hint.setText(T("⚠ ASIO 打开失败：") + _fail + T("（已回退到系统声音）"))
                    self.lbl_hint.setStyleSheet("color:#e0a030;font-size:11px;")
                elif snap.get("asio_active"):
                    self.lbl_hint.setText(T("✅ ASIO 已启用：") + _info)
                    self.lbl_hint.setStyleSheet("color:#4fc3f7;font-size:11px;")
                else:
                    self.lbl_hint.setText(T("ASIO：尚未启动（点「应用」后开始采集）"))
                    self.lbl_hint.setStyleSheet("color:#e0a030;font-size:11px;")
                self._asio_hint_on = True
            elif getattr(self, "_asio_hint_on", False):
                self.lbl_hint.setText("")
                self._asio_hint_on = False
        except Exception:                                        # noqa: BLE001
            pass
        # ★ 局域网音频状态行：对端 IP + 缓冲深度 + 抖动 + 丢包
        if self._src_key() == "lan":
            self.lbl_lan.show()
            info = snap.get("lan_info") or ""
            fail = snap.get("lan_failed") or ""
            if fail:
                self.lbl_lan.setText("⚠ " + fail)
                self.lbl_lan.setStyleSheet("color:#e0a030;font-size:11px;")
            elif info:
                self.lbl_lan.setText( T("局域网：") + info)
                self.lbl_lan.setStyleSheet("color:#4fc3f7;font-size:11px;")
            else:
                # 还没拿到状态：可能是还没点「应用」，或正在等待对端首包
                self.lbl_lan.setText(T("局域网：等待对端…（点「应用」后开始连接，"
                                       "最多等 5 秒）"))
                self.lbl_lan.setStyleSheet("color:#888;font-size:11px;")
        else:
            self.lbl_lan.hide()


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
        self.setWindowTitle( T("预览画面设置"))
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




