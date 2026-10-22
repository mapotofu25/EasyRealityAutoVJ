# -*- coding: utf-8 -*-
"""ERSND —— EasyReality 音频发送端（A 机独立小 exe 入口 + 界面）。

⚠ 原入口 `lan_audio_sender.pyw` 曾经丢失（git 从未跟踪），导致：
   · `tools/build_sender_exe.py` 无法打包；
   · `dist_icon2\EasyReality_音频发送端.exe` 停在上一个版本，
     **不含 2026-10-18 的「自回声 P0 修复」**（发送端会把自己广播的回声学成对端，
      音频只发回本机、对端收不到，界面却显示"已握手"）。
本文件按 `src/lan_audio_send.py` 的公开接口重建，只做三件事：
   ① 选采集设备 / 采样率 / 端口（可选手填对端 IP）；
   ② 起停 `Sender`（采集 + 发送都在它自己的线程里）；
   ③ 每秒刷新一行状态：模式 / 对端 / 包率 / 丢包 / 错误。

**新增入口脚本必须立刻 `git add`** —— 这是第二次丢入口了。
"""

import os
import sys

if not getattr(sys, "frozen", False):
    _here = os.path.dirname(os.path.abspath(__file__))
    _src = os.path.join(_here, "src")
    if os.path.isdir(_src) and _src not in sys.path:
        sys.path.insert(0, _src)

from PySide6.QtCore import Qt, QTimer                          # noqa: E402
from PySide6.QtWidgets import (QApplication, QComboBox, QFormLayout,  # noqa: E402
                               QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QMessageBox, QPushButton, QSpinBox, QVBoxLayout,
                               QWidget)

import lan_audio as LA                                          # noqa: E402
import lan_audio_send as LS                                     # noqa: E402

APP_TITLE = "EasyReality 音频发送端"


class SenderWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(560, 260)
        self.snd = None
        self._devs = []

        root = QWidget(self)
        self.setCentralWidget(root)
        v = QVBoxLayout(root)

        tip = QLabel(
            "把这台机器正在播放的声音（WASAPI 回环）发给另一台演出机。\n"
            "先点「开始」，再到另一台的音源里选「局域网」即可。")
        tip.setWordWrap(True)
        v.addWidget(tip)

        form = QFormLayout()
        self.cmb_dev = QComboBox()
        self.cmb_dev.setMinimumWidth(320)
        form.addRow("采集设备", self.cmb_dev)

        self.cmb_sr = QComboBox()
        for sr in (48000, 44100):
            self.cmb_sr.addItem("%d Hz" % sr, sr)
        form.addRow("采样率", self.cmb_sr)

        self.sp_port = QSpinBox()
        self.sp_port.setRange(1024, 65535)
        self.sp_port.setValue(int(getattr(LA, "DEFAULT_PORT", 47811)))
        form.addRow("端口", self.sp_port)

        self.ed_peer = QLineEdit()
        self.ed_peer.setPlaceholderText("留空 = 自动发现；也可填对端 IP（如 192.168.1.20）")
        form.addRow("对端 IP", self.ed_peer)
        v.addLayout(form)

        row = QHBoxLayout()
        self.btn_start = QPushButton("开始")
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_start.clicked.connect(self.start_send)
        self.btn_stop.clicked.connect(self.stop_send)
        row.addWidget(self.btn_start)
        row.addWidget(self.btn_stop)
        row.addStretch(1)
        v.addLayout(row)

        self.lbl = QLabel("未开始")
        self.lbl.setWordWrap(True)
        self.lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.lbl)

        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh_status)

        self.reload_devices()

    # ------------------------------------------------------------------
    def reload_devices(self):
        self.cmb_dev.clear()
        self._devs = []
        try:
            import soundcard as sc
            mics = sc.all_microphones(include_loopback=True)
        except Exception as e:                                   # noqa: BLE001
            self.lbl.setText("读取采集设备失败：%s" % e)
            return
        for m in mics:
            self._devs.append(m)
            # ★★ 与旧版（2026.10.08.2）一致：系统声音条目带前缀，
            #    `is_loopback` 由这个前缀决定 —— 见 start_send()。
            _is_loop = bool(getattr(m, "isloopback", False))
            self.cmb_dev.addItem(("[系统声音] " if _is_loop else "") + m.name, m.name)
        # 默认选中第一个"系统声音"（现场绝大多数就是要发系统声音）
        for i in range(self.cmb_dev.count()):
            if self.cmb_dev.itemText(i).startswith("[系统声音]"):
                self.cmb_dev.setCurrentIndex(i)
                break

    def _dest(self):
        ip = (self.ed_peer.text() or "").strip()
        if not ip:
            return None
        return (ip, int(self.sp_port.value()))

    def start_send(self):
        if self.snd is not None:
            return
        # ★★★ 与旧版逐参数对齐（从可用 exe 反解出来的）：
        #   is_loopback = "选中的是不是 [系统声音] 条目"，**绝不能用默认值/写死**。
        #   实测教训：默认 True 且名字对不上 ⇒ 一个 DATA 都没有；
        #             写死 False ⇒ 抓真麦克风 ⇒ 有包但全是静音。
        is_loop = self.cmb_dev.currentText().startswith("[系统声音]")
        dev = self.cmb_dev.currentData()          # 真实设备名（字符串）
        sr = int(self.cmb_sr.currentData() or 48000)
        try:
            # ★★ 2026-10-19 实测结论：`is_loopback` 必须显式给 False。
            #   默认是 True，而 True 时在这台机器上**只能发 HELLO、DATA 恒为 0**
            #   （采集会话拿不到数据）。显式 False 后 2.5 秒发出 245 个包、有 DATA。
            #   实测组合：None→3 包 / 名字(默认True)→3 包 / 名字+False→245 包。
            self.snd = LS.Sender(device=dev, sr=sr, port=int(self.sp_port.value()),
                                 dest=self._dest(), is_loopback=is_loop)
            ok = self.snd.start()
        except Exception as e:                                   # noqa: BLE001
            self.snd = None
            QMessageBox.warning(self, APP_TITLE, "启动失败：%s" % e)
            return
        if not ok:
            err = getattr(self.snd, "error", "") or "未知原因"
            self.snd = None
            QMessageBox.warning(self, APP_TITLE, "启动失败：%s" % err)
            return
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.timer.start()
        self.lbl.setText("已开始，正在等对端…")

    def stop_send(self):
        self.timer.stop()
        if self.snd is not None:
            try:
                self.snd.stop()
            except Exception:                                    # noqa: BLE001
                pass
            self.snd = None
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.lbl.setText("已停止")

    def refresh_status(self):
        if self.snd is None:
            return
        try:
            s = self.snd.snapshot()
        except Exception as e:                                   # noqa: BLE001
            self.lbl.setText("状态读取失败：%s" % e)
            return
        mode = s.get("mode") or "—"
        peer = s.get("peer") or "（还没握手）"
        pps = s.get("pps") or 0
        lost = s.get("dropped") or 0
        err = s.get("error") or ""
        txt = ("模式：%s ｜ 对端：%s ｜ 包率：%.1f 包/秒 ｜ 丢：%d"
               % (mode, peer, float(pps), int(lost)))
        if err:
            txt += "\n⚠ %s" % err
        self.lbl.setText(txt)

    def closeEvent(self, ev):
        self.stop_send()
        super().closeEvent(ev)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    try:
        from PySide6.QtGui import QIcon
        import appicon
        _p = appicon.icon_path("send")
        if _p:
            app.setWindowIcon(QIcon(_p))
    except Exception:                                            # noqa: BLE001
        pass
    w = SenderWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
