# -*- coding: utf-8 -*-
"""摘除码表（第 2 步）：src/ui_main.py

要点（易漏项）：
- 页号钳位 2 → 1（老配置里 cfg["ui"]["page"] 可能存着 2，会切到已删页面）
- NewLayerDialog 返回值 5 元组 → 4 元组，必须与 panels.py 同步改
- 删掉 cue_layer_menu / cue_layer_use / open_cuesheet 三个方法（互相关联）
"""
import io
import os
import sys

P = r"P:\AutoVJ\src\ui_main.py"
PAT = []


def rep(old, new, cnt=1):
    PAT.append((old, new, cnt))


def cut(start, end):
    PAT.append(("CUT", start, end, 0))


# --- 1) 模块 docstring：三界面 → 两界面 ------------------------------------
rep("""★ 2026-09-28 起主区拆成**三界面**（`QStackedWidget`，共用同一个引擎）：
  ① 演出台     —— 图层 + 预览 + 设置 + 输出（现场用）
  ② 素材·曲库·曲风 —— 素材库独占整幅宽 + 音乐曲库 / 曲风映射入口（演出前准备）
  ③ 码表编排   —— `cue_editor.CueSheetEditor`（编曲；惰性创建）
""",
    """★ 2026-09-28 起主区拆成**两界面**（`QStackedWidget`，共用同一个引擎）：
  ① 演出台     —— 图层 + 预览 + 设置 + 输出（现场用）
  ② 素材·曲库·曲风 —— 素材库独占整幅宽 + 音乐曲库 / 曲风映射入口（演出前准备）
""")

# --- 2) 去掉 cue_editor 导入 -----------------------------------------------
rep("from cue_editor import CueSheetEditor\n", "")

# --- 3) 码表注册表 ---------------------------------------------------------
rep("""        # ★ 已导入/新建的码表清单（名字 → {sheet, media, path}）。
        #   放在这里（而不是 cue_editor 内部）是因为**演出台**也要用：
        #   点码表图层的徽标要能直接选「这个图层用哪一张」。cue_editor 登记时同步过来。
        self.cue_sheets = {}
""", "")

# --- 4) 「⋯ 更多」里的码表入口 ---------------------------------------------
rep("""        self.act_cuesheet = self._more_menu.addAction(T("码表 / 时间码…"))
        self.act_cuesheet.setToolTip(T("导入 .ercue 码表、绑定到码表图层、选位置源"))
        self.act_cuesheet.triggered.connect(self.open_cuesheet)
""", "")

# --- 5) 页签相关注释/文案 --------------------------------------------------
rep("    # ================= 三界面（演出台 / 素材·曲库·曲风 / 码表编排）=================",
    "    # ================= 两界面（演出台 / 素材·曲库·曲风）=================")
rep('        """三界面页签样式（主题感知；注册进 theme，切亮暗时重设）。"""',
    '        """两界面页签样式（主题感知；注册进 theme，切亮暗时重设）。"""')

# --- 6) _build_pages 的 docstring + 页签元组 -------------------------------
rep("""        \"\"\"★ 2026-09-28：主区拆成**三界面**（`QStackedWidget`），共用同一个引擎。

        为什么拆（用户反馈「看起来乱乱的」）：演出控制、素材整理、码表编排是
        **三个完全不同的场合**（现场 / 演出前准备 / 编曲），混在一屏里互相挤。
        拆开后每页都能占满宽度，页签常显、一键来回切。
        ⚠ 第 3 页（码表编排）**惰性创建** —— 自绘时间轴与定时器不进启动路径。
        \"\"\"""",
    """        \"\"\"★ 主区拆成**两界面**（`QStackedWidget`），共用同一个引擎。

        为什么拆（用户反馈「看起来乱乱的」）：演出控制与素材整理是**两个完全不同的
        场合**（现场 / 演出前准备），混在一屏里互相挤。拆开后每页都能占满宽度，
        页签常显、一键来回切。
        \"\"\"""")
rep("""                ("🎵 曲库 · 曲风", "演出前准备：音乐曲库（导入音乐 + 扫描分析曲风）/ 曲风映射"),
                ("🎞 码表编排", "编曲：把 .ercue 码表对到拍上，绑定码表图层"))):""",
    """                ("🎵 曲库 · 曲风", "演出前准备：音乐曲库（导入音乐 + 扫描分析曲风）/ 曲风映射"))):""")

# --- 7) 第 3 页占位 + 页号钳位 ---------------------------------------------
rep("""        # ---- 第 3 页：码表编排（先占位，首次切入才建）----
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
""",
    """        try:
            idx = int(self.cfg["ui"].get("page", 0) or 0)
        except Exception:                                         # noqa: BLE001
            idx = 0
        # ⚠ 上界是 1（两页）。老配置里可能存着 2（码表编排页，已移除）⇒ 必须钳回来，
        #   否则 setCurrentIndex(2) 会切到不存在的页，界面一片空白。
        self.switch_page(idx if 0 <= idx <= 1 else 0, save=False)
""")

# --- 8) _ensure_cue_editor 整个方法 ----------------------------------------
rep("""    def _ensure_cue_editor(self):
        \"\"\"首次切到第 3 页时才建码表编排面板（自绘时间轴 + 500ms 定时器不进启动路径）。\"\"\"
        if self.cue_editor is not None:
            return self.cue_editor
        try:
            self.cue_editor = CueSheetEditor(self, embedded=True)
            self._page3_lay.addWidget(self.cue_editor, 1)
        except Exception:                                         # noqa: BLE001
            self.cue_editor = None
        return self.cue_editor

""", "")

# --- 9) switch_page 去掉码表分支 -------------------------------------------
rep("""    def switch_page(self, i, save=True):
        \"\"\"切到三界面之一（页签条 / 「⋯ 更多」菜单 / 建完码表图层都走这里）。

        ⚠ 码表编排只在**看得见的时候**才跑 500ms 刷新（CueSheetEditor.set_active）。
        \"\"\"
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
""",
    """    def switch_page(self, i, save=True):
        \"\"\"切到两界面之一（页签条 / 「⋯ 更多」菜单都走这里）。\"\"\"
        try:
            i = max(0, min(1, int(i)))
        except Exception:                                         # noqa: BLE001
            i = 0
        self.pages.setCurrentIndex(i)
""")

# --- 10) layer_add：去掉 cue_hint 与第 5 个返回值 --------------------------
rep("""    def layer_add(self, cue_hint=False):
        default = self._next_layer_name()
        dlg = NewLayerDialog(self, default, cue_hint=cue_hint)
        if dlg.exec() != QDialog.Accepted:
            return
        name, auto, role, is_kv, is_cue = dlg.result_data()
""",
    """    def layer_add(self):
        default = self._next_layer_name()
        dlg = NewLayerDialog(self, default)
        if dlg.exec() != QDialog.Accepted:
            return
        name, auto, role, is_kv = dlg.result_data()
""")
rep("""        lay = Layer((name or ("Kv 主视觉" if is_kv else
                              ("码表" if is_cue else default))).strip())
        lay.auto_mode = auto
        lay.is_kv = is_kv
        # 码表图层：容器图层，内部按 code 表的 composition 渲染多层（见 engine._draw_cuesheet_layer）
        lay.is_cuesheet = bool(is_cue)
        lay.role = role
        if role == "fg" and not is_kv and not is_cue:
""",
    """        lay = Layer((name or ("Kv 主视觉" if is_kv else default)).strip())
        lay.auto_mode = auto
        lay.is_kv = is_kv
        lay.role = role
        if role == "fg" and not is_kv:
""")
rep("""        self._sync_layers()
        if is_cue:
            # 建完直接打开码表窗口，顺着"导入 → 绑定"一条路走完
            self.open_cuesheet()
""",
    """        self._sync_layers()
""")

# --- 11) 三个码表方法整块删（cue_layer_menu / cue_layer_use / open_cuesheet）---
cut("""    def cue_layer_menu(self, idx):""",
    """    def _sync_more_menu(self):""")

# --- 12) 「⋯」菜单文案 -----------------------------------------------------
rep("""            self.act_cuesheet.setText(T("码表 / 时间码…"))
            self.act_cuesheet.setToolTip(
                T("切换到「码表编排」界面：导入 .ercue、绑定码表图层、选位置源"))
            self.btn_more.setToolTip(T("更多：码表 / 主题 / 语言 / 快捷键 / 关于"))
""",
    """            self.btn_more.setToolTip(T("更多：主题 / 语言 / 快捷键 / 关于"))
""")


def main():
    s = io.open(P, encoding="utf-8").read()
    orig = s
    for i, item in enumerate(PAT, 1):
        if len(item) == 4:                            # cut: ("CUT", start, end, 0)
            _k, a, b, cnt = item
        else:
            a, b, cnt = item
        if cnt == 0:                                  # cut
            if s.count(a) != 1 or s.count(b) != 1:
                print("✗ 补丁 %d(cut) 边界不唯一 start=%d end=%d"
                      % (i, s.count(a), s.count(b)))
                return 1
            ia, ib = s.index(a), s.index(b)
            if ib <= ia:
                print("✗ 补丁 %d(cut) 顺序不对" % i)
                return 1
            s = s[:ia] + s[ib:]
        else:
            n = s.count(a)
            if n != cnt:
                print("✗ 补丁 %d 命中 %d 次（应为 %d），整批不写" % (i, n, cnt))
                print("   锚点首行：", a.splitlines()[0][:90])
                return 1
            s = s.replace(a, b)
    io.open(P, "w", encoding="utf-8", newline="\n").write(s)
    print("ui_main.py：%d → %d 行（删了 %d 行）"
          % (orig.count("\n") + 1, s.count("\n") + 1,
             orig.count("\n") - s.count("\n")))
    import re
    left = [(i + 1, l) for i, l in enumerate(s.splitlines())
            if re.search(r"cue|码表|CueSheet", l, re.I)]
    print("残留 %d 处：" % len(left))
    for ln, l in left:
        print("   %5d| %s" % (ln, l.strip()[:110]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
