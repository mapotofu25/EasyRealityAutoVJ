# -*- coding: utf-8 -*-
"""待办清单 高-3/4/5 + 中-1/2/3/5。

- ui_main._update_preview 里还留着「每 6 帧刷一次播放高亮」的旧通路
  （高亮早已交给独立的 1 秒定时器）⇒ 删掉，省掉每 6 帧一次整表遍历。
  删完 `_hl_tick` 就成了死变量，一并删。
- ui_main.set_resolution_text 里的 `_loading_ui` 守卫：全库没有任何处置 True
  ⇒ 恒不生效的死守卫，删掉（行为与现在**完全一致**）。
- ui_main.rescan_music docstring 还在说「重算段落」——段落分析早已移除。
- ui_main 输出面板「重置窗口大小」的 tooltip 与实际行为不符，且没走 T()（英文界面还是中文）。
- panels.result_data 的长历史注释压短。
- i18n.missing_keys() 实现是错的（`sorted(k for k in ZH2EN if not k)` 恒为空）且无调用方
  ⇒ 删掉，真正的孤儿词条检查由 tools/i18n_coverage.py 负责。
- tagger.analyze_media 用 locals() 反射取分支内局部变量 ⇒ 改成显式初始化。
"""
import io
import os
import sys

ROOT = r"P:\AutoVJ"
PAT = []


def rep(rel, old, new, cnt=1):
    PAT.append((rel, old, new, cnt))


# ---------- 高-3：删掉旧的"每 6 帧刷高亮"通路 ----------
rep("src/ui_main.py",
    """        self.preview_panel.set_frame(img)
        self._hl_tick += 1
        if self._hl_tick % 6 == 0:      # ~10fps 刷新高亮，省 CPU
            self.layers_panel.refresh_playing()
""",
    """        self.preview_panel.set_frame(img)
        # ⚠ 播放高亮**不在这里刷**：已交给独立的 1 秒定时器（`self._hl_timer`）。
        #   这条旧通路是"每 6 帧 ≈10fps 重建整张高亮表"，而预览帧在换页/隐藏时根本不来
        #   ⇒ 既白烧 CPU（图层多时每轮遍历所有格子）又不可靠。别再搬回来。
""")
rep("src/ui_main.py", "        self._hl_tick = 0\n", "")

# ---------- 高-4：删掉恒不生效的 _loading_ui 守卫 ----------
rep("src/ui_main.py",
    """        import re
        if getattr(self, "_loading_ui", False):
            return
        m = re.search(r"(\\d{2,5})\\s*[xX\\u00d7*\\s]\\s*(\\d{2,5})", str(text or ""))""",
    """        import re
        # ⚠ 这里原来有个 `if getattr(self, "_loading_ui", False): return` 守卫，但全库
        #   没有任何一处把它置 True ⇒ 恒不生效的死守卫，删掉。程序化回填时若要抑制，
        #   正确做法是 `blockSignals(True/False)`（见 _load_settings_to_ui 里其它控件的写法）。
        m = re.search(r"(\\d{2,5})\\s*[xX\\u00d7*\\s]\\s*(\\d{2,5})", str(text or ""))""")

# ---------- 高-5：docstring 去掉"重算段落" ----------
rep("src/ui_main.py",
    '''        """重新分析扫描单首曲目（重建指纹 + 重查曲风 + 重算段落）"""''',
    '''        """重新分析扫描单首曲目（重建指纹 + 重查曲风 + 重算节拍网格）。

        段落分析已移除（见 `_music_scan` 的说明），别再写「重算段落」。"""''')

# ---------- 中-1：tooltip 与实际行为对齐 + 走 T() ----------
rep("src/ui_main.py",
    '        self.btn_reset_win.setToolTip("把输出窗口按当前分辨率设置重置（退出全屏/按比例缩放到屏幕内）")',
    '        # ⚠ 文案必须与 output_window.apply_window_size 的实际行为一致：它是**严格按输出\n'
    '        #   分辨率**重置窗口大小（不再做"缩到屏幕 80%"那种改动）。\n'
    '        self.btn_reset_win.setToolTip(T("退出全屏并按输出分辨率重置窗口大小"))')

# ---------- 中-2：panels 历史注释压短 ----------
rep("src/panels.py",
    '''        """返回 (name, auto, role, is_kv)。调用点只有 ui_main.layer_add。

        ⚠ 改返回值必须 grep 全部调用点（项目铁律）——
          2026-09-30 曾因为"码表图层"把它扩成 5 元组，摘除码表时又改回 4 元组。"""''',
    '''        """返回 (name, auto, role, is_kv)。唯一调用点：ui_main.layer_add。

        ⚠ 改返回值元数必须 grep 全部调用点（项目铁律）。"""''')

# ---------- 中-3：删掉实现有误且无人调用的 missing_keys ----------
rep("src/i18n.py",
    '''

def missing_keys():
    """返回词表里已有、但代码中已不存在的条目（用于清理），以及反向缺失检查辅助。"""
    return sorted(k for k in ZH2EN if not k)''', "")

# ---------- 中-5：tagger 显式初始化 has_alpha ----------
rep("src/tagger.py",
    """        motion, flicker, _diff = self.analyze_dynamic(path, kind)
        if kind == "image":""",
    """        motion, flicker, _diff = self.analyze_dynamic(path, kind)
        # ⚠ 显式初始化：视频分支不会赋值，以前靠 `locals().get("has_alpha", False)`
        #   反射当前帧的局部变量 —— 重构一次就错，且读代码的人根本看不出这里有赋值。
        has_alpha = False
        if kind == "image":""")
rep("src/tagger.py",
    '        alpha = bool(locals().get("has_alpha", False))',
    "        alpha = bool(has_alpha)")


def main():
    # 先整体校验锚点，再落盘
    for i, (rel, old, new, cnt) in enumerate(PAT, 1):
        s = io.open(os.path.join(ROOT, rel), encoding="utf-8").read()
        n = s.count(old)
        if n != cnt:
            print("✗ 第 %d 处（%s）命中 %d 次（应为 %d）" % (i, rel, n, cnt))
            print("   锚点首行：", old.splitlines()[0][:90])
            return 1
    for rel, old, new, cnt in PAT:
        p = os.path.join(ROOT, rel)
        s = io.open(p, encoding="utf-8").read()
        io.open(p, "w", encoding="utf-8", newline="\n").write(s.replace(old, new))
        print("✓", rel, "|", old.strip().splitlines()[0][:56])
    import py_compile
    for f in ("src/ui_main.py", "src/panels.py", "src/i18n.py", "src/tagger.py"):
        py_compile.compile(os.path.join(ROOT, f), doraise=True)
    print("语法 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
