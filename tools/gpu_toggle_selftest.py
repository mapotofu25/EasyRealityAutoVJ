"""GPU 解码开关自测（离屏，不弹模态框）

覆盖：
  1) 默认关：「GPU 解码（实验）」勾选框存在、未勾选、模块总闸为关
  2) 勾上：配置写入 perf.gpu_decode、模块总闸打开、普查线程能起来
  3) 取消：配置与总闸都回到关
  4) 分流联动：开 → DXV 素材交给 GpuDxvPlayer；关 → 回到 AvAlphaPlayer
  5) 英文界面下勾选框与提示都是英文（i18n 覆盖）

用法：venv\\Scripts\\python.exe tools\\gpu_toggle_selftest.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(ROOT, "src"))

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("[OK ]" if cond else "[FAIL]", name, ("  " + extra) if extra else ""))


def main():
    try:
        import main  # noqa: F401  触发入口的环境变量设置
    except Exception:
        pass
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    import config as cfgmod
    import theme
    c = cfgmod.Config()
    theme.apply(app, c["theme"])
    import media_manager as mm
    import ui_main
    from ui_main import MainWindow

    # 屏蔽模态框（GPU 不可用时 set_gpu_decode 会弹提示；测试里不能弹）
    shown = []
    _orig_info = ui_main.QMessageBox.information

    def _fake_info(*a, **k):
        shown.append(a[2] if len(a) > 2 else "")
        return None

    ui_main.QMessageBox.information = staticmethod(_fake_info)

    # 从「关」开始，避免受上次测试影响
    try:
        mm.set_gpu_decode(False)
        c.data.setdefault("perf", {})["gpu_decode"] = False
        c.save()
    except Exception:
        pass

    w = MainWindow()
    st = w.settings
    print("\n[1] 默认状态")
    check("勾选框存在", hasattr(st, "chk_gpu"))
    check("默认未勾选", st.chk_gpu.isChecked() is False)
    check("模块总闸为关", mm.gpu_decode_enabled() is False)
    check("提示文案不是空的", len(st.chk_gpu.toolTip()) > 20)

    print("\n[2] 勾上")
    st.chk_gpu.setChecked(True)
    app.processEvents()
    check("配置已写入", bool((w.cfg["perf"] or {}).get("gpu_decode")) is True)
    check("模块总闸已开", mm.gpu_decode_enabled() is True)
    ok, msg = mm.gpu_status()
    print("      gpu_status: %s / %s" % (ok, msg))
    if not ok and shown:
        print("      （弹了提示框：%s）" % str(shown[-1])[:60])
    probe = getattr(w, "codec_probe", None)
    check("编码普查线程已启动", probe is not None)
    if probe is not None:
        probe.wait(30000)
        n = sum(1 for it in list(w.library.values())
                if getattr(it, "_codec", None) is not None)
        print("      普查后有编码信息的素材：%d / %d" % (n, len(w.library)))
        check("普查填了编码信息", n > 0)

    print("\n[3] 分流联动（真实素材）")
    import av
    lib = list(w.library.values())
    dxv = next((it for it in lib if getattr(it, "_is_dxv", None) is True), None)
    if dxv is None:
        print("      （素材库里没有已普查出 DXV 的素材，跳过）")
    else:
        pl = mm.create_video_player(dxv)
        check("开着 → GpuDxvPlayer", type(pl).__name__ == "GpuDxvPlayer",
              "%s / %s" % (dxv.name[:24], type(pl).__name__))
        pl.close()

    print("\n[4] 取消勾选")
    st.chk_gpu.setChecked(False)
    app.processEvents()
    check("配置已写回 False", bool((w.cfg["perf"] or {}).get("gpu_decode")) is False)
    check("模块总闸已关", mm.gpu_decode_enabled() is False)
    if dxv is not None:
        pl = mm.create_video_player(dxv)
        check("关着 → 回到原路径", type(pl).__name__ in ("AvAlphaPlayer", "VideoPlayer"),
              type(pl).__name__)
        pl.close()

    print("\n[5] 工具条开关与设置面板双向同步")
    tb = getattr(w, "btn_gpu", None)
    check("工具条上有 GPU 开关", tb is not None)
    if tb is not None:
        check("工具条按钮是可勾选的", tb.isCheckable() is True)
        tb.setChecked(True)
        app.processEvents()
        check("工具条勾选 → 面板勾选框跟随", st.chk_gpu.isChecked() is True)
        check("工具条勾选 → 总闸打开", mm.gpu_decode_enabled() is True)
        st.chk_gpu.setChecked(False)
        app.processEvents()
        check("面板取消 → 工具条跟随", tb.isChecked() is False)
        check("面板取消 → 总闸关闭", mm.gpu_decode_enabled() is False)
        check("工具条按钮有提示文案", len(tb.toolTip()) > 20)

    print("\n[6] 英文界面")
    import i18n
    i18n.set_lang("en")
    i18n.retranslate(w)
    app.processEvents()
    en = st.chk_gpu.text()
    tip = st.chk_gpu.toolTip()
    check("勾选框已翻英文", all(ord(ch) < 128 for ch in en) and "GPU" in en, en)
    check("提示已翻英文", "让显卡硬件解压" not in tip and "GPU" in tip)
    i18n.set_lang(c["lang"])
    i18n.retranslate(w)

    ui_main.QMessageBox.information = _orig_info
    try:
        w.close()
    except Exception:
        pass
    app.processEvents()

    print("\n" + "=" * 60)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  ★ %s" % f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
