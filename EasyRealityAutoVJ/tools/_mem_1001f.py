# -*- coding: utf-8 -*-
"""记录：需求确认 + 高 DPI 修复。"""
import io
import os

MEM = r"C:\Users\Administrator\WorkBuddy\自动VJ软件\.workbuddy\memory"
LOG = os.path.join(MEM, "2026-10-01.md")
MEMF = os.path.join(MEM, "MEMORY.md")

TXT = """

## 十一、新需求确认 + 高 DPI 窗口尺寸（已修）

用户给了一批新需求（含群里三位群友的反馈），确认后的范围：

### A. UI 重构（未开始，已确认形态）
1. 页签条从两页扩到**三页**：① 演出台 ② **素材库（全屏副本）** ③ 曲库·曲风。
   ⚠ 用户明确：**演出台的素材库原样不动**，第②页是"复制一份出来、可以全屏操作"。
   页号 `cfg["ui"]["page"]` 上界要从 1 改回 2。
2. 「曲库管理」从**弹窗改成整页**，左侧加排序/筛选栏。
   用户先要的筛选组：**BPM 区间** + **时长区间 / 文件缺失**。
   （我给的完整候选清单里还有：有无节拍网格、曲风未识别、匹配级别/来源、指纹状态、
   最近播放/播放次数——这些字段在 `cfg["music_meta"]` 里都有或可现算。）

### B. 群友反馈的三条（未开始）
1. **ASIO 低延迟输入**（群友"大米"，建议用 Steinberg 免费 ASIO SDK）。
   技术路线：照 `src/native/dxvz.c` + `tools/build_dxvlz.py`（zig 编译原生 DLL）加一个
   ASIO 采集模块；⚠ ASIO 驱动独占 ⇒ 必须能**优雅回退 WASAPI**，做成"音源类型"第三项。
2. **高 DPI 窗口尺寸** —— ✅ 本次已修，见下。
3. **GitHub 协作**（群友讨论：用 GitHub 管版本、允许其他人/其他 AI 提 PR、固定原作者防版权纠纷）。
   缺：CONTRIBUTING.md、把最新代码推上去（远端还停在 9-27）、署名/许可固定；
   ⚠ **Discogs-EffNet 是 CC BY-NC-SA 4.0 仅限非商业**，开源后必须写清（Beat This 是 MIT 没问题）。

### ★ 高 DPI 窗口尺寸（本次已修 + 实测）
根因：全项目**没有任何 DPI 处理**；`apply_window_size()` 直接 `resize(1600,900)`，
而 Qt6 的尺寸是**逻辑像素** ⇒ 150% 缩放下物理变成 2400×1350 ⇒ **超出屏幕**
（群友 FlandreS 的诊断完全正确）。
算法（也是他给的）：**物理 1600 ÷ 1.5 = 逻辑 1067×600**，系统再 ×1.5 ⇒ 物理正好 1600×900。

改法（`src/output_window.py`）：
- 新增 `_dpr()` 取屏幕缩放比例；
- `resize(物理 / dpr)`；屏幕边界判断改用**物理**尺寸（`availableGeometry()` 是逻辑的）；
- `_restore_window_geom` 的"无历史几何"分支同样换算（历史几何本身是逻辑值，不动）；
- **渲染画布 / NDI / Spout 分辨率保持不变**（那是输出规格，不能被缩放带跑）。

实测（`tools/_test_dpi_window.py`，4 档缩放全绿）：
| QT_SCALE_FACTOR | 逻辑尺寸 | 物理尺寸 |
|---|---|---|
| 1.00 | 1600×900 | **1600×900** |
| 1.25 | 1280×720 | **1600×900** |
| 1.50 | 1067×600 | **1600×900** |
| 2.00 | 800×450 | **1600×900** |

⚠ 打包用的是 PyInstaller 默认清单（`dpiAware=true`，系统级 DPI 感知），单缩放场景够用；
  若要跨屏混合缩放更精确，需要自定义 manifest 声明 PerMonitorV2（暂未做）。
"""

io.open(LOG, "a", encoding="utf-8").write(TXT)
print("日志已写")

# MEMORY.md 记一条硬约定
s = io.open(MEMF, encoding="utf-8").read()
add = """- ★ **窗口尺寸必须按 DPI 反算**：用户填的"输出分辨率"是**物理像素**，而 Qt 的
  `resize()`/`geometry()` 是**逻辑像素** ⇒ 设窗口尺寸要 **除以 `screen().devicePixelRatio()`**
  （150% 时 1600 应设 1067），否则物理尺寸 ×1.5 会超出屏幕。**画布/NDI/Spout 的分辨率不要除**
  （那是输出规格）。回归测试 `tools/_test_dpi_window.py`（1/1.25/1.5/2 四档）。
"""
if "窗口尺寸必须按 DPI 反算" not in s:
    i = s.find("## 输出")
    s = s[:i] + add + "\n" + s[i:]
    io.open(MEMF, "w", encoding="utf-8", newline="\n").write(s)
    print("MEMORY.md 已补（%d 字符）" % len(s))
