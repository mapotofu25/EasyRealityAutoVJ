# -*- coding: utf-8 -*-
"""一键验证：cyndilib 0.1.1 的 AudioSendFrame.p_metadata 野指针就是 NDI 音频段错误的元凶。

做法：把「跨进程回环、只发音频」跑两遍 —— 一遍原样（PATCH=0），一遍在
`set_audio_frame()` 之后 / `open()` 之前把 `p_metadata` 置 NULL（PATCH=1）。

期望：
    PATCH=0 → 发送端 exit=3221225477（0xC0000005 段错误），接收端几乎收不到音频
    PATCH=1 → 发送端 exit=0，稳定发 8 秒，接收端收到大量「非静音」音频帧

用法：
    P:/AutoVJ/venv/Scripts/python.exe -u P:/AutoVJ/tools/_verify_pmetadata_fix.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
RUNNER = os.path.join(HERE, "_test_ndi_audio2.py")


def run(patch):
    env = dict(os.environ)
    env["PATCH"] = "1" if patch else "0"
    env["TX_KIND"] = "audio"
    env["TX_SECS"] = "8"
    env["RX_SECS"] = "16"
    p = subprocess.run([PY, "-u", RUNNER, "loop2"], cwd=os.path.dirname(HERE),
                       env=env, capture_output=True, text=True, timeout=180)
    return p.stdout + p.stderr


def pick(text, *keys):
    return [ln for ln in text.splitlines() if any(k in ln for k in keys)]


def main():
    for patch in (0, 1):
        print("=" * 74)
        print("PATCH=%d  (%s)" % (patch, "open() 前把 p_metadata 置 NULL" if patch else "原样"))
        print("=" * 74)
        out = run(patch)
        for ln in pick(out, "[tx] 输出", "[tx] p_metadata", "[tx] t=", "[tx] 结束",
                      "[rx] 收到", "[rx] 第一帧"):
            print(ln)
        print()


if __name__ == "__main__":
    main()
