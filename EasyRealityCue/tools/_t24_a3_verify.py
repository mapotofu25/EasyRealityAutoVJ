# -*- coding: utf-8 -*-
"""第三轮 T02 —— **A3/#20 变速一致性** 双路径实测（真窗口、无头）。

症状（用户实测复现）：新拖入的片段（`add_cue` 默认 `src_out_sec=None`）变速 0.5 后
`end` **不变**（实测 `end=7.330`，应 `14.667`）⇒ 时间轴块长与"源跨度/变速"不一致。

判据（**必须两条都过**）：
  ① **无出点**场景**收敛**：`src_out=None` 的新片段，0.5× ⇒ `end = at + (src_dur - src_in)/speed`；
  ② **有出点**场景**仍对**：设了 `src_out` 的片段，变速 ⇒ `end = at + (src_out - src_in)/speed`；
  ③ 护栏：**平移**片段（at/end 同移、时长不变）**不得**被误判成"改了时长"（`src_out` 不被改写）。

真入口：直接驱动「④ 片段属性」面板（`sp_speed` / `sp_src_out` / `sp_at` 的 valueChanged）
⇒ `props_panel.changed` ⇒ `window._on_props_changed()`（Q7 裁定的**单点** reconcile）。

跑法：<Cue>\\venv\\Scripts\\python.exe -u tools\\_t24_a3_verify.py [OUT_JSON]
"""
import json
import os
import sys

CUE = r"<项目根>\EasyRealityCue"
sys.path.insert(0, os.path.join(CUE, "src"))
os.environ.pop("QT_QPA_PLATFORM", None)
os.environ.setdefault("ERCUE_PROF_MAIN", "0")
os.environ["ERCUE_NO_AUTOSAVE"] = "1"             # P0：不落盘、不恢复（测试隔离）
import logging                                    # noqa: E402
logging.disable(logging.CRITICAL)

from PySide6.QtWidgets import QApplication        # noqa: E402
from PySide6.QtCore import Qt as _Qt              # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(CUE, "_t24_a3.json")
MOV = r"Y:\EasyRealityAutoVJ\素材库\001 (8).mov"

app = QApplication.instance() or QApplication([])
from ercue.ui.window import MainWindow             # noqa: E402

res = []


def rec(name, ok, detail=""):
    res.append({"项": name, "结论": "PASS" if ok else "FAIL", "detail": detail})
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, detail))


def pump(n=6):
    for _ in range(n):
        app.processEvents()


def _mk():
    w = MainWindow(restore_workspace=False, autosave=False)
    w._interactive = False
    w.setAttribute(_Qt.WA_DontShowOnScreen, True)
    w.resize(1256, 850)
    w.show()
    pump()
    return w


def _sel(w, ly, it):
    """选中片段（走 timeline → props 面板），等价于"在时间轴上点一下这个片段"。

    ⚠ **不**额外调 `_on_props_changed()` —— 真实用户"选中"只是 `refresh()` 回填（`_loading` 守卫，
    不写模型）；只有**改字段**才会 commit。多加一次 commit 属于测试伪影。
    """
    w.timeline.set_selected(ly.lyr_id, it.item_id)
    w._sync_props_selection()
    pump()


def main() -> int:
    if not os.path.isfile(MOV):
        print("!! 测试素材缺失：%s" % MOV)
        return 2
    w = _mk()
    try:
        ids = w.add_media_paths([MOV])
        w.add_track("A3曲目")
        ly = w._layers()[0]
        cid = ids[0]
        src_dur = w._clip_source_dur(cid)
        print("素材：%s  src_dur=%.4f 秒" % (os.path.basename(MOV), src_dur))
        rec("能读到素材全长（src_dur>0）", src_dur > 0.0, "src_dur=%.4f" % src_dur)

        # ---------------- ① 无出点（新片段默认） ----------------
        it = w.add_cue(ly.lyr_id, cid, 0.0)
        at0, end0 = float(it.at_sec), (None if it.end_sec is None else float(it.end_sec))
        rec("① 无出点：新片段 src_out 为 None", it.src_out_sec is None,
            "src_out=%r  end0=%r" % (it.src_out_sec, end0))
        _sel(w, ly, it)
        w.props_panel.sp_speed.setValue(0.5)       # 走真信号 → commit → reconcile
        pump()
        exp1 = at0 + (src_dur - float(it.src_in_sec)) / 0.5
        got1 = float(it.end_sec) if it.end_sec is not None else None
        ok1 = (got1 is not None and abs(got1 - exp1) < 0.05
               and abs(float(it.speed) - 0.5) < 1e-9 and it.src_out_sec is None)
        print("     无出点：end %.4f → %.4f（应 %.4f = at + src_dur/0.5）" % (end0, got1 or -1, exp1))
        rec("① 无出点：0.5× 后 end 收敛（不再保持 7.330 那类旧值）", ok1,
            "end0=%.4f  end_new=%.4f  expect=%.4f  src_out=%r" % (end0, got1 or -1, exp1, it.src_out_sec))

        # ---------------- ② 有出点（设了 src_out） ----------------
        it2 = w.add_cue(ly.lyr_id, cid, 0.0)
        _sel(w, ly, it2)
        w.props_panel.sp_src_out.setValue(7.0)     # 出点 = 7 秒（源范围权威）
        pump()
        set_ok = (it2.src_out_sec is not None and abs(float(it2.src_out_sec) - 7.0) < 1e-6)
        rec("② 有出点：设 src_out=7 生效", set_ok, "src_out=%r" % it2.src_out_sec)
        w.props_panel.sp_speed.setValue(0.5)
        pump()
        exp2 = float(it2.at_sec) + (float(it2.src_out_sec or 0.0) - float(it2.src_in_sec)) / 0.5
        got2 = float(it2.end_sec) if it2.end_sec is not None else None
        ok2 = (got2 is not None and abs(got2 - exp2) < 0.05
               and it2.src_out_sec is not None and abs(float(it2.src_out_sec) - 7.0) < 1e-6)
        print("     有出点：end=%.4f（应 %.4f = at + (7-0)/0.5）" % (got2 or -1, exp2))
        rec("② 有出点：0.5× 后 end 仍自洽（源范围权威）", ok2,
            "end=%.4f  expect=%.4f  src_out=%r" % (got2 or -1, exp2, it2.src_out_sec))

        # ---------------- ③ 护栏：平移不误判 ----------------
        dur_before = float(it2.end_sec) - float(it2.at_sec)
        out_before = it2.src_out_sec
        _sel(w, ly, it2)
        w.props_panel.sp_at.setValue(20.0)         # 平移到 20 秒（at/end 同移，时长不变）
        pump()
        dur_after = (None if it2.end_sec is None else float(it2.end_sec) - float(it2.at_sec))
        ok3 = (dur_after is not None and abs(dur_after - dur_before) < 0.05
               and it2.src_out_sec == out_before and abs(float(it2.at_sec) - 20.0) < 0.05)
        rec("③ 护栏：平移片段不被误判成'改时长'（src_out 不变、时长保持）", ok3,
            "at=%.3f  dur %.4f→%s  src_out %r→%r"
            % (float(it2.at_sec), dur_before, dur_after, out_before, it2.src_out_sec))

        # ---------------- ④ 护栏：只改"非几何字段"（备注）不应凭空写出 src_out ----------------
        # （2 位小数的 `sp_len` 回填会让 `end` 漂移 <5ms；若 reconcile 把它当成"用户改了时长"，
        #   就会给一个 `src_out=None` 的片段凭空写 `src_out` —— 这正是要防的"污染"。）
        it4 = w.add_cue(ly.lyr_id, cid, 0.0)
        _sel(w, ly, it4)
        w.props_panel.le_note.setText("只改备注")
        w.props_panel._on_edit()                    # 等价于用户敲了一次备注（emit changed）
        pump()
        ok4 = it4.src_out_sec is None
        rec("④ 护栏：只改备注不污染 src_out（保持 None）", ok4,
            "src_out=%r  end=%s" % (it4.src_out_sec, it4.end_sec))

        # ---------------- ⑤ lead 点名判据：设出点=5s（speed=1.0）⇒ end=5.000（保持绿） ----------------
        it5 = w.add_cue(ly.lyr_id, cid, 0.0)
        _sel(w, ly, it5)
        w.props_panel.sp_src_out.setValue(5.0)      # 出点 = 5 秒（speed 保持 1.0）
        pump()
        got5 = float(it5.end_sec) if it5.end_sec is not None else None
        ok5 = (got5 is not None and abs(got5 - 5.0) < 0.05
               and it5.src_out_sec is not None and abs(float(it5.src_out_sec) - 5.0) < 1e-6)
        rec("⑤ 判据：设出点=5s（speed=1.0）⇒ end=5.000 保持绿", ok5,
            "end=%s  src_out=%r" % (got5, it5.src_out_sec))
    finally:
        try:
            w.shutdown()
        except Exception:                          # noqa: BLE001
            pass
        pump(3)

    npass = sum(1 for r in res if r["结论"] == "PASS")
    nfail = sum(1 for r in res if r["结论"] == "FAIL")
    try:
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    print("\n" + "=" * 68)
    for r in res:
        print("  %-4s %-46s %s" % (r["结论"], r["项"], r["detail"][:60]))
    print("=" * 68)
    print("结果：PASS %d / FAIL %d" % (npass, nfail))
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
