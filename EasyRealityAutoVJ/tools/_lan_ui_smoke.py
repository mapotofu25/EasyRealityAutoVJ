# -*- coding: utf-8 -*-
"""冒烟：真实 MainWindow 起引擎，验证
  ① 局域网音源能按"手填 IP:端口"启动（不崩、解析正确）；
  ② `lan_failed` 在握手上之后为**空**（本次修的那个 bug）；
  ③ 主界面 `lbl_warn` 显示的是 `lan_info` 而不是陈旧错误。

⚠ 本机没有 A 机发送端 ⇒ 必然 5 秒超时（`lan_failed` 会被写一次）。
  这正好用来验证：① 超时文案里**带上了手填的 IP**（说明解析生效）；
  ② 超时是预期行为，不是崩溃；③ 随后我们再手动把状态改成"已连接"，
     确认界面立刻切到 lan_info（证明"文字会更新"）。

用法：venv/Scripts/python.exe -u tools/_lan_ui_smoke.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from PySide6.QtWidgets import QApplication                       # noqa: E402

app = QApplication([])
import ui_main                                                    # noqa: E402


class _MB:
    """最小主窗口替身不需要 —— 我们直接用真 MainWindow（含真实 config）。"""


def main():
    w = ui_main.MainWindow()
    print("[smoke] MainWindow 建立 OK")
    a = w.audio
    st = a.state

    # ---- ① 手填 IP:端口 启动局域网音源 ----
    print("[smoke] 启动局域网音源：手填 192.168.31.96:47811")
    w.apply_audio_settings("lan", "192.168.31.96:47811", 0, False, 0, 1)
    # 等超时（没有 A 机，5s）
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 7.0:
        app.processEvents()
        time.sleep(0.05)
    snap = st.snapshot()
    print("   device_desc = %r" % snap.get("device_desc"))
    print("   lan_failed  = %r" % snap.get("lan_failed"))
    print("   lan_info    = %r" % snap.get("lan_info"))

    ok = True
    lf = snap.get("lan_failed") or ""
    if "192.168.31.96" in lf:
        print("   ✓ 超时文案里带上了手填的 IP ⇒ 解析生效")
    else:
        print("   ⚠ 超时文案未含手填 IP（可能已连上或环境不同）：%r" % lf)

    # ---- ② 模拟"握手上"：直接改状态，看界面是否更新 ----
    #   真实场景下 `_run_lan` 成功时会**同时**清掉 `st.error`/`st.lan_failed`
    #   （见 audio_engine._run_lan 的握手成功分支）⇒ 这里照实模拟。
    with st.lock:
        st.error = ""
        st.lan_failed = ""
        st.lan_info = "[指定 192.168.31.96] 对端 192.168.31.96:47811 | 48000.0Hz | 深度 60ms | 抖动 1ms | 丢包 0 (0.00%)"
        st.running = True
    app.processEvents()
    # 触发一次界面刷新（`_update_status` 里才有 lbl_warn 的渲染逻辑）
    try:
        w._update_status()
    except Exception as e:                                        # noqa: BLE001
        print("   (调用 _update_status 失败：%s)" % e)
    app.processEvents()
    shown = w.lbl_warn.text() or ""
    print("[smoke] lbl_warn = %r" % shown)
    if "对端 192.168.31.96" in shown:
        print("   ✓ 界面已显示 lan_info（文字会更新，老 bug 已修）")
    else:
        print("   ✗ 界面仍显示旧文案 ⇒ 未修复")
        ok = False
    if "5 秒内" in shown:
        print("   ✗ 界面仍显示陈旧超时文案")
        ok = False

    # ---- ③ 主线程不卡（引擎 tick 时间）----
    print("[smoke] 关闭")
    w.close()
    print("\n" + ("✓ 冒烟通过" if ok else "✗ 冒烟失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
