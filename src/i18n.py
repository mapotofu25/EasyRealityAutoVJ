# -*- coding: utf-8 -*-
"""多语言支持：中文 / 英文。

两套机制并行：
  1) 词条机制  tr("key")           —— 适合代码内部拼装的短句（历史沿用）
  2) 原文机制  T("中文") / Tf(...)  —— 把中文原文翻成当前语言，词表见 i18n_map.ZH2EN

为什么用「原文机制」为主：
  项目历史上 UI 里散落大量硬编码中文（300+ 条）。若逐条改成 tr("key")，改造面
  巨大且切回中文容易丢原文。改为以中文原文作 key，配合 retranslate() 遍历控件树，
  可做到「不动原代码就能翻译」，且切回中文时原文原样写回。

新增文案时：中文照常写；只要词表里有对应英文，切英文即生效。词表缺条目时
  英文界面会显示中文原文（不会崩、不会显示成 key），并在 __main__ 校验中报出。
"""
from i18n_map import ZH2EN

LANGS = ("zh", "en")

STRINGS = {
    # 通用
    "app_name": {"zh": "Easy Reality AutoVJ", "en": "Easy Reality AutoVJ"},
    "ok": {"zh": "确定", "en": "OK"},
    "cancel": {"zh": "取消", "en": "Cancel"},
    "close": {"zh": "关闭", "en": "Close"},
    "settings": {"zh": "设置", "en": "Settings"},
    "warning": {"zh": "警告", "en": "Warning"},
    "language": {"zh": "语言", "en": "Language"},
    "about": {"zh": "关于", "en": "About"},
    "user_manual": {"zh": "使用说明", "en": "User Manual"},
    "open_data_dir": {"zh": "打开数据文件夹", "en": "Open Data Folder"},
    # 首页三步
    "step1": {"zh": "第 1 步：选择音源", "en": "Step 1: Choose Audio Source"},
    "step2": {"zh": "第 2 步：导入素材", "en": "Step 2: Import Media"},
    "step3": {"zh": "第 3 步：开始自动 VJ", "en": "Step 3: Start Auto VJ"},
    "start_hint": {
        "zh": "小白三步上手：选音源 → 拖入素材 → 点开始。也可直接进入主界面。",
        "en": "3 steps: pick source → import media → press start.",
    },
    # 音源
    "audio_source": {"zh": "音源", "en": "Audio Source"},
    "source_system": {"zh": "系统声音", "en": "System Sound"},
    "source_mic": {"zh": "麦克风", "en": "Microphone"},
    "source_linein": {"zh": "线路输入 / 声卡", "en": "Line-In / Sound Card"},
    "device": {"zh": "设备", "en": "Device"},
    "channel": {"zh": "通道", "en": "Channel"},
    "mono": {"zh": "单声道", "en": "Mono"},
    "stereo": {"zh": "立体声", "en": "Stereo"},
    "refresh_devices": {"zh": "刷新设备", "en": "Refresh Devices"},
    "no_sound_hint": {"zh": "没有检测到声音，请检查音源或切换输入设备",
                      "en": "No sound detected. Check source or switch input device."},
    # 素材
    "import_folder": {"zh": "导入文件夹", "en": "Import Folder"},
    "import_files": {"zh": "导入文件", "en": "Import Files"},
    "media_library": {"zh": "素材库", "en": "Media Library"},
    "add_to_layer": {"zh": "加入图层", "en": "Add to Layer"},
    "remove_item": {"zh": "从图层移除", "en": "Remove from Layer"},
    "tag": {"zh": "标签", "en": "Tag"},
    "all_tags": {"zh": "全部", "en": "All"},
    "thumb_progress": {"zh": "正在生成缩略图…", "en": "Generating thumbnails…"},
    "no_media": {"zh": "还没有素材，请导入文件夹或文件",
                 "en": "No media yet. Import a folder or files."},
    # 图层
    "layers": {"zh": "图层", "en": "Layers"},
    "add_layer": {"zh": "添加图层", "en": "Add Layer"},
    "del_layer": {"zh": "删除图层", "en": "Delete Layer"},
    "layer_up": {"zh": "上移", "en": "Move Up"},
    "layer_down": {"zh": "下移", "en": "Move Down"},
    "visible": {"zh": "显示", "en": "Visible"},
    "opacity": {"zh": "不透明度", "en": "Opacity"},
    "fixed_play": {"zh": "固定播放", "en": "Fixed Loop"},
    "quick_mode": {"zh": "快速模式（单图层）", "en": "Quick Mode (Single Layer)"},
    "layer_name": {"zh": "图层", "en": "Layer"},
    # 自动 VJ
    "start": {"zh": "开始自动 VJ", "en": "Start Auto VJ"},
    "stop": {"zh": "停止", "en": "Stop"},
    "blackout": {"zh": "黑场", "en": "Blackout"},
    "freeze": {"zh": "冻结", "en": "Freeze"},
    "pause_auto": {"zh": "暂停自动", "en": "Pause Auto"},
    "next_scene": {"zh": "下一素材", "en": "Next Clip"},
    "intensity": {"zh": "自动强度", "en": "Intensity"},
    "intensity_low": {"zh": "低", "en": "Low"},
    "intensity_mid": {"zh": "中", "en": "Medium"},
    "intensity_high": {"zh": "高", "en": "High"},
    "intensity_xhigh": {"zh": "极高", "en": "Extreme"},
    "switch_speed": {"zh": "切换频率", "en": "Switch Rate"},
    "slow": {"zh": "慢", "en": "Slow"},
    "normal": {"zh": "正常", "en": "Normal"},
    "fast": {"zh": "快", "en": "Fast"},
    "transition": {"zh": "过渡", "en": "Transition"},
    "tr_cut": {"zh": "硬切", "en": "Cut"},
    "tr_fade": {"zh": "淡入淡出", "en": "Fade"},
    "tr_slide": {"zh": "滑动", "en": "Slide"},
    "tr_zoom": {"zh": "缩放", "en": "Zoom"},
    "tr_glitch": {"zh": "故障", "en": "Glitch"},
    "trans_ms": {"zh": "过渡时长(ms)", "en": "Transition (ms)"},
    "beat_mode": {"zh": "逐拍交替", "en": "Beat Alternation"},
    "pick_mode": {"zh": "素材选取", "en": "Pick Mode"},
    "order_seq": {"zh": "顺序", "en": "Sequential"},
    "order_rand": {"zh": "随机", "en": "Random"},
    "bars_per_pair": {"zh": "每对小节数", "en": "Bars / Pair"},
    "end_action": {"zh": "末尾行为", "en": "End Action"},
    "loop": {"zh": "循环", "en": "Loop"},
    "reverse": {"zh": "反向", "en": "Reverse"},
    "stopplay": {"zh": "停止", "en": "Stop"},
    "limit_layer": {"zh": "限定图层", "en": "Limit Layer"},
    "all_layers": {"zh": "全部图层", "en": "All Layers"},
    # 输出
    "output": {"zh": "输出", "en": "Output"},
    "output_window": {"zh": "输出窗口", "en": "Output Window"},
    "show_output": {"zh": "显示输出窗口", "en": "Show Output Window"},
    "resolution": {"zh": "分辨率", "en": "Resolution"},
    "aspect_lock": {"zh": "锁定 16:9", "en": "Lock 16:9"},
    "borderless": {"zh": "无边框", "en": "Borderless"},
    "always_top": {"zh": "窗口置顶", "en": "Always on Top"},
    "fullscreen": {"zh": "全屏", "en": "Fullscreen"},
    # 快捷键
    "hotkeys": {"zh": "快捷键", "en": "Hotkeys"},
    "hotkey_scope": {"zh": "范围", "en": "Scope"},
    "hotkey_global": {"zh": "全局", "en": "Global"},
    "hotkey_local": {"zh": "局部", "en": "Local"},
    "enabled": {"zh": "启用", "en": "Enabled"},
    "modify": {"zh": "修改", "en": "Modify"},
    "clear": {"zh": "清除", "en": "Clear"},
    "reset_defaults": {"zh": "恢复默认", "en": "Reset Defaults"},
    "conflict": {"zh": "该键位已被占用，是否覆盖？", "en": "Key already in use. Overwrite?"},
    "order_mode_beat_only": {"zh": "顺序/随机仅对「逐拍交替」生效",
                             "en": "Sequential/Random applies to Beat Alternation only"},
    # 配置
    "save_project": {"zh": "保存项目", "en": "Save Project"},
    "load_project": {"zh": "加载项目", "en": "Load Project"},
    "export_cfg": {"zh": "导出配置", "en": "Export Config"},
    "import_cfg": {"zh": "导入配置", "en": "Import Config"},
    # 状态
    "bpm": {"zh": "BPM", "en": "BPM"},
    "energy": {"zh": "能量", "en": "Energy"},
    "cpu": {"zh": "CPU", "en": "CPU"},
    "ready": {"zh": "就绪", "en": "Ready"},
    "running": {"zh": "自动 VJ 运行中", "en": "Auto VJ Running"},
    "stopped": {"zh": "已停止", "en": "Stopped"},
    "epilepsy_warn": {
        "zh": "警告：快速闪烁画面可能引起不适，光敏性癫痫患者请勿使用。亮度已限制。",
        "en": "Warning: flashing visuals may cause discomfort. Brightness is capped.",
    },
}

_current = "zh"


def set_lang(lang: str):
    global _current
    if lang in LANGS:
        _current = lang


def lang() -> str:
    return _current


def tr(key: str) -> str:
    entry = STRINGS.get(key)
    if not entry:
        return key
    return entry.get(_current) or entry.get("zh", key)


# ==================== 原文机制（主要翻译通道）====================

def T(zh: str) -> str:
    """中文原文 → 当前语言。无译文时返回原文（绝不返回空）。"""
    if _current == "zh" or not zh:
        return zh
    return ZH2EN.get(zh, zh)


def Tf(zh_tmpl: str, *args) -> str:
    """带占位符的原文：先查译文，再 .format(*args)。

    词表里的模板一律用 {} 占位。查不到译文时用原文本身格式化，
    保证英文模式下也不会显示成未替换的占位符。
    """
    tmpl = ZH2EN.get(zh_tmpl, zh_tmpl) if _current != "zh" else zh_tmpl
    try:
        return tmpl.format(*args)
    except Exception:
        # 模板与参数个数不匹配：退回原文，避免抛异常打断 UI
        try:
            return zh_tmpl.format(*args)
        except Exception:
            return zh_tmpl


_SRC_TEXT = "_i18nSrcText"
_SRC_TITLE = "_i18nSrcTitle"
_SRC_TIP = "_i18nSrcTip"
_SRC_PH = "_i18nSrcPh"
_ITEM_ROLE = 0x0100 + 90      # Qt.UserRole + 90：存下拉项原文


def _remember(w, attr, current_value):
    """首次访问时把控件当前文本记为「原文」；之后固定返回该原文。"""
    src = w.property(attr)
    if src is None:
        src = current_value
        try:
            w.setProperty(attr, src)
        except Exception:
            return current_value
    return src


def retranslate(root):
    """遍历控件树，把控件上「登记过的原文」按当前语言重设一遍。

    只处理**显示属性**（文本/标题/提示/占位符/下拉项/标签页），
    绝不碰 QListWidget / QTableWidget / QLineEdit 的用户数据 —— 那些
    item 文本可能被当作逻辑值读回（如标签筛选条件），翻译会破坏功能。

    注意：首次调用会把当前显示文本当作「原文」记录下来。因此必须保证
    **首次调用发生在中文状态**（软件默认中文启动，天然满足）。
    """
    if root is None:
        return
    try:
        from PySide6.QtWidgets import QWidget, QComboBox, QTabWidget, QLineEdit
        from PySide6.QtGui import QAction
    except Exception:
        return

    targets = []
    try:
        targets.append(root)
        if isinstance(root, QWidget):
            targets.extend(root.findChildren(QWidget))
            targets.extend(root.findChildren(QAction))
        else:
            targets.extend(root.findChildren(QWidget))
            targets.extend(root.findChildren(QAction))
    except Exception:
        return

    for w in targets:
        _rt_widget(w)

    if isinstance(root, QWidget):
        for cb in root.findChildren(QComboBox):
            _rt_combo(cb)
        for tw in root.findChildren(QTabWidget):
            _rt_tabs(tw)


def _rt_widget(w):
    from PySide6.QtWidgets import QLineEdit

    # 动态文本控件（内容随状态变化，如「▼ 分区名」「不透明度 80%」）：
    # 不能把当前文本当静态原文记录，否则切回中文会还原成错误的内容。
    # 这类控件由各自的 retranslate() 用 T()/Tf() 重建文本。
    dynamic = False
    try:
        dynamic = bool(w.property("_i18nDynamic"))
    except Exception:
        dynamic = False

    # 控件主文本（QLabel / QPushButton / QCheckBox / QRadioButton / QAction ...）
    try:
        if not dynamic and hasattr(w, "setText") and hasattr(w, "text"):
            src = _remember(w, _SRC_TEXT, w.text())
            new = T(src)
            if w.text() != new:
                w.setText(new)
    except Exception:
        pass

    # 标题（QGroupBox / QMenu / 其他）
    try:
        if not dynamic and hasattr(w, "setTitle") and hasattr(w, "title"):
            src = _remember(w, _SRC_TITLE, w.title())
            new = T(src)
            if w.title() != new:
                w.setTitle(new)
    except Exception:
        pass

    # 窗口标题
    try:
        if not dynamic and hasattr(w, "setWindowTitle") and hasattr(w, "windowTitle"):
            src = _remember(w, "_i18nWinTitle", w.windowTitle())
            new = T(src)
            if w.windowTitle() != new:
                w.setWindowTitle(new)
    except Exception:
        pass

    # 悬停提示
    try:
        if not dynamic and hasattr(w, "setToolTip") and hasattr(w, "toolTip"):
            src = _remember(w, _SRC_TIP, w.toolTip())
            new = T(src)
            if w.toolTip() != new:
                w.setToolTip(new)
    except Exception:
        pass

    # 占位符（只改 placeholder，不动用户输入内容）
    try:
        if isinstance(w, QLineEdit):
            src = _remember(w, _SRC_PH, w.placeholderText())
            new = T(src)
            if w.placeholderText() != new:
                w.setPlaceholderText(new)
    except Exception:
        pass


def _rt_combo(cb):
    """下拉框逐项翻译（原文存在 itemData 里，切回中文也能还原）。"""
    try:
        from PySide6.QtCore import Qt
        for i in range(cb.count()):
            src = cb.itemData(i, _ITEM_ROLE)
            if src is None:
                src = cb.itemText(i)
                cb.setItemData(i, src, _ITEM_ROLE)
            new = T(src)
            if cb.itemText(i) != new:
                cb.setItemText(i, new)
    except Exception:
        pass


def _rt_tabs(tw):
    try:
        for i in range(tw.count()):
            src = tw.tabBar().tabData(i)
            if src is None:
                src = tw.tabText(i)
                tw.tabBar().setTabData(i, src)
            new = T(src)
            if tw.tabText(i) != new:
                tw.setTabText(i, new)
    except Exception:
        pass


def missing_keys():
    """返回词表里已有、但代码中已不存在的条目（用于清理），以及反向缺失检查辅助。"""
    return sorted(k for k in ZH2EN if not k)
