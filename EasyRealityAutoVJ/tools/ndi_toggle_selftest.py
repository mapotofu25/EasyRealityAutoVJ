"""NDI 输出开关自测（离屏，不弹模态框）

覆盖：
  1) `probe_runtime()` 能拿到 NDI 版本（自带运行库可加载，与分辨率无关）
  2) ★ **回归验证**：勾上「NDI 输出」时**不能弹任何提示**。
     2026-09-25 的 092502 就栽在这里 —— UI 用无参数的 `ndi._ensure()` 当探测，
     而 `_ensure` 已改成必须带真实分辨率（发送端帧尺寸只能在 `open()` 前定死），
     不给尺寸时按设计返回 False → 一勾选就弹「NDI 不可用」并把勾选自动取消，
     用户永远打不开 NDI（引擎侧其实已经修好了）。
  3) 勾选后按真实分辨率真正建立发送端（640x360，本机无接收端也能建立）
  4) 取消勾选：配置回 False、引擎释放实例
  5) i18n：新增文案有英文映射

用法：venv\\Scripts\\python.exe tools\\ndi_toggle_selftest.py
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
        import main  # noqa: F401  与真实运行一致
    except Exception:
        pass
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    import config as cfgmod
    import theme
    c = cfgmod.Config()
    theme.apply(app, c["theme"])
    import ui_main
    from ui_main import MainWindow

    # 屏蔽模态框：误报时 set_ndi_enabled 会弹「NDI 不可用」，测试里不能弹，
    # 但要把调用记录下来（这正是本测试要断言的）
    shown = []

    def _fake_warning(*a, **k):
        shown.append(a[2] if len(a) > 2 else "")
        return None

    ui_main.QMessageBox.warning = staticmethod(_fake_warning)

    print("\n[1] NDI 运行时探测（与分辨率无关）")
    import ndi_out
    ok, detail = ndi_out.probe_runtime()
    print("      probe_runtime → ok=%s   %s" % (ok, detail))
    check("运行时能加载（随软件自带）", ok)
    if not ok:
        print("      ★ 运行时都加载不了，后面的开关测试没有意义")
        return 1

    w = MainWindow()
    op = w.output_panel
    orig = bool((w.cfg["output"] or {}).get("ndi_enabled"))
    # 从「关」开始
    try:
        w.cfg["output"]["ndi_enabled"] = False
        w.cfg.save()
        op.chk_ndi.blockSignals(True)
        op.chk_ndi.setChecked(False)
        op.chk_ndi.blockSignals(False)
        w.engine._ndi = None
    except Exception:
        pass

    print("\n[2] 默认状态")
    check("勾选框存在", hasattr(op, "chk_ndi"))
    check("默认未勾选", op.chk_ndi.isChecked() is False)
    check("引擎未挂 NDI 实例", w.engine._ndi is None)

    print("\n[3] ★ 勾上（这里必须一个提示都不弹）")
    shown.clear()
    op.chk_ndi.setChecked(True)
    app.processEvents()
    check("没有弹「NDI 不可用」", len(shown) == 0,
          ("弹了：" + str(shown[0])[:60]) if shown else "")
    check("配置已写入 True", bool((w.cfg["output"] or {}).get("ndi_enabled")) is True)
    check("勾选框保持勾选（没被回滚）", op.chk_ndi.isChecked() is True)
    check("引擎已挂 NDI 实例", w.engine._ndi is not None)
    check("_ndi_fail 已清零", w.engine._ndi_fail is False)

    print("\n[4] 真正发一帧（按真实分辨率建立发送端）")
    from PySide6.QtGui import QImage
    import numpy as np
    qi = QImage(640, 360, QImage.Format_RGB32)
    qi.fill(0xFFFFFF00)
    b = np.frombuffer(qi.bits(), dtype=np.uint8, count=qi.bytesPerLine() * 360)
    w.engine._ndi.send_video(b, 640, 360)
    check("发送端按 640x360 建立成功", w.engine._ndi.enabled is True,
          "last_err=%s" % (w.engine._ndi._last_err or "无"))
    check("记录了实际分辨率", (w.engine._ndi._w, w.engine._ndi._h) == (640, 360),
          "%s" % str((w.engine._ndi._w, w.engine._ndi._h)))

    print("\n[5] 取消勾选")
    op.chk_ndi.setChecked(False)
    app.processEvents()
    check("配置已写回 False", bool((w.cfg["output"] or {}).get("ndi_enabled")) is False)
    check("引擎已释放 NDI", w.engine._ndi is None)

    print("\n[6] i18n")
    import i18n_map
    for s in ("NDI 不可用", "错误详情：", "NDI 输出（音画同步）"):
        check("有英文映射：%s" % s, s in i18n_map.ZH2EN)

    # 恢复用户原来的开关状态（测试不能改用户的现场设置）
    try:
        w.cfg["output"]["ndi_enabled"] = orig
        w.cfg.save()
        print("\n（已把 ndi_enabled 恢复为测试前的 %s）" % orig)
    except Exception:
        pass

    print("\n" + "=" * 60)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - %s" % f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
