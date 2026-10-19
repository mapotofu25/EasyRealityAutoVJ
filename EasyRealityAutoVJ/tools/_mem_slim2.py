# -*- coding: utf-8 -*-
"""整理 MEMORY.md：修掉被编辑弄丢的条目头 + 把细讲挪进 REFERENCE + 压回体积。"""
import io
import os

MEM = r"C:\Users\Administrator\WorkBuddy\自动VJ软件\.workbuddy\memory"
M = os.path.join(MEM, "MEMORY.md")
R = os.path.join(MEM, "REFERENCE.md")

# ① 修掉孤儿片段（把重复的残句删掉，恢复"删除不释放空间"条目头）
OLD1 = """  不是稳态开销（素材格式、分辨率）。我第一次归因到素材，方向错了。
：`os.remove` / `shutil.rmtree` 是**送进回收站**
  （删 155MB 后可用空间 0 变化），只有走**子进程的 `cmd` 删除**才真- ⚠⚠ **本机"删除"不释放空间**：`os.remove`/`shutil.rmtree` 是**送进回收站**（删 155MB 后可用
"""
NEW1 = """  不是稳态开销（素材格式、分辨率）。我第一次归因到素材，方向错了。
- ⚠⚠ **本机"删除"不释放空间**：`os.remove`/`shutil.rmtree` 是**送进回收站**（删 155MB 后可用
"""

# ② DPI 条目压成三行（细讲在 REFERENCE）
OLD2 = """- ★ **窗口尺寸必须按 DPI 反算**：用户填的"输出分辨率"是**物理像素**，而 Qt 的
  `resize()`/`geometry()` 是**逻辑像素** ⇒ 设窗口尺寸要 **除以 `screen().devicePixelRatio()`**
  （150% 时 1600 应设 1067），否则物理尺寸 ×1.5 会超出屏幕。**画布/NDI/Spout 的分辨率不要除**
  （那是输出规格）。回归测试 `tools/_test_dpi_window.py`（1/1.25/1.5/2 四档）。
"""
NEW2 = """- ★ **窗口尺寸必须按 DPI 反算**：用户填的分辨率是**物理像素**，Qt 的尺寸是**逻辑像素**
  ⇒ 设窗口要**除以 `devicePixelRatio()`**（150% 时 1600 应设 1067）；**画布/NDI/Spout 不要除**。
  回归测试 `tools/_test_dpi_window.py`（1/1.25/1.5/2 四档）。细讲见 REFERENCE §七。
"""

REF_ADD = """

## 七、高 DPI 窗口尺寸（2026-10-01）

**问题**（群友 FlandreS 的诊断，已在代码里核实）：全项目**没有任何 DPI 处理**；
`output_window.apply_window_size()` 直接 `resize(1600,900)`，而 Qt6 的尺寸是**逻辑像素**
⇒ 150% 缩放下物理变成 **2400×1350** ⇒ 在 2.5K 屏上**超出屏幕**（"没跟随系统缩放"）。

**算法**（他给的，正确）：物理 1600 ÷ 1.5 = **逻辑 1067×600**，系统再 ×1.5 渲染 ⇒ 物理正好 1600×900。

**改法**（`src/output_window.py`）：
- 新增 `_dpr()` 取屏幕缩放比例；
- `resize(物理 / dpr)`，屏幕边界判断改用**物理**尺寸（`availableGeometry()` 是逻辑的）；
- `_restore_window_geom` 的"无历史几何"分支同样换算（历史几何本身是逻辑值，不动）；
- **渲染画布 / NDI / Spout 保持不变**（输出规格不能跟着缩放跑）。

**实测**（`tools/_test_dpi_window.py`，4 档全绿）：
| QT_SCALE_FACTOR | 逻辑尺寸 | 物理尺寸 |
|---|---|---|
| 1.00 | 1600×900 | 1600×900 |
| 1.25 | 1280×720 | 1600×900 |
| 1.50 | 1067×600 | 1600×900 |
| 2.00 | 800×450 | 1600×900 |

⚠ 打包用 PyInstaller 默认清单（`dpiAware=true`，**系统级** DPI 感知），单缩放场景够用；
  跨屏混合缩放要更精确需自定义 manifest 声明 PerMonitorV2（未做）。
⚠ 写这类离屏测试：子进程若用 `os._exit(0)`，**必须给 python 加 `-u`**，否则 print 会被缓冲丢掉
  （我第一次就是这么"没输出"的）。
"""


def main():
    s = io.open(M, encoding="utf-8").read()
    n0 = len(s)
    for old, new, tag in ((OLD1, NEW1, "孤儿片段"), (OLD2, NEW2, "DPI 条目")):
        if s.count(old) != 1:
            print("✗ %s 锚点命中 %d 次" % (tag, s.count(old)))
            return 1
        s = s.replace(old, new)
    io.open(M, "w", encoding="utf-8", newline="\n").write(s)
    print("MEMORY.md：%d → %d 字符" % (n0, len(s)))
    bad = [l for l in s.splitlines() if l.startswith("：")]
    print(" 孤儿行检查：", bad or "无")

    t = io.open(R, encoding="utf-8").read()
    if "## 七、高 DPI 窗口尺寸" not in t:
        io.open(R, "a", encoding="utf-8").write(REF_ADD)
        print("REFERENCE.md 已追加 §七")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
