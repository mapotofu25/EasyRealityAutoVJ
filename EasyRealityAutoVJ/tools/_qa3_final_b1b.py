# -*- coding: utf-8 -*-
"""QA(严过关) —— 把 dist 复制到临时目录并删掉 make_zip 会跳过的文件，
用于"移走文件后 exe 仍能启动/渲染"的独立验证。绝不改 dist_new / src。

⚠ 多脚本编排的一环（**不是泄漏，默认不删**）：
  本脚本把精简版 dist 副本建到临时目录 `qa3_distcopy_*`，并把**指针**写入
  `%TEMP%/qa3_distcopy_path.txt`；下游 `tools/_qa3_realplat.py` 读该指针，
  **从那个目录启动 exe** 做真实平台冒烟（见 _qa3_realplat.py:14/15/56/60-61）。
  故该副本**必须存活到 realplat 跑完**（跨进程），**不可自动删除**。
  清理由人工或 run 完的一次性编排脚本负责；确实要清理时显式传 `--cleanup`
  或设环境变量 `QA3_CLEANUP=1`（**仅在这些情况下**才在写完指针后删）。
"""
import os
import shutil
import sys
import tempfile
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
import make_zip                                                     # noqa: E402

SRC = os.path.join(_ROOT, "dist_new", "EasyRealityAutoVJ")
ZIP = os.path.join(_ROOT, "EasyRealityAutoVJ_测试版100108.zip")

# 用户显式要求才清理；默认保留给下游 _qa3_realplat.py 用。
_CLEANUP = ("--cleanup" in sys.argv) or (os.environ.get("QA3_CLEANUP") == "1")

z = zipfile.ZipFile(ZIP)
skips = []
for it in z.infolist():
    if it.is_dir():
        continue
    n = it.filename                                   # EasyRealityAutoVJ/_internal/...
    p = os.path.join(_ROOT, "dist", n.replace("/", os.sep))
    if make_zip._is_qt_unused(p) or make_zip._is_cyndilib_junk(p):
        skips.append(os.path.join(SRC, os.path.relpath(n, "EasyRealityAutoVJ").replace("/", os.sep)))
print("skip list = %d" % len(skips))

dst_root = tempfile.mkdtemp(prefix="qa3_distcopy_")
dst = os.path.join(dst_root, "EasyRealityAutoVJ")
print("copying %s -> %s ..." % (SRC, dst))
shutil.copytree(SRC, dst)
removed = 0
for p in skips:
    rel = os.path.relpath(p, SRC)
    tp = os.path.join(dst, rel)
    if os.path.exists(tp):
        os.remove(tp)
        removed += 1
print("copied to: %s" % dst)
print("removed %d/%d skipped files" % (removed, len(skips)))
# 给下游 _qa3_realplat.py 用（**保留**：它读此指针后从该目录启动 exe 验证）
with open(os.path.join(tempfile.gettempdir(), "qa3_distcopy_path.txt"), "w") as f:
    f.write(dst)
print("DISTCOPY=%s" % dst)
if _CLEANUP:
    shutil.rmtree(dst_root, ignore_errors=True)
    print("已按 --cleanup/QA3_CLEANUP=1 删除副本目录（下游 realplat 将不可用）")
else:
    print("保留副本目录供 _qa3_realplat.py 消费；需清理请传 --cleanup 或设 QA3_CLEANUP=1")
