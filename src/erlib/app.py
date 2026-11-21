# -*- coding: utf-8 -*-
r"""erlib 界面 —— 「选曲库文件夹 → 扫描 → 勾选 → 导出 `.eravjlib`」。

## 为什么扫描先写"工作库"，导出再从工作库筛子集
如果边扫边写最终包，那么**没勾的歌也会在包里**。
所以拆成两步：

    工作库（本机持久，支持续扫）      →   导出（只挑勾选的）
    %LOCALAPPDATA%\AutoVJ\erlib_scan.eravjlib    你指定路径的 .eravjlib

好处：
1. **续扫**：大曲库跑一小时，中途关了下次接着扫（已扫的自动跳过）；
2. **一次扫描、多次导出**（这次导 100 首、下次导另外 200 首，不用重扫）；
3. **勾选真正生效**（没勾的一字节都不写进包里）。

## 扫描与导出都在 QThread 里跑
指纹 2.4 s/首 ⇒ 1452 首要一小时，绝不能卡界面。
"""

import os
import sys
import time
import traceback

from PySide6.QtCore import QEvent, QSize, QSettings, Qt, QThread, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFileDialog,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMainWindow, QMenu, QMessageBox, QProgressBar,
                               QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from erlib.format import LibReader, LibWriter
from erlib.wavecol import WaveDelegate, cover_pixmap, pack_wave                # noqa: E402
from erlib.scanner import AUDIO_EXT, Scanner                        # noqa: E402

WORK_NAME = "erlib_scan.eravjlib"
APP_TITLE = "ERLIB曲库分析"
# ★ 2026-11-08：对外版本通道（RC3 → RC4）。只加在主窗标题上，
#   `APP_TITLE` 保持原样 —— 弹窗文案与黑盒脚本的窗口查找都按它匹配。
APP_CHANNEL = "RC4"

# ★ 2026-11-12（用户反馈「打包后点进去没有之前的歌曲」）：启动诊断日志。
#   main() 里打开日志文件后把写函数放进 _BOOT，界面/数据加载路径可以随时 _blog()；
#   这样「窗口在、列表空」时日志里能看到 work 路径 / exists / 读到几首 / 异常栈。
#   `ERLIB_BOOT_LOG` 环境变量可覆盖日志路径（测试时指到工作区，避免写 AppData）。
_BOOT = {"write": None, "path": ""}


def _blog(msg):
    w = _BOOT.get("write")
    if w is None:
        return
    try:
        w(msg)
    except Exception:                                        # noqa: BLE001
        pass


def work_path():
    """工作库路径（扫描成果持久保留 ⇒ 支持续扫与多次导出）。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "AutoVJ")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:                                            # noqa: BLE001
        d = os.path.expanduser("~")
    return os.path.join(d, WORK_NAME)


def fmt_dur(sec):
    try:
        s = int(float(sec or 0))
        return "%d:%02d" % (s // 60, s % 60)
    except Exception:                                            # noqa: BLE001
        return ""


# ==========================================================================
# 后台线程
# ==========================================================================
class ScanThread(QThread):
    progress = Signal(int, int, str)
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, root, writer, use_online=False, parent=None, paths=None,
                 force=False):
        super().__init__(parent)
        self.root = root
        self.writer = writer
        self.use_online = use_online
        # ★ 2026-10-31：`paths` 非空 ⇒ 只扫这些文件（导入文件 / 拖拽）；
        #   为空 ⇒ 扫 `root` 整个目录（导入文件夹）。
        self.paths = list(paths or [])
        # ★ 2026-11-05：「重新分析」用 —— True 表示不跳过已扫过的歌，强制重算覆盖。
        self.force = bool(force)
        self._sc = None

    def stop(self):
        if self._sc is not None:
            self._sc.stop()

    def run(self):
        try:
            self._sc = Scanner(root=self.root, on_progress=self._prog,
                               use_online=self.use_online)
            st = (self._sc.scan_paths(self.paths, self.writer, force=self.force)
                  if self.paths else self._sc.scan_tree(self.root, self.writer))
            self.writer.close()
            self.finished_ok.emit(st)
        except Exception as e:                                   # noqa: BLE001
            try:
                self.writer.close()
            except Exception:                                    # noqa: BLE001
                pass
            self.failed.emit("%s: %s" % (type(e).__name__, e))

    def _prog(self, done, total, path):
        self.progress.emit(done, total, path)


class ExportThread(QThread):
    progress = Signal(int, int)
    finished_ok = Signal(str, dict)
    failed = Signal(str)

    def __init__(self, work, out_path, sids, parent=None):
        super().__init__(parent)
        self.work = work
        self.out_path = out_path
        self.sids = set(int(x) for x in sids)

    def run(self):
        try:
            from erlib.transfer import export_subset
            st = export_subset(
                self.work, self.out_path, self.sids,
                on_progress=lambda a, b: self.progress.emit(a, b),
                app_ver="erlib")
            self.finished_ok.emit(self.out_path, st)
        except Exception as e:                                   # noqa: BLE001
            self.failed.emit("%s: %s" % (type(e).__name__, e))


# ==========================================================================
# 主窗口
# ==========================================================================
COLS = ["", "封面", "文件名", "时长", "BPM", "波形", "状态"]
# ★ 2026-10-31（用户「波形图呢 / 专辑封面呢」）：封面与波形都做成**列**
#   —— 与 ERAVJ 曲库的「波形列 + 段落色带」观感一致（绘制代码见 erlib/wavecol.py）。
COL_COVER = 1
COL_NAME = 2
COL_WAVE = 5

# ★ 2026-11-06（用户：「Lib 增加一个调整每一行高度这个大小的开关」）：
#   给 4 个档位，不做连续滑块 —— 表格行高连续调没有意义（差 2px 看不出来），
#   档位一眼能选、能记住。封面基准图固定 `COVER_PX`，**显示尺寸跟着行高走**
#   （行高-12，夹在 32~112），所以调行高 = 同时调封面大小，不会出现"行拉高了图不动"。
# ★ 2026-11-06 下午（用户：「把 LIB 的行高全部都缩小一档」）：整条台阶往下挪一格
#   —— 原来是 56/76/96/124 ⇒ 现在 44/60/76/92（原来的"大"= 现在的"特大"）。
ROW_H_PRESETS = (("紧凑", 44), ("标准", 60), ("大", 76), ("特大", 92))
COVER_ROLE = int(Qt.ItemDataRole.UserRole) + 8   # 封面格里存"基准图 QPixmap"


def ui_settings():
    """LIB 的界面偏好落盘位置：`%LOCALAPPDATA%\\AutoVJ\\erlib_ui.ini`。

    ⚠ **不能写进工作库**（`erlib_scan.eravjlib`）：那是数据包，会被导出/校验/迁移，
      混进界面偏好只会让"包变了"这类判据变脏。
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "AutoVJ")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:                                            # noqa: BLE001
        d = os.path.expanduser("~")
    return QSettings(os.path.join(d, "erlib_ui.ini"), QSettings.Format.IniFormat)
# ★ 2026-10-31（用户「erlib 只管扫描导出啊」）：删掉「曲风」列与「联网补跑曲风」按钮 ——
#   曲风那一整套模块（music_meta/audio_genre/genre_lookup/genre_visual_default）已在
#   「大砍刀」里删除，留着只会点一下就 ImportError。erlib 现在只做**扫描 + 导出**。


class _HintLabel(QLabel):
    """工具栏里的长句提示：窗口够宽就完整显示，窄了可以被压到 0。

    ⚠ 两种"想当然"的写法都试过，都错（2026-10-31 实测）：
      · 普通 QLabel → `minimumSizeHint` = 整句文字宽度 ⇒ 窗口最小宽度被撑到 914px，
        用户**拖不窄**；
      · `QSizePolicy.Ignored` → 配合 `addStretch(1)` 时它被分配 0 宽，**整句消失**
        （截图里提示直接没了）。
    正确做法就是只把 `minimumSizeHint` 的水平部分归零：布局照旧按 sizeHint 给它
    文字宽度，空间不够时压缩它而不是撑大整个窗口。
    """

    def minimumSizeHint(self):                               # noqa: N802
        return QSize(0, super().minimumSizeHint().height())


class ErlibWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("%s · %s" % (APP_TITLE, APP_CHANNEL))
        self.resize(1180, 720)
        # ★ 2026-11-05：窗口被拖窄到 660px 时，工具栏 8 个按钮会被压成省略号
        #   （实测「开始扫描」变 `...`、「导出勾选…」被截断）。设一个最小宽度兜底，
        #   保证所有按钮文字完整；用户仍可往上拉大。
        self.setMinimumWidth(860)
        self.root = ""
        self.work = work_path()
        self.scan_th = None
        self.exp_th = None
        self._work_busy = False  # ★ 修复2：工作库写任务互斥（扫描 / 删除）
        self._n_checked = 0
        self._t0 = 0.0           # 本轮扫描起点（算"剩余约 X 分钟"用）
        # ★ 2026-11-06：行高（`ROW_H_PRESETS` 的下标）+ 封面显示边长（由行高推出）
        self.row_h_idx = 1
        self.cover_px = 64
        # ★ 2026-10-31：允许把音乐文件/文件夹**拖进窗口**导入
        self.setAcceptDrops(True)
        self._build()
        self.apply_row_h(self.load_row_h(), save=False)   # 用户上次选的行高
        self._load_existing()

    # ---------------- UI ----------------
    def _build(self):
        c = QWidget()
        self.setCentralWidget(c)
        v = QVBoxLayout(c)

        # ★ 2026-10-31（用户「去掉这个」= 顶部「选择曲库文件夹…」那一行）：
        #   导入改成「导入文件夹 / 导入文件 / 拖入」之后，曲库根目录由导入的路径
        #   自动推出（共同父目录），这一行纯属多余 ⇒ 删掉。当前曲库目录显示在底部状态栏。

        row2 = QHBoxLayout()
        # ★ 2026-10-31（用户「扫描改成导入文件夹和导入文件」）：两个入口，
        #   顺手支持把文件/文件夹**拖进窗口**导入（见 dragEnterEvent/dropEvent）。
        self.btn_scan = QPushButton("文件夹…")
        self.btn_scan.setToolTip("选一个文件夹，递归导入里面的所有音频（已在库里的自动跳过）")
        self.btn_scan.clicked.connect(self._on_import_dir)
        row2.addWidget(self.btn_scan)
        self.btn_import_files = QPushButton("文件…")
        self.btn_import_files.setToolTip("选若干音频文件导入（可多选）")
        self.btn_import_files.clicked.connect(self._on_import_files)
        row2.addWidget(self.btn_import_files)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._on_stop)
        row2.addWidget(self.btn_stop)
        # ★ 2026-10-31（用户「这个空白位置增加提示：直接拖文件进窗口也可以」）：
        #   工具栏右侧那片空白放一句常驻提示 —— 拖拽导入是**看不见的手势**，
        #   不写出来用户永远不会试（他本人就是问了才知道有这个功能）。
        # ★ 2026-11-05：文案**必须短**。原来那句「也可以直接把音乐文件 / 文件夹拖进这个窗口」
        #   25 个字约 330px，加上「重新分析」按钮后把右边三个按钮全挤成省略号
        #   （实测 `导入文件夹…` 显示成 `导入文件…`、`导出勾选的歌…` 显示成 `导出勾选...`）。
        #   短句 + tooltip 全文，是唯一既能保住提示、又不挤压按钮的写法。
        self.lbl_drop = _HintLabel("或直接拖文件进来")
        self.lbl_drop.setStyleSheet("color:palette(placeholder-text);")
        self.lbl_drop.setToolTip("把音频文件或整个文件夹拖到窗口任意位置即可导入")
        # ⚠ 别用 `QSizePolicy.Ignored`：配合下面的 `addStretch(1)` 会被分配 0 宽，
        #   整句提示**直接消失**（2026-10-31 实测截图里就是空的）。可压缩性由
        #   `_HintLabel.minimumSizeHint()` 处理，见类注释。
        # ★ 2026-11-05：提示**从工具栏挪到下面筛选框那一行**（见下方 row3）。
        #   原因：加了「重新分析」后，660px 宽（用户把默认 1180 拖小了）的工具栏
        #   装不下 8 个按钮 + 一句提示 —— 实测「开始扫描」被压成 `...`、
        #   「导出勾选的歌…」被截断。工具栏只留按钮，提示去借用筛选框右侧的空白。
        row2.addStretch(1)
        self.btn_all = QPushButton("全选")
        self.btn_all.clicked.connect(lambda: self._check_all(True))
        row2.addWidget(self.btn_all)
        self.btn_none = QPushButton("全不选")
        self.btn_none.clicked.connect(lambda: self._check_all(False))
        row2.addWidget(self.btn_none)
        # ★ 2026-11-05（用户：「全选/全不选旁边加一个重新分析当前勾选音频的选项」）：
        #   ⚠ 文案**不能叫「重新扫描」** —— 这个软件里「扫描」已经是专有名词
        #     （扫磁盘找歌 / 导入文件夹），叫扫描会让人以为要重新找文件。
        #   这个动作实际重算的是 BPM + 能量 + 分段（波形/封面顺带重建），
        #   所以叫「重新分析」，tooltip 里把这三样写清楚。
        self.btn_reanalyze = QPushButton("重新分析")
        self.btn_reanalyze.setToolTip(
            "对**勾选**的歌曲重算 BPM、能量分段与波形（封面一起更新）。\n"
            "· 会覆盖工作库里这几首的旧分析结果；\n"
            "· 你手工改过 BPM / 锚点 / 分段的歌**默认跳过**（弹窗里可勾选一并重算）；\n"
            "· 已经导出过的 .eravjlib 不受影响。")
        self.btn_reanalyze.clicked.connect(self._on_reanalyze)
        row2.addWidget(self.btn_reanalyze)
        self.btn_del = QPushButton("删除勾选")
        self.btn_del.setToolTip(
            "从工作库删除勾选的歌（指纹与封面一起删）。\n"
            "下次「开始扫描」会重新分析它们；已经导出过的包不受影响。")
        self.btn_del.clicked.connect(self._on_delete)
        row2.addWidget(self.btn_del)
        self.btn_export = QPushButton("导出勾选…")
        self.btn_export.clicked.connect(self._on_export)
        row2.addWidget(self.btn_export)
        v.addLayout(row2)

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("筛选：输入文件名里的字（回车生效）")
        self.filter.returnPressed.connect(self._apply_filter)
        # ★ 2026-11-05：拖拽提示挪到这里（筛选框右侧的空白），工具栏只留按钮。
        row3 = QHBoxLayout()
        row3.addWidget(self.filter, 1)
        # ★ 2026-11-06（用户：「Lib 增加一个调整每一行高度这个大小的一个开关」）：
        #   放**筛选框这一行**而不是工具栏 —— 工具栏 9 个按钮已经被压得很紧
        #   （见上面那段注释），再塞一个下拉就会把按钮文字挤成省略号。
        self.cmb_rowoh = QComboBox()
        self.cmb_rowoh.setToolTip("列表每一行的高度（封面跟着一起变大/变小）")
        for _name, _h in ROW_H_PRESETS:
            self.cmb_rowoh.addItem("行高：%s" % _name, int(_h))
        self.cmb_rowoh.currentIndexChanged.connect(self.on_row_h_combo)
        row3.addWidget(self.cmb_rowoh)
        row3.addWidget(self.lbl_drop)
        v.addLayout(row3)

        self.tbl = QTableWidget(0, len(COLS))
        self.tbl.setHorizontalHeaderLabels(COLS)
        self.tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.verticalHeader().setVisible(False)
        h = self.tbl.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.tbl.setColumnWidth(0, 34)
        # ★★ 2026-10-31（用户「这一栏拖动也有 bug」）：**全部 Interactive + 一列弹性**
        #   —— 原来 `文件名` 用 Stretch（Qt 的 Stretch 列**拖不动**，拖它的边界会去改
        #   左边那列的宽度，表现就是"拖一栏、另一栏跟着乱跳"），`封面/波形` 用 Fixed
        #   也拖不动。现在改成和 ERAVJ 曲库同一套：所有列都能拖，拖一条线只改它左右
        #   两列（Excel 语义），多出来的空间由 `_elastic` 那一列吸收。
        # ⚠ 第 0 列（勾选框）也要有默认宽度，否则 `_fit_cols` 会按兜底 100 把它撑宽
        self._COL_DEF = {0: 34, COL_COVER: 78, COL_NAME: 460, 3: 60, 4: 130,
                         COL_WAVE: 260, 6: 70}
        for _c, _w in self._COL_DEF.items():
            h.setSectionResizeMode(_c, QHeaderView.ResizeMode.Interactive)
            self.tbl.setColumnWidth(_c, _w)
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.tbl.setColumnWidth(0, 34)
        self._user_w = {}          # 用户亲手拖过的列宽
        self._elastic = COL_NAME   # 吃剩余宽度的那一列（默认文件名）
        self._dg = None            # 正在拖的边界：(列号, 按下时的宽度快照, 鼠标起点x)
        _hv = self.tbl.horizontalHeader()
        _hv.viewport().installEventFilter(self)
        self.tbl.viewport().installEventFilter(self)
        # 行高要放得下封面（64px + 上下各 6px 边距）
        self.tbl.verticalHeader().setDefaultSectionSize(76)
        # ⚠⚠ 光把 QIcon 做成 64px 没用：QTableWidget 画图标用的是 **view 的 iconSize**，
        #   默认只有 16×16 ⇒ 封面看起来还是个小方块（用户「封面太小了」的真相）。
        from erlib.wavecol import COVER_PX as _CPX
        self.tbl.setIconSize(QSize(_CPX, _CPX))
        self.tbl.setItemDelegateForColumn(COL_WAVE, WaveDelegate(self.tbl))
        self.tbl.itemChanged.connect(self._on_item_changed)
        # ★ 2026-10-27 曲库识别增强：右键/双击打开「歌曲详情」。
        self.tbl.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tbl.customContextMenuRequested.connect(self._on_table_menu)
        self.tbl.cellDoubleClicked.connect(self._on_cell_double)
        v.addWidget(self.tbl, 1)

        bot = QHBoxLayout()
        self.bar = QProgressBar()
        self.bar.setValue(0)
        bot.addWidget(self.bar, 1)
        self.lbl = QLabel("就绪")
        bot.addWidget(self.lbl)
        v.addLayout(bot)

    # ---------------- 列宽（Excel 语义：拖一条线只改左右两列） ----------------
    MIN_COL_W = 60

    def _fit_cols(self):
        """按 viewport 实际宽度分配：弹性列吃剩余，其余用用户宽度 / 默认宽度。

        ⚠ 触发时机：**viewport 自己的 Resize**（父窗口 resizeEvent 里读子控件几何
          拿到的是旧值，2026-10-30 在 ERAVJ 曲库上踩过同一个坑）。
        """
        try:
            if getattr(self, "_dg", None) is not None:
                return                      # 拖拽中别抢
            n = int(self.tbl.columnCount())
            vw = int(self.tbl.viewport().width())
            if vw < 120 or n < 7:
                return
            els = int(getattr(self, "_elastic", COL_NAME))
            user = getattr(self, "_user_w", {}) or {}
            tot = 0
            widths = {}
            for c in range(n):
                if c == els:
                    continue
                w = int(user.get(c, self._COL_DEF.get(c, 100)))
                widths[c] = w
                tot += w
            spare = vw - tot - 2
            if spare < self.MIN_COL_W:
                # 窗口太窄：按比例压缩固定列（**不写回** _user_w，拉宽能恢复）
                # ⚠ 必须**迭代**：每列都有 MIN 下限，按比例压一次后可能仍超出
                #   （实测 700 宽窗口压完还差 111px ⇒ 冒横向滚动条、后面的列被推出视口）。
                scale = max(0.25, (vw - 2 - self.MIN_COL_W) / float(max(1, tot)))
                for _ in range(24):
                    tot = 0
                    cand = {}
                    for c, w in widths.items():
                        cand[c] = max(self.MIN_COL_W, int(w * scale))
                        tot += cand[c]
                    if tot + self.MIN_COL_W <= vw - 2:
                        break
                    scale *= 0.94
                widths = cand
                spare = max(self.MIN_COL_W, vw - tot - 2)
            widths[els] = max(self.MIN_COL_W, spare)
            prev = getattr(self, "_fitting", False)
            self._fitting = True
            try:
                for c, w in widths.items():
                    if int(self.tbl.columnWidth(c)) != w:
                        self.tbl.setColumnWidth(c, w)
            finally:
                self._fitting = prev
        except Exception:                                        # noqa: BLE001
            pass

    def _hdr_col_at(self, x):
        """x（header viewport 坐标）落在哪条分隔线上 ⇒ 返回左边那列的列号。"""
        try:
            hv = self.tbl.horizontalHeader()
            n = int(self.tbl.columnCount())
            for c in range(n):
                edge = hv.sectionViewportPosition(c) + hv.sectionSize(c)
                if abs(int(x) - int(edge)) <= 5:
                    return c
        except Exception:                                        # noqa: BLE001
            pass
        return -1

    def _drag_pair(self, i, d):
        """把位移 `d` 落到「列 i + 它的邻居」上（两列之和不变）。"""
        n = int(self.tbl.columnCount())
        if i < 0 or i >= n:
            return
        j = i + 1 if i + 1 < n else i - 1
        if j < 0 or j >= n:
            return
        base = self._dg[1] if self._dg else None
        if not base:
            return
        wi = max(self.MIN_COL_W, base.get(i, 100) + d)
        d2 = wi - base.get(i, 100)
        wj = max(self.MIN_COL_W, base.get(j, 100) - d2)
        d2 = base.get(j, 100) - wj
        wi = base.get(i, 100) + d2
        if d2 == 0:
            return
        prev = getattr(self, "_fitting", False)
        self._fitting = True
        try:
            self.tbl.setColumnWidth(i, wi)
            self.tbl.setColumnWidth(j, wj)
        finally:
            self._fitting = prev

    def eventFilter(self, obj, ev):                              # noqa: N802
        try:
            et = ev.type()
            hv = self.tbl.horizontalHeader()
            if obj is hv.viewport():
                if et == QEvent.Type.MouseButtonPress:
                    c = self._hdr_col_at(ev.position().x())
                    if c >= 0:
                        self._dg = (c, {k: int(self.tbl.columnWidth(k))
                                        for k in range(self.tbl.columnCount())},
                                    int(ev.position().x()))
                        hv.viewport().grabMouse()
                        return True                     # 吞掉：不让 Qt 自己拖
                elif et in (QEvent.Type.MouseMove, QEvent.Type.HoverMove) and self._dg:
                    d = int(ev.position().x()) - self._dg[2]
                    self._drag_pair(self._dg[0], d)
                    return True
                elif et == QEvent.Type.MouseButtonRelease and self._dg:
                    c = int(self._dg[0])
                    d = int(ev.position().x()) - self._dg[2]
                    self._drag_pair(c, d)
                    if self._dg:
                        # ⚠ 必须把**每一列当前宽度**都记下来：拖动时被压缩的邻居列
                        #   若没记录，下一次 `_fit_cols()` 会把它弹回默认宽度
                        #   ⇒ 用户看到"我只拖了这条线，怎么还多出一列变了"
                        #   （2026-10-31 实测变化列 = [4,6,7]）。
                        n = int(self.tbl.columnCount())
                        j = c + 1 if c + 1 < n else c - 1
                        for k in range(n):
                            if k == j:
                                continue
                            self._user_w[k] = int(self.tbl.columnWidth(k))
                        self._elastic = j
                        self._user_w.pop(j, None)
                    self._dg = None
                    try:
                        hv.viewport().releaseMouse()
                    except Exception:                        # noqa: BLE001
                        pass
                    self._fit_cols()
                    return True
            elif obj is self.tbl.viewport():
                if et == QEvent.Type.Resize:
                    self._fit_cols()
                # ★ 2026-10-31：表格铺满整个窗口 —— 拖拽落点大概率在**表格上**。
                #   `QTableWidget` 默认不接收 drop，事件在子控件这里就断了（窗口的
                #   `dropEvent` 收不到）⇒ 这里把四个拖拽事件转发给窗口自己处理，
                #   保证"拖到窗口任意位置"都真的能导入。
                elif et == QEvent.Type.DragEnter:
                    self.dragEnterEvent(ev)
                    return True
                elif et == QEvent.Type.DragMove:
                    self.dragMoveEvent(ev)
                    return True
                elif et == QEvent.Type.DragLeave:
                    self.dragLeaveEvent(ev)
                    return True
                elif et == QEvent.Type.Drop:
                    self.dropEvent(ev)
                    return True
        except Exception:                                        # noqa: BLE001
            self._dg = None
        return super().eventFilter(obj, ev)

    # ---------------- 行高（每行高度 / 封面大小） ----------------
    def load_row_h(self):
        """上次选的行高档位（没有 / 损坏 ⇒ 标准）。"""
        try:
            i = int(ui_settings().value("ui/row_h", 1))
        except (TypeError, ValueError):
            i = 1
        return max(0, min(len(ROW_H_PRESETS) - 1, i))

    def save_row_h(self, idx):
        try:
            s = ui_settings()
            s.setValue("ui/row_h", int(idx))
            s.sync()          # 立刻落盘：下次启动要读到（别等进程退出才 flush）
        except Exception:                                        # noqa: BLE001
            pass

    def on_row_h_combo(self, idx):
        """用户在下拉框里选了某一档 ⇒ 应用 + 记住。"""
        self.apply_row_h(idx)

    def apply_row_h(self, idx, save=True):
        """应用行高档位：行高 + 封面显示尺寸 + 封面列宽，并重刷已有行。

        ⚠ **不读盘、不解码**：封面基准图在 `_add_row` 时已存进格子（`COVER_ROLE`），
          这里只是把它按新尺寸缩放一次 ⇒ 上千行也是毫秒级。
        ⚠ 必须**逐格重设 icon**：`setIconSize` 只改视图的图标尺寸，QTableWidget 里
          已有的 QIcon 不会自动按新尺寸重画 ⇒ 用户看到"行变高了图没变"。
        """
        try:
            idx = max(0, min(len(ROW_H_PRESETS) - 1, int(idx)))
        except (TypeError, ValueError):
            idx = 1
        _name, h = ROW_H_PRESETS[idx]
        self.row_h_idx = idx
        self.cover_px = max(32, min(112, int(h) - 12))
        self.tbl.verticalHeader().setDefaultSectionSize(int(h))
        self.tbl.setIconSize(QSize(self.cover_px, self.cover_px))
        # 封面列宽跟着走（原来的 78 = 64px 图 + 14px 边距，同一套算法）
        try:
            self._COL_DEF[COL_COVER] = int(self.cover_px) + 14
            self._user_w.pop(COL_COVER, None)     # 用户拖过的旧宽度作废，按新图宽重排
        except Exception:                                        # noqa: BLE001
            pass
        cb = getattr(self, "cmb_rowoh", None)
        if cb is not None and cb.currentIndex() != idx:
            cb.blockSignals(True)                 # 铁律 11：程序回填控件必须 blockSignals
            cb.setCurrentIndex(idx)
            cb.blockSignals(False)
        self._rescale_covers()
        try:
            self._fit_cols()
        except Exception:                                        # noqa: BLE001
            pass
        if save:
            self.save_row_h(idx)

    def _cover_scaled(self, pm):
        """基准封面 → 当前行高对应的显示尺寸（失败返回原图）。"""
        px = max(32, min(112, int(self.cover_px or 64)))
        if pm is None or pm.isNull() or (pm.width() == px and pm.height() == px):
            return pm
        return pm.scaled(px, px, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)

    def _rescale_covers(self):
        """把每行的封面图标按当前 `cover_px` 重画（只缩放内存里的基准图）。"""
        n = int(self.tbl.rowCount())
        if not n:
            return
        self.tbl.setUpdatesEnabled(False)
        try:
            for r in range(n):
                it = self.tbl.item(r, COL_COVER)
                if it is None:
                    continue
                base = it.data(COVER_ROLE)
                if base is None or base.isNull():
                    continue
                it.setIcon(QIcon(self._cover_scaled(base)))
        finally:
            self.tbl.setUpdatesEnabled(True)

    # ---------------- 数据 ----------------
    def _add_row(self, s, checked=True, reader=None):
        """加一行。`reader` = 已打开的 `LibReader`（用来取封面/波形的 BLOB）。

        ⚠ 封面与波形都是**加载时一次性**读出并降采样，paint 里不再碰磁盘/数据库
          （110 行逐行读 BLOB 约 0.2s，滚动时零成本）。
        """
        sid = int(s["song_id"])
        r = self.tbl.rowCount()
        self.tbl.insertRow(r)
        it = QTableWidgetItem("")
        it.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        it.setCheckState(Qt.CheckState.Checked if checked
                         else Qt.CheckState.Unchecked)
        it.setData(Qt.ItemDataRole.UserRole, sid)
        self.tbl.setItem(r, 0, it)

        # ---- 封面 ----
        cov = QTableWidgetItem("")
        cov.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        try:
            base = cover_pixmap(reader.cover(sid) if reader is not None else None)
            if base is not None:
                # ★ 2026-11-06：基准图存进格子 —— 换行高时只缩放它（零读盘、零解码）
                cov.setData(COVER_ROLE, base)
                cov.setIcon(QIcon(self._cover_scaled(base)))
                cov.setToolTip("专辑封面")
            else:
                cov.setToolTip("（无内嵌封面）")
        except Exception:                                        # noqa: BLE001
            pass
        self.tbl.setItem(r, COL_COVER, cov)

        # ★ 2026-10-31（用户「不要艺人和曲名」）：这两列删掉 —— 同步/扫描出来的
        #   艺人常是空的、曲名又常等于文件名，纯占地方。
        vals = [s.get("file_name", ""), fmt_dur(s.get("duration")),
                self._bpm_text(s)]
        for k, t in enumerate(vals, start=COL_NAME):
            self.tbl.setItem(r, k, QTableWidgetItem(str(t)))

        # ---- 波形（包络 + 段落色带）----
        wv = QTableWidgetItem("")
        wv.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        try:
            wf = reader.waveform(sid) if reader is not None else None
            got = pack_wave(wf, s.get("sections"), s.get("duration"))
            if got:
                wv.setData(Qt.ItemDataRole.UserRole, got)
                wv.setToolTip("整曲波形 · 蓝=低 黄=中 红=高动态")
            else:
                wv.setToolTip("（这首歌还没有波形数据 —— 重新扫描一次即可生成）")
        except Exception:                                        # noqa: BLE001
            pass
        self.tbl.setItem(r, COL_WAVE, wv)

        self.tbl.setItem(r, 6, QTableWidgetItem("已分析"))
        if checked:
            self._n_checked += 1

    @staticmethod
    def _bpm_text(s):
        """BPM（来源）列文案：有效 BPM（bpm_manual 覆盖自动值）；无 BPM 显示「—」。"""
        g = s.get("grid") or {}
        if not isinstance(g, dict):
            g = {}
        try:
            import beatgrid as _bg
            eg = _bg.effective_grid(g, s.get("manual"))
        except Exception:                                        # noqa: BLE001
            eg = g
        bpm = eg.get("bpm") if isinstance(eg, dict) else None
        if bpm:
            try:
                # ★ 2026-10-31（用户「bpm 不要写来源，纯本地分析」）：只显示数值
                return "%.1f" % float(bpm)
            except (TypeError, ValueError):
                pass
        return "—"

    def _sid_of_row(self, r):
        it = self.tbl.item(r, 0)
        if it is None:
            return 0
        try:
            return int(it.data(Qt.ItemDataRole.UserRole) or 0)
        except (TypeError, ValueError):
            return 0

    def _open_detail(self, sid):
        if not sid:
            return
        if self._work_busy_guard():
            return
        try:
            from erlib.detail import ErlibDetailDialog
            dlg = ErlibDetailDialog(self.work, sid, self.root, parent=self)
            dlg.exec()
            self._reload_table()       # 保存后 BPM 列/详情刷新
        except Exception as e:                                   # noqa: BLE001
            QMessageBox.warning(self, APP_TITLE, "打开详情失败：%s" % e)

    def _on_table_menu(self, pos):
        r = self.tbl.rowAt(pos.y())
        if r < 0:
            return
        self.tbl.selectRow(r)
        sid = self._sid_of_row(r)
        menu = self._build_row_menu(sid)
        menu.exec(self.tbl.viewport().mapToGlobal(pos))

    def _build_row_menu(self, sid):
        """构造某一行（某首歌）的右键菜单。**单独拆出来便于测试**（exec 会阻塞）。

        ★ 2026-10-31（用户「还有右键详情」/「eravj 这些都要在 erlib 实现」）：
          右键不只开详情，常用动作也放进来（与 ERAVJ 曲库右键的习惯一致）。
        """
        menu = QMenu(self)
        act = menu.addAction("歌曲详情…")
        act.triggered.connect(lambda _=False: self._open_detail(sid))
        menu.addSeparator()
        a_exp = menu.addAction("导出勾选的歌…")
        a_exp.triggered.connect(lambda _=False: self._on_export())
        a_del = menu.addAction("删除勾选的歌")
        a_del.triggered.connect(lambda _=False: self._on_delete())
        return menu

    def _on_cell_double(self, r, _c):
        self._open_detail(self._sid_of_row(r))

    def _load_existing(self):
        """启动时把工作库里已有的歌列出来（这样续扫/再次导出都能看到）。"""
        _blog("load_existing  work=%s  exists=%s"
              % (self.work, os.path.isfile(self.work)))
        if not os.path.isfile(self.work):
            return
        n = 0
        try:
            self.tbl.setUpdatesEnabled(False)
            with LibReader(self.work) as r:
                for s in r.songs():
                    self._add_row(s, checked=True, reader=r)
                    n += 1
                m = r.meta()
            self.root = m.get("root", "") or ""
            self._set_root(self.root)
            _blog("load_existing OK  songs=%d  root=%r" % (n, self.root))
        except Exception as e:                                   # noqa: BLE001
            # ★ 2026-11-12（用户反馈「打包后打开是空的」）：不能只写状态栏 ——
            #   弹出可见提示 + 记完整异常栈，否则用户只看到空表、我们也无从查。
            _blog("load_existing FAILED after %d rows: %r\n%s"
                  % (n, e, traceback.format_exc()))
            self.lbl.setText("读取工作库失败：%s" % e)
            QMessageBox.warning(
                self, APP_TITLE,
                "读取工作库失败：%s\n\n工作库：%s\n\n"
                "常见原因：1) 安全软件拦截读取；2) 内存不足；"
                "3) 工作库文件损坏（同目录有 .bak-* 备份可恢复）。\n"
                "详细日志：%%LOCALAPPDATA%%\\AutoVJ\\erlib_boot.log"
                % (e, self.work))
        finally:
            self.tbl.setUpdatesEnabled(True)
            self._update_status()

    def _all_sids(self):
        out = []
        for r in range(self.tbl.rowCount()):
            it = self.tbl.item(r, 0)
            if it is not None:
                out.append(int(it.data(Qt.ItemDataRole.UserRole) or 0))
        return out

    def _checked_sids(self):
        out = []
        for r in range(self.tbl.rowCount()):
            it = self.tbl.item(r, 0)
            if it is not None and it.checkState() == Qt.CheckState.Checked:
                out.append(int(it.data(Qt.ItemDataRole.UserRole) or 0))
        return out

    def _check_all(self, on):
        self.tbl.blockSignals(True)
        try:
            st = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
            for r in range(self.tbl.rowCount()):
                it = self.tbl.item(r, 0)
                if it is not None and not self.tbl.isRowHidden(r):
                    it.setCheckState(st)
        finally:
            self.tbl.blockSignals(False)
        self._update_status()

    def _on_item_changed(self, it):
        if it.column() == 0:
            self._update_status()

    def _apply_filter(self):
        q = self.filter.text().strip().lower()
        for r in range(self.tbl.rowCount()):
            if not q:
                self.tbl.setRowHidden(r, False)
                continue
            txt = " ".join(
                (self.tbl.item(r, c).text() if self.tbl.item(r, c) else "")
                for c in (1, 2, 3, 4)).lower()
            self.tbl.setRowHidden(r, q not in txt)
        self._update_status()

    @staticmethod
    def _build_stamp():
        """当前跑的是哪一份产物 —— 用户好几次把旧包当新包看（2026-10-31）。

        · 打包 exe：取**自身 exe 文件的修改时间**（这是唯一骗不了人的东西：
          源码 mtime、构建日志、"我明明重新打包了"都可能被误读）；
        · 源码运行：显示「源码」，一眼区分。
        """
        try:
            if getattr(sys, "frozen", False):
                return "构建 %s" % time.strftime(
                    "%m-%d %H:%M",
                    time.localtime(os.path.getmtime(sys.executable)))
        except Exception:                                        # noqa: BLE001
            pass
        return "源码"

    def _update_status(self):
        n = len(self._checked_sids())
        _root = os.path.basename(self.root.rstrip("\\/")) if self.root else "—"
        self.lbl.setText("共 %d 首 · 已勾选 %d 首 · 曲库：%s · 工作库：%s · %s"
                         % (self.tbl.rowCount(), n, _root,
                            os.path.basename(self.work), self._build_stamp()))

    # ---------------- 工作库写任务互斥（修复 2） ----------------
    def _set_work_busy(self, on):
        """置/清「会写工作库的长任务」忙状态（扫描 / 删除）。

        ★ 2026-10-10（T7 审计修复 2）：三个入口各自 new 一个 `LibWriter` 写**同一个**
          `erlib_scan.eravjlib`，此前互不检查 ⇒ 扫描大库（~1 小时）时点「删除」或
          「删除勾选」，会让两个写库并发 ⇒ `database is locked` 被 `except` 吞掉
          （表现为"失败 N 首" / "删除失败"），或删掉的行被扫描阶段的
          整条 `INSERT OR REPLACE` 覆盖。这里用一个共享标志 + 按钮互斥把它们**串行化**：
            忙 ⇒ 三个写工作库按钮全部禁用；空闲 ⇒ 恢复。
        ⚠ **不改线程模型**：扫描 / 补跑仍是各自的 `QThread`，只是不允许同时启动。
        """
        self._work_busy = bool(on)
        for b in (self.btn_scan, self.btn_import_files, self.btn_del):
            try:
                b.setEnabled(not self._work_busy)
            except Exception:                                    # noqa: BLE001
                pass

    def _work_busy_guard(self):
        """若有别的写工作库任务在跑，提示用户并返回 True（调用方据此早退）。"""
        if self._work_busy:
            QMessageBox.information(
                self, APP_TITLE,
                "已有任务正在进行（扫描 / 删除），"
                "请等它结束后再操作。")
            return True
        return False

    # ---------------- 动作 ----------------
    def _set_root(self, d):
        """记下曲库根目录（详情页试听 / rel_path 都用它），并刷新状态栏。"""
        self.root = str(d or "")
        try:
            self._update_status()
        except Exception:                                        # noqa: BLE001
            pass

    def _on_import_dir(self):
        """「导入文件夹…」：选目录 → 递归导入（原「开始扫描」的整目录语义）。"""
        d = QFileDialog.getExistingDirectory(
            self, "选择要导入的文件夹", self.root or "")
        if not d:
            return
        self._set_root(d)
        self._start_scan(None)

    def _on_import_files(self):
        """「导入文件…」：多选音频文件 → 只导入这些。"""
        files, _flt = QFileDialog.getOpenFileNames(
            self, "选择要导入的音频文件", self.root or "",
            "音频 (%s);;所有文件 (*.*)" % " ".join("*" + e for e in AUDIO_EXT))
        if not files:
            return
        self._start_scan(list(files))

    def _expand_drop(self, items):
        """拖进来的路径 → 音频文件列表（目录递归展开，非音频忽略）。"""
        from erlib.scanner import find_audio
        out = []
        for it in items or []:
            try:
                it = str(it)
                if os.path.isdir(it):
                    out.extend(find_audio(it))
                elif os.path.isfile(it) and it.lower().endswith(AUDIO_EXT):
                    out.append(it)
            except Exception:                                    # noqa: BLE001
                continue
        # 去重（拖同一文件两次）
        seen, uniq = set(), []
        for p in out:
            k = os.path.normcase(os.path.abspath(p))
            if k not in seen:
                seen.add(k)
                uniq.append(p)
        return uniq

    # 拖拽提示：平时是常驻那句，拖进来时变成"松开即导入"
    _DROP_HINT = "也可以直接把音乐文件 / 文件夹拖进这个窗口"

    def _drop_hint(self, on):
        """切换工具栏那句提示的文案/颜色（拖动悬停时高亮）。"""
        try:
            if on:
                self.lbl_drop.setText("松开鼠标即可导入…")
                self.lbl_drop.setStyleSheet(
                    "color:palette(highlight);font-weight:bold;")
            else:
                self.lbl_drop.setText(self._DROP_HINT)
                self.lbl_drop.setStyleSheet("color:palette(placeholder-text);")
        except Exception:                                        # noqa: BLE001
            pass

    def dragEnterEvent(self, ev):                                # noqa: N802
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
            self._drop_hint(True)
        else:
            super().dragEnterEvent(ev)

    def dragMoveEvent(self, ev):                                 # noqa: N802
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
        else:
            super().dragMoveEvent(ev)

    def dragLeaveEvent(self, ev):                                # noqa: N802
        self._drop_hint(False)
        super().dragLeaveEvent(ev)

    def dropEvent(self, ev):                                     # noqa: N802
        """把音乐文件/文件夹**拖进窗口**即导入（用户 2026-10-31 要求）。"""
        self._drop_hint(False)
        try:
            urls = ev.mimeData().urls() if ev.mimeData().hasUrls() else []
            items = [u.toLocalFile() for u in urls if u.isLocalFile()]
            paths = self._expand_drop(items)
        except Exception:                                        # noqa: BLE001
            paths = []
        if not paths:
            self.lbl.setText("拖进来的文件里没有可识别的音频")
            return
        ev.acceptProposedAction()
        self.lbl.setText("拖入 %d 个音频，开始导入…" % len(paths))
        self._start_scan(paths)

    def _start_scan(self, paths=None, force=False):
        """统一导入入口：`paths` 为空 ⇒ 扫 `self.root` 整个目录。

        :param force: 「重新分析勾选」用。True ⇒ 不跳过已有、强制重算；
              **且不改 `self.root`** —— 重扫的几首可能散在不同目录，
              取 commonpath 会把曲库根目录顶到很上层，污染后续 rel_path / 试听。
        """
        paths = list(paths or [])
        if paths and not force:
            # 曲库根目录取这些文件的**共同父目录**（rel_path / 试听都靠它）
            try:
                base = os.path.commonpath([os.path.dirname(p) for p in paths])
            except Exception:                                    # noqa: BLE001
                base = os.path.dirname(paths[0])
            if base and os.path.isdir(base):
                self._set_root(base)
        if not self.root or not os.path.isdir(self.root):
            QMessageBox.information(self, APP_TITLE, "先选一个曲库文件夹，或直接把音乐拖进来。")
            return
        if self.scan_th is not None and self.scan_th.isRunning():
            return
        if self._work_busy_guard():                              # ★ 修复2：互斥
            return
        self.tbl.setUpdatesEnabled(False)
        try:
            w = LibWriter(self.work, root=self.root, resume=True, app_ver="erlib")
        except Exception as e:                                   # noqa: BLE001
            # ★★ 2026-11-12「重新分析卡 0%」：原来这里只有 try/finally、没有 except，
            #   构造 LibWriter 抛异常（库被占用/安全软件拦截/文件损坏）时这个槽直接
            #   静默失败 —— 按钮状态都不变、进度条停在 0%，用户只能看到"卡 0%"。
            _blog("LibWriter 打开失败  work=%s  err=%r\n%s"
                  % (self.work, e, traceback.format_exc()))
            QMessageBox.warning(
                self, APP_TITLE,
                "打开工作库失败：%s\n\n"
                "工作库路径：%s\n\n"
                "常见原因：\n"
                "· 另一个 ERLIB 窗口（或上次没退干净的进程）正占用它\n"
                "· 安全软件拦截了数据库文件\n"
                "· 文件损坏（可先把上面这个文件改名备份，软件会重建空库）\n\n"
                "诊断日志：%s" % (e, self.work, _BOOT.get("path") or "(未写)"))
            return
        finally:
            self.tbl.setUpdatesEnabled(True)
        # 续扫：文件里已有的先列出来并标记为已分析
        have = w.existing_ids()
        self.tbl.setRowCount(0)
        self._n_checked = 0
        try:
            self.tbl.setUpdatesEnabled(False)
            with LibReader(self.work) as _r:
                for s in _r.songs():
                    self._add_row(s, checked=True, reader=_r)
        except Exception:                                        # noqa: BLE001
            pass
        finally:
            self.tbl.setUpdatesEnabled(True)
        self.lbl.setText("续扫：工作库里已有 %d 首，本次只扫新增的。" % len(have))
        self._t0 = time.perf_counter()

        self.btn_scan.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._set_work_busy(True)                                # ★ 修复2：置忙（禁用另两个）
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        # ★★ 回归修复（QA 实测）：`start()` 可能抛异常（线程资源不足等）。此时**必须回滚**
        #   忙标志，否则三个写工作库按钮会**永久禁用、只能重启软件**
        #   （旧代码最多只 disable 1 个按钮）。照 `_on_delete` 的 try/finally 精神处理。
        try:
            self.scan_th = ScanThread(self.root, w, use_online=False,
                                     parent=self, paths=paths, force=force)
            self.scan_th.progress.connect(self._on_prog)
            self.scan_th.finished_ok.connect(self._on_scan_done)
            self.scan_th.failed.connect(self._on_scan_fail)
            self.scan_th.start()
        except Exception as e:                                   # noqa: BLE001
            self._set_work_busy(False)                           # ★ 异常路径回滚
            self.btn_stop.setEnabled(False)
            QMessageBox.warning(self, APP_TITLE, "扫描启动失败：%s" % e)
            return

    def _on_stop(self):
        if self.scan_th is not None:
            self.scan_th.stop()
            self.lbl.setText("正在停止（等当前这首扫完）…")

    def closeEvent(self, ev):
        """关窗时先停后台线程，避免 QThread 在运行中被销毁（会直接 abort）。"""
        try:
            if self.scan_th is not None and self.scan_th.isRunning():
                self.scan_th.stop()
                self.scan_th.wait(5000)
        except Exception:                                        # noqa: BLE001
            pass
        try:
            if self.exp_th is not None and self.exp_th.isRunning():
                self.exp_th.wait(5000)
        except Exception:                                        # noqa: BLE001
            pass
        super().closeEvent(ev)

    def _on_prog(self, done, total, path):
        if total > 0:
            self.bar.setRange(0, total)
            self.bar.setValue(done)
        # ★ 2026-11-12：进度也写诊断日志 —— 「卡 0%」时至少能看到扫到第几首、
        #   是不是单首特别慢/在反复重试。
        try:
            _blog("scan_progress %d/%d  %s" % (done, total,
                                               os.path.basename(path or "")))
        except Exception:                                        # noqa: BLE001
            pass
        left = ""
        if done > 0 and total > done:
            per = 3.0
            if self._t0:
                per = max(0.01, (time.perf_counter() - self._t0) / done)
            left = " · 剩余约 %.0f 分钟" % (per * (total - done) / 60.0)
        self.lbl.setText("已扫 %d / %d%s · %s"
                         % (done, total, left, os.path.basename(path)))

    def _on_scan_done(self, st):
        self.btn_scan.setEnabled(True)
        try:
            self.btn_import_files.setEnabled(True)
        except Exception:                                        # noqa: BLE001
            pass
        self.btn_stop.setEnabled(False)
        self._set_work_busy(False)                               # ★ 修复2：解除互斥
        self.bar.setRange(0, 100)
        self.bar.setValue(100)
        self.tbl.setRowCount(0)
        self._n_checked = 0
        self._load_existing()
        msg = ("扫描完成：成功 %d 首，失败 %d 首，跳过（已扫过）%d 首。\n"
               "用时 %.1f 分钟。\n"
               % (st.get("ok", 0), st.get("fail", 0), st.get("skip", 0),
                  st.get("elapsed", 0) / 60.0))
        errs = st.get("errors") or []
        mem_err = any("MemoryError" in str(b) for a, b in errs)
        _blog("scan_done ok=%s fail=%s skip=%s elapsed=%.1fs mem_err=%s"
              % (st.get("ok"), st.get("fail"), st.get("skip"),
                 float(st.get("elapsed") or 0.0), mem_err))
        if errs:
            msg += "\n失败原因（前 5 条）：\n"
            msg += "\n".join("· %s\n    %s" % (a, b) for a, b in errs[:5])
            msg += "\n\n（完整原因见控制台输出）"
        if mem_err:
            # ★★ 2026-11-12「重新分析卡 0%」的真凶：fp 的 STFT + ORT arena 把
            #   单首峰值顶到 ~4 GB，本机 commit 余量不足时每首都是 MemoryError。
            #   现已降到 ~1.8 GB，但机器太满仍可能不够 ⇒ 明确告诉用户怎么办。
            msg += ("\n\n⚠ 检测到「内存不足」（MemoryError）。\n"
                    "请关掉浏览器等占内存的程序（或重启电脑）后再试；\n"
                    "软件本身已把单首峰值内存从约 4 GB 降到约 1.8 GB。")
        elif st.get("ok", 0) == 0:
            msg += ("\n⚠ 一首都没扫到。请确认：\n"
                    "· 选的文件夹里真的有音频文件（flac/mp3/m4a/wav…）\n"
                    "· 或者点「开始扫描」前先选好文件夹")
        else:
            msg += "\n现在可以勾选要导出的歌，然后点「导出勾选的歌」。"
        if st.get("fail", 0) and not st.get("ok", 0):
            QMessageBox.warning(self, APP_TITLE, msg)
        else:
            QMessageBox.information(self, APP_TITLE, msg)

    def _on_scan_fail(self, msg):
        self.btn_scan.setEnabled(True)
        try:
            self.btn_import_files.setEnabled(True)
        except Exception:                                        # noqa: BLE001
            pass
        self.btn_stop.setEnabled(False)
        self._set_work_busy(False)                               # ★ 修复2：解除互斥
        _blog("scan_fail %r" % (msg,))
        QMessageBox.warning(self, APP_TITLE, "扫描出错：%s" % msg)

    def _on_reanalyze(self):
        """重新分析**勾选**的歌：重算 BPM / 能量分段 / 波形，覆盖工作库旧结果。

        ★ 2026-11-05（用户：「全选/全不选旁边加一个重新分析当前勾选音频的选项」）。

        三个设计决定（都不是随手定的）：
        1. **默认跳过手工改过的歌**。`songs.manual` 非空 ⇒ 用户在详情页改过
           BPM / 锚点 / 分段，重算会冲掉他的手工成果。所以弹窗里给了勾选项，
           默认**不勾**（保守），勾了才一起重算。
        2. **必须强制重算**。`scan_paths` 原本"已扫过就跳过"（续扫的关键），
           不传 `force=True` 的话点完等于什么都没发生 —— 这是最容易踩的坑。
        3. **不改曲库根目录**。`_start_scan` 会用 `commonpath` 重设 root，
           而重扫的几首要能散在不同目录，取公共父目录会把 root 顶到很上层，
           污染 rel_path 和试听路径。所以 `force=True` 时跳过那一步。
        """
        sids = set(self._checked_sids())
        if not sids:
            QMessageBox.information(self, APP_TITLE, "还没勾选任何歌。")
            return
        if self._work_busy_guard():                              # 与扫描/删除互斥
            return

        picked, missing = [], []
        try:
            with LibReader(self.work) as r:
                for s in r.songs():
                    sid = int(s.get("song_id") or 0)
                    if sid not in sids:
                        continue
                    p = (s.get("src_path") or "").strip()
                    if not p or not os.path.isfile(p):
                        alt = os.path.join(self.root or "", s.get("rel_path") or "")
                        p = alt if os.path.isfile(alt) else ""
                    if not p:
                        missing.append(s.get("title") or s.get("file_name") or "?")
                        continue
                    picked.append({"sid": sid, "path": p,
                                   "manual": bool(s.get("manual")),
                                   "title": s.get("title") or os.path.basename(p)})
        except Exception as e:                                   # noqa: BLE001
            QMessageBox.warning(self, APP_TITLE, "读取工作库失败：%s" % e)
            return

        if not picked:
            QMessageBox.information(
                self, APP_TITLE,
                "勾选的歌在本机找不到源文件（可能被移动或删除了）。\n\n"
                "找不到的有 %d 首。" % len(missing))
            return

        n_manual = sum(1 for x in picked if x["manual"])
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(APP_TITLE)
        box.setText("重新分析勾选的 %d 首？" % len(picked))
        info = ("会把这几首的 BPM、能量分段和波形重算一遍，**覆盖工作库里的旧结果**。\n\n"
                "· 这次没勾选的歌不受影响\n"
                "· 已经导出过的 .eravjlib 不受影响\n"
                "· 源文件不会被改动")
        if n_manual:
            info += "\n\n其中 %d 首你手工改过 BPM / 锚点 / 分段。" % n_manual
        if missing:
            info += "\n另外 %d 首找不到源文件，会自动跳过。" % len(missing)
        box.setInformativeText(info)
        cb = None
        if n_manual:
            cb = QCheckBox("连手动修改过的 %d 首也一起重算（会冲掉你手工改的值）"
                           % n_manual)
            cb.setChecked(False)                                 # 默认保护手工成果
            box.setCheckBox(cb)
        box.setStandardButtons(QMessageBox.StandardButton.Yes
                               | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return

        allow_manual = bool(cb is not None and cb.isChecked())
        todo = [x["path"] for x in picked if allow_manual or not x["manual"]]
        skipped = len(picked) - len(todo)
        if not todo:
            QMessageBox.information(
                self, APP_TITLE,
                "勾选的 %d 首全都是手动修改过的，已全部跳过。\n\n"
                "要一起重算，请在弹窗里勾选「连手动修改过的也一起重算」。"
                % len(picked))
            return

        self.lbl.setText("重新分析 %d 首…（跳过手动修改的 %d 首）" % (len(todo), skipped))
        self._start_scan(paths=todo, force=True)

    def _on_delete(self):
        """从工作库删除勾选的歌（批量；指纹与封面一起删）。"""
        sids = self._checked_sids()
        if not sids:
            QMessageBox.information(self, APP_TITLE, "还没勾选任何歌。")
            return
        if self._work_busy_guard():                              # ★ 修复2：互斥
            return
        ret = QMessageBox.question(
            self, APP_TITLE,
            "从工作库删除勾选的 %d 首？\n\n"
            "· 它们的指纹与封面会一起删掉；\n"
            "· 下次「开始扫描」会重新分析它们；\n"
            "· 已经导出过的 .eravjlib 不受影响。" % len(sids),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._set_work_busy(True)                                # ★ 修复2：置忙
        try:
            try:
                w = LibWriter(self.work, root=self.root, resume=True, app_ver="erlib")
                try:
                    n = w.delete_songs(sids)
                finally:
                    w.close()
            except Exception as e:                               # noqa: BLE001
                QMessageBox.warning(self, APP_TITLE, "删除失败：%s" % e)
                return
        finally:
            self._set_work_busy(False)                           # ★ 修复2：解除互斥
        self._reload_table()
        self.lbl.setText("已删除 %d 首（工作库剩 %d 首）"
                         % (n, self.tbl.rowCount()))

    def _reload_table(self):
        """从工作库重新加载整张表（扫描完成 / 删除后用）。"""
        self.tbl.setRowCount(0)
        self._n_checked = 0
        try:
            self.tbl.setUpdatesEnabled(False)
            with LibReader(self.work) as r:
                for s in r.songs():
                    self._add_row(s, checked=True, reader=r)
        except Exception as e:                                   # noqa: BLE001
            self.lbl.setText("读取工作库失败：%s" % e)
        finally:
            self.tbl.setUpdatesEnabled(True)
            self._update_status()

    def _on_export(self):
        sids = self._checked_sids()
        if not sids:
            QMessageBox.information(self, APP_TITLE, "还没勾选任何歌。")
            return
        p, _ = QFileDialog.getSaveFileName(
            self, "导出分析包", "我的曲库.eravjlib",
            "erlib 分析包 (*.eravjlib)")
        if not p:
            return
        # ★ 2026-10-19 审计修复：**这里先挡一次**，给即时反馈。
        #   底层 `export_subset` 也会挡（线程里抛会被回成"导出失败"），
        #   但用户选到工作库时应该在点确定的瞬间就知道，而不是等线程报错。
        if os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(self.work)):
            QMessageBox.warning(
                self, APP_TITLE,
                "导出路径不能是工作库本身（那会先把工作库清空）。\n\n"
                "请换一个文件名，例如放到桌面。")
            return
        self.btn_export.setEnabled(False)
        self.bar.setRange(0, len(sids))
        self.bar.setValue(0)
        # ★★ 2026-11-01（用户「没有单独的 eravjlib 文件吗」）：导出**过程中**最终文件名
        #   还不存在（`transfer.export_subset` 先写 `.tmp`、成功后才原子改名）⇒
        #   这里明确告诉用户"还没好，别急着拷/发"。用户就是在这个窗口期把半成品发出去的。
        self.lbl.setText("正在导出…完成前请不要复制/发送该文件")
        self.exp_th = ExportThread(self.work, p, sids, parent=self)
        self.exp_th.progress.connect(
            lambda a, b: (self.bar.setValue(a),
                          self.lbl.setText("导出中 %d/%d —— 完成前请不要复制/发送该文件"
                                           % (a, b))))
        self.exp_th.finished_ok.connect(self._on_export_done)
        self.exp_th.failed.connect(self._on_export_fail)
        self.exp_th.start()

    def _on_export_done(self, path, st):
        self.btn_export.setEnabled(True)
        self.bar.setRange(0, 100)
        self.bar.setValue(100)
        mb = os.path.getsize(path) / 1e6 if os.path.isfile(path) else 0
        # ★★ 2026-11-01：只有走到这里，最终文件名才真的存在且自包含（见 transfer）
        #   ⇒ 才可以明确说"可以发送"。此前窗口期发出去的必然是 4 KB 空壳 + 大 `-wal`。
        QMessageBox.information(
            self, APP_TITLE,
            "导出完成，可以发送：\n%s\n\n%d 首歌 · %d 条指纹 · %.1f MB\n\n"
            "导出目录里只会有一个 .eravjlib（没有 -wal/-shm 附属文件）。\n"
            "把这个文件拷到演出机，在 ERAVJ 里导入即可。"
            % (path, st.get("songs", 0), st.get("fingerprints", 0), mb))
        self.lbl.setText("导出完成，可以发送：%s" % os.path.basename(path))

    def _on_export_fail(self, msg):
        self.btn_export.setEnabled(True)
        QMessageBox.warning(self, APP_TITLE, "导出出错：%s" % msg)


def main():
    # ★ 2026-11-05：启动日志。打包后的 exe 出现「进程活着但不出窗口」，
    #   而源码运行正常 —— 必需有日志才知道卡在 QApplication 之前还是之后。
    # ★ 2026-11-12：支持 `ERLIB_BOOT_LOG` 覆盖路径（测试时指到工作区）；
    #   并登记到 _BOOT，让 _load_existing 等数据路径也能写诊断。
    _blog = None
    _bpath = os.environ.get("ERLIB_BOOT_LOG") or os.path.join(
        os.environ.get("LOCALAPPDATA", "") or os.path.expanduser("~"),
        "AutoVJ", "erlib_boot.log")
    try:
        os.makedirs(os.path.dirname(os.path.abspath(_bpath)), exist_ok=True)
        _blog = open(_bpath, "w", encoding="utf-8")

        def _log(msg):
            try:
                _blog.write("%s  %s\n" % (time.strftime("%H:%M:%S"), msg))
                _blog.flush()
            except Exception:                                    # noqa: BLE001
                pass
    except Exception:                                            # noqa: BLE001
        def _log(msg):
            pass

    _BOOT["write"] = _log
    _BOOT["path"] = _bpath
    _log("start  frozen=%s  exe=%s  log=%s"
         % (getattr(sys, "frozen", False), sys.executable, _bpath))
    from PySide6.QtWidgets import QApplication
    _log("import QApplication OK")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    _log("QApplication OK  platform=%s" % app.platformName())
    # ★ 应用图标（仅图标相关）：窗口 + 任务栏统一用品牌 ER 标。找不到文件就跳过，
    #   绝不影响启动（打包后从 _MEIPASS/assets 取，源码运行从项目根 assets/ 取）。
    try:
        from PySide6.QtGui import QIcon
        import appicon
        _icon = appicon.icon_path("erlib")
        if _icon:
            app.setWindowIcon(QIcon(_icon))
        _log("icon OK %s" % bool(_icon))
    except Exception as e:                                       # noqa: BLE001
        _log("icon 跳过: %s" % e)
    _log("准备构造 ErlibWindow")
    w = ErlibWindow()
    _log("ErlibWindow 构造完成 %dx%d  minW=%d"
         % (w.width(), w.height(), w.minimumWidth()))
    _log("work=%s  exists=%s"
         % (getattr(w, "work", "?"), os.path.isfile(getattr(w, "work", ""))))
    w.show()
    app.processEvents()
    _log("show 完成  visible=%s  geometry=%s"
         % (w.isVisible(), w.geometry()))
    w.raise_()
    w.activateWindow()
    try:
        import sounddevice as _sd
        _log("sounddevice OK  devices=%d  default=%r"
             % (len(_sd.query_devices()), getattr(_sd.default, "device", None)))
    except Exception as e:                                       # noqa: BLE001
        _log("sounddevice 不可用: %r" % (e,))
    _log("进入事件循环")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
