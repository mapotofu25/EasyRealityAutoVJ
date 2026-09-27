# -*- coding: utf-8 -*-
"""主题管理：亮/暗双主题，QPalette + 全局 QSS。

用 palette 覆盖大部分控件配色（Fusion 风格遵守 palette），全局 QSS 处理
palette 覆盖不到的细节（菜单、滚动条、tooltip、进度条等）。
个别控件有 widget 级 setStyleSheet（优先级高于全局 QSS），这些样式里引用
V() 变量，并通过 register() 注册刷新回调——切换主题时逐个重设样式。
"""
_current = "dark"
_refreshers = []          # [(owner, callable)] 切换主题时调用重设 widget 级样式

# 颜色变量（两套）
VARS = {
    "dark": {
        "bg":      "#1e1e1e",   # 窗口背景
        "panel":   "#252525",   # 面板背景
        "panel2":  "#2e2e2e",   # 列表/更深背景
        "row":     "#141414",   # 图层行等深底
        "head":    "#2d2d2d",   # 区块头
        "btn":     "#242424",   # 普通按钮
        "btn_hov": "#2f2f2f",
        "border":  "#3a3a3a",
        "text":    "#e8e8e8",
        "muted":   "#9a9a9a",
        "sel":     "#1f3f5f",   # 选中/高亮底
        "sel_bg":  "#2e7d32",
        "sel_text": "#ffffff",  # 选中项文字色（⚠ 亮色主题必须换深色，否则白字浅蓝底读不出来）
        "accent":  "#4a9eff",   # 强调色：勾选框选中 / 滑杆 / 进度等
    },
    "light": {
        "bg":      "#f2f2f2",
        "panel":   "#ffffff",
        "panel2":  "#ececec",
        "row":     "#fafafa",
        "head":    "#e2e2e2",
        "btn":     "#e8e8e8",
        "btn_hov": "#dcdcdc",
        "border":  "#c4c4c4",
        "text":    "#222222",
        "muted":   "#666666",
        "sel":     "#cfe3f7",
        "sel_bg":  "#2e7d32",
        "sel_text": "#1a1a1a",
        "accent":  "#2f7fd8",
    },
}


def current():
    return _current


def V(name):
    """当前主题的颜色变量（供 widget 级样式 f-string 引用）"""
    return VARS[_current][name]


def register(owner, fn):
    """注册 widget 级样式刷新回调（切换主题时调用）。owner 存弱引用防泄漏。"""
    import weakref
    _refreshers.append((weakref.ref(owner), fn))


def _run_refreshers():
    dead = []
    for ref, fn in _refreshers:
        w = ref()
        if w is None:
            dead.append((ref, fn))
            continue
        try:
            fn()
        except RuntimeError:
            dead.append((ref, fn))   # C++ 对象已删
    for d in dead:
        try:
            _refreshers.remove(d)
        except ValueError:
            pass


def _palette(name):
    from PySide6.QtGui import QPalette, QColor
    v = VARS[name]
    p = QPalette()
    def c(x):
        return QColor(v[x])
    p.setColor(QPalette.Window, c("bg"))
    p.setColor(QPalette.WindowText, c("text"))
    p.setColor(QPalette.Base, c("row"))
    p.setColor(QPalette.AlternateBase, c("panel2"))
    p.setColor(QPalette.Text, c("text"))
    p.setColor(QPalette.Button, c("btn"))
    p.setColor(QPalette.ButtonText, c("text"))
    p.setColor(QPalette.ToolTipBase, c("head"))
    p.setColor(QPalette.ToolTipText, c("text"))
    p.setColor(QPalette.Highlight, QColor("#2e7d32"))
    p.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    p.setColor(QPalette.PlaceholderText, c("muted"))
    for grp in (QPalette.Disabled,):
        p.setColor(grp, QPalette.WindowText, c("muted"))
        p.setColor(grp, QPalette.Text, c("muted"))
        p.setColor(grp, QPalette.ButtonText, c("muted"))
    return p


def danger_icon(size=11):
    """危险动作的小红点图标（删除类菜单项用，2026-09-27）。

    Qt 的 QAction **没法直接设文字颜色**，所以用一个「红点」前缀把删除类操作标出来，
    免得它和相邻的普通操作混在一起被看错（图层菜单里"删除图层"就挨着"重置本层设置"）。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    try:
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#e24b4a" if _current == "dark" else "#c62828"))
        p.drawEllipse(1, 1, size - 2, size - 2)
    finally:
        p.end()
    return QIcon(pm)


def _qss(name):
    """全局 QSS。

    ★ 2026-09-27 视觉统一（用户："看起来乱乱的"）：原来只覆盖了最基本的几类控件，
      于是「控件高度不一、圆角/内边距各不相同、勾选框和滑杆是系统默认样、菜单项挤在一起」
      这些不一致全都露在脸上。这里统一给出：
        · **统一控件高度**（表单类 24px；设置里的按钮 26px；工具条上的大按钮由代码
          `setMinimumHeight(32)` 覆盖 ⇒ 现场按钮仍更大好点，配置区更紧凑）；
        · 统一圆角 5px / 内边距 / hover 态；
        · 补齐原来没样式的：勾选框指示器、水平滑杆、菜单项、下拉箭头、微调按钮、
          分组框（标题压线问题）、滚动条 hover、工具提示内边距。
      ⚠ 各控件的 **widget 级 setStyleSheet 优先级更高**（色板、图层头等），不会被这里覆盖。
      ⚠ 写 QSS 时 f-string 里的花括号必须双写 `{{ }}`。
    """
    v = VARS[name]
    return f"""
QMainWindow, QDialog {{ background: {v['bg']}; }}
QWidget {{ color: {v['text']}; }}

QMenuBar {{ background: {v['panel']}; color: {v['text']}; }}
QMenuBar::item {{ padding: 3px 10px; }}
QMenuBar::item:selected {{ background: {v['sel']}; }}
QMenu {{ background: {v['panel']}; color: {v['text']};
         border: 1px solid {v['border']}; border-radius: 5px; padding: 4px; }}
QMenu::item {{ padding: 5px 26px 5px 22px; border-radius: 4px; }}
QMenu::item:selected {{ background: {v['sel']}; color: {v['sel_text']}; }}
QMenu::item:disabled {{ color: {v['muted']}; }}
QMenu::separator {{ height: 1px; background: {v['border']}; margin: 4px 8px; }}
QMenu::indicator {{ width: 14px; height: 14px; }}

QPushButton {{ background: {v['btn']}; color: {v['text']};
               border: 1px solid {v['border']}; border-radius: 5px; padding: 4px 10px;
               min-height: 26px; }}
QPushButton:hover {{ background: {v['btn_hov']}; }}
QPushButton:pressed {{ background: {v['sel']}; }}
QPushButton:disabled {{ color: {v['muted']}; }}

QComboBox {{ background: {v['btn']}; color: {v['text']};
             border: 1px solid {v['border']}; border-radius: 5px;
             padding: 2px 6px; min-height: 24px; }}
QComboBox:hover {{ border-color: {v['muted']}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox::down-arrow {{ width: 8px; height: 8px; }}
QComboBox QAbstractItemView {{ background: {v['panel']}; color: {v['text']};
             border: 1px solid {v['border']};
             selection-background-color: {v['sel']}; selection-color: {v['sel_text']}; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QTimeEdit {{ background: {v['row']}; color: {v['text']};
             border: 1px solid {v['border']}; border-radius: 5px;
             padding: 2px 6px; min-height: 24px; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {v['accent']}; }}
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {{ width: 15px; background: {v['btn']};
             border: none; border-left: 1px solid {v['border']}; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {v['btn_hov']}; }}

QCheckBox, QRadioButton {{ color: {v['text']}; spacing: 6px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 16px; height: 16px;
             border: 1px solid {v['border']}; border-radius: 4px; background: {v['row']}; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {v['accent']}; }}
/* ★ 用户反馈（2026-09-27）：选中态**不要整块填满** —— 要「里面的色块比外轮廓小一圈」。
   做法：用 3px 边框把填色往里缩，外轮廓仍是 border 色，中间露出 accent 方块。 */
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
             background: {v['accent']}; border: 3px solid {v['border']}; }}
QCheckBox::indicator:checked:hover, QRadioButton::indicator:checked:hover {{
             border-color: {v['accent']}; }}
QRadioButton::indicator, QRadioButton::indicator:checked {{ border-radius: 8px; }}

QSlider::groove:horizontal {{ height: 4px; background: {v['panel2']}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {v['accent']}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 12px; margin: -5px 0; border-radius: 6px;
             background: {v['accent']}; }}
QSlider::handle:horizontal:hover {{ background: {v['text']}; }}

QListWidget {{ background: {v['panel2']}; color: {v['text']};
               border: 1px solid {v['border']}; border-radius: 5px; }}
QListWidget::item {{ padding: 3px 4px; border-radius: 4px; }}
QListWidget::item:selected {{ background: {v['sel']}; color: {v['sel_text']}; }}
QListWidget::item:hover {{ background: {v['btn_hov']}; }}

QGroupBox {{ color: {v['text']}; border: 1px solid {v['border']};
             border-radius: 6px; margin-top: 8px; padding-top: 6px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; color: {v['muted']}; }}

QProgressBar {{ background: {v['panel2']}; border: none; color: {v['text']}; }}
QToolTip {{ background: {v['head']}; color: {v['text']}; border: 1px solid {v['border']};
            border-radius: 5px; padding: 5px 8px; }}

QSplitter::handle {{ background: {v['border']}; }}
QSplitter::handle:horizontal {{ width: 4px; }}
QSplitter::handle:vertical {{ height: 4px; }}
QSplitter::handle:hover {{ background: {v['accent']}; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {v['border']}; border-radius: 4px; min-height: 24px;
                               margin: 2px; }}
QScrollBar::handle:vertical:hover {{ background: {v['muted']}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {v['border']}; border-radius: 4px; min-width: 24px;
                                 margin: 2px; }}
QScrollBar::handle:horizontal:hover {{ background: {v['muted']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QTabWidget::pane {{ border: 1px solid {v['border']}; border-radius: 5px; }}
QTabBar::tab {{ background: {v['btn']}; color: {v['text']};
                padding: 5px 12px; border: 1px solid {v['border']};
                border-top-left-radius: 5px; border-top-right-radius: 5px; margin-right: 2px; }}
QTabBar::tab:hover {{ background: {v['btn_hov']}; }}
QTabBar::tab:selected {{ background: {v['sel']}; color: {v['sel_text']}; }}

QHeaderView::section {{ background: {v['head']}; color: {v['text']};
                        border: 1px solid {v['border']}; padding: 3px 6px; }}
QToolBox::tab {{ background: {v['head']}; color: {v['text']}; border: 1px solid {v['border']};
                 border-radius: 5px; padding: 4px 8px; }}
"""


def apply(app, name=None):
    """应用主题（app 级 palette + QSS）并重设已注册的 widget 级样式。持久化由调用方负责。"""
    global _current
    if name not in VARS:
        name = "dark"
    _current = name
    app.setPalette(_palette(name))
    app.setStyleSheet(_qss(name))
    _run_refreshers()


def toggle(app):
    apply(app, "light" if _current == "dark" else "dark")
    return _current
