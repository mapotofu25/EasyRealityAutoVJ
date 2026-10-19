# -*- coding: utf-8 -*-
"""让 CueAudio 兼容两种调用 + 画面跟着音频停（小步、安全，避免源码半迁移）。

背景：`CueAudio` 已改成「整首解码 + 按帧号定位」，但编辑器那边还按旧签名
`audio.play(path, start)` 调用 ⇒ 必须先兼容，否则 ▶ 直接抛 TypeError。
"""
import io
import os

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")

# ---- ① CueAudio.play 兼容旧签名（给 path 就先 load）----
p = os.path.join(SRC, "cue_preview.py")
t = io.open(p, encoding="utf-8").read()
old = '    def play(self, start_sec=None):
'
new = ('    def play(self, path=None, start_sec=None):
'
       '        """开始/继续播（兼容  与 ）。
'
       '        ⚠ load() 要几百 ms，调用方应丢到后台线程。"""
'
       '        if path://n'
       '            if not self.load(path, float(start_sec or 0.0)):
'
       '                return False
'
       '            start_sec = float(start_sec or 0.0)
')
assert t.count(old) == 1, ("play", t.count(old))
t = t.replace(old, new)
t = t.replace('        """开始/继续播（ 给定则从那里开始）。"""',
              '        """开始/继续播。"""')
io.open(p, "w", encoding="utf-8", newline="").write(t)
print("cue_preview.CueAudio.play 已兼容")

# ---- ② 编辑器：放完就停（画面跟着停）+ 位置以音频为准 ----
p2 = os.path.join(SRC, "cue_editor.py")
t2 = io.open(p2, encoding="utf-8").read()
old2 = ('        nxt = None\n'
        '        # ★ 有音频 ⇒ **以音频为准**（听得到的就是真位置）；没有音频 ⇒ 退回挂钟推进\n'
        '        try:\n'
        '            if self.audio.running():\n'
        '                nxt = (self.audio.position() - self._audio_anchor_sec()) / bl\n'
        '        except Exception:                                        # noqa: BLE001\n'
        '            nxt = None\n')
new2 = ('        nxt = None\n'
        '        # ★ 有音频 ⇒ **以音频为准**（位置 = 真的交给声卡播到第几帧）；\n'
        '        #   音频放完/停了 ⇒ **画面也停**（用户报过：把音乐关了画面还在往前走）。\n'
        '        try:\n'
        '            if self.audio.running():\n'
        '                nxt = (self.audio.position() - self._audio_anchor_sec()) / bl\n'
        '            elif self.audio.duration() > 0 and not self.audio.running():\n'
        '                self._set_pos((self.audio.position() - self._audio_anchor_sec()) / bl)\n'
        '                self._toggle_play(False)\n'
        '                self._log(T("音频放完了 —— 画面也停在这里（想重来点 ⏮ 或 ▶）"))\n'
        '                return\n'
        '        except Exception:                                        # noqa: BLE001\n'
        '            nxt = None\n')
assert t2.count(old2) == 1, ("tick", t2.count(old2))
t2 = t2.replace(old2, new2)
io.open(p2, "w", encoding="utf-8", newline="").write(t2)
print("cue_editor._play_tick 已改：放完就停")
