# -*- coding: utf-8 -*-
"""① `send_video` 的写失败**不能把 NDI 输出永久关掉**（一次瞬时错误就永久死）。
      实测：没有接收端连接时写视频帧会抛 `Object is not writable` ⇒ 老代码 `enabled=False`
      ⇒ 此后每帧静默丢弃，**接收端后面接上来也再也没有画面/声音**。
   ② 测试改成直接用 `_ensure()` 开发送端再喂音频（不依赖视频帧能否写成功）。
"""
import io
import os

ROOT = r"P:\AutoVJ"
P = os.path.join(ROOT, "src", "ndi_out.py")

OLD = """        except Exception as e:
            self._last_err = str(e)[:160]
            self.enabled = False

    def set_audio_rate(self, sr):"""
NEW = """        except Exception as e:
            # ⚠⚠ **绝不能**在这里 `enabled = False`（2026-10-01 实测踩到）：
            #   没有接收端连接时写视频帧会抛 `Object is not writable`，老代码就永久关掉
            #   整个 NDI 输出 ⇒ 接收端**后面再接上来也永远没画面/没声音**。
            #   正确做法：丢掉这一帧、记一次日志，保持 enabled，下一帧继续尝试（接收端
            #   一旦连上就自然恢复）。
            self._last_err = str(e)[:160]
            self._dropped += 1
            if self._audio_err_logged is None and not self._err_logged:
                self._err_logged = self._last_err
                _log("!! NDI 发送视频失败（只记一次，后续继续尝试）：%s" % self._last_err)

    def set_audio_rate(self, sr):"""


def main():
    s = io.open(P, encoding="utf-8").read()
    if s.count(OLD) != 1:
        print("✗ 锚点命中 %d 次" % s.count(OLD))
        return 1
    s = s.replace(OLD, NEW)
    # 补一个只记一次的标记
    s = s.replace("""        self._audio_err_logged = None   # 只把**第一条**错误写日志，避免刷屏""",
                  """        self._audio_err_logged = None   # 只把**第一条**错误写日志，避免刷屏
        self._err_logged = None         # 视频侧同样只记第一条""")
    io.open(P, "w", encoding="utf-8", newline="\n").write(s)
    print("✓ send_video 不再因一次写失败永久关闭输出")
    import py_compile
    py_compile.compile(P, doraise=True)
    print("语法 OK")

    # 测试改用 _ensure 直连
    T = os.path.join(ROOT, "tools", "_test_ndi_audio.py")
    t = io.open(T, encoding="utf-8").read()
    t = t.replace('''    o = ndi_out.NDIOutput("autovj_audiotest")
    o.set_audio_rate(48000)
    o.send_video(bytes(320 * 180 * 4), 320, 180)
    check("NDI 输出已就绪", bool(o.enabled), "enabled=%s err=%s" % (o.enabled, o._last_err))''',
                  '''    o = ndi_out.NDIOutput("autovj_audiotest")
    o.set_audio_rate(48000)
    # 直接按视频分辨率把发送端建起来（没有接收端时写视频帧会失败，与音频无关）
    o._ensure(320, 180)
    check("NDI 发送端已就绪", bool(o.enabled), "enabled=%s err=%s" % (o.enabled, o._last_err))''')
    t = t.replace('''    o2 = ndi_out.NDIOutput("autovj_audiotest44")
    o2.set_audio_rate(44100)
    o2.send_video(bytes(320 * 180 * 4), 320, 180)''',
                  '''    o2 = ndi_out.NDIOutput("autovj_audiotest44")
    o2.set_audio_rate(44100)
    o2._ensure(320, 180)''')
    io.open(T, "w", encoding="utf-8", newline="\n").write(t)
    print("✓ 测试改用 _ensure() 直连")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
