# -*- coding: utf-8 -*-
"""摘除码表（第 3 步）：src/panels.py

易漏项：NewLayerDialog.result_data 返回值 5 元组 → 4 元组（与 ui_main.layer_add 成对）。
"""
import io
import os
import sys

P = r"P:\AutoVJ\src\panels.py"
PAT = []


def rep(old, new, cnt=1):
    PAT.append((old, new, cnt))


# --- 1) 对话框 docstring / 构造参数 ----------------------------------------
rep('    """新建图层对话框：名称 + 类型(手动/自动) + 角色(前景/背景)。自动图层按曲风匹配画面。"""',
    '    """新建图层对话框：名称 + 类型(手动 / 自动 / Kv 主视觉) + 角色(前景/背景)。\n\n'
    '    自动图层按曲风匹配画面；Kv 主视觉是唯一的待机层。\n    """')
rep('    def __init__(self, parent=None, default_name="图层 1", cue_hint=False):',
    '    def __init__(self, parent=None, default_name="图层 1"):')

# --- 2) 去掉「码表图层」单选项 ---------------------------------------------
rep("""        self.rb_kv = QRadioButton("Kv 主视觉图层（待机层：无声音时切入）")
        self.rb_cue = QRadioButton("码表图层（按导入的码表自动切画面）")
        self.rb_manual.setChecked(True)
        if cue_hint:
            self.rb_cue.setChecked(True)
""",
    """        self.rb_kv = QRadioButton("Kv 主视觉图层（待机层：无声音时切入）")
        self.rb_manual.setChecked(True)
""")
rep("""        self._grp_type.addButton(self.rb_kv)
        self._grp_type.addButton(self.rb_cue)
""",
    """        self._grp_type.addButton(self.rb_kv)
""")
rep("""        tv.addWidget(self.rb_kv)
        tv.addWidget(self.rb_cue)
""",
    """        tv.addWidget(self.rb_kv)
""")
rep("        for rb in (self.rb_manual, self.rb_auto, self.rb_kv, self.rb_cue):",
    "        for rb in (self.rb_manual, self.rb_auto, self.rb_kv):")

# --- 3) 类型提示文案 -------------------------------------------------------
rep("""        elif self.rb_cue.isChecked():
            self._hint.setText(
                "码表图层：按导入的 .ercue 码表自动切画面（码表内可多层，可设位置/大小/混合/裁切）。\\n"
                "添加后到「⋯ 更多 → 码表 / 时间码」导入并绑定；"
                "**图层的小眼睛关掉就完全不检测位置源**。")
""", "")
rep("""            if self.rb_kv.isChecked():
                self.name_edit.setText(self.KV_DEFAULT_NAME)
            elif self.rb_cue.isChecked():
                self.name_edit.setText("码表")
            else:
                self.name_edit.setText(self._default_name)
""",
    """            if self.rb_kv.isChecked():
                self.name_edit.setText(self.KV_DEFAULT_NAME)
            else:
                self.name_edit.setText(self._default_name)
""")

# --- 4) result_data 回到 4 元组 --------------------------------------------
rep('''    def result_data(self):
        """返回 (name, auto, role, is_kv, is_cue)。

        ⚠ 2026-09-28 由 4 元组扩成 5 元组（新增码表图层）；调用点只有 ui_main.layer_add。
          改返回值必须 grep 全部调用点（项目铁律）。"""
        name = self.name_edit.text().strip()
        auto = self.rb_auto.isChecked()
        is_kv = self.rb_kv.isChecked()
        is_cue = self.rb_cue.isChecked()
        role = "fg" if self.rb_fg.isChecked() else "bg"
        return name, auto, role, is_kv, is_cue
''',
    '''    def result_data(self):
        """返回 (name, auto, role, is_kv)。调用点只有 ui_main.layer_add。

        ⚠ 改返回值必须 grep 全部调用点（项目铁律）——
          2026-09-30 曾因为"码表图层"把它扩成 5 元组，摘除码表时又改回 4 元组。"""
        name = self.name_edit.text().strip()
        auto = self.rb_auto.isChecked()
        is_kv = self.rb_kv.isChecked()
        role = "fg" if self.rb_fg.isChecked() else "bg"
        return name, auto, role, is_kv
''')

# --- 5) 码表徽标三个方法整块删 ---------------------------------------------
rep("""    # ---------------- 码表图层专属徽标 ----------------
    @staticmethod
    def _cue_badge_text(lay):
        \"\"\"徽标文字：没绑就直说「未绑定」（橙色虚线，一眼看出要处理）。\"\"\"
        sid = (getattr(lay, "cuesheet_id", "") or "").strip()
        if not sid:
            return T("码表：未绑定")
        return "🎞 " + (sid[:9] + "…" if len(sid) > 10 else sid)

    @staticmethod
    def _cue_badge_style(bound):
        if bound:
            return ("#btnCue{padding:0;border:1px solid #2f9e8f;border-radius:3px;"
                    "font-size:11px;background:#123c36;color:#7fe6d0;}"
                    "#btnCue:hover{background:#164a43;}")
        return ("#btnCue{padding:0;border:1px dashed #a8682f;border-radius:3px;"
                "font-size:11px;background:#2a1c12;color:#ffb74d;}"
                "#btnCue:hover{background:#3a2618;}")

    def _cue_badge_tip(self, lay):
        \"\"\"徽标悬停说明：绑了哪张码表 / 现在放的是哪首 / 位置源有没有在推。\"\"\"
        sid = (getattr(lay, "cuesheet_id", "") or "").strip()
        names = sorted((getattr(self.main, "cue_sheets", None) or {}).keys())
        if not sid:
            if not names:
                return T("这个码表图层还没绑定码表：点这里进「码表编排」界面导入并绑定")
            return T("点这里选「这个图层用哪一张码表」（已导入 {} 张）").format(len(names))
        lines = [T("已绑定码表：") + sid]
        try:
            st = self.main.engine.cuesheet_status() or {}
        except Exception:                                         # noqa: BLE001
            st = {}
        dk = st.get("deck") or {}
        if dk.get("title"):
            lines.append(T("正在放：") + str(dk.get("title")))
        elif dk.get("source"):
            lines.append(T("位置源：") + str(dk.get("source")))
        pos = st.get("pos_beats")
        lines.append(Tf("播放位置：{} 拍", "%.1f" % float(pos)) if pos is not None
                     else T("位置源：未启动（在编排界面选位置源后点「开始」）"))
        lines.append(T("点这里换用别的码表 / 进「码表编排」界面"))
        return "\\n".join(lines)

""", "")

# --- 6) 图层头配色：去掉青绿分支 -------------------------------------------
rep('''        """图层头样式：普通层中性边框；自动层角色色（前景橙/背景蓝）；
        Kv 主视觉图层专属紫金；**码表图层专属青绿**（与前景/背景/Kv 同款结构，一眼区分）。"""''',
    '''        """图层头样式：普通层中性边框；自动层角色色（前景橙 / 背景蓝）；
        Kv 主视觉图层专属紫金。"""''')
rep("""        elif getattr(lay, "is_cuesheet", False):
            # ★ 2026-09-28 用户要求：码表图层要有自己的颜色（青绿，与橙/蓝/紫金都不撞）
            border, bg = "#2f9e8f", "#102824"
""", "")

# --- 7) 徽标控件构建 -------------------------------------------------------
rep("""        # 码表图层专属：**码表徽标**。
        # ⚠ α（保留透明通道）／🔒（锁定画面）／混合模式 这三样对码表**容器图层**都不参与
        #   渲染（逐片段的大小/位置/混合/不透明度都在码表里设，见引擎 _draw_cuesheet_layer）
        #   ⇒ 直接藏起来，把位置让给徽标：一眼看出绑没绑码表、绑的哪一张，点一下进编排界面。
        #   这也是用户要的"码表图层不一样的 UI 显示"。
        btn_cue = None
        if getattr(lay, "is_cuesheet", False):
            for _w in (btn_alpha, btn_lock, blend):
                _w.hide()
            btn_cue = QPushButton(self._cue_badge_text(lay))
            btn_cue.setFixedSize(150, 22)
            btn_cue.setObjectName("btnCue")
            btn_cue.setProperty("_i18nDynamic", True)   # 含码表名/状态，重建
            btn_cue.setStyleSheet(self._cue_badge_style(bool(getattr(lay, "cuesheet_id", ""))))
            btn_cue.setToolTip(self._cue_badge_tip(lay))
            # ★ 用户要求：点徽标直接选「这个图层用哪一张码表」（换绑不用进编排界面）
            btn_cue.clicked.connect(lambda _c=False, i=idx: self.main.cue_layer_menu(i))
            h.addWidget(btn_cue)

""", "")
rep("""        f._btn_role = btn_role
        f._btn_cue = btn_cue
""",
    """        f._btn_role = btn_role
""")

# --- 8) refresh_header 里的徽标刷新 ----------------------------------------
rep("""            # 码表徽标（绑定状态会变：导入/绑定/解绑后都要重画）
            btn_cue = getattr(f, "_btn_cue", None)
            if btn_cue is not None:
                btn_cue.setText(self._cue_badge_text(lay))
                btn_cue.setStyleSheet(self._cue_badge_style(
                    bool(getattr(lay, "cuesheet_id", ""))))
                btn_cue.setToolTip(self._cue_badge_tip(lay))
""", "")

# --- 9) 文件尾的历史注释 ---------------------------------------------------
rep("""# ⚠ 旧的独立「码表 / 时间码」对话框（CueSheetDialog）已删除：
#   已并入 src/cue_editor.py 的 CueSheetEditor —— 用户要求「把码表管理和编排放一起」，
#   那里是「管理 + PR 式时间轴编排」合一，且内嵌素材解在包旁边（不占 C 盘）。
""", "")


def main():
    s = io.open(P, encoding="utf-8").read()
    orig = s
    for i, (a, b, cnt) in enumerate(PAT, 1):
        n = s.count(a)
        if n != cnt:
            print("✗ 补丁 %d 命中 %d 次（应为 %d），整批不写" % (i, n, cnt))
            print("   锚点首行：", a.splitlines()[0][:90])
            return 1
        s = s.replace(a, b)
    io.open(P, "w", encoding="utf-8", newline="\n").write(s)
    print("panels.py：%d → %d 行（删了 %d 行）"
          % (orig.count("\n") + 1, s.count("\n") + 1, orig.count("\n") - s.count("\n")))
    import re
    left = [(i + 1, l) for i, l in enumerate(s.splitlines())
            if re.search(r"cue|码表|CueSheet", l, re.I)]
    print("残留 %d 处：" % len(left))
    for ln, l in left:
        print("   %5d| %s" % (ln, l.strip()[:110]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
