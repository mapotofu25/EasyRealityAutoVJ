"""曲库查重对话框（用户 2026-10-29 要求）。

一句话：扫到同一首歌的两份时，把两条**并排摆出来**让用户选留哪一份。

为什么要它：同一段音频在曲库里放两份，指纹投票会被劈成两半
（实测 `ptot=244` vs 第二名 `223`），「总票领先 2 倍」这类判据永远不成立，
症状是**有时认得出、有时认不出**，现场极难查。

⚠ 这里的「移除」只删**曲库条目**（`music_library` + `music_meta`），
  **不动磁盘文件**。要连文件一起处理是另一条路（用户自己删/挪），
  本对话框不做 —— 误删音频是不可逆的，不该在一次点击里发生。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QHeaderView)

import dups as D
from i18n import T


def _fmt_dur(sec):
    try:
        s = int(round(float(sec)))
    except (TypeError, ValueError):
        return "—"
    return "%d:%02d" % (s // 60, s % 60)


class DuplicateDialog(QDialog):
    """两个疑似重复的曲目并排对比。

    `choice()` 返回 `"a"` / `"b"` / `"both"` / `"skip"`：
      · `"a"` / `"b"` = 保留这一侧、移除另一侧
      · `"both"`      = 都保留（误报时的出路）
      · `"skip"`      = 暂时不决定（关窗口等同此）
    """

    def __init__(self, parent, pair, meta_a, meta_b):
        super().__init__(parent)
        self._choice = "skip"
        self.pair = dict(pair or {})
        self._a = D.describe(pair.get("a", ""), meta_a)
        self._b = D.describe(pair.get("b", ""), meta_b)
        self.setWindowTitle(T("发现重复曲目"))
        self.resize(760, 420)

        root = QVBoxLayout(self)

        # ---- 结论行 ----
        ov = pair.get("overlap")
        if pair.get("why") == "fp":
            why = T("指纹重合 {}%（名字不同，但确认是同一段音频）").format(
                "%d" % round((ov or 0) * 100))
        else:
            why = T("标题相似度 {}%，时长相差 {} 秒").format(
                "%.0f%%" % (100 * float(pair.get("title_sim") or 0)),
                "%.1f" % abs(float(pair.get("dur_a") or 0) - float(pair.get("dur_b") or 0)))
        lab = QLabel(why)
        lab.setWordWrap(True)
        root.addWidget(lab)

        tip = QLabel(T("同一段音频在曲库里放两份，认歌的票会被劈成两半 —— "
                       "表现是「有时认得出、有时认不出」。建议只留一份。"))
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#9aa4b2;")
        root.addWidget(tip)

        # ---- 对比表 ----
        tbl = QTableWidget(7, 3)
        tbl.setHorizontalHeaderLabels([T("项目"), T("文件 A"), T("文件 B")])
        tbl.verticalHeader().setVisible(False)
        tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        tbl.setSelectionMode(QTableWidget.NoSelection)
        rows = [
            (T("文件名"), self._a["name"], self._b["name"]),
            (T("标题"), self._a["title"], self._b["title"]),
            (T("作者"), self._a["artist"], self._b["artist"]),
            (T("时长"), _fmt_dur(self._a["dur"]), _fmt_dur(self._b["dur"])),
            (T("格式 / 大小"),
             "%s · %.1f MB" % (self._a["fmt"] or "?", self._a["size_mb"]),
             "%s · %.1f MB" % (self._b["fmt"] or "?", self._b["size_mb"])),
            (T("BPM"),
             ("%.1f" % self._a["bpm"]) if self._a["bpm"] > 0 else "—",
             ("%.1f" % self._b["bpm"]) if self._b["bpm"] > 0 else "—"),
            (T("完整路径"), self._a["path"], self._b["path"]),
        ]
        for r, (k, va, vb) in enumerate(rows):
            for c, txt in enumerate((k, va, vb)):
                it = QTableWidgetItem(str(txt))
                if c == 0:
                    it.setForeground(Qt.gray)
                if c == 2:
                    it.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                tbl.setItem(r, c, it)
        hh = tbl.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        root.addWidget(tbl, 1)

        # ---- 建议 ----
        # ⚠ **故意不给"建议保留哪一份"**。
        #   试过按"文件更大/无损"推荐，但那可能把用户**平时播放的那份**移掉 ——
        #   移除后它的 song_id 就不在白名单里了（`_init_recognizer` 用 music_meta
        #   的 song_id 建 `set_allowed`），下次放那个文件反而认不出来。
        #   唯一靠谱的判据是"你平时放哪个"，只有用户知道。
        rl = QLabel(T("请保留你平时播放的那一份 —— 另一份只是重复入库。"))
        rl.setStyleSheet("color:#8ec07c;")
        rl.setWordWrap(True)
        root.addWidget(rl)

        # ---- 按钮 ----
        btn = QHBoxLayout()
        b_keep_a = QPushButton(T("保留 A，移除 B"))
        b_keep_b = QPushButton(T("保留 B，移除 A"))
        b_both = QPushButton(T("都保留"))
        b_skip = QPushButton(T("稍后决定"))

        b_keep_a.clicked.connect(lambda: self._done("a"))
        b_keep_b.clicked.connect(lambda: self._done("b"))
        b_both.clicked.connect(lambda: self._done("both"))
        b_skip.clicked.connect(lambda: self._done("skip"))
        for b in (b_keep_a, b_keep_b, b_both):
            btn.addWidget(b)
        btn.addStretch(1)
        btn.addWidget(b_skip)
        root.addLayout(btn)

        note = QLabel(T("移除只删曲库条目，不会删除磁盘上的音频文件。"))
        note.setWordWrap(True)
        note.setStyleSheet("color:#d0a050;")
        root.addWidget(note)

    def _done(self, v):
        self._choice = v
        self.accept()

    def choice(self):
        return self._choice
