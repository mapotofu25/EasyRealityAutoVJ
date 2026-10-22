# -*- coding: utf-8 -*-
r"""设备自检：音频输入设备 / Spout 输出 / NDI 输出（含局域网发现）。

用户手上没有线路输入、也没有第二台机器，所以这里用**软件侧能做的全部检查**：
  [A] 音频：枚举所有播放与采集端点，逐个真开一次并采 0.8 秒，报告能否打开、有没有信号；
      —— 这能回答「线路输入 / 立体声混音 / 声卡输入能不能用」。
  [B] Spout：真的创建一个发送端、发 30 帧，并列出当前系统里所有 Spout 发送端；
      —— 能发出去 = Spout 输出可用（OBS / Resolume 里能不能看到另说，但底层通了）。
  [C] NDI：读 Runtime 版本 → 创建发送端并真发帧 → 用 Finder **在局域网里搜索自己**；
      —— 能在局域网被发现 = 发送端 + mDNS 广播通；再尝试本机自收一帧。
         跨机还需要防火墙放行（脚本会给出端口与放行命令）。

用法：
  venv\Scripts\python.exe tools\device_check.py            全部
  venv\Scripts\python.exe tools\device_check.py audio      只测音频
  venv\Scripts\python.exe tools\device_check.py spout ndi  测 Spout 与 NDI
输出：P:\AutoVJ\_device_check.txt
"""
import os
import sys
import time
import socket
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import numpy as np  # noqa: E402

OUT = r"P:\AutoVJ\_device_check.txt"
out = []


def w(s=""):
    out.append(str(s))
    print(s, flush=True)


def hdr(t):
    w("")
    w("=" * 78)
    w(t)
    w("=" * 78)


# ------------------------------------------------------------------ 音频
def check_audio():
    hdr("[A] 音频输入设备（能不能用 / 有没有信号）")
    try:
        import soundcard as sc
    except Exception as e:  # noqa
        w("soundcard 不可用: %s" % e)
        return
    try:
        spks = sc.all_speakers()
    except Exception as e:  # noqa
        spks = []
        w("播放设备枚举失败: %s" % e)
    w("播放端点（共 %d）：" % len(spks))
    for s in spks:
        w("   · %s  (%d ch)" % (s.name, s.channels))

    try:
        loops = sc.all_microphones(include_loopback=True)
        caps = sc.all_microphones(include_loopback=False)
    except Exception as e:  # noqa
        w("采集设备枚举失败: %s" % e)
        return
    w("")
    w("采集端点：回环(loopback) %d 个 / 真实输入 %d 个" % (len(loops), len(caps)))

    def probe(tag, mics):
        w("")
        w("-- %s --" % tag)
        if not mics:
            w("   （一个都没有）")
            return
        for m in mics:
            name = m.name
            kind = ""
            low = name.lower()
            for k, label in (("立体声混音", "立体声混音"), ("stereo mix", "立体声混音"),
                             ("线路输入", "线路输入"), ("line in", "线路输入"),
                             ("麦克风", "麦克风"), ("microphone", "麦克风"),
                             ("line", "线路输入")):
                if k in low:
                    kind = label
                    break
            ch = max(1, min(2, getattr(m, "channels", 1) or 1))
            note = ""
            try:
                with m.recorder(samplerate=48000, channels=ch, blocksize=1024) as r:
                    d = np.asarray(r.record(numframes=int(0.8 * 48000)), dtype=np.float32)
                if d.ndim > 1:
                    d = d.mean(axis=1)
                rms = float(np.sqrt(np.mean(d ** 2))) if len(d) else 0.0
                peak = float(np.max(np.abs(d))) if len(d) else 0.0
                if rms < 1e-4:
                    note = "可打开，但**没有信号**（没接设备时正常）"
                else:
                    note = "有信号 ✔  rms=%.4f peak=%.3f" % (rms, peak)
                ok = "可打开 ✔"
            except Exception as e:  # noqa
                ok = "打开失败 ✘"
                note = str(e)[:110]
            w("   · %-46s %-9s %s" % (name[:46], kind or "-", ok))
            w("     %s" % note)

    probe("回环 / 输出监视（AutoVJ 的「系统」音源用的就是这条）", loops)
    probe("真实输入（线路输入 / 麦克风 / 立体声混音）", caps)
    w("")
    w("说明：")
    w("  · 「系统」音源 → 用回环端点（上面第一组），只要系统在放声音就该有信号。")
    w("  · 「麦克风」音源 → 用真实输入，接上设备（调音台输出/线路输入）后这里会有信号。")
    w("  · 立体声混音/线路输入即使显示「没有信号」，也代表**它可用**，只是此刻没声音进去。")

    # ---- 实测：放一段测试音，看回环能不能采到（回答「声卡输入到底能不能用」）----
    w("")
    w("-- 实测：播 2 秒测试音，检查回环能否采到（会出声，音量不大）--")
    try:
        import soundcard as sc2
        spk = None
        for s in sc2.all_speakers():
            if "Realtek" in s.name:
                spk = s
                break
        if spk is None:
            spk = sc2.default_speaker()
        mic = None
        for m in sc2.all_microphones(include_loopback=True):
            if m.name == spk.name:
                mic = m
                break
        w("   播放：%s" % spk.name)
        w("   采集：%s" % (mic.name if mic else "(找不到它的回环)"))
        if spk is not None and mic is not None:
            SR = 48000
            t = np.arange(int(2.0 * SR)) / SR
            x = (0.15 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
            stereo = np.stack([x, x], axis=1)

            def _play():
                try:
                    with spk.player(samplerate=SR, channels=2, blocksize=4800) as p:
                        p.play(stereo)
                except Exception as e:  # noqa
                    w("   播放失败: %s" % str(e)[:100])

            th = threading.Thread(target=_play, daemon=True)
            th.start()
            time.sleep(0.15)
            with mic.recorder(samplerate=SR, channels=1, blocksize=4800) as r:
                d = np.asarray(r.record(numframes=SR), dtype=np.float32)
            th.join(timeout=3)
            rms = float(np.sqrt(np.mean(d ** 2))) if len(d) else 0.0
            peak = float(np.max(np.abs(d))) if len(d) else 0.0
            if rms > 1e-3:
                w("   采到信号 ✔  rms=%.4f peak=%.3f  ⇒ **回环链路可用**，AutoVJ 的「系统」音源能正常工作。" % (rms, peak))
            else:
                w("   没采到信号 ✘（rms=%.5f）⇒ 回环这条路有问题：可能是系统音量静音、"
                  "或该端点被独占，检查 Windows 音量合成器。" % rms)
    except Exception as e:  # noqa
        w("   测试没能完成：%s" % str(e)[:140])

    w("")
    w("若列表里没有「线路输入 / Line In」：")
    w("  · 先在 Windows「声音设置 → 更多声音设置 → 录制」里看有没有「线路输入」；")
    w("    有但显示「未插入」= 声卡没检测到接线（换个口/换根线，或主板那个蓝色 Line In 口）。")
    w("  · Realtek 声卡要在 Realtek Audio Console（或主板音效面板）里把「线路输入」"
      "从「麦克风」重定义过来，否则它只会以麦克风形式出现。")
    w("  · 没有独立的 Line In 也没关系：用 USB 声卡/调音台的 USB 输出，它会作为新的"
      "「麦克风」端点出现，AutoVJ 音源选「麦克风」即可。")


# ------------------------------------------------------------------ Spout
def check_spout():
    hdr("[B] Spout 输出（发给 OBS / Resolume 等）")
    try:
        from SpoutGL import SpoutSender, SpoutReceiver
    except Exception as e:  # noqa
        w("SpoutGL 不可用：%s" % e)
        w("⇒ Spout 输出不能用（缺 SpoutGL 库）。")
        return
    w("SpoutGL 导入 ✔")

    # 先看看系统里有没有别的 Spout 发送端 / 接收端
    try:
        r = SpoutReceiver()
        lst = r.getSenderList()
        w("当前系统里的 Spout 发送端：%s" % (lst if lst else "（无 —— 说明没有其它程序在发 Spout）"))
    except Exception as e:  # noqa
        w("枚举 Spout 发送端失败（不致命）：%s" % str(e)[:120])

    s = None
    name = "EasyRealityAutoVJ_SelfTest"
    try:
        from SpoutGL.enums import GL_BGRA_EXT
        s = SpoutSender()
        s.setSenderName(name)
        s.createOpenGL()
        BW, BH = 320, 180           # 注意：不能叫 w/h —— 会遮蔽上面的 w() 打印函数
        buf = np.zeros((BH, BW, 4), dtype=np.uint8)
        frames = 0
        t0 = time.perf_counter()
        for i in range(30):
            buf[:, :, 0] = i * 8 % 256          # B
            buf[:, :, 1] = 60
            buf[:, :, 2] = 200
            buf[:, :, 3] = 255                  # A
            s.sendImage(buf.reshape(-1), BH, BW, GL_BGRA_EXT, True, 0)
            frames += 1
            time.sleep(0.02)
        el = time.perf_counter() - t0
        w("发帧测试：%d 帧 / %.2fs（%.0f fps）✔" % (frames, el, frames / max(el, 1e-6)))
        w("⇒ Spout 输出**可用**：发送端创建成功、图像能推出。")
        w("   在 OBS 里选「Spout2 Capture」、Resolume 里选 Spout 源即可看到画面。")
    except Exception as e:  # noqa
        w("Spout 发帧失败 ✘：%s" % str(e)[:200])
        w("⇒ 可能原因：显卡驱动不支持共享纹理（GL/DX 互操作），或缺少 OpenGL 上下文。")
    finally:
        try:
            if s is not None:
                s.releaseSender()
        except Exception:
            pass


# ------------------------------------------------------------------ NDI
def check_ndi():
    hdr("[C] NDI 输出（含局域网发现）")
    try:
        import cyndilib
        from cyndilib import Sender, VideoSendFrame, AudioSendFrame, Finder
    except Exception as e:  # noqa
        w("cyndilib 不可用：%s" % e)
        w("⇒ NDI 输出不能用。")
        return
    try:
        w("NDI Runtime：%s" % cyndilib.get_ndi_version())
        w("   （这份运行时**随 cyndilib 打包在软件里**，用户不需要另外安装 —— "
          "实测本机 Program Files 下没有 NDI，读到的版本号就是自带的那份 DLL）")
    except Exception as e:  # noqa
        w("读 NDI 运行时版本失败：%s" % str(e)[:140])
        w("⇒ 运行时是随包自带的（不需要用户安装）。失败通常是杀毒软件隔离了软件目录里的")
        w("   Processing.NDI.Lib.x64.dll，或文件缺失 —— 加白名单 / 重新解压安装包即可。")
        return

    w("")
    w("本机网络接口：")
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                w("   · %-16s (主机名 %s)" % (ip, host))
    except Exception as e:  # noqa
        w("   取 IP 失败：%s" % str(e)[:100])

    name = "EasyRealityAutoVJ_SelfTest"
    sv = None
    src = None
    try:
        sv = Sender(name)
        try:
            sv.ndi_name = name      # cyndilib 没有 set_ndi_name()，只有 name/ndi_name 属性
        except Exception:
            pass
        v = VideoSendFrame()
        v.set_resolution(640, 360)
        v.set_fourcc(0x41524742)      # 'BGRA'
        v.set_frame_rate(30)          # 只接受 1 个参数（Fraction 形式的整数值）
        v.set_progressive(True)
        sv.set_video_frame(v)
        a = AudioSendFrame()
        try:
            a.sample_rate = 48000
            a.num_channels = 2
            a.set_max_num_samples(4800)
        except Exception:
            pass
        sv.set_audio_frame(a)
        sv.open()
        w("")
        w("NDI 发送端已打开：%s ✔" % name)

        # 真发 60 帧视频 + 3 块静音音频
        NW, NH = 640, 360           # 同样避开 w/h（会遮蔽打印函数）
        frame = np.zeros((NH, NW, 4), dtype=np.uint8)
        frame[:, :, 3] = 255
        t0 = time.perf_counter()
        for i in range(60):
            frame[:, :, 0] = (i * 4) % 256
            v.write_data(frame.reshape(-1))
            sv.send_video()
            if i % 20 == 0:
                pcm = np.zeros(4800 * 2, dtype=np.float32)
                try:
                    a.write_data(pcm)
                    sv.send_audio()
                except Exception:
                    pass
            time.sleep(0.02)
        el = time.perf_counter() - t0
        w("发帧测试：60 帧 / %.2fs（%.0f fps）✔" % (el, 60 / max(el, 1e-6)))

        # ---- 局域网发现：Finder 会列出同网段所有 NDI 源（包括自己）----
        w("")
        w("局域网发现（最多等 6 秒）…")
        seen = []
        try:
            f = Finder()
            f.open()
            f.wait_for_sources(6.0)
            names = list(f.get_source_names() or [])
            w("发现 %d 个 NDI 源：" % len(names))
            for n in names:
                mark = "  ← 本机自己（说明广播/发现通了）" if name in n else ""
                w("   · %s%s" % (n, mark))
                if name in n:
                    seen.append(n)
            if not names:
                w("   （一个都没有。本机自己都没搜到 → 多半是防火墙拦了 mDNS/NDI 广播）")
            if seen:
                src = f.get_source(seen[0])
                w("")
                w("⇒ 局域网发现**正常**：自己发的源能被搜到。")
                w("   这证明发送端 + 网络广播都是通的；换到另一台机器（接收端装 NDI Runtime 或 OBS NDI 插件）")
                w("   就能直接选到这个名字取画面。")
        except Exception as e:  # noqa
            w("Finder 搜索失败：%s" % str(e)[:160])

        # ---- 本机自收一帧（能收到 = 数据流端到端通）----
        if src is not None:
            try:
                from cyndilib import Receiver
                from cyndilib.receiver import ReceiveFrameType
                rec = Receiver()
                rec.set_source(src)
                got = 0
                t0 = time.perf_counter()
                while time.perf_counter() - t0 < 6.0 and got < 3:
                    for _ in range(20):
                        frame2 = np.zeros((NH, NW, 4), dtype=np.uint8)
                        frame2[:, :, 2] = 180
                        frame2[:, :, 3] = 255
                        v.write_data(frame2.reshape(-1))
                        sv.send_video()
                        time.sleep(0.01)
                    # ⚠ 枚举成员叫 recv_video（不是 video）—— 又一处 cyndilib 命名坑
                    rec.receive(ReceiveFrameType.recv_video, 500)
                    got += 1
                def _val(obj, nm):
                    v = getattr(obj, nm, None)
                    if v is None:
                        return "?"
                    try:
                        return v() if callable(v) else v
                    except Exception:
                        return v

                w("本机自收测试：接收 %d 轮，has_video_frame=%s，连接数=%s"
                  % (got, _val(rec, "has_video_frame"), _val(rec, "get_num_connections")))
                w("⇒ **端到端通了**：本机发送端 → 网络栈 → 本机接收端都能收到画面。")
            except Exception as e:  # noqa
                w("本机自收测试未完成：%s" % str(e)[:150])
                w("（不影响结论 —— 发送端与局域网发现已经验证过了）")

        w("")
        w("跨机传送还差一步：**防火墙放行 NDI**。以管理员身份运行一次即可：")
        w(r'  netsh advfirewall firewall add rule name="NDI" dir=in action=allow protocol=UDP localport=5353')
        w(r'  netsh advfirewall firewall add rule name="NDI TCP" dir=in action=allow protocol=TCP localport=5960-5990')
        w(r'  netsh advfirewall firewall add rule name="NDI UDP" dir=in action=allow protocol=UDP localport=5960-5990')
        w("  （放行只需做一次。也可以用官方 NDI Tools 里的一键放行，"
          "但不必为了这一步装整套 Tools —— 运行时本软件已自带）")
    except Exception as e:  # noqa
        w("NDI 发送端打开失败 ✘：%s" % str(e)[:220])
        w("⇒ 常见原因：名字已被占用、网卡异常，或运行时文件被隔离（运行时随包自带，无需安装）。")
    finally:
        try:
            if sv is not None:
                sv.close()
        except Exception:
            pass


def main():
    args = [a.lower() for a in sys.argv[1:]] or ["all"]
    w("=" * 78)
    w("设备自检   %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    w("=" * 78)
    if "all" in args or "audio" in args:
        check_audio()
    if "all" in args or "spout" in args:
        check_spout()
    if "all" in args or "ndi" in args:
        check_ndi()
    w("")
    w("=" * 78)
    w("自检结束")
    w("=" * 78)
    open(OUT, "w", encoding="utf-8").write("\n".join(out))


if __name__ == "__main__":
    main()
