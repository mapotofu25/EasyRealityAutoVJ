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


def _qss(name):
    v = VARS[name]
    return f"""
QMainWindow, QDialog {{ background: {v['bg']}; }}
QWidget {{ color: {v['text']}; }}
QMenuBar {{ background: {v['panel']}; color: {v['text']}; }}
QMenuBar::item:selected {{ background: {v['sel']}; }}
QMenu {{ background: {v['panel']}; color: {v['text']}; border: 1px solid {v['border']}; }}
QMenu::item:selected {{ background: {v['sel']}; }}
QPushButton {{ background: {v['btn']}; color: {v['text']};
               border: 1px solid {v['border']}; border-radius: 3px; padding: 4px 10px; }}
QPushButton:hover {{ background: {v['btn_hov']}; }}
QPushButton:disabled {{ color: {v['muted']}; }}
QComboBox {{ background: {v['btn']}; color: {v['text']};
             border: 1px solid {v['border']}; border-radius: 3px; padding: 2px 8px; }}
QComboBox QAbstractItemView {{ background: {v['panel']}; color: {v['text']};
             selection-background-color: {v['sel']}; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QTimeEdit {{ background: {v['row']}; color: {v['text']};
             border: 1px solid {v['border']}; border-radius: 3px; padding: 2px 4px; }}
QListWidget {{ background: {v['panel2']}; color: {v['text']};
               border: 1px solid {v['border']}; }}
QListWidget::item {{ padding: 1px; }}
QListWidget::item:selected {{ background: {v['sel']}; color: #ffffff; }}
QCheckBox, QRadioButton {{ color: {v['text']}; }}
QGroupBox {{ color: {v['text']}; border: 1px solid {v['border']}; margin-top: 6px; }}
QGroupBox::title {{ subcontrol-origin: margin; color: {v['muted']}; }}
QProgressBar {{ background: {v['panel2']}; border: none; color: {v['text']}; }}
QToolTip {{ background: {v['head']}; color: {v['text']}; border: 1px solid {v['border']}; }}
QSplitter::handle {{ background: {v['border']}; }}
QScrollBar:vertical {{ background: {v['bg']}; width: 10px; }}
QScrollBar::handle:vertical {{ background: {v['border']}; border-radius: 4px; min-height: 24px; }}
QScrollBar:horizontal {{ background: {v['bg']}; height: 10px; }}
QScrollBar::handle:horizontal {{ background: {v['border']}; border-radius: 4px; min-width: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QTabWidget::pane {{ border: 1px solid {v['border']}; }}
QTabBar::tab {{ background: {v['btn']}; color: {v['text']};
                padding: 4px 10px; border: 1px solid {v['border']}; }}
QTabBar::tab:selected {{ background: {v['sel']}; }}
QHeaderView::section {{ background: {v['head']}; color: {v['text']};
                        border: 1px solid {v['border']}; padding: 2px 4px; }}
QToolBox::tab {{ background: {v['head']}; color: {v['text']}; border: 1px solid {v['border']}; }}
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
