# -*- coding: utf-8 -*-
"""清掉项目根目录里的测试残留 + 给 .gitignore 补规则，并把漏传的源文件加进仓库。

背景（2026-09-30 仓库体检）：
- 测试脚本曾用 `tempfile.mkdtemp(prefix=..., dir=项目根)` / `_cue_demo.make_demo(项目根)`
  造夹具 ⇒ 项目根留下一堆 `demo_src/`、`demochk*/`、`ercue_*/`、`hlreal_*/`、
  `示例码表.ercue(.media)/`，**且 .gitignore 没覆盖** ⇒ 下次 `git add -A` 会一并提交
  （其中 ercue_pr_* 每个 21MB，还含 wav 与码表包）。
- `src/phrase_bt.py` 反过来：**从没被 add 过**，而 beatgrid.py 会 import 它
  ⇒ 克隆下来功能静默降级（小节相位从"21/21 判对"退回"实测全错"的自算）。
"""
import glob
import io
import os
import shutil
import subprocess
import sys

ROOT = r"P:\AutoVJ"
JUNK_GLOBS = ["demo_src", "demochk*", "ercue_pr*", "ercue_ui*", "hl_rr_*", "hlreal_*",
              "_energy_one.csv", "示例码表.ercue", "示例码表.media"]
IGNORE_ADD = """
# ---------- 2026-09-30：测试夹具残留（测试脚本曾在**项目根**建临时目录）----------
# ⚠ 测试脚本请用 `tempfile.mkdtemp()`（不带 dir=），夹具一律落系统临时目录；
#   这里再兜一层，免得手滑 `git add -A` 把几十 MB 的测试素材提交上去。
/demo_src/
/demochk*/
/ercue_pr*/
/ercue_ui*/
/hl_rr_*/
/hlreal_*/
/_energy_one.csv
/示例码表.ercue
/示例码表.media/
"""


def main():
    # 1) 删残留（先报后删）
    targets = []
    for g in JUNK_GLOBS:
        targets += glob.glob(os.path.join(ROOT, g))
    targets = sorted(set(targets))
    if not targets:
        print("没有残留需要清理")
    for p in targets:
        try:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
                print("删目录", os.path.basename(p))
            else:
                os.remove(p)
                print("删文件", os.path.basename(p))
        except Exception as e:                                        # noqa: BLE001
            print("!! 删不掉", p, e)

    # 2) .gitignore 追加规则（幂等）
    gi = os.path.join(ROOT, ".gitignore")
    s = io.open(gi, encoding="utf-8").read()
    if "/demochk*/" in s:
        print(".gitignore 已有该规则，跳过")
    else:
        if not s.endswith("\n"):
            s += "\n"
        io.open(gi, "w", encoding="utf-8", newline="\n").write(s + IGNORE_ADD)
        print(".gitignore 已追加测试残留规则")

    # 3) 把漏传的源文件加进索引
    for f in ["src/phrase_bt.py"]:
        r = subprocess.run(["git", "add", "--", f], cwd=ROOT,
                           capture_output=True, text=True)
        print("git add", f, "→", "OK" if r.returncode == 0 else r.stderr.strip())

    # 4) 复查：还有没有"未跟踪且未忽略"的东西
    r = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                       cwd=ROOT, capture_output=True, text=True)
    left = [l for l in r.stdout.splitlines() if l.startswith("??")]
    print("\n仍未跟踪的文件 %d 个：" % len(left))
    for l in left[:20]:
        print("   ", l)
    return 0


if __name__ == "__main__":
    sys.exit(main())
