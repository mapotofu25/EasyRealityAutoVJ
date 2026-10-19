# -*- coding: utf-8 -*-
"""EasyReality 音频发送端 —— A 机独立小 exe（局域网音频采集发送）。

## 用途
把 A 机（放歌那台）的**系统声音**采集出来，通过局域网实时发给 B 机的 ERAVJ。
配合 ERAVJ 的「局域网音频（另一台电脑）」音源使用。

## 为什么单独做一个小软件
1. A 机通常就在跑 VirtualDJ —— 把采集塞进主程序会和它抢 ASIO/WASAPI 独占；
2. 独立 exe 只有 ~15MB（对比 NDI 的 27MB 单个 DLL / 107MB 整包），
   A 机不必装整个 ERAVJ；
3. 崩溃隔离：采集端崩了不会带走 B 机的演出。

## 依赖
只用 `soundcard` + `wasapi_capture` + numpy + 标准库 socket。**零第三方协议依赖。**

## 界面
极简：设备选择 / 目标地址 / 端口 / 电平表 / 状态行 / 开始-停止。
"""

import os
import sys
import time

# 让 `src/` 里的模块能被 import（打包后 PyInstaller 会把它们放在同一层）
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(_HERE, "src"), _HERE):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import numpy as np
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import (QApplication, QComboBox, QFrame, QGridLayout,
                                   QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                                   QProgressBar, QPushButton, QVBoxLayout, QWidget)
except Exception as _e:                                            # noqa: BLE001
    print("缺少运行库：%s" % _e)
    raise

import lan_audio as LA              # noqa: E402
import lan_audio_send as LS         # noqa: E402

APP_VER = "2026.10.08.2"


class SenderWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("EasyReality 音频发送端  ·  局域网音频采集")
        self.setMinimumWidth(560)
        self.snd = None
        self._ip = LA.local_ip()

        w = QWidget()
        self.setCentralWidget(w)
        root = QVBoxLayout(w)

        tip = QLabel(
            "本程序把<b>这台电脑的声音</b>实时发送到另一台电脑的 Easy Reality AutoVJ。\n"
            "另一台电脑在「音源设置 → 音源类型」里选<b>「局域网音频（另一台电脑）」</b>即可收到。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#9fdcff;background:#14202c;padding:8px;border-radius:4px;")
        root.addWidget(tip)

        g = QGridLayout()

        g.addWidget(QLabel("采集设备"), 0, 0)
        self.dev = QComboBox()
        self.dev.setMinimumWidth(300)
        g.addWidget(self.dev, 0, 1, 1, 2)
        btn_rescan = QPushButton("重新扫描")
        btn_rescan.clicked.connect(self.scan)
        g.addWidget(btn_rescan, 0, 3)

        g.addWidget(QLabel("目标地址"), 1, 0)
        self.dest = QLineEdit("")
        self.dest.setPlaceholderText("留空 = 广播（自动发现）；或填对端 IP，如 192.168.1.20")
        g.addWidget(self.dest, 1, 1, 1, 3)

        g.addWidget(QLabel("端口"), 2, 0)
        self.port = QLineEdit(str(LA.DEFAULT_PORT))
        self.port.setMaximumWidth(120)
        g.addWidget(self.port, 2, 1)
        g.addWidget(QLabel("（与对端保持一致，默认 47811）"), 2, 2, 1, 2)
        root.addLayout(g)

        # 电平表
        box = QFrame()
        box.setStyleSheet("QFrame{background:#111;border:1px solid #2a3340;}")
        bl = QVBoxLayout(box)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(14)
        self.bar.setStyleSheet("QProgressBar{background:#1b2026;border:none;}"
                               "QProgressBar::chunk{background:#4fc3f7;}")
        bl.addWidget(QLabel("发送电平"))
        bl.addWidget(self.bar)
        self.lbl_status = QLabel("未运行")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setStyleSheet("color:#bbb;font-size:12px;")
        bl.addWidget(self.lbl_status)
        root.addWidget(box)

        self.btn = QPushButton("开始发送")
        self.btn.setFixedHeight(40)
        f = QFont()
        f.setPointSize(12)
        f.setBold(True)
        self.btn.setFont(f)
        self.btn.setStyleSheet("background:#2e7d32;color:#fff;border-radius:4px;")
        self.btn.clicked.connect(self.toggle)
        root.addWidget(self.btn)

        info = QLabel(self._info_text())
        info.setStyleSheet("color:#777;font-size:11px;")
        info.setWordWrap(True)
        root.addWidget(info)
        self._info = info

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(120)
        self.scan()

    def _info_text(self):
        ip = self._ip or "（未检测到局域网连接）"
        return ("本机局域网 IP：<b>%s</b>　　版本 %s\n"
                "若对端找不到，请把上面的 IP 填进对端的地址栏；"
                "两台电脑需在同一局域网，防火墙放行 UDP。"
                % (ip, APP_VER))

    # ---------------- 扫描 ----------------
    def scan(self):
        self.dev.blockSignals(True)
        self.dev.clear()
        try:
            import soundcard as sc
            mics = sc.all_microphones(include_loopback=True)
            loops = [m for m in mics if bool(getattr(m, "isloopback", False))]
            others = [m for m in mics if not bool(getattr(m, "isloopback", False))]
            for m in loops:
                self.dev.addItem("[系统声音] " + m.name, m.name)
            for m in others:
                self.dev.addItem("[输入] " + m.name, m.name)
            if not loops and not others:
                self.dev.addItem("（未找到设备）", "")
        except Exception as e:                                     # noqa: BLE001
            self.dev.addItem("扫描失败：%s" % e, "")
        self.dev.blockSignals(False)

    # ---------------- 运行 ----------------
    def toggle(self):
        if self.snd is not None:
            self.stop()
        else:
            self.start()

    def start(self):
        name = self.dev.currentData() or ""
        if not name:
            self.lbl_status.setText("请先选择采集设备")
            return
        try:
            port = int("".join(c for c in self.port.text() if c.isdigit()) or LA.DEFAULT_PORT)
        except Exception:                                          # noqa: BLE001
            port = LA.DEFAULT_PORT
        d = (self.dest.text() or "").strip()
        dest = (d, port) if d else None
        # loopback 设备名前面带 "[系统声音] " 标记
        is_loop = self.dev.currentText().startswith("[系统声音]")
        self.snd = LS.Sender(device=name, sr=48000, dest=dest, port=port,
                             is_loopback=is_loop)
        if not self.snd.start():
            self.lbl_status.setText("启动失败：%s" % self.snd.error)
            self.snd = None
            return
        self.btn.setText("停止发送")
        self.btn.setStyleSheet("background:#8a2f2f;color:#fff;border-radius:4px;")
        self.dev.setEnabled(False)
        self.dest.setEnabled(False)
        self.port.setEnabled(False)

    def stop(self):
        if self.snd is not None:
            try:
                self.snd.stop()
            except Exception:                                      # noqa: BLE001
                pass
            self.snd = None
        self.btn.setText("开始发送")
        self.btn.setStyleSheet("background:#2e7d32;color:#fff;border-radius:4px;")
        self.dev.setEnabled(True)
        self.dest.setEnabled(True)
        self.port.setEnabled(True)
        self.lbl_status.setText("未运行")
        self.bar.setValue(0)

    def tick(self):
        if self.snd is None:
            return
        snap = self.snd.snapshot()
        pps = snap["pps"]
        # 电平：用发包速率近似（10ms 包 ⇒ 理想 100pps）
        self.bar.setValue(int(max(0.0, min(1.0, pps / 100.0)) * 100))
        # ★ 对端状态要**分模式**说清 —— 原来不管什么模式都写"等待对端回应（广播模式下正常）"，
        #   这在点对点模式下是**误导**（用户明明填了 IP，没收到也以为正常）。
        if snap["peer"]:
            peer_txt = snap["peer"] + "（已握手）"
        elif snap["mode"] == "广播":
            peer_txt = "尚未收到对端回应 —— 请确认 B 机已把音源选为「局域网音频」"
        else:
            peer_txt = ("尚未收到对端回应 —— 请检查 IP / 端口、"
                        "防火墙是否放行 UDP %d" % self._port())
        # ★ 自愈重连提示：设备中断后若已自动恢复，必须**明确告诉用户**
        #   （否则用户看到过一次报错，会以为程序坏了要重启 —— 其实是它自己好了）。
        rc_txt = ""
        if snap.get("reconnects"):
            ago = time.perf_counter() - (snap.get("last_reconnect") or 0.0)
            if snap["error"]:
                rc_txt = "\n♻ 设备中断，正在自动重连…（第 %d 次）" % snap["reconnects"]
            elif ago < 6.0:
                rc_txt = "\n♻ 设备曾中断，已自动恢复（共 %d 次）" % snap["reconnects"]
            else:
                rc_txt = "\n♻ 本场自动重连 %d 次" % snap["reconnects"]
        self.lbl_status.setText(
            "设备：%s\n采样率 %d Hz　端口 %d　模式 %s\n"
            "已发 %d 包（%.1f pps，%.2f Mbps）　队列丢弃 %d\n对端：%s%s%s"
            % (snap["device"] or "-", snap["sr"] or 0, self._port(), snap["mode"],
               snap["packets"], pps, snap["mbps"], snap["dropped"],
               peer_txt, rc_txt,
               ("\n⚠ " + snap["error"]) if snap["error"] else ""))

    def _port(self):
        try:
            return int("".join(c for c in self.port.text() if c.isdigit()))
        except Exception:                                          # noqa: BLE001
            return LA.DEFAULT_PORT

    def closeEvent(self, e):
        self.stop()
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    w = SenderWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
