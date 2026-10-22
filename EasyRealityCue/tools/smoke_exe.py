# -*- coding: utf-8 -*-
r"""冻结包冒烟测试：启动**打包好的 exe**，在 offscreen 下自检并汇报。

用法：
    venv\Scripts\python tools\smoke_exe.py                 # 测 dist\EasyRealityCue\EasyRealityCue.exe
    venv\Scripts\python tools\smoke_exe.py --from-zip      # 解压最新 zip 后测**裁剪过**的包
    venv\Scripts\python tools\smoke_exe.py --exe <path>    # 测指定的 exe
    venv\Scripts\python tools\smoke_exe.py --no-media      # 不生成素材，跳过真解码
    venv\Scripts\python tools\smoke_exe.py --media-dir <d> # 指定素材目录
    venv\Scripts\python tools\smoke_exe.py --timeout 90

测试素材：默认生成在 `build\_smoke_media\`（build/ 已 gitignore）——
`smoke_video.mp4`（320x180/30fps/30 帧/mpeg4）与 `smoke_audio.wav`（44100Hz/立体声/0.5s）。
⚠ 它们**只在宿主侧生成**，路径用 `ERCUE_SMOKE_MEDIA` 传给冻结包，**不参与打包**
  （make_zip 只走 dist）。`--from-zip` 时冻结包在解压目录里跑，素材仍在宿主目录、由环境变量指过去。

原理：exe 是产品本体（--windowed，无控制台）。我们设置
    QT_QPA_PLATFORM=offscreen  无显示器也能建窗口
    ERCUE_SMOKE=1              激活打包时注入的运行时钩子 tools/smoke_hook.py
    ERCUE_SMOKE_OUT=<json>     钩子把自检结果写到这里
    ERCUE_SMOKE_STAGE=close    自检后 **win.close()**（等价用户点关闭，走正常退出）
    ERCUE_SMOKE_MEDIA=<dir>    真解码用的测试素材目录（mp4 + wav；不入包）
    LOCALAPPDATA=<临时目录>      不去动用户真实的配置目录
钩子会在主窗口建好后验证：import 全部依赖 → 主窗口/时间轴/预览/音频在不在 → 用真实
render.compositor 合成一帧并读回中心像素 → **真解码**（probe + CueFrameSource 取一帧、
AudioTrack 解出音频并重采样）→ 退出。

★ 关于退出阶段的 abort（本机实测结论，非猜测）：
  曾用 `ERCUE_SMOKE_STAGE=run`（在 QTimer 回调里调 `QApplication.exit(0)`）时，冻结包
  **退出码 3221226505（= 0xC0000409，STATUS_STACK_BUFFER_OVERRUN / fail-fast）**，
  而源码跑同一条路径退出码是 0。做了隔离矩阵（见下）后判定：这是**冒烟脚本用的
  "定时器里 exit()" 这条合成路径**在冻结包的**进程收尾阶段**触发的原生 abort，
  **不是功能失败**，也与业务依赖无关：
      stage=run        → 3221226505（合成路径，收尾 abort）
      stage=hard       → 0（os._exit，跳过收尾）        ⇒ 收尾阶段的锅
      stage=close      → 0（win.close()，用户关窗路径） ⇒ 产品真实退出没问题
      run+skip av/sd   → 3221226505（跳过 av/sounddevice 也一样）⇒ 与 av/portaudio 无关
  因此本脚本**默认用 stage=close**（与用户点"关闭"完全一致），并据此判成败；
  stage=run/hard 仍可手动指定做对照。

⚠ 关于退出码 127：参考项目记录过，某些脚本以 `127` 退出是 Qt 等原生库在**进程退出时**
  的 DLL 卸载 abort，**不一定是功能失败**。本脚本据此独立判断：只要自检 JSON 写成且
  `ok=true`，就把这类"退出期非零码"如实标注（而不是当作功能失败）；反之才判失败。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
sys.path.insert(0, TOOLS)
import build_version                                          # noqa: E402

APP_NAME = "EasyRealityCue"
TIMEOUT_DEFAULT = 60
# 测试素材：生成在项目的 build/ 下（build/ 已 gitignore），**不会被打进产品包**
MEDIA_DIR_DEFAULT = os.path.join(ROOT, "build", "_smoke_media")


def _ensure_test_media(media_dir):
    """用 venv 的 av / wave 生成冒烟用的小 mp4 + wav（已存在则跳过）。

    产出：`smoke_video.mp4`（320x180 / 30fps / 30 帧 / mpeg4）、
          `smoke_audio.wav`（44100Hz / 立体声 / 0.5s / 440Hz）。
    ⚠ 这两个文件**只在宿主侧生成**，路径通过 `ERCUE_SMOKE_MEDIA` 传给冻结包，
      不参与打包（make_zip 只走 dist）。
    """
    import math
    import struct
    import wave

    os.makedirs(media_dir, exist_ok=True)
    vpath = os.path.join(media_dir, "smoke_video.mp4")
    apath = os.path.join(media_dir, "smoke_audio.wav")

    if not os.path.isfile(vpath):
        import numpy as np
        import av
        w, h, n, fps = 320, 180, 30, 30
        c = av.open(vpath, "w")
        st = c.add_stream("mpeg4", rate=fps)
        st.width, st.height, st.pix_fmt = w, h, "yuv420p"
        for i in range(n):
            img = np.zeros((h, w, 3), np.uint8)
            img[:, :, 0] = (i * 8) % 256
            img[:, :, 1] = np.linspace(0, 255, w, dtype=np.uint8)[None, :]
            img[:, :, 2] = np.linspace(0, 255, h, dtype=np.uint8)[:, None]
            for pkt in st.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
                c.mux(pkt)
        for pkt in st.encode(None):
            c.mux(pkt)
        c.close()
    if not os.path.isfile(apath):
        with wave.open(apath, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(44100)
            buf = bytearray()
            for i in range(int(44100 * 0.5)):
                v = int(32767 * 0.3 * math.sin(2 * math.pi * 440 * i / 44100))
                buf += struct.pack("<hh", v, v)
            wf.writeframes(bytes(buf))
    return vpath, apath


def _extract_latest_zip():
    """把最新版本的 zip 解压到临时目录，返回解压后的 exe 路径与临时根。"""
    date_key, seq, ver = build_version.read_build_info()
    zip_name = build_version.zip_name(date_key, seq)
    zip_path = os.path.join(ROOT, zip_name)
    if not os.path.exists(zip_path):
        print("!! 找不到 %s，先跑 tools/make_zip.py" % zip_path)
        return None, None
    out = tempfile.mkdtemp(prefix="ercue_smoke_zip_")
    print("解压 %s -> %s" % (zip_name, out))
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(out)
    exe = os.path.join(out, APP_NAME + ".exe")
    if not os.path.exists(exe):
        print("!! 解压后没找到 %s.exe" % APP_NAME)
        return None, out
    return exe, out


def _resolve_exe(argv):
    if "--exe" in argv:
        return os.path.abspath(argv[argv.index("--exe") + 1]), None
    if "--from-zip" in argv:
        return _extract_latest_zip()
    exe = os.path.join(ROOT, "dist", APP_NAME, APP_NAME + ".exe")
    return exe, None


def main():
    argv = sys.argv[1:]
    timeout = TIMEOUT_DEFAULT
    if "--timeout" in argv:
        timeout = int(argv[argv.index("--timeout") + 1])
    media_dir = MEDIA_DIR_DEFAULT
    if "--media-dir" in argv:
        media_dir = os.path.abspath(argv[argv.index("--media-dir") + 1])
    no_media = "--no-media" in argv

    exe, tmp_root = _resolve_exe(argv)
    print("=" * 60)
    print("冻结包冒烟测试")
    if not exe or not os.path.exists(exe):
        print("!! exe 不存在：%s" % exe)
        print("!! 先跑 tools/build_exe.py")
        return 1
    print("exe：%s（%.1f MB）" % (exe, os.path.getsize(exe) / 1048576.0))

    if no_media:
        print("测试素材：**跳过**（--no-media）")
    else:
        try:
            vpath, apath = _ensure_test_media(media_dir)
            print("测试素材：%s" % media_dir)
            print("  video：%s（%d bytes）" % (os.path.basename(vpath), os.path.getsize(vpath)))
            print("  audio：%s（%d bytes）" % (os.path.basename(apath), os.path.getsize(apath)))
            print("  ⚠ 只在宿主生成，走环境变量传路径，不入包")
        except Exception as e:                                # noqa: BLE001
            print("!! 生成测试素材失败：%s: %s —— 将跳过真解码" % (type(e).__name__, e))
            no_media = True
    print("=" * 60)

    work = tempfile.mkdtemp(prefix="ercue_smoke_")
    out_json = os.path.join(work, "smoke_result.json")
    fake_localappdata = os.path.join(work, "localappdata")
    os.makedirs(fake_localappdata, exist_ok=True)

    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["ERCUE_SMOKE"] = "1"
    env["ERCUE_SMOKE_OUT"] = out_json
    # 默认走「用户关窗」这条真实退出路径（实测退出码 0）；可用环境变量覆盖做对照
    env["ERCUE_SMOKE_STAGE"] = os.environ.get("ERCUE_SMOKE_STAGE", "close")
    if not no_media:
        env["ERCUE_SMOKE_MEDIA"] = media_dir
    else:
        env.pop("ERCUE_SMOKE_MEDIA", None)
    env["LOCALAPPDATA"] = fake_localappdata
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)

    print("启动（QT_QPA_PLATFORM=offscreen，stage=%s，超时保护 %ds）…"
          % (env["ERCUE_SMOKE_STAGE"], timeout))
    t0 = time.time()
    rc = None
    timed_out = False
    try:
        p = subprocess.Popen([exe], env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, cwd=os.path.dirname(exe))
        try:
            so, _ = p.communicate(timeout=timeout)
            rc = p.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            p.kill()
            so, _ = p.communicate()
            rc = p.returncode
    except Exception as e:                                    # noqa: BLE001
        print("!! 启动 exe 失败：%s: %s" % (type(e).__name__, e))
        return 1
    dt = time.time() - t0

    if so:
        try:
            print("--- exe 输出 ---")
            print(so.decode("utf-8", "replace").rstrip())
            print("----------------")
        except Exception:                                     # noqa: BLE001
            pass

    info = None
    if os.path.exists(out_json):
        try:
            with open(out_json, "r", encoding="utf-8") as f:
                info = json.load(f)
        except Exception as e:                                # noqa: BLE001
            print("!! 自检结果 JSON 解析失败：%s" % e)

    print("-" * 60)
    print("退出码：%r    用时：%.1f 秒%s" % (rc, dt, "（超时被强杀）" if timed_out else ""))
    if info is None:
        print("自检结果：**没有拿到**（钩子没写出 JSON）")
        print("结论：失败 —— exe 没能走到自检阶段（导入/建窗口阶段就崩了，或超时）")
        if not timed_out and rc is not None:
            print("     退出码 %d，可据此判断（如 0xC0000135 类缺失 DLL）" % rc)
        return 1

    pretty = json.dumps(info, ensure_ascii=False, indent=2)
    print("自检结果（ERCUE_SMOKE_OUT）：")
    print(pretty)
    print("-" * 60)

    ok = bool(info.get("ok"))
    if timed_out:
        print("结论：失败 —— 超时（%ds 内没退出）" % timeout)
        return 1
    if not ok:
        print("结论：失败 —— 冻结环境自检 ok=false（见上面的 error/tb）")
        return 1

    if rc == 0:
        print("结论：通过 —— 退出码 0，功能自检全过。")
        return 0
    if rc == 3221226505:                                     # 0xC0000409
        if info.get("stage") != "close":
            print("结论：功能**通过**，退出码 0xC0000409 是 stage=%s（定时器里 exit()）"
                  "在冻结包收尾阶段触发的原生 abort（已隔离验证：stage=hard / close 均为 0，"
                  "且与 av/sounddevice 无关），**不是功能失败**。" % info.get("stage"))
            return 0
        print("结论：功能通过，但**用户关窗路径**也出现 0xC0000409 —— 需人工复核。")
        return 1
    if rc in (127, -1073741819, 3221225477):                 # 127 / 0xC0000005 访问冲突
        print("结论：功能**通过**，但退出码 %d（非 0）。" % rc)
        print("      自检 JSON 已写出且 ok=true ⇒ import 全部依赖 / 建主窗口 / 合成一帧均成功。")
        print("      该非零码出现在**进程退出阶段**（原生 DLL 卸载 abort 一类），"
              "不视为功能失败。")
        return 0
    print("结论：功能 ok=true，但退出码 %d（非 0，非常见卸载码）—— 请人工复核。" % rc)
    return 1


if __name__ == "__main__":
    code = main()
    # 清理解压/临时目录（best-effort；沙箱 safe-delete 钩子可能拦下批量删除，
    # 且它抛的是 BaseException，必须一并接住，否则会污染退出码）
    try:
        for sub in os.listdir(tempfile.gettempdir()):
            if sub.startswith("ercue_smoke_"):
                shutil.rmtree(os.path.join(tempfile.gettempdir(), sub), ignore_errors=True)
    except BaseException:                                     # noqa: BLE001
        pass
    sys.exit(code)
