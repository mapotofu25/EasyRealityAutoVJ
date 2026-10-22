# -*- coding: utf-8 -*-
"""高 DPI：窗口尺寸按系统缩放**反向换算**，让物理尺寸正好等于设置的分辨率。

问题（用户群里 FlandreS 的诊断，我在代码里核实了）：
  · 全项目**没有任何 DPI 处理**；
  · `apply_window_size()` 直接 `resize(1600,900)`，而 Qt6 的尺寸是**逻辑像素**
    ⇒ 150% 缩放下物理变成 **2400×1350**，在 2.5K 屏上直接**超出屏幕**。

用户给的算法（正确）：
  物理 1600 ÷ 1.5 = **逻辑 1067×600**，系统再 ×1.5 渲染 ⇒ 物理正好 1600×900。

实现要点：
  1. 新增 `_dpr()` 取当前屏幕缩放比例；
  2. `resize(物理/dpr)`；屏幕边界判断改用**物理**尺寸（availableGeometry 是逻辑的）；
  3. `_restore_window_geom` 的"没有历史几何"分支同样换算（历史几何本身就是逻辑值，不动）；
  4. ⚠ 渲染画布/NDI/Spout 的分辨率**保持用户设置值不变**（那是输出规格，不能被缩放带跑）。
"""
import io
import os
import sys

P = r"P:\AutoVJ\src\output_window.py"
PAT = []


def rep(old, new, cnt=1):
    PAT.append((old, new, cnt))


# 1) _dpr 助手
rep("""    def apply_window_size(self):""",
    """    def _dpr(self, scr=None):
        \"\"\"当前屏幕的缩放比例（Windows 150% ⇒ 1.5；100% ⇒ 1.0）。

        ⚠ 必须用它：Qt6 的 `resize()` / `geometry()` 全是**逻辑像素**，而用户在
          设置里填的"输出分辨率 1600×900"指的是**物理像素**。
          150% 缩放下直接 `resize(1600,900)` ⇒ 物理 2400×1350 ⇒ **超出屏幕**
          （用户 2026-10-01 报的就是这个）。正确做法：1600÷1.5 = 逻辑 1067×600，
          系统再 ×1.5 渲染，物理正好 1600×900。
        \"\"\"
        try:
            s = scr if scr is not None else (self.screen() or QGuiApplication.primaryScreen())
            return float(s.devicePixelRatio()) if s is not None else 1.0
        except Exception:                                          # noqa: BLE001
            return 1.0

    def apply_window_size(self):""")

# 2) apply_window_size：逻辑尺寸 + 物理边界
rep("""        w = int(self.cfg["output"].get("width", 1280))
        h = int(self.cfg["output"].get("height", 720))
        self.resize(w, h)
        scr = self.screen() or QGuiApplication.primaryScreen()
        sg = scr.availableGeometry() if scr is not None else None
        if sg is None:
            return
        if w > sg.width() or h > sg.height():
            self.move(sg.x(), sg.y())
            return
        geo = getattr(self, "_win_geom", None) or self.cfg["output"].get("geometry")
        x = y = None
        if geo and len(geo) == 4:
            x, y = int(geo[0]), int(geo[1])
        if (x is None or y is None
                or not (sg.x() - 50 <= x <= sg.x() + sg.width())
                or not (sg.y() - 50 <= y <= sg.y() + sg.height())):
            x = sg.x() + (sg.width() - w) // 2
            y = sg.y() + (sg.height() - h) // 2
        x = max(sg.x(), min(x, sg.x() + sg.width() - w))
        y = max(sg.y(), min(y, sg.y() + sg.height() - h))
        self.move(x, y)""",
    """        # 用户填的是**物理**分辨率；Qt 用的是**逻辑**像素 ⇒ 先除以缩放比例。
        w = int(self.cfg["output"].get("width", 1280))
        h = int(self.cfg["output"].get("height", 720))
        scr = self.screen() or QGuiApplication.primaryScreen()
        dpr = self._dpr(scr)
        lw = max(1, int(round(w / dpr)))
        lh = max(1, int(round(h / dpr)))
        self.resize(lw, lh)
        sg = scr.availableGeometry() if scr is not None else None
        if sg is None:
            return
        # ⚠ 边界判断要用**物理**尺寸：availableGeometry() 给的是逻辑尺寸
        pw, ph = int(sg.width() * dpr), int(sg.height() * dpr)
        if w > pw or h > ph:
            self.move(sg.x(), sg.y())
            return
        geo = getattr(self, "_win_geom", None) or self.cfg["output"].get("geometry")
        x = y = None
        if geo and len(geo) == 4:
            x, y = int(geo[0]), int(geo[1])
        if (x is None or y is None
                or not (sg.x() - 50 <= x <= sg.x() + sg.width())
                or not (sg.y() - 50 <= y <= sg.y() + sg.height())):
            x = sg.x() + (sg.width() - lw) // 2
            y = sg.y() + (sg.height() - lh) // 2
        x = max(sg.x(), min(x, sg.x() + sg.width() - lw))
        y = max(sg.y(), min(y, sg.y() + sg.height() - lh))
        self.move(x, y)""")

# 3) 没有历史几何时的初值同样换算
rep("""            if sg is not None:
                w = min(int(ow), int(sg.width() * 0.65))
                h = min(int(oh), int(sg.height() * 0.65))
                if ow and oh:
                    k = min(w / ow, h / oh)
                    w, h = max(320, int(ow * k)), max(180, int(oh * k))
                geo = None
                self.resize(w, h)""",
    """            if sg is not None:
                # ⚠ 同上：ow/oh 是**物理**分辨率，屏幕尺寸是**逻辑**的，先换算再比
                dpr = self._dpr(scr)
                lw0 = max(1.0, ow / dpr)
                lh0 = max(1.0, oh / dpr)
                w = min(int(lw0), int(sg.width() * 0.65))
                h = min(int(lh0), int(sg.height() * 0.65))
                if lw0 and lh0:
                    k = min(w / lw0, h / lh0)
                    w, h = max(320, int(lw0 * k)), max(180, int(lh0 * k))
                geo = None
                self.resize(w, h)""")


def main():
    s = io.open(P, encoding="utf-8").read()
    for i, (old, new, cnt) in enumerate(PAT, 1):
        n = s.count(old)
        if n != cnt:
            print("✗ 第 %d 处命中 %d 次（应为 %d）" % (i, n, cnt))
            return 1
    for old, new, cnt in PAT:
        s = s.replace(old, new)
    io.open(P, "w", encoding="utf-8", newline="\n").write(s)
    print("✓ output_window.py 已改为按 DPI 反算窗口尺寸（%d 处）" % len(PAT))
    import py_compile
    py_compile.compile(P, doraise=True)
    print("语法 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
