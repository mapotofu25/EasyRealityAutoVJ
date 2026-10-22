# -*- coding: utf-8 -*-
"""修正我自己的失误：连接数门控**只能用在音频上，不能用在视频上**。

原因（回环实测发现的）：NDI 是"接收端收到数据后连接才建立"——
发送端若因为 `get_num_connections()==0` 而不发视频，接收端就**永远连不上**
（连接数永远是 0）⇒ 视频和音频一起废掉。老代码一直是"发了再说"，这是对的。

所以：
  · `send_video`：**去掉门控**（继续无条件发；写失败只丢帧、不关输出、保持 warmup 守卫）；
  · `feed_audio`：**保留门控**（没人连时 `send_audio()` 会段错误）。
    视频在发 ⇒ 接收端一连上，连接数立刻 > 0，音频自然开始发。
"""
import io
import os
import sys

P = r"P:\AutoVJ\src\ndi_out.py"
OLD = """                # ⚠ 没有接收端连上就**不要碰 native**（实测音频那条路会段错误）
                if self._conns() <= 0:
                    self._dropped += 1
                    return
"""
NEW = """                # ⚠ 视频**不能**做连接数门控：NDI 是"接收端收到数据后连接才建立"，
                #   不发视频 ⇒ 接收端永远连不上（连接数恒 0）⇒ 音视频一起废。
                #   这里只靠 warmup 守卫 + "写失败只丢帧不关输出"就够安全了。
"""


def main():
    s = io.open(P, encoding="utf-8").read()
    if s.count(OLD) != 1:
        print("✗ 锚点命中 %d 次" % s.count(OLD))
        return 1
    io.open(P, "w", encoding="utf-8", newline="\n").write(s.replace(OLD, NEW))
    print("✓ 已去掉视频侧的连接数门控（音频侧保留）")
    import py_compile
    py_compile.compile(P, doraise=True)
    print("语法 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
