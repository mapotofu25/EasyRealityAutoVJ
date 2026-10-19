# -*- coding: utf-8 -*-
"""最小修复：CueAudio.play 兼容旧调用（编辑器还在用 play(path, start)）。"""
import io
import os

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")

p = os.path.join(SRC, "cue_preview.py")
t = io.open(p, encoding="utf-8").read()
old = "    def play(self, start_sec=None):\n"
assert t.count(old) == 1, t.count(old)
new = (
    "    def play(self, path=None, start_sec=None):\n"
    "        # 兼容两种调用：play(path, sec) 会顺手把整首解码好；play(sec) 只换位置。\n"
    "        # 注意 load() 要几百 ms ~ 几秒，调用方应当丢到后台线程（见 cue_editor）。\n"
    "        if path:\n"
    "            if not self.load(path, float(start_sec or 0.0)):\n"
    "                return False\n"
    "            start_sec = float(start_sec or 0.0)\n"
)
t = t.replace(old, new)
io.open(p, "w", encoding="utf-8", newline="").write(t)
print("cue_preview.CueAudio.play 已兼容旧调用")

# 编辑器：放完就停（画面跟着停）—— 用户报「把音乐关了画面还在走」
p2 = os.path.join(SRC, "cue_editor.py")
t2 = io.open(p2, encoding="utf-8").read()
old2 = ("        try:\n"
        "            if self.audio.running():\n"
        "                nxt = (self.audio.position() - self._audio_anchor_sec()) / bl\n"
        "        except Exception:                                        # noqa: BLE001\n"
        "            nxt = None\n")
assert t2.count(old2) == 1, t2.count(old2)
new2 = ("        try:\n"
        "            if self.audio.running():\n"
        "                nxt = (self.audio.position() - self._audio_anchor_sec()) / bl\n"
        "            elif self.audio.duration() > 0:\n"
        "                # 音频放完了 / 停了 ⇒ **画面也停**，不要继续按挂钟往前走\n"
        "                self._set_pos((self.audio.position() - self._audio_anchor_sec()) / bl)\n"
        "                self._toggle_play(False)\n"
        "                self._log(T(\"音频播完了 —— 画面也停在这里（重来点 ⏮ 或 ▶）\"))\n"
        "                return\n"
        "        except Exception:                                        # noqa: BLE001\n"
        "            nxt = None\n")
t2 = t2.replace(old2, new2)
io.open(p2, "w", encoding="utf-8", newline="").write(t2)
print("cue_editor._play_tick 已改：放完就停")
