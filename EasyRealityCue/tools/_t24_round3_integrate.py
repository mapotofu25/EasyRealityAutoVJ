# -*- coding: utf-8 -*-
"""★ 第三轮**整合验收编排器**（software-engineer-5-2-2）。

作用：在 T02/T03/T04 全部收工、工作树**静止**后，一键跑完整合回归并落结构化结果。
**只读产品代码**：不改 `src/`；只新增本脚本 + 输出目录。

流程（对应 team-lead 的整合清单）：
  0. **静止检查**：`window.py`/`timeline_widget.py`/`props_panel.py`/`preview.py` mtime 静态 ≥ `--idle`(默认180s)
  1. `compileall -q src/`
  2. `tools/selftest.py`
  3. `tests/` 全量（逐个 test_*.py，无头 + 禁 autosave）
  4. `_walk_ui_audit.py` → 解析 `_walk.json` 的 **B1–B7 逐值** 对标基线
  5. `_fps_probe.py` → **video_fps / compose p50** 对标基线（≈30.3 / ≈3.5ms）
  6. 交互验证：`_t24_t02_verify` / `_t24_t03_indep_verify` / `_t24_t04_verify` /
     `_t24_t04_verify_indep` / `_t24_a1_verify` / `_t24_a2_verify` / `_t24_a3_verify`
  7. 汇总 PASS/FAIL + 落 `P:\\ERAVJ\\brand\\ui_audit\\_round3_verify_out.json`

跑法：
    venv\\Scripts\\python.exe -u tools\\_t24_round3_integrate.py            # 完整
    venv\\Scripts\\python.exe -u tools\\_t24_round3_integrate.py --dry-run  # 只列步骤
    venv\\Scripts\\python.exe -u tools\\_t24_round3_integrate.py --skip-fps # 跳过 fps 探针（慢）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, "venv", "Scripts", "python.exe")
AUDIT = r"<项目根>\brand\ui_audit"
WALK = os.path.join(AUDIT, "_walk_ui_audit.py")
FPSTEMP = os.path.join(AUDIT, "_fps_probe.py")
OUT_JSON = os.path.join(AUDIT, "_round3_verify_out.json")
WALK_OUT = os.path.join(AUDIT, "_round3_walk")
FPS_OUT = os.path.join(AUDIT, "_round3_fps.json")

# 需要静止的核心文件（T02/T03/T04 的落点）
QUIESCE_FILES = [
    os.path.join(ROOT, "src", "ercue", "ui", "window.py"),
    os.path.join(ROOT, "src", "ercue", "ui", "timeline_widget.py"),
    os.path.join(ROOT, "src", "ercue", "ui", "props_panel.py"),
    os.path.join(ROOT, "src", "ercue", "ui", "preview.py"),
]

# 走查 B1–B7 基线（来自 _baseline_before_t02t04t05.md）
BASE_B1_LAYERS_AFTER = 2
BASE_B2_FINAL = 20000.0
BASE_B2B = 66.47
BASE_B6_NONBLACK = 0.86
BASE_B7_NONBLACK = 0.86
# 帧率基线
BASE_VIDEO_FPS = 30.3
BASE_COMPOSE_P50 = 3.5

RESULTS = []


def _mk_env():
    e = dict(os.environ)
    e.pop("QT_QPA_PLATFORM", None)
    e["ERCUE_NO_AUTOSAVE"] = "1"
    e.setdefault("ERCUE_PROF_MAIN", "0")
    return e


def _rec(label, ok, detail, rc=None):
    RESULTS.append({"label": label, "ok": bool(ok), "detail": detail, "rc": rc})
    print("  [%s] %s\n        %s" % ("PASS" if ok else "FAIL", label, detail))


def _run(label, argv, env=None, timeout=1200, cwd=ROOT):
    print("\n--- %s ---" % label)
    print("    $ %s" % " ".join(argv))
    try:
        p = subprocess.run(argv, cwd=cwd, env=env or _mk_env(),
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        _rec(label, False, "超时 %ss" % timeout)
        return None
    tail = "\n".join((p.stdout or "").strip().splitlines()[-6:])
    ok = p.returncode == 0
    _rec(label, ok, "rc=%d | %s" % (p.returncode, tail[-400:]), rc=p.returncode)
    return p


def check_quiescence(idle_needed):
    now = time.time()
    idles = {}
    for f in QUIESCE_FILES:
        try:
            idles[os.path.basename(f)] = int(now - os.path.getmtime(f))
        except OSError:
            idles[os.path.basename(f)] = -1
    worst = min(idles.values()) if idles else 0
    ok = worst >= idle_needed
    _rec("① 工作树静止（≥%ds）" % idle_needed, ok,
         "各文件 idle(s)=%s" % idles)
    return ok


def parse_walk():
    path = os.path.join(WALK_OUT, "_walk.json")
    if not os.path.isfile(path):
        _rec("④ 走查 B1–B7", False, "未找到 %s" % path)
        return
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    d = raw.get("probes", raw) if isinstance(raw, dict) else {}
    g = lambda k: d.get(k, {})
    b1 = g("B1_del_layer_two_layers_no_select").get("layers_after")
    b2 = g("B2_zoom_out_x20").get("final")
    b2c = g("B2_zoom_out_x20").get("capped")
    b2b = g("B2b_zoom_in_x16").get("view_span")
    b3 = (g("B3_del_cue_no_select").get("log_last") or "")
    b4 = g("B4_click_layer_name_gutter").get("sel_after")
    b5 = g("B5_cue_without_media").get("items")
    b6 = g("B6_preview_after_scrub_no_play").get("nonblack_ratio")
    b7 = g("B7_preview_after_set_active").get("nonblack_ratio")
    checks = {
        "B1 layers_after==2": b1 == BASE_B1_LAYERS_AFTER,
        "B2 capped & final==20000": (b2c is True and abs((b2 or 0) - BASE_B2_FINAL) < 1e-6),
        "B2b view_span≈66.47": (b2b is not None and abs(b2b - BASE_B2B) < 1.0),
        "B3 无副作用日志": "先在时间轴上点一个片段" in b3,
        "B4 sel_after==[None,None]": b4 == [None, None],
        "B5 items==[]": b5 == [],
        "B6 nonblack≈0.86": (b6 is not None and abs(b6 - BASE_B6_NONBLACK) < 0.05),
        "B7 nonblack≈0.86": (b7 is not None and abs(b7 - BASE_B7_NONBLACK) < 0.05),
    }
    ok = all(checks.values())
    _rec("④ 走查 B1–B7 逐值对标基线", ok,
         "值=%s | 逐项=%s" % ({"B1": b1, "B2": b2, "B2b": b2b, "B6": b6, "B7": b7}, checks))


def parse_fps(skip):
    if skip:
        _rec("⑤ 帧率探针", True, "SKIP（--skip-fps）")
        return
    if not os.path.isfile(FPS_OUT):
        _rec("⑤ 帧率探针", False, "未找到 %s" % FPS_OUT)
        return
    with open(FPS_OUT, encoding="utf-8") as f:
        d = json.load(f)
    vfps = float(d.get("video_fps", 0.0))
    cp50 = float((d.get("compose_interval_ms") or {}).get("p50", 0.0))
    ok = vfps >= BASE_VIDEO_FPS * 0.8
    _rec("⑤ video_fps 不退化（基线≈%.1f）" % BASE_VIDEO_FPS, ok,
         "video_fps=%.1f | compose p50=%.2fms（基线≈%.1f）" % (vfps, cp50, BASE_COMPOSE_P50))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--idle", type=int, default=180)
    ap.add_argument("--skip-fps", action="store_true")
    ap.add_argument("--force", action="store_true", help="即使未静止也继续")
    args = ap.parse_args()

    print("=" * 74)
    print("第三轮整合验收编排器")
    print("  ROOT=%s" % ROOT)
    print("=" * 74)

    # 步骤清单（dry-run 也打印）
    steps_preview = [
        "compileall -q src/",
        "tools/selftest.py",
        "tests/test_*.py（全量）",
        "_walk_ui_audit.py → B1–B7",
        ("(skip)" if args.skip_fps else "_fps_probe.py → video_fps"),
        "_t24_t02_verify.py",
        "_t24_t03_indep_verify.py",
        "_t24_t04_verify.py",
        "_t24_t04_verify_indep.py",
        "_t24_a1_verify.py / _t24_a2_verify.py / _t24_a3_verify.py",
    ]
    print("步骤：")
    for s in steps_preview:
        print("   • %s" % s)
    if args.dry_run:
        print("DRY_RUN_OK")
        return 0

    # ① 静止
    if not args.force and not check_quiescence(args.idle):
        print("\n!! 工作树尚未静止（< %ds）⇒ 中止，避免固化半成品。" % args.idle)
        print("   如确要继续加 --force。")
        _dump()
        print("HAS_FAILURES")
        return 1

    # ② compileall
    _run("② compileall", [PY, "-m", "compileall", "-q", "src/"])

    # ③ selftest
    _run("③ selftest", [PY, "tools/selftest.py"])

    # ③b tests 全量
    tests = sorted(f for f in os.listdir(os.path.join(ROOT, "tests"))
                   if f.startswith("test_") and f.endswith(".py"))
    tfail = []
    for t in tests:
        p = _run("③ 测试 %s" % t, [PY, os.path.join("tests", t)], timeout=600)
        if p is None or p.returncode != 0:
            tfail.append(t)
    _rec("③ tests 全量汇总", not tfail,
         "%d/%d 通过" % (len(tests) - len(tfail), len(tests))
         + ("" if not tfail else " | 失败: %s" % tfail))

    # ④ 走查
    os.makedirs(WALK_OUT, exist_ok=True)
    _run("④ 走查 _walk_ui_audit", [PY, WALK, WALK_OUT], timeout=900)
    parse_walk()

    # ⑤ 帧率
    if not args.skip_fps:
        _run("⑤ 帧率探针 _fps_probe", [PY, FPSTEMP, FPS_OUT, "3", "6"], timeout=600)
    parse_fps(args.skip_fps)

    # ⑥ 交互验证脚本
    for name in ("_t24_t02_verify.py", "_t24_t03_indep_verify.py", "_t24_t04_verify.py",
                 "_t24_t04_verify_indep.py", "_t24_a1_verify.py", "_t24_a2_verify.py",
                 "_t24_a3_verify.py"):
        path = os.path.join(ROOT, "tools", name)
        if not os.path.isfile(path):
            _rec("⑥ %s" % name, False, "脚本不存在")
            continue
        _run("⑥ %s" % name, [PY, path], timeout=600)

    # 汇总
    npass = sum(1 for r in RESULTS if r["ok"])
    nfail = len(RESULTS) - npass
    print("\n" + "=" * 74)
    print("整合验收：PASS %d / FAIL %d" % (npass, nfail))
    for r in RESULTS:
        print("   %s %s" % ("✓" if r["ok"] else "✗", r["label"]))
    print("=" * 74)
    _dump()
    print("INTEGRATE_OK" if nfail == 0 else "INTEGRATE_FAIL")
    return 0 if nfail == 0 else 1


def _dump():
    try:
        os.makedirs(AUDIT, exist_ok=True)
        with open(OUT_JSON, "w", encoding="utf-8") as f:
            json.dump(RESULTS, f, ensure_ascii=False, indent=1)
        print("(结果已落 %s)" % OUT_JSON)
    except OSError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
