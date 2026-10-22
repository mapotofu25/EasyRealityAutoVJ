# -*- coding: utf-8 -*-
"""最小连贯集：秒制（标尺 + 播放头 + 滑杆 + 位置文字）一次接对。

只做**必须一致**的四处：`tl.beat_len`（标尺靠它）、视口/滑杆范围（秒）、
位置文字（mm:ss）、播放头换算。属性面板暂留"拍"（它自己标着 拍，内部一致）。
"""
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def edit(path, pairs):
    p = os.path.join(ROOT, "src", path)
    t = io.open(p, encoding="utf-8").read()
    orig = t
    for old, new, cnt in pairs:
        got = t.count(old)
        if got != cnt:
            raise SystemExit("%s: 期望 %d 处实得 %d\n---\n%s" % (path, cnt, got, old[:220]))
        t = t.replace(old, new)
    io.open(p, "w", encoding="utf-8", newline="").write(t)
    print("  已写入 %s（%+d 字节）" % (path, len(t) - len(orig)))


edit("cue_editor.py", [
    # ① 视口 / 滑杆：秒；并喂 beat_len
    ('        """按内容设定视口与滑杆范围。"""\n'
     '        # 视口长度 = 最后一个 cue 的结束拍（含 end_beats），没有就 64 拍\n'
     '        tb = 0.0\n'
     '        tr = self.tl.track\n'
     '        if tr is not None:\n'
     '            for ly in tr.layers:\n'
     '                for it in ly.items:\n'
     '                    tb = max(tb, float(it.end_beats if it.end_beats is not None else it.at_beats))\n'
     '        if tb <= 0:\n'
     '            tb = 64.0\n'
     '        self.tl.set_view(0, max(16.0, tb * 1.15))\n'
     '        self.sld.setRange(0, int(tb))\n'
     '        bl = self.main.engine._cue_beat_len(self.main.audio.state.snapshot()) or 0.5\n'
     '        self._bl = bl\n',
     '        """按内容设定视口与滑杆范围（**单位都是秒**）。"""\n'
     '        bl = self.main.engine._cue_beat_len(self.main.audio.state.snapshot()) or 0.5\n'
     '        self._bl = bl\n'
     '        self.tl.beat_len = bl       # ★ 标尺按秒画必须靠它（漏了 ⇒ 非 120BPM 时秒数全错）\n'
     '        # 编排总长（秒）：有音频就用**这首歌的真实时长**（编排长度与歌一致）；否则用最后一个 cue\n'
     '        dur = 0.0\n'
     '        try:\n'
     '            dur = float(self.audio.duration())\n'
     '        except Exception:                                        # noqa: BLE001\n'
     '            dur = 0.0\n'
     '        tb = 0.0\n'
     '        tr = self.tl.track\n'
     '        if tr is not None:\n'
     '            for ly in tr.layers:\n'
     '                for it in ly.items:\n'
     '                    e = it.end_beats if it.end_beats is not None else it.at_beats\n'
     '                    tb = max(tb, float(e) * bl)\n'
     '        total = max(dur, tb + 10.0, 30.0)\n'
     '        self.tl.set_view(0, max(8.0, total) / max(0.02, bl))\n'
     '        self.sld.setRange(0, int(total))\n', 1),
    # ② 位置：秒（文字 mm:ss、播放头按拍、预览按拍、给模拟源秒）
    ('        b = max(0.0, float(b))\n'
     '        self._pos = b\n'
     '        if not from_slider:\n'
     '            self.sld.blockSignals(True)\n'
     '            self.sld.setValue(int(round(b)))\n'
     '            self.sld.blockSignals(False)\n'
     '        bl = getattr(self, "_bl", 0.5)\n'
     '        self.lbl_pos.setText(T("{} 拍（{:.1f}s）").format("%.1f" % b, b * bl))\n'
     '        self.tl.set_playhead(b)\n'
     '        try:\n'
     '            self.prev.set_pos(b)          # 独立预览画布跟着位置走\n'
     '        except Exception:                                        # noqa: BLE001\n'
     '            pass\n',
     '        # ★ 位置统一以**秒**为单位（用户：坐标轴就是时间）；要"拍"的地方再除 beat_len\n'
     '        sec = max(0.0, float(b))\n'
     '        self._pos = sec\n'
     '        if not from_slider:\n'
     '            self.sld.blockSignals(True)\n'
     '            self.sld.setValue(int(round(sec)))\n'
     '            self.sld.blockSignals(False)\n'
     '        bl = max(0.02, float(getattr(self, "_bl", 0.5) or 0.5))\n'
     '        self.lbl_pos.setText(_mmss(sec) + "（%.1f 秒）" % sec)\n'
     '        self.tl.set_playhead(sec / bl)\n'
     '        try:\n'
     '            self.prev.set_pos(sec / bl)   # 独立预览画布按拍走\n'
     '        except Exception:                                        # noqa: BLE001\n'
     '            pass\n', 1),
    ('                       bpm=bpm, position_sec=b * bl,\n',
     '                       bpm=bpm, position_sec=sec,\n', 1),
    # ③ 播放推进：秒
    ('            if self.audio.running():\n'
     '                nxt = (self.audio.position() - self._audio_anchor_sec()) / bl\n'
     '            elif self.audio.duration() > 0:\n'
     '                # 音频放完了 / 停了 ⇒ **画面也停**，不要继续按挂钟往前走\n'
     '                self._set_pos((self.audio.position() - self._audio_anchor_sec()) / bl)\n',
     '            if self.audio.running():\n'
     '                nxt = self.audio.position() - self._audio_anchor_sec()      # 秒\n'
     '            elif self.audio.duration() > 0:\n'
     '                # 音频放完了 / 停了 ⇒ **画面也停**，不要继续按挂钟往前走\n'
     '                self._set_pos(self.audio.position() - self._audio_anchor_sec())\n', 1),
    ('            nxt = float(getattr(self, "_pos", 0.0)) + dt / bl\n'
     '        top = float(self.sld.maximum())\n',
     '            nxt = float(getattr(self, "_pos", 0.0)) + dt       # 秒\n'
     '        top = float(self.sld.maximum())\n', 1),
    # ④ 加 cue：位置传秒
    ('        at = float(self._pos if at_beats is None else at_beats)\n'
     '        self._append_cue(ly, cid, at)\n',
     '        sec = float(self._pos if at_beats is None else at_beats)\n'
     '        self._append_cue(ly, cid, sec)\n', 1),
    # ⑤ 文案：横轴按时间
    ('        self.btn_rew.setToolTip(T("回到开头（第 0 拍）"))\n',
     '        self.btn_rew.setToolTip(T("回到开头（0:00）"))\n', 1),
    ('        self.lbl_tl_hint = QLabel(T("横轴 = 拍，每层一条轨道；点选片段后在左边「③ 片段属性」里改"))\n',
     '        self.lbl_tl_hint = QLabel(T("横轴 = 时间（秒）；点选片段后在左边「4 属性」里改"))\n', 1),
])
print("秒制最小连贯集完成")
