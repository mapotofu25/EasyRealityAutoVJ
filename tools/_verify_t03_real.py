# -*- coding: utf-8 -*-
"""T03 真机验证：**真实桌面窗口 + Windows 真实鼠标事件**走一遍「歌曲详情…」全流程。

为什么单独写这个脚本（项目铁律）：`tools/_test_t03.py` 是 offscreen 真类测试，只能证明
「类和信号能跑」，证明不了「在真机主程序里右键菜单没被别的分支吃掉 / 鼠标点得下去 /
点完真的落盘 / 关掉后引擎立刻拿到新段」。本脚本：

  · **真实窗口**：`QT_QPA_PLATFORM` 强制非 offscreen，MainWindow 真显示在桌面上；
  · **真实输入**：右键/左键/拖拽全部走 `user32.SetCursorPos` + `mouse_event`
    （与用户手动操作同一条 Windows 输入路径），不是 `QTest` / `action.trigger()` 假装；
  · **真实断言**：在进程内读 `cfg["music_meta"]` 与 `engine._song_grid_lookup`，
    打印每一个数字；每步都截图存证。

步骤（对应 team-lead 的 ①②③④）：
  1. 曲库列表右键一首本机歌 → 菜单里**必须**有「歌曲详情…」（且不被别的分支吃掉）
  2. 详情页：鼠标点「标八拍」→ 波形上点 2 个八拍锚 → 点「标分段」→ 拖动标 1 段
     → 点「保存」→ 关掉 → 重新右键打开，确认读回
  3. 演出中生效：关掉详情页后 `_inject_song_grids` 立即注入，
     `engine._grid_segment_of()` 取到新段/新锚
  4. 任何异常 → 打印 traceback 并整体判 FAIL

用法：venv\\Scripts\\python.exe -u tools\\_verify_t03_real.py [--target 关键词]
退出码：0=全绿，1=有失败步骤
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

# ★ 必须**不是** offscreen：本脚本要真实窗口 + 真实鼠标
os.environ.pop("QT_QPA_PLATFORM", None)

# ★★ 关键：让 `config.app_base_dir()` 指向**打包 exe 的数据目录**（%LOCALAPPDATA%/AutoVJ）。
#   源码运行时 base = 项目根 ⇒ 那里的 config.json 是空的（没有曲库/没有指纹库），
#   拿不到真实歌单，「真机验证」就退化成空跑。置 `sys.frozen` 后与 exe 同一份数据：
#   真实 66 首曲库 + 真实 grid + 真实 fingerprints.db。
#   （其它模块只用 `sys._MEIPASS` 且有源码兜底，不受影响，见 grep sys.frozen。）
if not getattr(sys, "frozen", False):
    sys.frozen = True

from PySide6.QtCore import Qt, QTimer                          # noqa: E402
from PySide6.QtWidgets import (QApplication, QDialogButtonBox,  # noqa: E402
                               QInputDialog, QMenu, QMessageBox, QPushButton)

u32 = ctypes.windll.user32
u32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
u32.WindowFromPoint.argtypes = [wt.POINT]
u32.WindowFromPoint.restype = wt.HWND
u32.GetAncestor.argtypes = [wt.HWND, ctypes.c_uint]
u32.GetAncestor.restype = wt.HWND
GA_ROOT = 2

LEFTDOWN, LEFTUP = 0x0002, 0x0004
RIGHTDOWN, RIGHTUP = 0x0008, 0x0010

TARGET_KEY = "Dust"
for _a in sys.argv[1:]:
    if _a.startswith("--target="):
        TARGET_KEY = _a.split("=", 1)[1]

REPORT = []
FAILS = []


def w(msg):
    print(msg, flush=True)
    REPORT.append(msg)


def _move(x, y):
    u32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)


def click(x, y, right=False):
    _move(x, y)
    dn, up = (RIGHTDOWN, RIGHTUP) if right else (LEFTDOWN, LEFTUP)
    u32.mouse_event(dn, 0, 0, 0, 0)
    time.sleep(0.04)
    u32.mouse_event(up, 0, 0, 0, 0)
    time.sleep(0.12)


def drag(x0, y0, x1, y1, steps=12):
    _move(x0, y0)
    u32.mouse_event(LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    for i in range(1, steps + 1):
        _move(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps)
    time.sleep(0.05)
    u32.mouse_event(LEFTUP, 0, 0, 0, 0)
    time.sleep(0.15)


def foreground(hwnd):
    u32.ShowWindow(hwnd, 9)
    u32.BringWindowToTop(hwnd)
    u32.SetForegroundWindow(hwnd)
    time.sleep(0.2)


app = QApplication.instance() or QApplication([])

import config as cfgmod                     # noqa: E402
import music_detail as MD                   # noqa: E402
from ui_main import MainWindow              # noqa: E402

# ---------------- 真实窗口 ----------------
WIN = MainWindow()
WIN.resize(1500, 900)
WIN.show()
foreground(int(WIN.winId()))
for _ in range(40):
    app.processEvents()
    time.sleep(0.05)
WIN.switch_page(2, save=False)          # 第 3 页：曲库·曲风
app.processEvents()
PANEL = WIN.music_panel
PANEL.refresh()
app.processEvents()
time.sleep(0.4)
app.processEvents()

# ⚠ 必须用 **主窗口自己的 cfg 对象**（Config() 每次会新建实例；用另一个实例写盘会被
#   主窗口退出时的 save() 覆盖回去 ⇒ 还原不了用户的真实配置）。
CFG = WIN.cfg

TARGET = None
for _p, _m in (CFG["music_meta"] or {}).items():
    if _p and os.path.isfile(_p) and TARGET_KEY.lower() in os.path.basename(_p).lower():
        if (_m.get("grid") or {}).get("bpm"):
            TARGET = _p
            break
if TARGET is None:
    print("找不到目标歌（关键词 %r），退出" % TARGET_KEY)
    sys.exit(2)
SID = int((CFG["music_meta"][TARGET] or {}).get("song_id") or -1)
OLD_MANUAL = (CFG["music_meta"][TARGET] or {}).get("grid_manual")

w("目标曲目：%s" % os.path.basename(TARGET))
w("  song_id = %s ; 自动 BPM = %s ; 现有 grid_manual = %s"
  % (SID, ((CFG["music_meta"][TARGET] or {}).get("grid") or {}).get("bpm"),
     "有" if OLD_MANUAL else "无"))

_wr = wt.RECT()
u32.GetWindowRect(int(WIN.winId()), ctypes.byref(_wr))
WBOX = (_wr.left, _wr.top, _wr.right, _wr.bottom)
w("主窗口 rect = %s" % (WBOX,))

try:
    from PIL import ImageGrab
except Exception:                                            # noqa: BLE001
    ImageGrab = None


def shot(tag):
    if ImageGrab is None:
        return
    try:
        ImageGrab.grab(bbox=WBOX).save(os.path.join(HERE, "_verify_t03_%s.png" % tag))
    except Exception as e:                                   # noqa: BLE001
        w("  (截图 %s 失败：%s)" % (tag, e))


def item_for_target():
    for i in range(PANEL.list.count()):
        it = PANEL.list.item(i)
        if it is not None and str(it.data(Qt.ItemDataRole.UserRole)) == TARGET:
            return it
    return None


def global_point_of_item(it):
    """把列表项中心换算成**屏幕绝对坐标**（真实鼠标点这个位置）。"""
    PANEL.list.scrollToItem(it)
    app.processEvents()
    r = PANEL.list.visualItemRect(it)
    p = PANEL.list.viewport().mapToGlobal(r.center())
    return p.x(), p.y()


def global_point_of_widget(wid):
    p = wid.mapToGlobal(wid.rect().center())
    return p.x(), p.y()


def find_dlg():
    for t in app.topLevelWidgets():
        if isinstance(t, MD.MusicDetailDialog) and t.isVisible():
            return t
    return None


def find_popup_menu():
    for t in app.topLevelWidgets():
        if isinstance(t, QMenu) and t.isVisible():
            return t
    return None


def dismiss_modal():
    """关掉任何挡路的模态小窗（QMessageBox / QInputDialog）——真鼠标点它的默认按钮。"""
    for t in app.topLevelWidgets():
        if not t.isVisible():
            continue
        if isinstance(t, QMessageBox):
            b = t.button(QMessageBox.StandardButton.Ok) or t.buttons()[0]
            click(*global_point_of_widget(b))
            w("    · 已点掉 QMessageBox(%r)" % t.text()[:40])
            return True
        if isinstance(t, QInputDialog):
            box = t.findChild(QDialogButtonBox)
            b = box.button(QDialogButtonBox.StandardButton.Ok) if box else None
            if b is not None:
                click(*global_point_of_widget(b))
                w("    · 已确认 QInputDialog（分段 BPM 默认值）")
            return True
    return False


def buttons_of(dlg):
    return [b for b in dlg.findChildren(QPushButton) if b.isVisible()]


def click_text_button(dlg, text):
    for b in buttons_of(dlg):
        if b.text().strip() == text:
            click(*global_point_of_widget(b))
            return True
    return False


# ---------------- 状态机 ----------------
STATE = {"i": 0, "fail": []}


def step(fn):
    STATE.setdefault("steps", []).append(fn)
    return fn


def record(ok, tag, msg):
    (w if ok else (lambda m: (w(m), FAILS.append(m))))("[%s] %s" % (tag, msg))
    shot(tag.replace(" ", "_"))

# ---- 步骤定义（每个 tick 跑一个）----
# ⚠ 状态机**先推进下标、再执行步骤**（见 tick()），且 Qt 对一个合成点击的处理是
#   **异步**的（按钮的 clicked 槽、QInputDialog 的返回值都要等下一轮事件循环）。
#   因此纪律是：**一个 tick 只做一个动作，验证放在下一个 tick**。
#   早先版本在同一步里"点完立刻断言"，实测读到的是点击前的旧状态（八拍=[] / 分段=0），
#   属于断言时机错误，不是功能问题。
@step
def s01_rightclick():
    it = item_for_target()
    if it is None:
        record(False, "①右键", "列表里找不到目标行")
        return
    gx, gy = global_point_of_item(it)
    w("① 真机右键列表行 '%s' → 屏幕 (%d,%d)"
      % (it.text().splitlines()[0][:46], gx, gy))
    click(gx, gy)                 # 先左键聚焦（与人工操作一致）
    click(gx, gy, right=True)
    shot("01_ctxmenu")


@step
def s02_menu_and_click_detail():
    m = find_popup_menu()
    if m is None:
        record(False, "①菜单", "右键后没有弹出 QMenu")
        return
    texts = [a.text() for a in m.actions() if a.text()]
    w("① 菜单项 = %s" % texts)
    hit = [a for a in m.actions() if "歌曲详情" in a.text()]
    record(bool(hit), "①菜单", "菜单里找到「歌曲详情…」: %s" % (hit[0].text() if hit else "无"))
    if not hit:
        return
    r = m.actionGeometry(hit[0])
    p = m.mapToGlobal(r.center())
    click(p.x(), p.y())           # 真机左键点菜单项
    w("① 真机左键点菜单项 (%d,%d)" % (p.x(), p.y()))


@step
def s03_open_and_anchor_mode():
    dlg = find_dlg()
    if dlg is None:
        record(False, "②打开", "详情页没打开")
        return
    record(True, "②打开", "详情页已打开（标题=%r，波形=%s，自动BPM=%.1f）"
           % (dlg.windowTitle(), "有" if dlg._wf else "无", dlg._auto_bpm))
    w("② 真机点「标八拍」")
    click_text_button(dlg, "标八拍")


@step
def s04_check_mode_and_anchor1():
    dlg = find_dlg()
    if dlg is None:
        record(False, "②模式", "详情页不在")
        return
    record(dlg.wave._mode == "anchor", "②模式",
           "点「标八拍」后 wave.mode=%r" % dlg.wave._mode)
    wv = dlg.wave
    r = wv.rect()
    c = wv.mapToGlobal(wv.rect().center())
    x = c.x() - r.width() // 2 + int(r.width() * 0.28)
    y = c.y() - r.height() // 2 + int(r.height() * 0.45)
    w("② 真机点波形 28%% 处 → (%d,%d)" % (x, y))
    click(x, y)


@step
def s05_check_anchor1_and_anchor2():
    dlg = find_dlg()
    if dlg is None:
        record(False, "②锚1", "详情页不在")
        return
    a = dlg.wave.anchors()
    record(len(a) == 1, "②锚1", "点 1 次后八拍锚 = %s" % (a,))
    wv = dlg.wave
    r = wv.rect()
    c = wv.mapToGlobal(wv.rect().center())
    x = c.x() - r.width() // 2 + int(r.width() * 0.62)
    y = c.y() - r.height() // 2 + int(r.height() * 0.45)
    w("② 真机点波形 62%% 处 → (%d,%d)" % (x, y))
    click(x, y)


@step
def s06_check_anchor2_and_segment_mode():
    dlg = find_dlg()
    if dlg is None:
        record(False, "②锚2", "详情页不在")
        return
    a = dlg.wave.anchors()
    record(len(a) == 2, "②锚2", "八拍锚 = %s" % (a,))
    shot("02_anchors")
    w("② 真机点「标分段」")
    click_text_button(dlg, "标分段")


@step
def s07_check_mode_and_drag():
    dlg = find_dlg()
    if dlg is None:
        record(False, "②分段", "详情页不在")
        return
    record(dlg.wave._mode == "segment", "②分段",
           "点「标分段」后 wave.mode=%r" % dlg.wave._mode)
    wv = dlg.wave
    r = wv.rect()
    c = wv.mapToGlobal(wv.rect().center())
    x0 = c.x() - r.width() // 2 + int(r.width() * 0.08)
    x1 = c.x() - r.width() // 2 + int(r.width() * 0.48)
    y = c.y() - r.height() // 2 + int(r.height() * 0.45)
    w("② 真机拖拽标分段 (%d,%d) → (%d,%d)" % (x0, y, x1, y))
    drag(x0, y, x1, y)
    shot("03_drag")


@step
def s08_confirm_segment_bpm():
    ok = dismiss_modal()
    record(ok, "②分段BPM", "已确认分段 BPM 输入框（默认=自动 BPM）")


@step
def s09_check_segments():
    dlg = find_dlg()
    if dlg is None:
        record(False, "②分段数", "详情页不在")
        return
    segs = dlg.wave.segments()
    record(len(segs) >= 1, "②分段数",
           "分段数 = %d ; 列表 = %s"
           % (len(segs), [dlg.lst_segments.item(i).text()
                          for i in range(dlg.lst_segments.count())]))
    w("② 保存前控件状态：八拍=%s ; 分段=%s ; dirty=%s"
      % (dlg.wave.anchors(), [("%.1fs/%.1f" % (s["t0"], s["bpm"])) for s in segs],
         dlg._dirty))
    STATE["want_anchors"] = list(dlg.wave.anchors())
    STATE["want_segments"] = len(segs)
    w("② 真机点「保存」")
    click_text_button(dlg, "保存")


@step
def s10_check_saved_cfg():
    dismiss_modal()               # "已保存手工标注。" 提示框
    gm = (CFG["music_meta"][TARGET] or {}).get("grid_manual")
    ok = bool(gm and gm.get("anchors") and gm.get("segments"))
    record(ok, "②落盘", "music_meta[path].grid_manual = %s" % (gm,))
    STATE["saved"] = gm


@step
def s11_close_detail():
    dlg = find_dlg()
    if dlg is None:
        record(False, "②关闭", "详情页不在")
        return
    w("② 真机点「关闭」")
    click_text_button(dlg, "关闭")


@step
def s12_check_engine_injected():
    dlg = find_dlg()
    gl = getattr(WIN.engine, "_song_grid_lookup", {}) or {}
    g = gl.get(SID)
    ok = bool(dlg is None and g and (g.get("segments") or g.get("anchors")))
    record(ok, "③注入", "详情页已关=%s ; engine._song_grid_lookup[sid] 段数=%s 锚数=%s"
           % (dlg is None, len((g or {}).get("segments") or []),
              len((g or {}).get("anchors") or [])))
    if not g:
        return
    from engine import AutoVJEngine
    w("③ engine._grid_segment_of（引擎每帧用它取当前段）：")
    for off in (5.0, 10.0, 21.0, 60.0, 200.0):
        bl, anc, sk = AutoVJEngine._grid_segment_of(g, off)
        w("    off=%6.1fs -> beat_len=%.4f anchor=%.3f segkey=%s" % (off, bl, anc, sk))
    w("③ cfg['auto']['beat_grid'] = %r（网格/HUD 段显示的总开关）"
      % (CFG["auto"].get("beat_grid"),))
    STATE["hud_off"] = 59.6
    STATE["hud_second"] = False


def _hud_feed(off):
    """喂一个「可信识别窗」给引擎网格时钟（票数 300、速率 1.0，与真实 audio_engine 同字段）。"""
    import time as _t
    snap = {"recognized_song_id": SID, "recognized_offset": off,
            "recognized_t": _t.perf_counter(), "recognized_rate": 1.0,
            "recognized_votes": 300, "bpm": 128.0, "beat_active": True,
            "beat_count": 0, "beat_phase": 0.0, "energy": 0.5, "energy_trend": 0.0}
    WIN.engine._grid_observe(snap)
    return snap


@step
def s12b_hud_window1():
    """HUD 取文本需要**两个**间隔 0.5s 的可信窗才能锁上网格时钟（GRID_CONSIST_*）。

    本步喂第 1 个窗；下一步（500ms 后）喂第 2 个窗并打印 HUD 实文本。
    """
    _hud_feed(59.6)
    STATE["hud_snap"] = True
    w("③ HUD：已喂第 1 个可信识别窗（off=59.6s，票数 300）")


@step
def s12c_hud_segment_text():
    """与 `_update_status` 完全同一条路径：`ui_snapshot()["grid_seg"]` → `_hud_grid_seg`。"""
    from ui_main import _hud_grid_seg
    snap = _hud_feed(60.1)
    info = WIN.engine.grid_segment_info(snap)
    txt = _hud_grid_seg({"grid_seg": info})
    record(info is not None and txt.strip() != "", "③HUD",
           "引擎锁定后 HUD 追加文本 = %r（grid_segment_info=%s）" % (txt, info))
    w("③ 完整 HUD 示例：%s"
      % ("常规  |  还有 12 拍切换  |  距八拍头 3.2 拍（1.5 秒）" + txt))
    # 段 1 也取一次（off 在 21.63s 之前）
    snap1 = _hud_feed(10.0)
    info1 = WIN.engine.grid_segment_info(snap1)
    w("③ HUD（段1，off=10s）= %r  ← 换段后文案跟着变"
      % (_hud_grid_seg({"grid_seg": info1}),))


@step
def s13_reopen_rightclick():
    it = item_for_target()
    if it is None:
        record(False, "②重开", "列表里找不到目标行")
        return
    gx, gy = global_point_of_item(it)
    click(gx, gy)
    click(gx, gy, right=True)
    w("② 重开：真机右键 (%d,%d)" % (gx, gy))


@step
def s14_reopen_click_detail():
    m = find_popup_menu()
    if m is None:
        record(False, "②重开", "第二次右键没弹菜单")
        return
    hit = [a for a in m.actions() if "歌曲详情" in a.text()]
    if not hit:
        record(False, "②重开", "菜单里没有「歌曲详情…」")
        return
    r = m.actionGeometry(hit[0])
    p = m.mapToGlobal(r.center())
    click(p.x(), p.y())
    w("② 重开：真机点「歌曲详情…」(%d,%d)" % (p.x(), p.y()))


@step
def s15_check_readback():
    dlg = find_dlg()
    if dlg is None:
        record(False, "②读回", "重开后详情页没打开")
        return
    want_a = STATE.get("want_anchors") or []
    want_n = STATE.get("want_segments") or 0
    got_a = dlg.wave.anchors()
    got_n = len(dlg.wave.segments())
    ok = (len(got_a) == len(want_a)
          and all(abs(x - y) < 1e-6 for x, y in zip(sorted(got_a), sorted(want_a)))
          and got_n == want_n)
    record(ok, "②读回",
           "重开读回：八拍=%s（保存前 %s）; 分段=%d（保存前 %d）; dirty=%s"
           % (got_a, want_a, got_n, want_n, dlg._dirty))
    shot("04_reopen")
    w("② 真机点「关闭」（读回后不保存，不该有二次确认）")
    click_text_button(dlg, "关闭")


@step
def s16_check_closed_and_restore():
    dlg = find_dlg()
    record(dlg is None, "②收尾", "二次关闭成功（详情页已关=%s）" % (dlg is None))
    if OLD_MANUAL:
        CFG["music_meta"][TARGET]["grid_manual"] = OLD_MANUAL
    else:
        CFG["music_meta"][TARGET].pop("grid_manual", None)
    CFG.save()
    gm = (CFG["music_meta"][TARGET] or {}).get("grid_manual")
    w("④ 已还原目标曲目的 grid_manual（现值=%r）—— 不动用户真实演出配置" % (gm,))
    WIN._inject_song_grids()
    g = (getattr(WIN.engine, "_song_grid_lookup", {}) or {}).get(SID) or {}
    w("④ 还原后再注入：该曲 segments=%d 条（无 manual ⇒ 空 = 单段模式，与改动前一致）"
      % len(g.get("segments") or []))


@step
def s17_finish():
    STATE["i"] = 10 ** 6
    w("")
    w("================ 结果 ================")
    if FAILS:
        w("❌ 失败 %d 项：" % len(FAILS))
        for f in FAILS:
            w("   - " + f)
    else:
        w("✅ 真机验证全部通过（①菜单 ②详情页鼠标标注/落盘/读回 ③引擎注入）")
    app.exit(1 if FAILS else 0)

STEPS = STATE["steps"]


def tick():
    i = STATE["i"]
    if i >= len(STEPS):
        return
    STATE["i"] = i + 1
    fn = STEPS[i]
    try:
        fn()
    except Exception:                                        # noqa: BLE001
        msg = "步骤 %s 抛异常：\n%s" % (fn.__name__, traceback.format_exc())
        w(msg)
        FAILS.append(msg)


def watchdog():
    i = STATE["i"]
    if i < len(STEPS) and i < 10 ** 6:
        w("⚠ 看门狗触发：卡在步骤 %d（%d/%d）" % (i, i, len(STEPS)))
        FAILS.append("watchdog: 卡在 %d" % i)
        STATE["i"] = 10 ** 6
        app.exit(1)


TIMER = QTimer()
TIMER.setInterval(500)
TIMER.timeout.connect(tick)
TIMER.start()
QTimer.singleShot(150000, watchdog)

shot("00_start")
rc = app.exec()
w("退出码 = %r" % rc)
sys.exit(0 if not FAILS else 1)
