# -*- coding: utf-8 -*-
"""审查修复（锚点修正版）+ 审查补充项 M9/M11。"""
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
            raise SystemExit("%s: 期望 %d 处实得 %d\n---\n%s" % (path, cnt, got, old[:240]))
        t = t.replace(old, new)
    io.open(p, "w", encoding="utf-8", newline="").write(t)
    print("  已写入 %s（%+d 字节）" % (path, len(t) - len(orig)))


edit("cue_editor.py", [
    # S1 总长取错对象
    ('        dur = 0.0\n'
     '        try:\n'
     '            dur = float(self.audio.duration())\n'
     '        except Exception:                                        # noqa: BLE001\n'
     '            dur = 0.0\n',
     '        # ⚠ 不能读 self.audio.duration()：那是**预览播放缓冲**的时长（没按过 ▶ 时为 0，\n'
     '        #   按过之后一直留着上一首的时长）⇒ 5 分钟的歌被撑成 10~20 分钟（审查 S1 真根因）。\n'
     '        dur = 0.0\n'
     '        try:\n'
     '            dur = float(getattr(getattr(self._track(), "identity", None),\n'
     '                                "duration_sec", 0.0) or 0.0)\n'
     '        except Exception:                                        # noqa: BLE001\n'
     '            dur = 0.0\n'
     '        if dur <= 1.0:                       # 曲目没填时长 ⇒ 直接量一下那个音频文件\n'
     '            try:\n'
     '                import cue_preview as _cp\n'
     '                _pth = self._track_audio_path()\n'
     '                if _pth:\n'
     '                    dur = float(_cp.probe_duration(_pth))\n'
     '            except Exception:                                    # noqa: BLE001\n'
     '                dur = 0.0\n', 1),
    # S3 ▶ 不依赖图层/位置源
    ('            if self._running != "sim":\n'
     '                # 顺手把模拟位置源起起来，少一步操作（VDJ 在推时就不该用播放）\n'
     '                try:\n'
     '                    self.cb_src.setCurrentIndex(0)\n'
     '                    self._toggle_run()\n'
     '                except Exception:                                # noqa: BLE001\n'
     '                    pass\n'
     '                if self._running != "sim":\n'
     '                    return\n',
     '            # ★ 独立预览**不需要**码表图层、也不需要位置源（审查 S3）：\n'
     '            #   ▶ 自己按音频/挂钟推进画面。有图层时顺带把模拟源推起来（失败不管）。\n'
     '            if self._running is None:\n'
     '                try:\n'
     '                    if self.main.engine.cuesheet_wanted():\n'
     '                        self.cb_src.setCurrentIndex(0)\n'
     '                        self._toggle_run()\n'
     '                except Exception:                                # noqa: BLE001\n'
     '                    pass\n', 1),
    # M2 循环要真的重播音频
    ('            if self.chk_loop.isChecked():\n'
     '                self._set_pos(0.0)\n'
     '                return\n',
     '            if self.chk_loop.isChecked():\n'
     '                # ★ 循环必须**把音频也重起来**（审查 M2）：只把画面拉回 0，\n'
     '                #   下一帧还会判成"播完了"又停一次。\n'
     '                self._set_pos(0.0)\n'
     '                try:\n'
     '                    _ap = self._track_audio_path()\n'
     '                    if _ap and not self.audio.running():\n'
     '                        self.audio.play(_ap, self._audio_anchor_sec())\n'
     '                except Exception:                                # noqa: BLE001\n'
     '                    pass\n'
     '                return\n', 1),
    ('            elif self.audio.duration() > 0:\n'
     '                # 音频放完了 / 停了 ⇒ **画面也停**，不要继续按挂钟往前走\n',
     '            elif self.audio.duration() > 0 and not self.chk_loop.isChecked():\n'
     '                # 音频放完了 / 停了 ⇒ **画面也停**（勾了循环不停，交给上面重播）\n', 1),
    # M3 播放中不要被刷新覆盖播放头
    ('        # 播放头\n        self.tl.set_playhead(st["pos_beats"])\n',
     '        # 播放头：★ 编辑器自己在播时以 _pos 为准（审查 M3）\n'
     '        if not getattr(self, "_playing", False):\n'
     '            _b = max(0.02, float(getattr(self, "_bl", 0.5) or 0.5))\n'
     '            self.tl.set_playhead(st["pos_beats"] * _b\n'
     '                                 if st.get("pos_beats") is not None else None)\n', 1),
    # M4 改 BPM/时长后重算时间轴与标尺
    ('        self._fill_track_list()\n'
     '        self._rebuild_runtime()\n'
     '        self._log(T("曲目信息已更新（认歌用「曲名 + 艺术家 + 时长」）"))\n',
     '        self._fill_track_list()\n'
     '        self._rebuild_runtime()\n'
     '        self._sync_view()      # ★ 改了 BPM/时长 ⇒ 重算标尺与时间轴长度（审查 M4）\n'
     '        self._log(T("曲目信息已更新（认歌用「曲名 + 艺术家 + 时长」）"))\n', 1),
    # M1 吸附网格
    ('        bl = max(0.02, float(self.beat_len or 0.5))\n'
     '        grid_beat = {"off": 0.01, "beat": 0.25, "bar": 0.5, "phrase": 1.0}.get(\n'
     '            it.snap or "off", 0.01)\n'
     '        g = grid_beat * bl if (it.snap or "off") != "off" else 0.1\n'
     '        return max(0.0, round(float(beats) * bl / g) * g / bl)\n',
     '        # ★ 吸附按**拍**网格（审查 M1：旧数值把"小节"写成了 0.5 拍）\n'
     '        kind = it.snap or "off"\n'
     '        if kind == "off":\n'
     '            return max(0.0, float(beats))\n'
     '        g = {"beat": 1.0, "bar": 4.0, "phrase": 8.0}.get(kind, 1.0)\n'
     '        return max(0.0, round(float(beats) / g) * g)\n', 1),
    # M11 at_sec 在统一入口维护
    ('        it.at_beats = at\n'
     '        ly.items.append(it)\n',
     '        it.at_beats = at\n'
     '        it.at_sec = at_sec          # ★ 冗余字段一起维护（审查 M11，导出要用）\n'
     '        ly.items.append(it)\n', 1),
])

edit("cue_preview.py", [
    # 探测时长工具
    ('class CueFrameSource:',
     'def probe_duration(path):\n'
     '    """只探时长（不解码整首）：编排页算"这首歌多长"用它，别读播放缓冲。"""\n'
     '    try:\n'
     '        import av\n'
     '        with av.open(path, metadata_errors="replace") as c:\n'
     '            return float(c.duration / 1000000.0) if c.duration else 0.0\n'
     '    except Exception:                                          # noqa: BLE001\n'
     '        return 0.0\n'
     '\n'
     '\n'
     'class CueFrameSource:', 1),
    # 清缓冲（换歌别把上一首的时长留着）
    ('    def load(self, path, start_sec=0.0):\n',
     '    def clear(self):\n'
     '        """丢下缓冲（换歌/换码表时调）。"""\n'
     '        self.stop()\n'
     '        self._buf = None\n'
     '        self._i = 0\n'
     '        self._src = ""\n'
     '\n'
     '    def load(self, path, start_sec=0.0):\n', 1),
    # M9 淡入被平方：opacity*alpha 已经含淡入，别再乘 trans_p
    ('                        p.setOpacity(max(0.0, min(1.0, float(st.opacity) * float(st.alpha)\n'
     '                                                  * float(getattr(st, "trans_p", 1.0)))))\n',
     '                        # ★ 只乘一次（审查 M9）：运行时的 alpha 已含淡入量（alpha=trans_p=a_in），\n'
     '                        #   再乘 trans_p 会把淡入变成平方曲线、比现场"硬"。\n'
     '                        p.setOpacity(max(0.0, min(1.0, float(st.opacity) * float(st.alpha))))\n', 1),
])

edit("engine.py", [
    ('        self._cue_sid = sid\n'
     '        self._cue_level = lvl or self._cue_level\n'
     '        if tr is None or tr is self._cue_track:\n'
     '            return\n',
     '        if tr is None:\n'
     '            # ★ 匹配失败**不要**提交 sid（审查 严重-4a）：否则这首歌首次没匹配上后永不重试\n'
     '            return\n'
     '        self._cue_sid = sid\n'
     '        self._cue_level = lvl or self._cue_level\n'
     '        if tr is self._cue_track:\n'
     '            return\n', 1),
])
print("审查修复完成（含 M1/M2/M3/M4/M9/M11 + 引擎严重-4a）")
