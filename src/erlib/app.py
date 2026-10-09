# -*- coding: utf-8 -*-
r"""erlib 界面 —— 「选曲库文件夹 → 扫描 → 勾选 → 导出 `.eravjlib`」。

## 为什么扫描先写"工作库"，导出再从工作库筛子集
如果边扫边写最终包，那么**没勾的歌也会在包里**。
所以拆成两步：

    工作库（本机持久，支持续扫）      →   导出（只挑勾选的）
    %LOCALAPPDATA%\AutoVJ\erlib_scan.eravjlib    你指定路径的 .eravjlib

好处：
1. **续扫**：大曲库跑一小时，中途关了下次接着扫（已扫的自动跳过）；
2. **一次扫描、多次导出**（这次导 100 首、下次导另外 200 首，不用重扫）；
3. **勾选真正生效**（没勾的一字节都不写进包里）。

## 扫描与导出都在 QThread 里跑
指纹 2.4 s/首 ⇒ 1452 首要一小时，绝不能卡界面。
"""

import os
import sys
import time

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMainWindow,
                               QMessageBox, QProgressBar, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from erlib.format import LibReader, LibWriter                       # noqa: E402
from erlib.scanner import Scanner                                   # noqa: E402

WORK_NAME = "erlib_scan.eravjlib"
APP_TITLE = "ERLIB曲库分析"


def work_path():
    """工作库路径（扫描成果持久保留 ⇒ 支持续扫与多次导出）。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "AutoVJ")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:                                            # noqa: BLE001
        d = os.path.expanduser("~")
    return os.path.join(d, WORK_NAME)


def fmt_dur(sec):
    try:
        s = int(float(sec or 0))
        return "%d:%02d" % (s // 60, s % 60)
    except Exception:                                            # noqa: BLE001
        return ""


# ==========================================================================
# 后台线程
# ==========================================================================
class ScanThread(QThread):
    progress = Signal(int, int, str)
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, root, writer, use_online=False, parent=None):
        super().__init__(parent)
        self.root = root
        self.writer = writer
        self.use_online = use_online
        self._sc = None

    def stop(self):
        if self._sc is not None:
            self._sc.stop()

    def run(self):
        try:
            self._sc = Scanner(root=self.root, on_progress=self._prog,
                               use_online=self.use_online)
            st = self._sc.scan_tree(self.root, self.writer)
            self.writer.close()
            self.finished_ok.emit(st)
        except Exception as e:                                   # noqa: BLE001
            try:
                self.writer.close()
            except Exception:                                    # noqa: BLE001
                pass
            self.failed.emit("%s: %s" % (type(e).__name__, e))

    def _prog(self, done, total, path):
        self.progress.emit(done, total, path)


class ExportThread(QThread):
    progress = Signal(int, int)
    finished_ok = Signal(str, dict)
    failed = Signal(str)

    def __init__(self, work, out_path, sids, parent=None):
        super().__init__(parent)
        self.work = work
        self.out_path = out_path
        self.sids = set(int(x) for x in sids)

    def run(self):
        try:
            from erlib.transfer import export_subset
            st = export_subset(
                self.work, self.out_path, self.sids,
                on_progress=lambda a, b: self.progress.emit(a, b),
                app_ver="erlib")
            self.finished_ok.emit(self.out_path, st)
        except Exception as e:                                   # noqa: BLE001
            self.failed.emit("%s: %s" % (type(e).__name__, e))


# ==========================================================================
# 主窗口
# ==========================================================================
COLS = ["", "文件名", "艺人", "曲名", "曲风", "时长", "状态"]


class OnlineThread(QThread):
    """「联网补跑曲风」：对工作库里**还没有精确在线结果**的歌重跑曲风解析。

    与主程序 `ui_main.MusicScanThread` 的串行阶段（阶段 2）**同款策略**：
      · 只跑"需要补"的（`level != track` 且 `source` 不是 discogs/itunes）；
      · 进来先 `clear_online_cache()` —— 扫描阶段联网是关的，那些"查不到"会被
        缓存成 None，不清掉的话每首都命中失败缓存、**根本不会发请求**（实测 0.2 秒跑完）；
      · 被限流（`genre_lookup` 自动置位 `_blocked`）⇒ 提前收手（后续走本地 AI 兜底）；
      · 连续 5 首单首 > 2.5 秒 ⇒ 判定网络不佳，提前结束；
      · 每 5 首落盘 ⇒ **断点续跑**：中断/关闭后重跑会自然接着来（已升级的不再重跑）。

    ⚠ 只更新曲风相关列（`LibWriter.update_genres`），**绝不动**网格/指纹/封面。
    """

    progress = Signal(int, int, str)      # 已试, 总数, 当前歌名
    finished_ok = Signal(int, int)        # 成功补到, 实际尝试
    failed = Signal(str)

    def __init__(self, work, root, parent=None):
        super().__init__(parent)
        self.work = work
        self.root = root
        self._stop = False

    def stop(self):
        self._stop = True

    def pending(self):
        """待补跑清单：`[(sid, 完整路径), ...]`（文件不在的跳过）。"""
        out = []
        with LibReader(self.work) as r:
            for s in r.songs():
                src = (s.get("source") or "").lower()
                lvl = (s.get("level") or "").lower()
                if lvl == "track" or src in ("discogs", "itunes"):
                    continue
                rel = s.get("rel_path") or s.get("file_name") or ""
                p = os.path.join(self.root, rel) if (rel and self.root) else ""
                if p and os.path.isfile(p):
                    out.append((int(s["song_id"]), p))
        return out

    def run(self):
        try:
            self._run()
        except Exception as e:                                   # noqa: BLE001
            self.failed.emit("%s: %s" % (type(e).__name__, e))

    def _run(self):
        import music_meta as MM
        import genre_lookup as GL
        todo = self.pending()
        total = len(todo)
        if not total:
            self.finished_ok.emit(0, 0)
            return
        w = LibWriter(self.work, root=self.root, resume=True, app_ver="erlib")
        fixed = tried = slow = 0
        try:
            MM.clear_online_cache()          # ⚠ 见类 docstring（不清 = 一首都不查）
            GL.set_online_enabled(True)
            for sid, p in todo:
                if self._stop or GL.online_blocked():
                    break
                self.progress.emit(tried, total, os.path.basename(p))
                t0 = time.perf_counter()
                try:
                    r2 = MM.resolve_music_genre(p)
                    if r2 and (r2.get("level")
                               or (r2.get("source") in ("discogs", "itunes"))):
                        w.update_genres(sid, r2)
                        fixed += 1
                        if fixed % 5 == 0:
                            w.commit()   # 每 5 首落盘（断点续跑）
                except Exception:                                # noqa: BLE001
                    pass
                tried += 1
                dt = time.perf_counter() - t0
                slow = slow + 1 if dt > 2.5 else 0
                if slow >= 5:            # 网络不佳，别再耗
                    break
            w.commit()
        finally:
            GL.set_online_enabled(True)
            w.close()
        self.finished_ok.emit(fixed, tried)


class ErlibWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1180, 720)
        self.root = ""
        self.work = work_path()
        self.scan_th = None
        self.exp_th = None
        self.on_th = None        # 联网补跑线程
        self._work_busy = False  # ★ 修复2：工作库写任务互斥（扫描/联网补跑/删除）
        self._n_checked = 0
        self._t0 = 0.0           # 本轮扫描起点（算"剩余约 X 分钟"用）
        self._build()
        self._load_existing()

    # ---------------- UI ----------------
    def _build(self):
        c = QWidget()
        self.setCentralWidget(c)
        v = QVBoxLayout(c)

        top = QHBoxLayout()
        self.btn_dir = QPushButton("选择曲库文件夹…")
        self.btn_dir.clicked.connect(self._pick_dir)
        top.addWidget(self.btn_dir)
        self.lbl_root = QLineEdit()
        self.lbl_root.setReadOnly(True)
        self.lbl_root.setPlaceholderText("（还没选曲库文件夹）")
        top.addWidget(self.lbl_root, 1)
        v.addLayout(top)

        row2 = QHBoxLayout()
        self.btn_scan = QPushButton("开始扫描")
        self.btn_scan.clicked.connect(self._on_scan)
        row2.addWidget(self.btn_scan)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._on_stop)
        row2.addWidget(self.btn_stop)
        self.btn_online = QPushButton("联网补跑曲风")
        self.btn_online.setToolTip(
            "扫描阶段默认**不联网**（Discogs 未认证限 25 次/分钟，很慢）。\n"
            "扫完之后点这里，对「还没查到曲风」的歌补跑在线查询（可随时停）。")
        self.btn_online.clicked.connect(self._on_online)
        row2.addWidget(self.btn_online)
        row2.addStretch(1)
        self.btn_all = QPushButton("全选")
        self.btn_all.clicked.connect(lambda: self._check_all(True))
        row2.addWidget(self.btn_all)
        self.btn_none = QPushButton("全不选")
        self.btn_none.clicked.connect(lambda: self._check_all(False))
        row2.addWidget(self.btn_none)
        self.btn_del = QPushButton("删除勾选的歌")
        self.btn_del.setToolTip(
            "从工作库删除勾选的歌（指纹与封面一起删）。\n"
            "下次「开始扫描」会重新分析它们；已经导出过的包不受影响。")
        self.btn_del.clicked.connect(self._on_delete)
        row2.addWidget(self.btn_del)
        self.btn_export = QPushButton("导出勾选的歌…")
        self.btn_export.clicked.connect(self._on_export)
        row2.addWidget(self.btn_export)
        v.addLayout(row2)

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("筛选：输入歌名 / 艺人 / 曲风（回车生效）")
        self.filter.returnPressed.connect(self._apply_filter)
        v.addWidget(self.filter)

        self.tbl = QTableWidget(0, len(COLS))
        self.tbl.setHorizontalHeaderLabels(COLS)
        self.tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.verticalHeader().setVisible(False)
        h = self.tbl.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.tbl.setColumnWidth(0, 34)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for i in (2, 3, 4, 5, 6):
            h.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        self.tbl.itemChanged.connect(self._on_item_changed)
        v.addWidget(self.tbl, 1)

        bot = QHBoxLayout()
        self.bar = QProgressBar()
        self.bar.setValue(0)
        bot.addWidget(self.bar, 1)
        self.lbl = QLabel("就绪")
        bot.addWidget(self.lbl)
        v.addLayout(bot)

    # ---------------- 数据 ----------------
    def _add_row(self, s, checked=True):
        r = self.tbl.rowCount()
        self.tbl.insertRow(r)
        it = QTableWidgetItem("")
        it.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        it.setCheckState(Qt.CheckState.Checked if checked
                         else Qt.CheckState.Unchecked)
        it.setData(Qt.ItemDataRole.UserRole, int(s["song_id"]))
        self.tbl.setItem(r, 0, it)
        g = s.get("genres") or []
        if isinstance(g, str):
            g = [g]
        vals = [s.get("file_name", ""), s.get("artist", ""), s.get("title", ""),
                " / ".join(str(x) for x in g[:3]), fmt_dur(s.get("duration")),
                "已分析"]
        for i, t in enumerate(vals, start=1):
            self.tbl.setItem(r, i, QTableWidgetItem(str(t)))
        if checked:
            self._n_checked += 1

    def _load_existing(self):
        """启动时把工作库里已有的歌列出来（这样续扫/再次导出都能看到）。"""
        if not os.path.isfile(self.work):
            return
        try:
            self.tbl.setUpdatesEnabled(False)
            with LibReader(self.work) as r:
                for s in r.songs():
                    self._add_row(s, checked=True)
            m = LibReader(self.work).meta()
            self.root = m.get("root", "") or ""
            self.lbl_root.setText(self.root)
        except Exception as e:                                   # noqa: BLE001
            self.lbl.setText("读取工作库失败：%s" % e)
        finally:
            self.tbl.setUpdatesEnabled(True)
            self._update_status()

    def _all_sids(self):
        out = []
        for r in range(self.tbl.rowCount()):
            it = self.tbl.item(r, 0)
            if it is not None:
                out.append(int(it.data(Qt.ItemDataRole.UserRole) or 0))
        return out

    def _checked_sids(self):
        out = []
        for r in range(self.tbl.rowCount()):
            it = self.tbl.item(r, 0)
            if it is not None and it.checkState() == Qt.CheckState.Checked:
                out.append(int(it.data(Qt.ItemDataRole.UserRole) or 0))
        return out

    def _check_all(self, on):
        self.tbl.blockSignals(True)
        try:
            st = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
            for r in range(self.tbl.rowCount()):
                it = self.tbl.item(r, 0)
                if it is not None and not self.tbl.isRowHidden(r):
                    it.setCheckState(st)
        finally:
            self.tbl.blockSignals(False)
        self._update_status()

    def _on_item_changed(self, it):
        if it.column() == 0:
            self._update_status()

    def _apply_filter(self):
        q = self.filter.text().strip().lower()
        for r in range(self.tbl.rowCount()):
            if not q:
                self.tbl.setRowHidden(r, False)
                continue
            txt = " ".join(
                (self.tbl.item(r, c).text() if self.tbl.item(r, c) else "")
                for c in (1, 2, 3, 4)).lower()
            self.tbl.setRowHidden(r, q not in txt)
        self._update_status()

    def _update_status(self):
        n = len(self._checked_sids())
        self.lbl.setText("共 %d 首 · 已勾选 %d 首 · 工作库：%s"
                         % (self.tbl.rowCount(), n, os.path.basename(self.work)))

    # ---------------- 工作库写任务互斥（修复 2） ----------------
    def _set_work_busy(self, on):
        """置/清「会写工作库的长任务」忙状态（扫描 / 联网补跑 / 删除）。

        ★ 2026-10-10（T7 审计修复 2）：三个入口各自 new 一个 `LibWriter` 写**同一个**
          `erlib_scan.eravjlib`，此前互不检查 ⇒ 扫描大库（~1 小时）时点「联网补跑」或
          「删除勾选」，会让两个写库并发 ⇒ `database is locked` 被 `except` 吞掉
          （表现为"失败 N 首" / "补跑 0 首"），或 `update_genres` 的曲风更新被扫描阶段的
          整条 `INSERT OR REPLACE` 覆盖。这里用一个共享标志 + 按钮互斥把它们**串行化**：
            忙 ⇒ 三个写工作库按钮全部禁用；空闲 ⇒ 恢复。
        ⚠ **不改线程模型**：扫描 / 补跑仍是各自的 `QThread`，只是不允许同时启动。
        """
        self._work_busy = bool(on)
        for b in (self.btn_scan, self.btn_online, self.btn_del):
            try:
                b.setEnabled(not self._work_busy)
            except Exception:                                    # noqa: BLE001
                pass

    def _work_busy_guard(self):
        """若有别的写工作库任务在跑，提示用户并返回 True（调用方据此早退）。"""
        if self._work_busy:
            QMessageBox.information(
                self, APP_TITLE,
                "已有任务正在进行（扫描 / 联网补跑曲风 / 删除），"
                "请等它结束后再操作。")
            return True
        return False

    # ---------------- 动作 ----------------
    def _pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择曲库文件夹", self.root or "")
        if d:
            self.root = d
            self.lbl_root.setText(d)

    def _on_scan(self):
        if not self.root or not os.path.isdir(self.root):
            QMessageBox.information(self, APP_TITLE, "先选一个曲库文件夹。")
            return
        if self.scan_th is not None and self.scan_th.isRunning():
            return
        if self._work_busy_guard():                              # ★ 修复2：互斥
            return
        self.tbl.setUpdatesEnabled(False)
        try:
            w = LibWriter(self.work, root=self.root, resume=True, app_ver="erlib")
        finally:
            self.tbl.setUpdatesEnabled(True)
        # 续扫：文件里已有的先列出来并标记为已分析
        have = w.existing_ids()
        self.tbl.setRowCount(0)
        self._n_checked = 0
        try:
            self.tbl.setUpdatesEnabled(False)
            with LibReader(self.work) as _r:
                for s in _r.songs():
                    self._add_row(s, checked=True)
        except Exception:                                        # noqa: BLE001
            pass
        finally:
            self.tbl.setUpdatesEnabled(True)
        self.lbl.setText("续扫：工作库里已有 %d 首，本次只扫新增的。" % len(have))
        self._t0 = time.perf_counter()

        self.btn_scan.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._set_work_busy(True)                                # ★ 修复2：置忙（禁用另两个）
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        # ★★ 回归修复（QA 实测）：`start()` 可能抛异常（线程资源不足等）。此时**必须回滚**
        #   忙标志，否则三个写工作库按钮会**永久禁用、只能重启软件**
        #   （旧代码最多只 disable 1 个按钮）。照 `_on_delete` 的 try/finally 精神处理。
        try:
            self.scan_th = ScanThread(self.root, w, use_online=False, parent=self)
            self.scan_th.progress.connect(self._on_prog)
            self.scan_th.finished_ok.connect(self._on_scan_done)
            self.scan_th.failed.connect(self._on_scan_fail)
            self.scan_th.start()
        except Exception as e:                                   # noqa: BLE001
            self._set_work_busy(False)                           # ★ 异常路径回滚
            self.btn_stop.setEnabled(False)
            QMessageBox.warning(self, APP_TITLE, "扫描启动失败：%s" % e)
            return

    def _on_stop(self):
        if self.scan_th is not None:
            self.scan_th.stop()
            self.lbl.setText("正在停止（等当前这首扫完）…")
        if self.on_th is not None and self.on_th.isRunning():
            self.on_th.stop()          # 联网补跑也能中途停（已补的已落盘）
            self.lbl.setText("正在停止联网补跑（等当前这首查完）…")

    def _on_prog(self, done, total, path):
        if total > 0:
            self.bar.setRange(0, total)
            self.bar.setValue(done)
        left = ""
        if done > 0 and total > done:
            per = 3.0
            if self._t0:
                per = max(0.01, (time.perf_counter() - self._t0) / done)
            left = " · 剩余约 %.0f 分钟" % (per * (total - done) / 60.0)
        self.lbl.setText("已扫 %d / %d%s · %s"
                         % (done, total, left, os.path.basename(path)))

    def _on_scan_done(self, st):
        self.btn_scan.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._set_work_busy(False)                               # ★ 修复2：解除互斥
        self.bar.setRange(0, 100)
        self.bar.setValue(100)
        self.tbl.setRowCount(0)
        self._n_checked = 0
        self._load_existing()
        msg = ("扫描完成：成功 %d 首，失败 %d 首，跳过（已扫过）%d 首。\n"
               "用时 %.1f 分钟。\n"
               % (st.get("ok", 0), st.get("fail", 0), st.get("skip", 0),
                  st.get("elapsed", 0) / 60.0))
        errs = st.get("errors") or []
        if errs:
            msg += "\n失败原因（前 5 条）：\n"
            msg += "\n".join("· %s\n    %s" % (a, b) for a, b in errs[:5])
            msg += "\n\n（完整原因见控制台输出）"
        elif st.get("ok", 0) == 0:
            msg += ("\n⚠ 一首都没扫到。请确认：\n"
                    "· 选的文件夹里真的有音频文件（flac/mp3/m4a/wav…）\n"
                    "· 或者点「开始扫描」前先选好文件夹")
        else:
            msg += "\n现在可以勾选要导出的歌，然后点「导出勾选的歌」。"
        if st.get("fail", 0) and not st.get("ok", 0):
            QMessageBox.warning(self, APP_TITLE, msg)
        else:
            QMessageBox.information(self, APP_TITLE, msg)

    def _on_scan_fail(self, msg):
        self.btn_scan.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._set_work_busy(False)                               # ★ 修复2：解除互斥
        QMessageBox.warning(self, APP_TITLE, "扫描出错：%s" % msg)

    def _on_online(self):
        """联网补跑曲风：对工作库里还没有精确在线结果的歌重跑（见 OnlineThread）。"""
        if self.on_th is not None and self.on_th.isRunning():
            return
        if self._work_busy_guard():                              # ★ 修复2：互斥
            return
        if not os.path.isfile(self.work):
            QMessageBox.information(self, APP_TITLE, "还没有工作库，先「开始扫描」。")
            return
        if not self.root:
            try:
                self.root = (LibReader(self.work).meta() or {}).get("root", "") or ""
                self.lbl_root.setText(self.root)
            except Exception:                                    # noqa: BLE001
                self.root = ""
        if not self.root or not os.path.isdir(self.root):
            QMessageBox.information(
                self, APP_TITLE,
                "找不到曲库文件夹了。\n\n"
                "工作库记录的路径：%s\n\n"
                "请先「选择曲库文件夹」选中原来的曲库，再点「联网补跑曲风」。"
                % (self.root or "（空）"))
            return
        self.btn_online.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._set_work_busy(True)                                # ★ 修复2：置忙（禁用另两个）
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.lbl.setText("联网补跑：准备中…")
        # ★★ 回归修复（QA 实测）：同 `_on_scan` —— `start()` 失败必须回滚忙标志，
        #   否则按钮永久禁用。
        try:
            self.on_th = OnlineThread(self.work, self.root, parent=self)
            self.on_th.progress.connect(self._on_on_prog)
            self.on_th.finished_ok.connect(self._on_online_done)
            self.on_th.failed.connect(self._on_online_fail)
            self.on_th.start()
        except Exception as e:                                   # noqa: BLE001
            self._set_work_busy(False)                           # ★ 异常路径回滚
            self.btn_stop.setEnabled(False)
            QMessageBox.warning(self, APP_TITLE, "联网补跑启动失败：%s" % e)
            return

    def _on_on_prog(self, done, total, name):
        if total > 0:
            self.bar.setRange(0, total)
            self.bar.setValue(done)
        self.lbl.setText("联网补跑 %d / %d · %s" % (done, total, name))

    def _on_online_done(self, fixed, tried):
        self.btn_online.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._set_work_busy(False)                               # ★ 修复2：解除互斥
        self.bar.setRange(0, 100)
        self.bar.setValue(100 if tried else 0)
        try:
            import genre_lookup as GL
            blocked = GL.online_blocked()
        except Exception:                                        # noqa: BLE001
            blocked = False
        if tried == 0:
            QMessageBox.information(
                self, APP_TITLE,
                "没有需要补跑的歌（都已有精确在线结果，或曲库文件对不上）。")
        else:
            QMessageBox.information(
                self, APP_TITLE,
                "联网补跑完成：新查到 %d 首（共尝试 %d 首）。%s\n\n"
                "还有没补上的，可以直接再点一次「联网补跑曲风」 —— "
                "已经补好的不会重跑。" % (
                    fixed, tried,
                    "\n⚠ 触发了限流（Discogs 约 25 次/分钟），"
                    "剩余的歌过几分钟再点一次即可。" if blocked else ""))
        self._reload_table()

    def _on_online_fail(self, msg):
        self.btn_online.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._set_work_busy(False)                               # ★ 修复2：解除互斥
        QMessageBox.warning(self, APP_TITLE, "联网补跑出错：%s" % msg)

    def _on_delete(self):
        """从工作库删除勾选的歌（批量；指纹与封面一起删）。"""
        sids = self._checked_sids()
        if not sids:
            QMessageBox.information(self, APP_TITLE, "还没勾选任何歌。")
            return
        if self._work_busy_guard():                              # ★ 修复2：互斥
            return
        ret = QMessageBox.question(
            self, APP_TITLE,
            "从工作库删除勾选的 %d 首？\n\n"
            "· 它们的指纹与封面会一起删掉；\n"
            "· 下次「开始扫描」会重新分析它们；\n"
            "· 已经导出过的 .eravjlib 不受影响。" % len(sids),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._set_work_busy(True)                                # ★ 修复2：置忙
        try:
            try:
                w = LibWriter(self.work, root=self.root, resume=True, app_ver="erlib")
                try:
                    n = w.delete_songs(sids)
                finally:
                    w.close()
            except Exception as e:                               # noqa: BLE001
                QMessageBox.warning(self, APP_TITLE, "删除失败：%s" % e)
                return
        finally:
            self._set_work_busy(False)                           # ★ 修复2：解除互斥
        self._reload_table()
        self.lbl.setText("已删除 %d 首（工作库剩 %d 首）"
                         % (n, self.tbl.rowCount()))

    def _reload_table(self):
        """从工作库重新加载整张表（删除 / 联网补跑后用）。"""
        self.tbl.setRowCount(0)
        self._n_checked = 0
        try:
            self.tbl.setUpdatesEnabled(False)
            with LibReader(self.work) as r:
                for s in r.songs():
                    self._add_row(s, checked=True)
        except Exception as e:                                   # noqa: BLE001
            self.lbl.setText("读取工作库失败：%s" % e)
        finally:
            self.tbl.setUpdatesEnabled(True)
            self._update_status()

    def _on_export(self):
        sids = self._checked_sids()
        if not sids:
            QMessageBox.information(self, APP_TITLE, "还没勾选任何歌。")
            return
        p, _ = QFileDialog.getSaveFileName(
            self, "导出分析包", "我的曲库.eravjlib",
            "erlib 分析包 (*.eravjlib)")
        if not p:
            return
        self.btn_export.setEnabled(False)
        self.bar.setRange(0, len(sids))
        self.bar.setValue(0)
        self.exp_th = ExportThread(self.work, p, sids, parent=self)
        self.exp_th.progress.connect(lambda a, b: (self.bar.setValue(a),
                                                   self.lbl.setText("导出中 %d/%d" % (a, b))))
        self.exp_th.finished_ok.connect(self._on_export_done)
        self.exp_th.failed.connect(self._on_export_fail)
        self.exp_th.start()

    def _on_export_done(self, path, st):
        self.btn_export.setEnabled(True)
        self.bar.setRange(0, 100)
        self.bar.setValue(100)
        mb = os.path.getsize(path) / 1e6 if os.path.isfile(path) else 0
        QMessageBox.information(
            self, APP_TITLE,
            "导出完成：\n%s\n\n%d 首歌 · %d 条指纹 · %.1f MB\n\n"
            "把这个文件拷到演出机，在 ERAVJ 里导入即可。"
            % (path, st.get("songs", 0), st.get("fingerprints", 0), mb))
        self.lbl.setText("导出完成：%s" % os.path.basename(path))

    def _on_export_fail(self, msg):
        self.btn_export.setEnabled(True)
        QMessageBox.warning(self, APP_TITLE, "导出出错：%s" % msg)


def main():
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    w = ErlibWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
