"""alpha 校正迁移自测

① dry-run：探测全库，列出「计划改成 PNG（真带 alpha）」和「计划改成 JPG（整段不透明）」，
   不动任何文件，先看清影响面
② 真跑 `AlphaMigrationWorker`（会改缩略图文件），计时
③ 验证判定链自洽：PNG 缩略图 ⇔ `_has_alpha` ⇔ 真带 alpha；且不会 PNG/JPG 同时存在
④ 幂等：再独立探测一遍，计划变更数应为 0（说明跑一次就够了）
⑤ 抽样复核：对改判为「带 alpha」的素材取帧，确认 alpha 真的有 < 255

用法：venv\\Scripts\\python.exe tools\\alpha_migration_selftest.py
"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(ROOT, "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

import av  # noqa: E402

import media_manager as mm  # noqa: E402

LIB = r"Y:\EasyRealityAutoVJ\素材库"
OUT = []
FAIL = []


def w(s=""):
    print(s, flush=True)
    OUT.append(str(s))


def chk(cond, msg):
    w("   [%s] %s" % ("OK  " if cond else "FAIL", msg))
    if not cond:
        FAIL.append(msg)


def plan(items):
    """返回 (计划改 PNG 的, 计划改 JPG 的, 真带 alpha 的)"""
    to_png, to_jpg, has_list = [], [], []
    for it in items:
        _, has = mm._av_probe_alpha(it.path)
        if has:
            has_list.append(it.name)
        png = os.path.exists(it._thumb_path(".png"))
        jpg = os.path.exists(it._thumb_path(".jpg"))
        if has and not png:
            to_png.append(it.name)
        elif (not has) and png:
            to_jpg.append(it.name)
    return to_png, to_jpg, has_list


def main():
    paths = [os.path.join(LIB, f) for f in sorted(os.listdir(LIB))
             if f.lower().endswith((".mov", ".mp4", ".mkv", ".webm", ".avi"))]
    items = [mm.MediaItem(p) for p in paths]
    w("=" * 96)
    w("alpha 校正迁移自测   %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    w("=" * 96)
    w("素材 %d 个视频" % len(items))

    before_png = sum(1 for it in items if os.path.exists(it._thumb_path(".png")))
    before_jpg = sum(1 for it in items if os.path.exists(it._thumb_path(".jpg")))
    w("迁移前缩略图：PNG %d 个 / JPG %d 个" % (before_png, before_jpg))

    w("")
    w("[1] dry-run（不改文件）")
    t0 = time.time()
    to_png, to_jpg, has_list = plan(items)
    w("   探测耗时 %.1fs" % (time.time() - t0))
    w("   真带 alpha：%d 个" % len(has_list))
    w("   计划改成 PNG：%d 个 → %s" % (len(to_png), to_png[:12]))
    w("   计划改成 JPG：%d 个 → %s" % (len(to_jpg), to_jpg[:6]))
    chk(len(has_list) > 0, "全库至少有一个真带 alpha 的素材")

    if os.environ.get("ALPHA_MIG_DRY") == "1":
        w("\n（ALPHA_MIG_DRY=1：只干跑，不动文件）")
        return 0

    w("")
    w("[2] 真跑迁移")
    t0 = time.time()
    work = mm.AlphaMigrationWorker(items)
    got = {}
    work.done.connect(lambda a, b: got.update({"a": a, "b": b}))
    work.start()
    work.wait()
    w("   耗时 %.1fs   报告：改 PNG %s 个 / 改 JPG %s 个"
      % (time.time() - t0, got.get("a"), got.get("b")))

    w("")
    w("[3] 验证判定链自洽")
    after_png = sum(1 for it in items if os.path.exists(it._thumb_path(".png")))
    after_jpg = sum(1 for it in items if os.path.exists(it._thumb_path(".jpg")))
    w("   迁移后缩略图：PNG %d 个 / JPG %d 个" % (after_png, after_jpg))
    bad_dup, bad_flag, bad_ext = [], [], []
    for it in items:
        png = os.path.exists(it._thumb_path(".png"))
        jpg = os.path.exists(it._thumb_path(".jpg"))
        if png and jpg:
            bad_dup.append(it.name)
        if it._has_alpha is not None and bool(it._has_alpha) != png:
            bad_flag.append("%s(png=%s,flag=%s)" % (it.name, png, it._has_alpha))
    chk(not bad_dup, "没有 PNG/JPG 同时存在的素材（%s）" % bad_dup[:3])
    chk(not bad_flag, "缩略图扩展名与 _has_alpha 一致（%s）" % bad_flag[:3])
    chk(after_png == len(has_list), "PNG 缩略图数(%d) == 真带 alpha 数(%d)"
        % (after_png, len(has_list)))

    w("")
    w("[4] 幂等（独立重判，应无变更）")
    to_png2, to_jpg2, has2 = plan(items)
    chk(not to_png2 and not to_jpg2,
        "重复跑没有新的变更（PNG %d / JPG %d）" % (len(to_png2), len(to_jpg2)))

    w("")
    w("[5] 抽样复核 alpha 真的存在")
    for name in to_png[:3]:
        p = os.path.join(LIB, name)
        c = av.open(p, metadata_errors="replace")
        try:
            mn = 255
            for k, fr in enumerate(c.decode(video=0)):
                arr = fr.to_ndarray(format="rgba")
                mn = min(mn, int(arr[:, :, 3].min()))
                if mn < 255 or k > 40:
                    break
        finally:
            c.close()
        chk(mn < 255, "%s 的 alpha 最小值 %d（应 < 255）" % (name[:36], mn))

    w("")
    w("=" * 96)
    w("结果：%s" % ("全部通过" if not FAIL else "有 %d 项失败" % len(FAIL)))
    for f in FAIL:
        w("  ★ %s" % f)
    with open(os.path.join(ROOT, "_alpha_migration_selftest.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    print("\n→ 已写入 _alpha_migration_selftest.txt")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
