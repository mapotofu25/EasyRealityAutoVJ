# -*- coding: utf-8 -*-
"""第二部分：界面统一按**秒**（位置 / 滑杆 / 标尺 / 属性面板），内部仍存拍。"""
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
            raise SystemExit("%s: 期望 %d 处实得 %d\n---\n%s" % (path, cnt, got, old[:260]))
        t = t.replace(old, new)
    io.open(p, "w", encoding="utf-8", newline="").write(t)
    print("  已写入 %s（%+d 字节）" % (path, len(t) - len(orig)))


edit("cue_editor.py", [
    # ---- ① 视口/滑杆按秒 ----
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
     '        self._bl = bl\n'
     '        self._on_slide(self.sld.value())\n'
     '        _ = tr\n',
     '        """按内容设定视口与滑杆范围（**单位都是秒**）。"""\n'
     '        bl = self.main.engine._cue_beat_len(self.main.audio.state.snapshot()) or 0.5\n'
     '        self._bl = bl\n'
     '        self.tl.beat_len = bl\n'
     '        # 时长 = 素材音频的真实时长（有音频就用它，编排长度和歌一致）；\n'
     '        # 没有音频就退回"最后一个 cue 的秒数 + 余量"。\n'
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
     '        self.tl.set_view(0, max(8.0, total) / max(0.02, bl))     # 视口以拍存，长度=秒/bl\n'
     '        self.sld.setRange(0, int(total))\n'
     '        self._on_slide(self.sld.value())\n'
     '        _ = tr\n', 1),
    # ---- ② 位置：秒 ----
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
     '        # ★ 位置统一以**秒**为单位（用户：坐标轴按时间）；内部要拍的地方再除 beat_len\n'
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
    ('    def _set_pos(self, b, from_slider=False):\n'
     '        """位置（拍）的**唯一入口**：手拖滑杆和自动播放都走这里，保证三者同步\n'
     '        （滑杆 / 位置文字 / 时间轴播放头 / 交给引擎的模拟位置）。\n'
     '        """\n',
     '    def _set_pos(self, b, from_slider=False):\n'
     '        """位置（**秒**）的唯一入口：手拖滑杆和自动播放都走这里，四处同步\n'
     '        （滑杆 / 位置文字 / 时间轴播放头 / 交给引擎的模拟位置）。\n'
     '        """\n', 1),
    # 推给模拟源用秒（原来乘了一次 beat_len，现在本来就是秒）
    ('        bpm = float(getattr(ident, "bpm", 0) or 0) or 120.0\n'
     '        self._sim.push(title=getattr(ident, "title", "") or "",\n'
     '                       artist=getattr(ident, "artist", "") or "",\n'
     '                       bpm=bpm, position_sec=b * bl,\n',
     '        bpm = float(getattr(ident, "bpm", 0) or 0) or 120.0\n'
     '        self._sim.push(title=getattr(ident, "title", "") or "",\n'
     '                       artist=getattr(ident, "artist", "") or "",\n'
     '                       bpm=bpm, position_sec=sec,\n', 1),
    # ---- ③ 播放推进按秒 ----
    ('        bl = max(0.05, float(getattr(self, "_bl", 0.5) or 0.5))\n'
     '        nxt = None\n',
     '        bl = max(0.02, float(getattr(self, "_bl", 0.5) or 0.5))\n'
     '        nxt = None\n', 1),
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
    ('        if nxt is None or nxt < 0.0:\n'
     '            nxt = float(getattr(self, "_pos", 0.0)) + dt / bl\n'
     '        top = float(self.sld.maximum())\n',
     '        if nxt is None or nxt < 0.0:\n'
     '            nxt = float(getattr(self, "_pos", 0.0)) + dt       # 秒\n'
     '        top = float(self.sld.maximum())\n', 1),
    # 起播时的起始秒
    ('        bl = max(0.05, float(getattr(self, "_bl", 0.5) or 0.5))\n'
     '        start = self._audio_anchor_sec() + float(getattr(self, "_pos", 0.0)) * bl\n',
     '        start = self._audio_anchor_sec() + float(getattr(self, "_pos", 0.0))\n', 1),
    # ---- ④ 属性面板按秒 ----
    ('            self.sp_at.setValue(float(it.at_beats))\n'
     '            self.sp_len.setValue(float(it.end_beats - it.at_beats) if it.end_beats else 0.0)\n',
     '            _bl = max(0.02, float(getattr(self, "_bl", 0.5) or 0.5))\n'
     '            self.sp_at.setValue(float(it.at_beats) * _bl)\n'
     '            self.sp_len.setValue((float(it.end_beats) - float(it.at_beats)) * _bl\n'
     '                                 if it.end_beats else 0.0)\n', 1),
    ('            self.sp_tin.setValue(float(it.trans_in_beats or 0))\n',
     '            self.sp_tin.setValue(float(it.trans_in_beats or 0) * _bl)\n', 1),
    ('            self.sp_tout.setValue(float(it.trans_out_beats or 0))\n',
     '            self.sp_tout.setValue(float(it.trans_out_beats or 0) * _bl)\n', 1),
    ('        if it is not None:\n'
     '            it.clip_id = self.cb_clip.currentData() or ""\n'
     '            it.at_beats = float(self.sp_at.value())\n'
     '            ln = float(self.sp_len.value())\n'
     '            it.end_beats = (it.at_beats + ln) if ln > 0 else None\n'
     '            it.trans_in = self.cb_tin.currentData() or "fade"\n'
     '            it.trans_in_beats = float(self.sp_tin.value())\n'
     '            it.trans_out = self.cb_tout.currentData() or "fade"\n'
     '            it.trans_out_beats = float(self.sp_tout.value())\n',
     '        if it is not None:\n'
     '            _bl = max(0.02, float(getattr(self, "_bl", 0.5) or 0.5))\n'
     '            it.clip_id = self.cb_clip.currentData() or ""\n'
     '            it.at_beats = float(self.sp_at.value()) / _bl      # 面板是秒，存的是拍\n'
     '            ln = float(self.sp_len.value()) / _bl\n'
     '            it.end_beats = (it.at_beats + ln) if ln > 0 else None\n'
     '            it.trans_in = self.cb_tin.currentData() or "fade"\n'
     '            it.trans_in_beats = float(self.sp_tin.value()) / _bl\n'
     '            it.trans_out = self.cb_tout.currentData() or "fade"\n'
     '            it.trans_out_beats = float(self.sp_tout.value()) / _bl\n', 1),
    # ---- ⑤ 文案改成秒 ----
     ('        self.sp_at.setSuffix(T(" 拍"))\n', '        self.sp_at.setSuffix(T(" 秒"))\n', 1),
     ('        self.sp_len.setSuffix(T(" 拍"))\n', '        self.sp_len.setSuffix(T(" 秒"))\n', 1),
     ('        self.sp_tin.setSuffix(T(" 拍"))\n', '        self.sp_tin.setSuffix(T(" 秒"))\n', 1),
     ('        self.sp_tout.setSuffix(T(" 拍"))\n', '        self.sp_tout.setSuffix(T(" 秒"))\n', 1),
     ('        self.btn_rew.setToolTip(T("回到开头（第 0 拍）"))\n',
      '        self.btn_rew.setToolTip(T("回到开头（0:00）"))\n', 1),
     ('        self.lbl_tl_hint = QLabel(T("横轴 = 拍，每层一条轨道；点选片段后在左边「③ 片段属性」里改"))\n',
      '        self.lbl_tl_hint = QLabel(T("横轴 = 时间（秒），每层一条轨道；点选片段后在左边「4 属性」里改"))\n',
      1),
     ('        self.lbl_tl_hint = QLabel(T("横轴 = 拍，每层一条轨道；点选片段后在左边「③ 片段属性」里改"))\n',
      '        self.lbl_tl_hint = QLabel(T("横轴 = 时间（秒），每层一条轨道；点选片段后在左边「4 属性」里改"))\n',
      1),
     # 属性面板里那几行标签也点明单位
     ('        add_row(T("起点"), self.sp_at)\n', '        add_row(T("起点"), self.sp_at)\n', 1),
     # ---- ⑥ 加 cue / 移动 都按秒 ----
     ('        bl = max(0.05, float(getattr(self, "_bl", 0.5) or 0.5))\n'
      '        start = self._audio_anchor_sec() + float(getattr(self, "_pos", 0.0))\n',
      '        start = self._audio_anchor_sec() + float(getattr(self, "_pos", 0.0))\n', 1),
     ('        at = float(self._pos if at_beats is None else at_beats)\n'
      '        self._append_cue(ly, cid, at)\n',
      '        sec = float(self._pos if at_beats is None else at_beats)\n'
      '        self._append_cue(ly, cid, sec)\n', 1),
     ('        kind = self.cb_snap.currentData() or "bar"\n'
      '        at = self._snap_beats(kind, beats)\n'
      '        it = C.CueItem(item_id="i%d" % (len(ly.items) + 1), clip_id=clip_id,\n'
      '                       at_beats=at, trans_in="fade", trans_in_beats=2.0,\n'
      '                       trans_out="fade", trans_out_beats=2.0, snap=kind).normalized()\n'
      '        it.at_beats = at\n',
      '        bl = max(0.02, float(getattr(self, "_bl", 0.5) or 0.5))\n'
      '        kind = self.cb_snap.currentData() or "bar"\n'
      '        at_sec = self._snap_sec(kind, beats)          # 参数名沿用 beats，值其实是秒\n'
      '        at = at_sec / bl\n'
      '        it = C.CueItem(item_id="i%d" % (len(ly.items) + 1), clip_id=clip_id,\n'
      '                       at_beats=at, at_sec=at_sec, trans_in="fade",\n'
      '                       trans_in_beats=2.0 / bl,\n'
      '                       trans_out="fade", trans_out_beats=2.0 / bl, snap=kind).normalized()\n'
      '        it.at_beats = at\n', 1),
     ('        self._log(T("已把素材加到「{}」的 {} 拍上").format(ly.name or ly.lyr_id,\n'
      '                                                        "%.2f" % at))\n',
      '        self._log(T("已把素材加到「{}」的 {}").format(ly.name or ly.lyr_id,\n'
      '                                                    _mmss(at_sec)))\n', 1),
     ('    @staticmethod\n'
      '    def _snap_beats(kind, beats):\n'
      '        grid = {"off": 0.25, "beat": 1.0, "bar": 4.0, "phrase": 8.0}.get(kind or "off", 0.25)\n'
      '        return max(0.0, round(float(beats) / grid) * grid)\n',
      '    def _snap_sec(self, kind, sec):\n'
      '        """吸附（**单位秒**）：自由=0.1 秒；拍/小节/乐句用 beat_len 换算。"""\n'
      '        bl = max(0.02, float(getattr(self, "_bl", 0.5) or 0.5))\n'
      '        g = {"off": 0.1, "beat": bl, "bar": 4.0 * bl,\n'
      '             "phrase": 8.0 * bl}.get(kind or "off", 0.1)\n'
      '        return max(0.0, round(float(sec) / g) * g)\n', 1),
     ('        self._log(T("移动 cue 到 {} 拍").format("%.2f" % it.at_beats))\n',
      '        self._log(T("已把 cue 挪到 {}").format(_mmss(it.at_beats * self._bl)))\n', 1),
     ('        at = float(self.sld.value()) if self.sld else 0.0\n'
      '        n = len(ly.items) + 1\n'
      '        it = C.CueItem(item_id="i%d" % n, clip_id=(self.cb_clip.currentData() or ""),\n'
      '                       at_beats=at, trans_in="fade", trans_in_beats=2.0,\n'
      '                       trans_out="fade", trans_out_beats=2.0,\n'
      '                       snap=self.cb_snap.currentData() or "bar").normalized()\n'
      '        ly.items.append(it)\n'
      '        ly.items.sort(key=lambda x: (x.at_beats, x.item_id))\n'
      '        self.tl.set_selected(ly.lyr_id, it.item_id)\n'
      '        self._sync_props()\n'
      '        self._rebuild_runtime()\n'
      '        self.tl.update()\n'
      '        self._log(T("已在 {} 拍加了一个 cue（层 {}）").format("%.2f" % at, ly.name))\n',
      '        sec = float(self.sld.value()) if self.sld else 0.0\n'
      '        self._append_cue(ly, (self.cb_clip.currentData() or ""), sec)\n', 1),
     # 状态行/预览角标按秒
     ('                 T("位置：{}").format(("%.2f 拍" % st["pos_beats"]) if st["pos_beats"] is not None\n',
      '                 T("位置：{}").format(_mmss(st["pos_beats"] * self._bl)\n'
      '                                     if st["pos_beats"] is not None\n', 1),
     ('            nm, ("%.1f 拍" % pb) if pb is not None else T("未开始")))\n',
      '            nm, _mmss(pb * self._bl) if pb is not None else T("未开始")))\n', 1),
])

# add_row 的「起点」标签加单位说明
edit("cue_editor.py", [
    ('        add_row(T("起点"), self.sp_at)\n',
     '        add_row(T("起点（秒）"), self.sp_at)\n', 1),
    ('        add_row(T("时长"), self.sp_len)\n',
     '        add_row(T("时长（秒）"), self.sp_len)\n', 1),
    ('        add_row(T("入场时长"), self.sp_tin)\n',
     '        add_row(T("入场时长（秒）"), self.sp_tin)\n', 1),
    ('        add_row(T("出场时长"), self.sp_tout)\n',
     '        add_row(T("出场时长（秒）"), self.sp_tout)\n', 1),
])
print("第二部分（界面按秒）完成")
