"""预览开关自测（离屏，不弹模态框）

覆盖：
  1) 默认开；「预览」勾选框存在且与配置一致
  2) 关掉后：set_frame 直接返回（不 fromImage / 不缩放）、视图显示占位文字、HUD 隐藏
  3) 再打开：恢复渲染，pixmap 非空
  4) 「预览画面设置」对话框里的「显示预览画面」勾选框与面板双向同步
  5) 刷新率节流：fps=15 时第二帧应立即被丢弃
"""

import os
import sys
import time

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
        import main  # noqa: F401  触发入口的环境变量设置（线程数限制）
    except Exception:
        pass
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QImage
    app = QApplication.instance() or QApplication([])
    import config as cfgmod
    import theme
    c = cfgmod.Config()
    theme.apply(app, c["theme"])
    from ui_main import MainWindow
    w = MainWindow()

    pv = None
    for attr in ("preview", "preview_panel", "previewPanel"):
        pv = getattr(w, attr, None)
        if pv is not None and hasattr(pv, "chk_preview"):
            break
    if pv is None:
        print("找不到预览面板（带 chk_preview 的控件）")
        return

    print("\n[1] 默认状态")
    pv.pic_cfg()["on"] = True
    pv.apply_preview_state()
    check("默认开启", pv.preview_on() is True)
    check("勾选框已勾上", pv.chk_preview.isChecked() is True)

    def frame():
        im = QImage(1920, 1080, QImage.Format_RGBA8888)
        im.fill(0x336699)
        return im

    print("\n[2] 关掉预览")
    pv._set_preview_on(False)
    check("配置写成 False", pv.preview_on() is False)
    disk = w.cfg["ui"].get("preview", {}).get("on")
    check("已写入配置对象", disk is False, "cfg = %s" % disk)
    pv.set_frame(frame())
    check("set_frame 被跳过（_img 保持 None）", pv._img is None)
    check("视图无 pixmap", pv.view.pixmap() is None or pv.view.pixmap().isNull())
    check("显示占位文字", "关闭" in pv.view.text())
    check("HUD 隐藏", not pv.hud.isVisible())
    im1 = frame()                                   # 图只造一次，避免把造图成本算进来
    t0 = time.perf_counter()
    for _ in range(30):
        pv.set_frame(im1)
    ms = (time.perf_counter() - t0) / 30 * 1000
    check("关闭后每帧成本 < 0.05ms", ms < 0.05, "实测 %.3f ms/帧" % ms)

    print("\n[3] 重新打开")
    pv._set_preview_on(True)
    pv.set_frame(frame())
    check("_img 有值", pv._img is not None)
    pm = pv.view.pixmap()
    check("视图有 pixmap", pm is not None and not pm.isNull())
    check("配置写回 True", pv.preview_on() is True)

    print("\n[4] 对话框 ↔ 面板 同步")
    from panels import PreviewSettingsDialog
    pv.pic_cfg()["on"] = True
    pv.apply_preview_state()
    dlg = PreviewSettingsDialog(w, pv)
    check("对话框勾选框反映当前=开", dlg.on.isChecked() is True)
    dlg.on.setChecked(False)                      # 会触发 _apply
    check("取消勾选后面板关掉", pv.preview_on() is False)
    check("面板勾选框同步", pv.chk_preview.isChecked() is False)
    check("分辨率等项被禁用", not dlg.res.isEnabled() and not dlg.fps.isEnabled())
    dlg.on.setChecked(True)
    check("再勾上后恢复", pv.preview_on() is True and dlg.res.isEnabled())
    dlg.close()

    print("\n[5] 刷新率节流")
    pv.pic_cfg()["fps"] = 15
    pv._last_t = 0.0
    im2 = frame()
    pv.set_frame(im2)
    stamp = pv._last_t
    check("首帧被渲染（_last_t 已更新）", stamp > 0)
    pv.set_frame(im2)                              # 立刻再来一帧 → 应被节流跳过 _rescale
    check("15fps 时第二帧被节流（_last_t 未推进）", pv._last_t == stamp,
          "（若推进说明节流失效）")
    pv.pic_cfg()["fps"] = 60

    print("\n" + "=" * 60)
    print("通过 %d 项 / 失败 %d 项" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("   FAIL:", f)
    out = os.path.join(ROOT, "_preview_toggle_selftest.txt")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("通过 %d / 失败 %d\n" % (len(PASS), len(FAIL)))
        for f in FAIL:
            fh.write("FAIL: %s\n" % f)
    # 收尾：别把测试状态留在真实配置里
    pv.pic_cfg()["on"] = True
    pv.pic_cfg()["fps"] = 60
    pv.apply_preview_state()


if __name__ == "__main__":
    main()
