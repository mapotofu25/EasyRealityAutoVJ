# -*- coding: utf-8 -*-
"""092930 全库审计后的**高危修复**（逐条都有实证）。

A. 4 处「同名方法定义两遍」——后一份静默覆盖前一份，改了看着对的那份却没反应
   · engine.manual_transition      第 2 份把 @_locked 的版本顶掉了（GUI 回调变成无锁）
   · panels.ClipRow.mousePress/MoveEvent  第 2 份才是活的，第 1 份是死代码
   · output_window.resizeEvent     第 2 份顶掉了第 1 份，于是「拖动/缩放期间暂停重绘」失效
B. postprocess.fx_trails(None, reset=True) 会 AttributeError（被上层吞掉）⇒ 拖影缓冲永远清不掉
C. 13 个被 GUI 直接调用的引擎方法**没有 @_locked** ⇒ 与渲染线程抢同一份状态
D. 音频：识别窗口 4.5s 硬编码在两处 + 环形缓冲按 48k 写死（96k 设备时窗口被截短）
E. charge_engine.MatchTick.evidence_votes 未初始化
"""
import io
import os
import sys

ROOT = r"P:\AutoVJ"
PAT = []


def rep(rel, old, new, cnt=1):
    PAT.append((rel, old, new, cnt))


# ============================== A1 engine.manual_transition
rep("src/engine.py",
    '''

    def manual_transition(self):
        self.switch_pos = -999  # 强制下一拍重切
        self._next_due = None
        self.next_scene()
''', "")

# ============================== A2 panels.ClipRow 的死方法
rep("src/panels.py",
    '''    # ---- 手动接管拖动（保证左键拖素材一定起效果）----
    def mousePressEvent(self, e):
        self._press_pos = None
        self._press_item = None
        if e.button() == Qt.LeftButton:
            pt = e.position().toPoint() if hasattr(e, "position") else e.pos()
            it = self.itemAt(pt)
            if it is not None:
                self._press_item = it
                self._press_pos = pt
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if not (e.buttons() & Qt.LeftButton) or self._press_item is None or self._press_pos is None:
            super().mouseMoveEvent(e)
            return
        pt = e.position().toPoint() if hasattr(e, "position") else e.pos()
        if (pt - self._press_pos).manhattanLength() < QApplication.startDragDistance():
            super().mouseMoveEvent(e)
            return
        # 自行发起拖动
        m = self._press_item.data(Qt.UserRole)
        self._drag_paths = [m.path]
        md = QMimeData()
        md.setData(CLIP_MIME, m.path.encode("utf-8"))
        md.setText(m.path)
        drag = QDrag(self)
        drag.setMimeData(md)
        pm = m.pixmap(CELL_W - 8, CELL_H - 10)
        if not pm.isNull():
            drag.setPixmap(pm)
            drag.setHotSpot(QPoint(pm.width() // 2, pm.height() // 2))
        drag.exec(Qt.CopyAction | Qt.MoveAction, Qt.CopyAction)
        self._drag_paths = []
        self._press_item = None
        self._press_pos = None
        e.accept()

''', "")

# ============================== A3 output_window.resizeEvent 合并
rep("src/output_window.py",
    '''    def resizeEvent(self, e):
        self._pause_repaint()
        super().resizeEvent(e)

''', "")
rep("src/output_window.py",
    '''    def resizeEvent(self, ev):
        super().resizeEvent(ev)
''',
    '''    def resizeEvent(self, ev):
        # ⚠ 这里曾经有**两份** resizeEvent：先定义的这份带 _pause_repaint()，后定义的
        #   只调 super() —— 后者静默覆盖前者，「移动/缩放窗口期间暂停重绘」就失效了
        #   （表现为拖窗口时预览抢主线程，越拖越卡）。合并成一份。
        self._pause_repaint()
        super().resizeEvent(ev)
''')

# ============================== B fx_trails 容忍 bgr=None
rep("src/postprocess.py",
    '''def fx_trails(bgr, strength, reset=False):
    """拖影：上一帧衰减后与本帧取 max（不叠白）。强度越大残影越久"""
    prev = _TRAIL["buf"]
''',
    '''def fx_trails(bgr, strength, reset=False):
    """拖影：上一帧衰减后与本帧取 max（不叠白）。强度越大残影越久。

    ⚠ `bgr=None` 是**清空拖影缓冲**的约定（引擎在「效果关闭 / 被 Kv 接管」时
      调 `fx_trails(None, 0.0, reset=True)`）。旧实现直接 `bgr.copy()` ⇒
      `AttributeError` ⇒ 被上层 `except Exception: pass` 吞掉 ⇒ **缓冲永远清不掉**，
      下次再开拖影会闪一下上一场的残影（症状很隐蔽，日志里什么都没有）。
    """
    if bgr is None:
        _TRAIL["buf"] = None
        return None
    prev = _TRAIL["buf"]
''')

# ============================== C 13 个 GUI 入口补锁
C_LOCK = [
    ("src/engine.py", "    def set_canvas_size(self, w, h):"),
    ("src/engine.py", "    def set_render_fps(self, fps):"),
    ("src/engine.py", "    def manual_mode(self):"),
    ("src/engine.py", "    def phrase_countdown(self, snap):"),
    ("src/engine.py", "    def flip_phrase_now(self, snap):"),
    ("src/engine.py", "    def beat_grid_view(self, snap):"),
    ("src/engine.py", "    def switch_countdown(self, snap):"),
    ("src/engine.py", "    def next_clip_for(self, lay):"),
    ("src/engine.py", "    def lock_clip(self):"),
    ("src/engine.py", "    def kv_layer(self):"),
    ("src/engine.py", "    def set_blackout(self, on):"),
    ("src/engine.py", "    def set_freeze(self, on):"),
    ("src/engine.py", "    def set_pause_auto(self, on):"),
]
for rel, line in C_LOCK:
    rep(rel, line, "    @_locked\n" + line)

# ============================== D1 识别窗口常量
rep("src/audio_engine.py",
    "class _AudioRing:",
    """# 现场指纹识别的**窗口长度**（秒）：每次取最近这么长的一段音频去做匹配。
# ⚠ 这个数在**两处**必须一致 —— ① 切窗 `last(sr*FP_WIN_SEC)`；② 位置补偿
#   `recognized_offset = off + FP_WIN_SEC * r`（窗口起点→窗口末端的原曲秒数）。
#   以前两处各写一个 4.5，改一处就会让「现在放到第几秒」整体偏移几秒（很难查）。
FP_WIN_SEC = 4.5


class _AudioRing:""")
rep("src/audio_engine.py",
    "                buf = self._genre_buf.last(int(self._genre_sr * 4.5))",
    "                buf = self._genre_buf.last(int(self._genre_sr * FP_WIN_SEC))")
rep("src/audio_engine.py",
    """                        # ⚠ 变速倍率 r≠1 时，4.5s 挂钟对应 4.5*r 秒原曲 —— 旧写法固定 +4.5 是错的。
                        self.state.recognized_offset = off + 4.5 * r""",
    """                        # ⚠ 变速倍率 r≠1 时，窗口秒数对应 FP_WIN_SEC*r 秒原曲
                        #   —— 旧写法固定 +4.5（且与切窗各写一份）是错的。
                        self.state.recognized_offset = off + FP_WIN_SEC * r""")

# ============================== D2 环形缓冲按实际采样率给足
rep("src/audio_engine.py",
    "            self._genre_sr = sr",
    """            with self._genre_lock:
                self._genre_sr = sr
                # ★ 缓冲容量必须按**实际采样率**给足：原来写死 `48000 * 8`，
                #   设备跑 96k 时 8 秒的缓冲只剩 4 秒 ⇒ 识别窗（FP_WIN_SEC）被截短、
                #   票数变少、甚至低于对齐门槛。12 秒足够容纳识别窗 + 曲风窗（8s）。
                need = int(sr * 12)
                if self._genre_buf.cap < need:
                    self._genre_buf = _AudioRing(need)""")

# ============================== E MatchTick 字段补齐
rep("src/charge_engine.py",
    """        self.streak_votes = 0
        self.streak_ticks = 0
""",
    """        self.streak_votes = 0
        self.streak_ticks = 0
        self.evidence_votes = 0     # ⚠ __slots__ 里声明了就必须初始化：
                                    #   漏了会 AttributeError（谁读谁炸，而且很难联想）
""")


def main():
    by_file = {}
    for rel, old, new, cnt in PAT:
        by_file.setdefault(rel, []).append((old, new, cnt))
    for rel, items in by_file.items():
        p = os.path.join(ROOT, rel)
        s = io.open(p, encoding="utf-8").read()
        orig = s
        for k, (old, new, cnt) in enumerate(items, 1):
            n = s.count(old)
            if n != cnt:
                print("✗ %s 第 %d 处：命中 %d 次（应为 %d），本文件不写" % (rel, k, n, cnt))
                print("   锚点首行：", old.splitlines()[0][:90])
                return 1
            s = s.replace(old, new)
        io.open(p, "w", encoding="utf-8", newline="\n").write(s)
        print("✓ %-24s %d → %d 行" % (rel, orig.count("\n") + 1, s.count("\n") + 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
