# -*- coding: utf-8 -*-
"""把 MEMORY.md 压到 15.0KB 以下（输出小节压紧 + 快捷键并进工作方式）。"""
import io
import os

P = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")),
                 "WorkBuddy", "自动VJ软件", ".workbuddy", "memory", "MEMORY.md")
t = io.open(P, encoding="utf-8").read()
before = len(t.encode("utf-8"))

i = t.find("\n## 输出（Spout / NDI）")
j = t.find("\n## 关于界面 / 许可")
assert i > 0 and j > i
NEW = """## 输出（Spout / NDI）
- **Spout 协议层面只有画面、没有音频**；**NDI 运行库随包自带**（只有接收端要装 NDI Runtime）。
- `cfg["output"]["ndi_audio"]` **默认关**（采集的是系统回环，接收端再监听同一路会双声/回声），
  且必须先开 NDI 输出才可勾。
- 采集线程**只入队**（`AudioEngine.push_ndi_audio`，`deque(maxlen=48)`），`_ndi_audio_loop` 独立线程发，
  **队列满丢最老的块**（见铁律 9）。
"""
t = t[:i + 1] + NEW + t[j + 1:]

# 快捷键并进工作方式
k = t.find("\n## 快捷键")
assert k > 0
t = t[:k].rstrip() + "\n"
t = t.replace("## 工作方式\n",
              "## 工作方式\n- 快捷键默认全部**窗口内**（全局无修饰键会吞键）；HotkeyDialog 点「应用」才生效，"
              "关闭时 `_dirty` 弹二级框（用按钮对象判断）。\n", 1)

io.open(P, "w", encoding="utf-8", newline="").write(t)
print("MEMORY.md  %d -> %d 字节" % (before, len(t.encode("utf-8"))))
