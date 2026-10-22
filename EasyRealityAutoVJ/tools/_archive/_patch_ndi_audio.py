# -*- coding: utf-8 -*-
"""一次性补丁：设计文档 §11 —— NDI 音频开关 + 独立转发线程 + Spout 不含音频说明。

背景（2026-09-28 查代码）：
  · `ndi_out.feed_audio()` 早就写好了，采集循环里也在调 —— 但**没有独立开关**
    （跟着 NDI 输出总开关走），且**直接在采集线程上调用**。
  · ⚠ 采集线程同时干着：能量分析、拍钟、曲风环形缓冲、指纹识别。`feed_audio()` 要写
    cyndilib 的音频缓冲并 `send_audio()`，接收端稍慢/网络一抖就会把它拖住 ⇒
    **现场直接掉拍**。所以必须改成「采集线程只入队，独立线程发送，队列满丢帧」。
  · Spout 协议层面只有画面、没有音频 ⇒ 加一行说明，别让用户以为勾了就有声音。

用法：venv/Scripts/python.exe -u tools/_patch_ndi_audio.py
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")

FAILS = []


def edit(path, pairs):
    p = os.path.join(SRC, path)
    t = io.open(p, encoding="utf-8").read()
    orig = t
    for old, new, cnt in pairs:
        got = t.count(old)
        if got != cnt:
            FAILS.append("%s: 期望 %d 处，实得 %d 处\n--- old ---\n%s\n---" % (path, cnt, got, old[:400]))
            continue
        t = t.replace(old, new)
    if t != orig:
        io.open(p, "w", encoding="utf-8", newline="").write(t)
        print("  已写入 %s（%+d 字节）" % (path, len(t) - len(orig)))
    else:
        print("  未变化 %s" % path)


# ======================================================================
# 一、audio_engine.py：采集线程只入队，独立线程发送
# ======================================================================
edit("audio_engine.py", [
    # 队列上限常量
    (
        'BLOCK = 1024\n',
        'BLOCK = 1024\n'
        '\n'
        '# NDI 音频转发队列上限（单位=块）：BLOCK=1024 @48kHz ≈ 21ms/块 ⇒ 48 块 ≈ 1 秒。\n'
        '# 满了自动丢最老的块 —— 宁可少发一秒音频，也绝不让采集线程等（见 _ndi_audio_loop）。\n'
        '_NDI_Q_MAX = 48\n',
        1),
    # 起线程
    (
        '        self._dev_worker = threading.Thread(\n'
        '            target=self._dev_loop, daemon=True, name="autovj-audio-dev")\n'
        '        self._dev_worker.start()\n',
        '        self._dev_worker = threading.Thread(\n'
        '            target=self._dev_loop, daemon=True, name="autovj-audio-dev")\n'
        '        self._dev_worker.start()\n'
        '        # ---- NDI 音频转发：**独立线程 + 有界队列**（见 _ndi_audio_loop）----\n'
        '        # ⚠ 铁律：`ndi.feed_audio()` 要写 cyndilib 缓冲并 send_audio，接收端稍慢就会\n'
        '        #   把它拖住 ⇒ **绝不能放在采集线程上** —— 那条线程还担着能量分析、拍钟、\n'
        '        #   指纹识别，一堵就是整场掉拍。这里只入队（deque 带 maxlen ⇒ 满自动丢最老）。\n'
        '        self._ndi_q = deque(maxlen=_NDI_Q_MAX)\n'
        '        self._ndi_cv = threading.Condition()\n'
        '        self._ndi_stop = False\n'
        '        self._ndi_thread = threading.Thread(\n'
        '            target=self._ndi_audio_loop, daemon=True, name="autovj-ndi-audio")\n'
        '        self._ndi_thread.start()\n',
        1),
    # 新增两个方法
    (
        '    def _worker_loop(self):\n',
        '    def _ndi_audio_loop(self):\n'
        '        """把采集线程投递的 PCM 转发给 NDI 发送器（**独立线程**，见 __init__）。\n'
        '\n'
        '        队列满由 `deque(maxlen=…)` 自动丢最老的块；NDI 没接线时直接丢弃。\n'
        '        全程**不阻塞采集线程**，也不做任何等待。\n'
        '        """\n'
        '        while not self._ndi_stop:\n'
        '            item = None\n'
        '            try:\n'
        '                with self._ndi_cv:\n'
        '                    if self._ndi_q:\n'
        '                        item = self._ndi_q.popleft()\n'
        '                    else:\n'
        '                        self._ndi_cv.wait(0.2)\n'
        '            except Exception:                                 # noqa: BLE001\n'
        '                time.sleep(0.1)\n'
        '                continue\n'
        '            if item is None:\n'
        '                continue\n'
        '            fn = self.state.ndi_feed\n'
        '            if fn is None:\n'
        '                continue\n'
        '            try:\n'
        '                fn(item[0], item[1])\n'
        '            except Exception:                                 # noqa: BLE001\n'
        '                pass\n'
        '\n'
        '    def push_ndi_audio(self, pcm_interleaved, sr):\n'
        '        """采集线程调用：**只入队**（O(1) 不阻塞），真正发送在 `_ndi_audio_loop`。"""\n'
        '        try:\n'
        '            with self._ndi_cv:\n'
        '                self._ndi_q.append((pcm_interleaved, sr))\n'
        '                self._ndi_cv.notify()\n'
        '        except Exception:                                     # noqa: BLE001\n'
        '            pass\n'
        '\n'
        '    def _worker_loop(self):\n',
        1),
    # 采集循环：改入队
    (
        '                    # NDI 音频（音画同步）：把采集到的立体声 PCM 喂给 NDI 发送器\n'
        '                    # 未启用 NDI 时**直接跳过**，省掉每块的 asarray/reshape/copy（原来无条件执行）\n'
        '                    try:\n'
        '                        ndi = st.ndi_feed\n'
        '                        if ndi is not None and st.ndi_audio_on:\n'
        '                            arr = _np.asarray(data, dtype=_np.float32)\n'
        '                            if arr.ndim == 1:\n'
        '                                arr = _np.column_stack([arr, arr])\n'
        '                            elif arr.shape[1] == 1:\n'
        '                                arr = _np.repeat(arr, 2, axis=1)\n'
        '                            ndi(arr.reshape(-1).copy(), sr)\n'
        '                    except Exception:\n'
        '                        pass\n',
        '                    # NDI 音频（音画同步）：把采集到的立体声 PCM **入队**给独立转发线程\n'
        '                    # ★ 2026-09-28 改：不再在本线程直接调 `ndi.feed_audio()` —— 它会写\n'
        '                    #   cyndilib 缓冲并 send_audio，接收端一慢就把采集线程拖住，\n'
        '                    #   连带能量/拍位/指纹全停（现场掉拍）。现在只投递，队列满丢帧。\n'
        '                    #   未启用时**直接跳过**，省掉每块的 asarray/reshape/copy。\n'
        '                    try:\n'
        '                        if st.ndi_audio_on and st.ndi_feed is not None:\n'
        '                            arr = _np.asarray(data, dtype=_np.float32)\n'
        '                            if arr.ndim == 1:\n'
        '                                arr = _np.column_stack([arr, arr])\n'
        '                            elif arr.shape[1] == 1:\n'
        '                                arr = _np.repeat(arr, 2, axis=1)\n'
        '                            self.push_ndi_audio(arr.reshape(-1).copy(), sr)\n'
        '                    except Exception:\n'
        '                        pass\n',
        1),
    # shutdown 收线程
    (
        '    def shutdown(self):\n'
        '        self._gen += 1\n'
        '        self._genre_stop.set()\n',
        '    def shutdown(self):\n'
        '        self._gen += 1\n'
        '        self._genre_stop.set()\n'
        '        self._ndi_stop = True\n'
        '        try:\n'
        '            with self._ndi_cv:\n'
        '                self._ndi_q.clear()\n'
        '                self._ndi_cv.notify_all()\n'
        '        except Exception:                                     # noqa: BLE001\n'
        '            pass\n',
        1),
])

# ======================================================================
# 二、ui_main.py：输出区加开关
# ======================================================================
edit("ui_main.py", [
    # OutputPanel 控件
    (
        '        self.ndi_name = QLineEdit()\n'
        '        self.ndi_name.setPlaceholderText("NDI 源名称")\n'
        '        go.addWidget(self.ndi_name, 6, 1)\n',
        '        self.ndi_name = QLineEdit()\n'
        '        self.ndi_name.setPlaceholderText("NDI 源名称")\n'
        '        go.addWidget(self.ndi_name, 6, 1)\n'
        '        # ★ NDI 音频单独一个开关，**默认关**：把系统回环再发出去，若接收端同时\n'
        '        #   监听着同一路声音会听到**双声/回声**。NDI 输出没开时置灰。\n'
        '        self.chk_ndi_audio = QCheckBox("NDI 传输音频（48 kHz 立体声）")\n'
        '        self.chk_ndi_audio.setToolTip(\n'
        '            "把采集到的声音随 NDI 一起发出去（48 kHz 立体声）。默认关闭："\n'
        '            "接收端若本来就在监听同一路声音，会听到双声/回声；"\n'
        '            "只在接收端需要单独取声音时才打开。")\n'
        '        self.chk_ndi_audio.setEnabled(False)\n'
        '        go.addWidget(self.chk_ndi_audio, 7, 0, 1, 2)\n'
        '        # Spout 协议层面**没有音频**，必须写清楚，否则用户会以为勾了就有声音\n'
        '        self.lbl_spout_audio = QLabel(\n'
        '            "Spout 只传画面，不含音频。需要音频请用 NDI 或虚拟声卡。")\n'
        '        self.lbl_spout_audio.setWordWrap(True)\n'
        '        self.lbl_spout_audio.setStyleSheet(\n'
        '            "color:" + theme.V("muted") + ";font-size:11px;")\n'
        '        go.addWidget(self.lbl_spout_audio, 8, 0, 1, 2)\n',
        1),
    # 信号连接
    (
        '        self.ndi_name.editingFinished.connect(main.set_ndi_name)\n',
        '        self.ndi_name.editingFinished.connect(main.set_ndi_name)\n'
        '        self.chk_ndi_audio.stateChanged.connect(main.set_ndi_audio)\n',
        1),
    # 启动回填
    (
        '        self.output_panel.chk_ndi.setChecked(o.get("ndi_enabled", False))\n'
        '        self.output_panel.ndi_name.setText(o.get("ndi_name", "EasyRealityAutoVJ"))\n',
        '        self.output_panel.chk_ndi.setChecked(o.get("ndi_enabled", False))\n'
        '        self.output_panel.ndi_name.setText(o.get("ndi_name", "EasyRealityAutoVJ"))\n'
        '        self.output_panel.chk_ndi_audio.setChecked(bool(o.get("ndi_audio", False)))\n'
        '        # NDI 输出开着才允许勾"传输音频"（否则勾了也没意义）\n'
        '        self.output_panel.chk_ndi_audio.setEnabled(bool(o.get("ndi_enabled", False)))\n',
        1),
    # 开关联动
    (
        '        # 同步给采集循环：未启用 NDI 时它就不再为每块音频做 asarray/reshape/copy\n'
        '        try:\n'
        '            self.audio.state.ndi_audio_on = bool(self.cfg["output"].get("ndi_enabled"))\n'
        '        except Exception:\n'
        '            pass\n',
        '        # NDI 音频：**必须 NDI 输出开着 + 用户另勾了「NDI 传输音频」**才发。\n'
        '        # 给采集循环同步一个布尔量：False 时它连每块的 asarray/reshape/copy 都省掉。\n'
        '        try:\n'
        '            self.audio.state.ndi_audio_on = bool(\n'
        '                s and self.cfg["output"].get("ndi_audio", False))\n'
        '        except Exception:\n'
        '            pass\n'
        '        try:\n'
        '            self.output_panel.chk_ndi_audio.setEnabled(bool(s))\n'
        '        except Exception:\n'
        '            pass\n',
        1),
    # 新增 set_ndi_audio
    (
        '    def _ndi_audio_feed(self, pcm, sr):\n',
        '    def set_ndi_audio(self, s):\n'
        '        """「NDI 传输音频」开关（**默认关**）。\n'
        '\n'
        '        ⚠ 默认关是有意的：采集的是**系统回环**，接收端若本来就在监听同一路声音，\n'
        '          再发一遍会变成**双声/回声**。只有接收端需要单独取声音时才打开。\n'
        '        """\n'
        '        s = bool(s) and bool(self.cfg["output"].get("ndi_enabled"))\n'
        '        self.cfg["output"]["ndi_audio"] = s\n'
        '        self.cfg.save()\n'
        '        try:\n'
        '            self.audio.state.ndi_audio_on = s\n'
        '        except Exception:                                     # noqa: BLE001\n'
        '            pass\n'
        '\n'
        '    def _ndi_audio_feed(self, pcm, sr):\n',
        1),
])

# ======================================================================
# 三、config.py 默认值
# ======================================================================
edit("config.py", [
    (
        '        "ndi_name": "EasyRealityAutoVJ",\n',
        '        "ndi_name": "EasyRealityAutoVJ",\n'
        '        "ndi_audio": False,           # NDI 是否随画面一起发声音（默认关：怕双声/回声）\n',
        1),
])

if FAILS:
    print("\n!! 补丁未全部应用：")
    for f in FAILS:
        print("---", f)
    sys.exit(1)
print("\n补丁应用完毕。")
