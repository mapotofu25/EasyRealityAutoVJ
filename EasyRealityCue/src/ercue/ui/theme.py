# -*- coding: utf-8 -*-
"""配色与尺寸常量（`ui/` 层唯一"长什么样"的地方）。

为什么要单独一个模块：旧实现把颜色散落在各处，主题切换时要满文件找。
新项目**只保留深色一套**（编排工具，单窗口），颜色集中在 `PALETTE` 字典里，
尺寸集中在下方常量里。UI 各处一律引用本模块，**不写裸色值**（时间长了自己都不知道哪个是哪个）。

纪律：本模块**不 import 任何 Qt**（纯常量），任何 UI 模块都可以安全引用。
"""

from __future__ import annotations

# ==========================================================================
# 深色配色（仿 PR 的中性灰）
# ==========================================================================
PALETTE = {
    "bg":          "#1e1e1e",     # 窗口底色
    "panel":       "#252525",     # 面板 / 标尺底色
    "panel2":      "#2b2b2b",     # 次级面板（列表行微高亮）
    "border":      "#3a3a3a",     # 分隔线 / 网格
    "grid":        "#333333",     # 时间轴网格线
    "fg":          "#d0d0d0",     # 主文字
    "fg_dim":      "#8a8a8a",     # 次要文字 / 说明
    "accent":      "#378ADD",     # 强调（选中边框、活动项）
    "accent2":     "#5DCAA5",     # 次级强调（音频轨）
    "playhead":    "#ff4d4d",     # 播放头（红竖线）
    "missing":     "#e06666",     # 缺失素材（红）
    "select_bg":   "#2d3f52",     # 列表选中底色
    "mark_phrase": "#c08a2a",     # 乐句标记 ◆（橙）
    "mark_bar":    "#9a9a9a",     # 小节标记 ●（灰）
    "audio":       "#1f3b34",     # 音频轨带底色
}

# 片段色（按层序循环；与主题无关，纯区分用）—— 照旧实现 8 色
TRACK_COLORS = ["#378ADD", "#1D9E75", "#D85A30", "#7F77DD",
                "#EF9F27", "#D4537E", "#5DCAA5", "#888780"]

# ==========================================================================
# 尺寸常量
# ==========================================================================
GUTTER = 74          # 时间轴左侧「层名列」宽度
RULER_H = 24         # 顶部时间标尺高度
ROW_H = 56           # 每条轨道默认行高（Alt+滚轮可调）
                     # ★ 2026-10-11：34→56（用户反馈「波形在时间轴的占比加大一点」）
                     #   音频轨取这条行高 ⇒ 它直接决定波形能画多高。

ROW_H_MIN = 18.0
ROW_H_MAX = 160.0    # ★ 2026-10-11：90→160（配合上面，让想更大的人能自己调）

# 标尺自适应阈值（架构方案 §4-C5：集中在一处，写成常量便于自测核对）
RULER_LABEL_MIN_PX = 34.0     # 数字标签（`mm:ss`）至少这么宽才画
RULER_LINE_MIN_PX = 2.0       # 长竖线间距至少 2px，否则整屏条纹
RULER_BAR_MIN_PX = 7.0        # ● 小节标记
RULER_PHRASE_MIN_PX = 7.0     # ◆ 乐句标记（8 拍）
RULER_BEAT_MIN_PX = 6.0       # 普通拍短刻度

# 拖拽命中片段边缘的容差（像素）—— 拉边改时长用
EDGE_TOL = 5

# 缩放上下限（秒跨度）
VIEW_SPAN_MIN = 2.0
VIEW_SPAN_MAX = 20000.0


def c(name: str) -> str:
    """按名字取色（拿不到回落 `fg`，绝不抛）。"""
    return PALETTE.get(str(name), PALETTE["fg"])


# ==========================================================================
# 全局深色样式（QSS）
# ==========================================================================
def stylesheet() -> str:
    """返回整套深色 QSS（在主窗口 `setStyleSheet` 一次即可）。

    字体优先用「微软雅黑」（Windows 中文界面/无头渲染都更稳），拿不到系统自会回落。
    """
    p = PALETTE
    return """
    QWidget { background: %(bg)s; color: %(fg)s;
              font-family: "Microsoft YaHei", "PingFang SC", sans-serif; font-size: 12px; }
    QMainWindow::separator { background: %(border)s; }
    QPushButton { background: %(panel2)s; border: 1px solid %(border)s;
                  padding: 3px 9px; border-radius: 3px; }
    QPushButton:hover { background: %(accent)s; color: #ffffff; }
    QPushButton:disabled { color: %(fg_dim)s; background: %(panel)s; }
    QComboBox, QLineEdit, QDoubleSpinBox, QSpinBox {
        background: %(panel2)s; border: 1px solid %(border)s; border-radius: 3px; padding: 2px 4px; }
    QComboBox QAbstractItemView { background: %(panel)s; selection-background-color: %(select_bg)s; }
    QListWidget { background: %(panel)s; border: 1px solid %(border)s; }
    QListWidget::item { padding: 2px; }
    QListWidget::item:selected { background: %(select_bg)s; color: #ffffff; }
    QTabWidget::pane { border: 1px solid %(border)s; }
    QTabBar::tab { background: %(panel)s; padding: 4px 12px; border: 1px solid %(border)s;
                   border-bottom: none; }
    QTabBar::tab:selected { background: %(panel2)s; color: #ffffff; }
    QPlainTextEdit { background: %(panel)s; border: 1px solid %(border)s; }
    QGroupBox { border: 1px solid %(border)s; border-radius: 3px; margin-top: 9px; padding-top: 4px; }
    QGroupBox::title { subcontrol-origin: margin; left: 8px; color: %(fg_dim)s; }
    QSlider::groove:horizontal { height: 4px; background: %(border)s; border-radius: 2px; }
    QSlider::handle:horizontal { background: %(accent)s; width: 10px; margin: -4px 0; border-radius: 5px; }
    QSplitter::handle { background: %(border)s; }
    QCheckBox { spacing: 5px; }
    QToolTip { background: %(panel2)s; color: %(fg)s; border: 1px solid %(border)s; }
    """ % p
