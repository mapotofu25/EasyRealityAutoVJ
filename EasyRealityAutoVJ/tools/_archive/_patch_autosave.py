# -*- coding: utf-8 -*-
"""③ 新建码表**立刻落盘** + 改动后**自动保存**（用户：「他不会保存的」）。"""
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def edit(path, pairs, adds=()):
    p = os.path.join(ROOT, "src", path)
    t = io.open(p, encoding="utf-8").read()
    orig = t
    for old, new, cnt in pairs:
        got = t.count(old)
        if got != cnt:
            raise SystemExit("%s: 期望 %d 处实得 %d\n---\n%s" % (path, cnt, got, old[:220]))
        t = t.replace(old, new)
    for anchor, ins in adds:
        if t.count(anchor) != 1:
            raise SystemExit("%s: 插入锚点 %d 次: %r" % (path, t.count(anchor), anchor[:100]))
        t = t.replace(anchor, ins + anchor, 1)
    io.open(p, "w", encoding="utf-8", newline="").write(t)
    print("  已写入 %s（%+d 字节）" % (path, len(t) - len(orig)))


edit("cue_editor.py", [
    # 定时器 + dirty 标记
    ('        self._audio_ready = False\n',
     '        self._audio_ready = False\n'
     '        # ★ 自动保存：改动后 1.5 秒写回盘（用户报过「新建的码表不保存」）\n'
     '        self._dirty = False\n'
     '        self._save_t = QTimer(self)\n'
     '        self._save_t.setSingleShot(True)\n'
     '        self._save_t.timeout.connect(self._autosave)\n', 1),
    # 新建：立刻落盘
    ('        self._register_sheet(name, sheet, {}, "")     # path 为空 = 还没导出过\n',
     '        # ★ 立刻落盘：以前只放内存，一重启就全没了（用户以为的"不保存"就是这个）\n'
     '        path = ""\n'
     '        try:\n'
     '            safe = "".join(c for c in name if c not in \'\\\\/:*?"<>|\').strip() or "ercue"\n'
     '            path = os.path.join(self._sheet_dir(), safe + cs.EXT)\n'
     '            k = 2\n'
     '            while os.path.exists(path):\n'
     '                path = os.path.join(self._sheet_dir(), "%s_%d%s" % (safe, k, cs.EXT))\n'
     '                k += 1\n'
     '            cs.save(sheet, path, media_paths={}, embed=False)\n'
     '        except Exception as e:                                    # noqa: BLE001\n'
     '            path = ""\n'
     '            self._log(T("自动落盘失败（不影响继续编排）：") + str(e)[:80])\n'
     '        self._register_sheet(name, sheet, {}, path)\n', 1),
], adds=[
    ('    def _track_audio_path(self):\n', '''    # ==================================================================
    # 保存（新建即落盘 + 改动自动保存）
    # ==================================================================
    def _sheet_dir(self):
        """码表包放哪：优先「码表 ▾ → 设置码表目录…」，否则 exe 同级的 cuesheets/。"""
        d = ""
        try:
            d = str(self.main.cfg["cue"]["cue_dir"] or "")
        except Exception:                                          # noqa: BLE001
            d = ""
        if not d:
            try:
                from config import app_base_dir
                d = os.path.join(app_base_dir(), "cuesheets")
            except Exception:                                      # noqa: BLE001
                d = os.path.join(os.path.expanduser("~"), "AutoVJ_cuesheets")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:                                          # noqa: BLE001
            pass
        return d

    def _mark_dirty(self):
        """任何改动都调它：1.5 秒后自动写回（QTimer 合并连续改动，不会狂写盘）。"""
        self._dirty = True
        try:
            self._save_t.start(1500)
        except Exception:                                          # noqa: BLE001
            pass

    def _autosave(self):
        """把当前码表写回它自己的 .ercue（只对已有路径的做自动保存）。"""
        if not self._dirty or self._cur is None:
            return
        rec = self._sheets.get(self.cb_sheet.currentText()) or {}
        p = rec.get("path") or ""
        if not p:
            return
        try:
            import cuesheet as cs
            cs.save(self._cur, p, media_paths=self._media, embed=False)
            self._dirty = False
            self._log(T("已自动保存：{}").format(os.path.basename(p)))
            self._refresh_sheet_labels()
        except Exception as e:                                     # noqa: BLE001
            self._log(T("自动保存失败：") + str(e)[:80])

'''),
])

# 改动点：加 _mark_dirty()
edit("cue_editor.py", [
    ('        self._rebuild_runtime()\n        self.tl.update()\n'
     '        self._log(T("已把素材加到「{}」的 {}").format(ly.name or ly.lyr_id,\n',
     '        self._rebuild_runtime()\n        self.tl.update()\n        self._mark_dirty()\n'
     '        self._log(T("已把素材加到「{}」的 {}").format(ly.name or ly.lyr_id,\n', 1),
    ('            self.tl.set_selected(ly.lyr_id if ly is not None else "", it.item_id)\n'
     '        self._rebuild_runtime()\n',
     '            self.tl.set_selected(ly.lyr_id if ly is not None else "", it.item_id)\n'
     '        self._rebuild_runtime()\n        self._mark_dirty()\n', 1),
    ('        self._fill_track_list()\n        self._rebuild_runtime()\n'
     '        self._log(T("曲目信息已更新（认歌用「曲名 + 艺术家 + 时长」）"))\n',
     '        self._fill_track_list()\n        self._rebuild_runtime()\n        self._mark_dirty()\n'
     '        self._log(T("曲目信息已更新（认歌用「曲名 + 艺术家 + 时长」）"))\n', 1),
    ('        self._log(T("已加入 {} 个素材（同名复用 {} 个），这张码表现在共 {} 个")\n'
     '                  .format(added, reused, len(self._cur.media)))\n',
     '        self._mark_dirty()\n'
     '        self._log(T("已加入 {} 个素材（同名复用 {} 个），这张码表现在共 {} 个")\n'
     '                  .format(added, reused, len(self._cur.media)))\n', 1),
    ('        self._log(T("已从码表里去掉素材：{}").format(os.path.basename(p) or cid))\n',
     '        self._mark_dirty()\n'
     '        self._log(T("已从码表里去掉素材：{}").format(os.path.basename(p) or cid))\n', 1),
    ('        self._log(T("已删除层：{}").format(ly.name or ly.lyr_id))\n',
     '        self._mark_dirty()\n'
     '        self._log(T("已删除层：{}").format(ly.name or ly.lyr_id))\n', 1),
])
print("完成 ③ 自动保存")
