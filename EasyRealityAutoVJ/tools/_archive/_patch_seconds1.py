# -*- coding: utf-8 -*-
"""收尾三项：
① 音频整首解码挪到后台线程（解码完自动从当前位置起播）
② 时间轴坐标轴改成**时间（秒）**：标尺按整秒画、位置/滑杆/属性面板都用秒
③ 新建码表**立刻落盘** + 改动后**自动保存**

设计取舍（②）：
  · `CueItem.at_beats` 仍是**主坐标**（引擎/解释器都按拍工作），不换算就不碰它；
  · 界面层统一用秒 = `beats × beat_len`，只在"显示/输入"边界做一次乘除；
  · 标尺按**整秒**画刻度（这才是时间轴），拍/小节/乐句线在放得下时才叠加上去。
"""
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
            raise SystemExit("%s: 期望 %d 处实得 %d\n---\n%s" % (path, cnt, got, old[:260]))
        t = t.replace(old, new)
    for anchor, ins in adds:
        got = t.count(anchor)
        if got != 1:
            raise SystemExit("%s: 插入锚点 %d 次：%s" % (path, got, anchor[:120]))
        t = t.replace(anchor, ins + anchor, 1)
    io.open(p, "w", encoding="utf-8", newline="").write(t)
    print("  已写入 %s（%+d 字节）" % (path, len(t) - len(orig)))


# ======================================================================
# 一、CueTimeline：标尺按秒画（坐标轴 = 时间）
# ======================================================================
edit("cue_editor.py", [
    # 拍长（秒/拍）：标尺换秒要用它
    ('        self.playhead = None        # 当前拍位（None=不画）\n',
     '        self.playhead = None        # 当前拍位（None=不画）\n'
     '        self.beat_len = 0.5         # 秒/拍：标尺按"秒"画要靠它换算\n', 1),
    # _ruler_plan 换成按"秒"排布
    ('        span = max(1.0, float(self.view_beats))\n'
     '        pxb = max(1e-4, (W - GUTTER) / span)          # 每拍像素\n'
     '        (lab_step, line_beats, show_label, show_bar_mark,\n'
     '         show_phrase_mark, show_beat_tick) = self._ruler_plan(pxb)\n'
     '        p.setFont(QFont("", 8))\n'
     '        k0 = int(math.floor(self.view0))\n'
     '        k1 = int(math.ceil(self.view0 + self.view_beats))\n'
     '        for k in range(k0, k1 + 1):\n'
     '            x = self._x(float(k))\n'
     '            if x < GUTTER - 1:\n'
     '                continue\n'
     '            # --- 竖线：按 line_beats 抽稀（保证线之间至少 2px，绝不糊成条纹）---\n'
     '            if k % line_beats == 0:\n'
     '                p.setPen(QPen(grid, 1))\n'
     '                p.drawLine(QPointF(x, RULER_H - 8), QPointF(x, self.height()))\n'
     '            elif show_beat_tick:\n'
     '                p.setPen(QPen(grid, 1, Qt.DotLine))\n'
     '                p.drawLine(QPointF(x, RULER_H - 4), QPointF(x, RULER_H))\n'
     '            # --- 数字 / 标记（按 lab_step 抽稀；放不下就一个都不画）---\n'
     '            if not show_label or (k % lab_step):\n'
     '                continue\n'
     '            is_ph = (k % 8 == 0)\n'
     '            is_bar = (k % 4 == 0)\n'
     '            if is_ph and show_phrase_mark:\n'
     '                p.setPen(QColor("#c08a2a") if dark else QColor("#b07000"))\n'
     '                p.drawText(int(x) - 3, 12, "◆")\n'
     '                p.setPen(fg)\n'
     '                p.drawText(int(x) + 7, 12, str(k))\n'
     '            elif is_bar and show_bar_mark:\n'
     '                p.setPen(dim)\n'
     '                p.drawText(int(x) - 2, 20, "●")\n'
     '                p.setPen(fg)\n'
     '                p.drawText(int(x) + 4, 20, str(k))\n'
     '            else:\n'
     '                p.setPen(fg if is_ph else dim)\n'
     '                p.drawText(int(x) + 2, 12 if is_ph else 20, str(k))\n',
     '        # ★ 坐标轴 = **时间（秒）**（用户 2026-09-29：导入的视频多长，编排就该按真实时间对）\n'
     '        bl = max(0.02, float(self.beat_len or 0.5))\n'
     '        span_beats = max(1.0, float(self.view_beats))\n'
     '        pxb = max(1e-4, (W - GUTTER) / span_beats)     # 每拍像素\n'
     '        pxs = pxb * bl                                 # 每秒像素\n'
     '        sec0 = self.view0 * bl\n'
     '        sec1 = (self.view0 + span_beats) * bl\n'
     '        (step_sec, line_sec, show_label, show_bar_mark,\n'
     '         show_phrase_mark, show_beat_tick) = self._ruler_plan(pxs)\n'
     '        p.setFont(QFont("", 8))\n'
     '        # --- 整秒刻度（时间轴的主刻度）---\n'
     '        s = int(math.floor(sec0))\n'
     '        while s <= sec1 + 1:\n'
     '            if s >= 0:\n'
     '                x = self._x(s / bl)\n'
     '                if x >= GUTTER - 1:\n'
     '                    major = (s % line_sec == 0)\n'
     '                    if major:\n'
     '                        p.setPen(QPen(grid, 1))\n'
     '                        p.drawLine(QPointF(x, RULER_H - 8), QPointF(x, self.height()))\n'
     '                    elif show_beat_tick:\n'
     '                        p.setPen(QPen(grid, 1, Qt.DotLine))\n'
     '                        p.drawLine(QPointF(x, RULER_H - 4), QPointF(x, RULER_H))\n'
     '                    if show_label and (s % step_sec == 0):\n'
     '                        p.setPen(fg)\n'
     '                        p.drawText(int(x) + 2, 12, _mmss(s))\n'
     '            s += 1\n'
     '        # --- 拍 / 小节 / 乐句线（有 BPM 时才叠加上去，淡一点）---\n'
     '        if pxs >= 2.0:\n'
     '            k0 = int(math.floor(self.view0))\n'
     '            k1 = int(math.ceil(self.view0 + span_beats))\n'
     '            for k in range(k0, k1 + 1):\n'
     '                if k % 4:\n'
     '                    continue\n'
     '                x = self._x(float(k))\n'
     '                if x < GUTTER - 1:\n'
     '                    continue\n'
     '                is_ph = (k % 8 == 0)\n'
     '                if is_ph and show_phrase_mark:\n'
     '                    p.setPen(QColor("#c08a2a") if dark else QColor("#b07000"))\n'
     '                    p.drawLine(QPointF(x, RULER_H - 8), QPointF(x, RULER_H))\n'
     '                    p.drawText(int(x) - 3, 22, "◆")\n'
     '                elif show_bar_mark:\n'
     '                    p.setPen(QPen(grid.darker(120), 1))\n'
     '                    p.drawLine(QPointF(x, RULER_H - 6), QPointF(x, RULER_H))\n'
     '                    p.setPen(dim)\n'
     '                    p.drawText(int(x) - 2, 22, "●")\n', 1),
    # _ruler_plan 的语义从"每拍像素"改成"每秒像素"
    ('    def _ruler_plan(pxb):\n'
     '        """按「每拍多少像素」算出标尺该画什么（抽成静态方法便于自测）。\n'
     '\n'
     '        返回 `(lab_step, line_beats, show_label, show_bar_mark, show_phrase_mark, show_beat_tick)`。\n'
     '        缩小时逐级抽稀：\n'
     '          · 数字至少 30px 才排得下，排不下就一个都不画；排得下但挤 ⇒ 每 2/4/8/16/32/64/128 拍标一次\n'
     '          · 竖线之间至少留 2px（line_beats 逐级 4→8→16→32→64），否则整屏都是条纹\n'
     '          · ● ◆ 与普通拍刻度都只在放得下时才画\n'
     '        """\n'
     '        lab_step = 128\n'
     '        for cand in (1, 2, 4, 8, 16, 32, 64, 128):\n'
     '            lab_step = cand\n'
     '            if cand * pxb >= 30:\n'
     '                break\n'
     '        line_beats = 64\n'
     '        for cand in (4, 8, 16, 32, 64):\n'
     '            line_beats = cand\n'
     '            if cand * pxb >= 2.0:\n'
     '                break\n'
     '        return (lab_step, line_beats, lab_step * pxb >= 30,\n'
     '                pxb * 4 >= 7, pxb * 8 >= 7, pxb >= 4.0)',
     '    def _ruler_plan(pxs):\n'
     '        """按「**每秒**多少像素」算出标尺该画什么（抽成静态方法便于自测）。\n'
     '\n'
     '        返回 `(数据步长秒, 长线每几秒, 画数字?, 画●?, 画◆?, 画短刻度?)`。\n'
     '        缩小时逐级抽稀：数字至少 34px 才排得下（`mm:ss` 比纯数字宽），\n'
     '        长线之间至少留 2px，否则整屏条纹。\n'
     '        """\n'
     '        step_sec = 1\n'
     '        for cand in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600):\n'
     '            step_sec = cand\n'
     '            if cand * pxs >= 34:\n'
     '                break\n'
     '        line_sec = 1\n'
     '        for cand in (1, 2, 5, 10, 15, 30, 60, 300, 600):\n'
     '            line_sec = cand\n'
     '            if cand * pxs >= 2.0:\n'
     '                break\n'
     '        return (step_sec, line_sec, step_sec * pxs >= 34,\n'
     '                pxs * 2.0 >= 7, pxs * 4.0 >= 7, pxs >= 6.0)', 1),
    # 吸附：自由档 = 0.1 秒
    ('    def _snap(self, it, beats):\n'
     '        grid = {"off": 0.25, "beat": 1.0, "bar": 4.0, "phrase": 8.0}.get(it.snap or "off", 0.25)\n'
     '        return max(0.0, round(float(beats) / grid) * grid)\n',
     '    def _snap(self, it, beats):\n'
     '        """吸附档位按**秒**定义，再换算回拍（坐标轴是时间了）。"""\n'
     '        bl = max(0.02, float(self.beat_len or 0.5))\n'
     '        grid_beat = {"off": 0.01, "beat": 0.25, "bar": 0.5, "phrase": 1.0}.get(\n'
     '            it.snap or "off", 0.01)\n'
     '        g = grid_beat * bl if (it.snap or "off") != "off" else 0.1\n'
     '        return max(0.0, round(float(beats) * bl / g) * g / bl)\n', 1),
], adds=[
    # mm:ss 小工具（模块级）
    ('CLIP_MIME = "application/x-ercue-clip"\n', 'def _mmss(sec):\n'
     '    """秒 → mm:ss（标尺用；超过 1 小时显示 h:mm:ss）。"""\n'
     '    s = max(0, int(round(float(sec))))\n'
     '    if s >= 3600:\n'
     '        return "%d:%02d:%02d" % (s // 3600, (s % 3600) // 60, s % 60)\n'
     '    return "%d:%02d" % (s // 60, s % 60)\n\n\n'),
])

print("第一部分（时间轴按秒）完成")
