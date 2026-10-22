# -*- coding: utf-8 -*-
"""补丁 2：① 撤回 theme 的 ::item 改动（实测证伪：app 级样式表并不影响 item 底色）
          ② 播放高亮改成**按素材路径**比对，并加"播放器兜底"

为什么改比对方式：原实现是
    playing = {lay.clips[lay.cur]}          # MediaItem 对象
    if it.data(Qt.UserRole) in playing:     # 对象身份比对
只要 `lay.clips` 被整体替换（自动图层每次重算素材池都会 `lay.clips = list(picked)`）
或行里的项被重建，**身份就对不上 ⇒ 一个都不亮**。改比路径后，无论对象怎么重建都亮。
再加一层兜底：引擎停止 / 处于"待切换"（`cur == -1`）时，用图层**播放器正在解的那个文件**
来高亮 —— 用户要的是"正在使用的素材"的底色，而不是"引擎此刻的索引"。
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATCHES = []


def patch(rel, old, new):
    PATCHES.append((rel, old, new))


# ---------------------------------------------------------------- ① 撤回 theme
patch("src/theme.py",
      """/* 这里**不能**写基态 `QListWidget::item` 规则：只要存在任何 ::item 规则，
   Qt 就改用样式表画列表项背景，`item.setBackground()` 会被整个忽略
   ⇒ 演出台「正在使用的素材」金黄打底、码表徽标底色统统消失。
   实测见 tools/_hl_qss_test.py（带这条规则 #2e2e2e，删掉就恢复金黄）。
   代价只是列表项少了 3px/4px 内边距；:hover / :selected 仍然保留。 */
""",
      "QListWidget::item {{ padding: 3px 4px; border-radius: 4px; }}\n")

# ---------------------------------------------------------------- ② 高亮改按路径
patch("src/panels.py",
      """    def refresh_playing(self):
        for i, (_, grid) in enumerate(self.rows):
            if i >= len(self.main.engine.layers):
                continue
            lay = self.main.engine.layers[i]
            playing = set()
            if 0 <= lay.cur < len(lay.clips):
                playing.add(lay.clips[lay.cur])
            if lay.trans_active and 0 <= lay.prev_idx < len(lay.clips):
                playing.add(lay.clips[lay.prev_idx])
            for k in range(grid.count()):
                it = grid.item(k)
                m = it.data(Qt.UserRole)
                if m in playing:
""",
      """    def refresh_playing(self):
        \"\"\"给"正在使用的素材"打金黄底。

        ⚠⚠ **必须按素材路径比对，不能按对象身份比对**（2026-09-29 用户报"底色没了"）：
          自动图层每次重算素材池都会 `lay.clips = list(picked)`（engine._refresh_auto_layers），
          `sync_layer()` 又把行里的项整批重建 ⇒ 行里存的 MediaItem 和 `lay.clips` 里的
          已经不是同一批对象，`m in playing` 永远为假 ⇒ **一个格子都不亮**。
          换成路径比对后，对象怎么重建都能对上。

        ⚠ 再加一层兜底：`cur == -1`（引擎停了 / 正在等切换点）时 `lay.clips[lay.cur]` 取不到，
          但图层播放器还在解那个文件 —— 用播放器的路径高亮。用户要的是"正在使用的素材"
          的底色，而不是"引擎此刻的索引"。
        \"\"\"
        def _key(p):
            return os.path.normcase(str(p or ""))

        for i, (_, grid) in enumerate(self.rows):
            if i >= len(self.main.engine.layers):
                continue
            lay = self.main.engine.layers[i]
            playing = set()
            for idx in (getattr(lay, "cur", -1), getattr(lay, "prev_idx", -1)):
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
""")


def main():
    for rel, old, new in PATCHES:
        path = os.path.join(ROOT, rel)
        s = io.open(path, encoding="utf-8").read()
        n = s.count(old)
        if n != 1:
            print("✗ %s：锚点命中 %d 次（应为 1），本次不写" % (rel, n))
            return 1
        io.open(path, "w", encoding="utf-8", newline="\n").write(s.replace(old, new))
        print("✓ %s" % rel)
    print("\n补丁全部落地")
    return 0


if __name__ == "__main__":
    sys.exit(main())
