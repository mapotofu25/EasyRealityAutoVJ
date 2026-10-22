# -*- coding: utf-8 -*-
"""重写日志里被 shell 吃掉的那段（反引号被当命令替换执行了）。

⚠ 教训（本轮第 3 次）：正文里带反引号/`$(...)`，**一律用文件方式执行**，
   绝不能塞进 `python -c "..."`。
"""
import io
import re

P = r"C:\Users\Administrator\WorkBuddy\自动VJ软件\.workbuddy\memory\2026-09-30.md"
MARK = "## 四、仓库体检"

ADD = """## 四、仓库体检（用户问「有没有不该上传的东西」）

**结论：远端是干净的**（67 个文件，全是源码 / 文档 / 构建脚本 / selftest）。
「不该上传」清单逐项核对全过 —— venv、dist、build、spec、exe、发行 zip、
assets/models、开发截图、更新内容类文案、内部工程笔记、config.json、.workbuddy、
失效的「更新到新版.bat」：**一个都没混进去**。
敏感信息扫描（token / 密码 / C 盘用户目录 / P 盘项目路径 / W 盘等个人路径）**0 命中**。
远端只有 main 一个分支 + 2 个 tag。`.gitattributes` 已正确标 DLL 为 binary、bat 保 CRLF。

**★ 查出两个真问题（已修）**

1. **该传没传**：`src/phrase_bt.py` **从没被 git add 过** —— 31 个 src 下的 .py 只有 30 个在仓库里。
   而 `beatgrid.py:334` 会 import 它（有 try/except 兜底 ⇒ 别人克隆下来**不会崩**，
   但会**静默降级**：小节相位从「Beat This! 21/21 判对」退回「自算相位实测 9/9 全错」）。
   已加入索引。
   附带说明：它依赖 `assets/models/beat_this/small0_fixed.onnx`，而模型**按设计不进仓库**
   ⇒ 克隆者需要自备模型。README 应写明「缺模型会自动降级」，否则就成了
   「代码在、功能永远不可用」，比报错更难发现。

2. **不该传的差点传上去**：项目根有一批**未跟踪、且 .gitignore 没覆盖**的测试夹具
   （我的责任：测试脚本用了 tempfile.mkdtemp 并把目录指到了项目根）：
   项目根下的 示例码表.ercue 与 示例码表.media、demochk 开头的 4 个、
   ercue_pr / ercue_ui 开头的 7 个（**其中两个各 21MB，内含 wav 与码表包**）、
   hl_rr / hlreal 开头的 9 个、以及 _energy_one.csv。
   只要执行一次 git add -A 就会被提交 —— 而 ercue 那批正是**刚被删掉的码表的测试数据**，
   等于「删了功能却把测试数据传上去」。
   ⇒ 已全部删除；`.gitignore` 加了规则；测试脚本改成落系统临时目录。
   现在未跟踪文件 **0 个**。

**一处可商榷（不算错）**：`src/_dxvlz.dll`（187KB 二进制）在仓库里。它有源码
`src/native/dxvlz.c` 与构建脚本 `tools/build_dxvlz.py`，属「预编译好让用户免装编译器」。
风险是**改了 C 源码却忘了重编 DLL**，两者静默不一致 ⇒ 建议在 README / NOTICE 写明
「改 dxvlz.c 后必须重编并一起提交」，或加一个自检比对。
"""


def main():
    s = io.open(P, encoding="utf-8").read()
    i = s.find(MARK)
    if i < 0:
        print("没找到标记，追加到末尾")
        s = s.rstrip() + "\n\n" + ADD
    else:
        # 回退到该小节上方那行空行
        j = s.rfind("\n## ", 0, i)
        if j < 0:
            j = i
        s = s[:j].rstrip() + "\n\n" + ADD
    io.open(P, "w", encoding="utf-8", newline="\n").write(s)
    t = io.open(P, encoding="utf-8").read()
    print("日志已重写：%d 字符" % len(t))
    print("完好性检查：")
    print("  有 '测?试夹具' 段：", "git add -A" in t)
    print("  无 shell 报错残留：", "command not found" not in t and "syntax error" not in t)
    bad = [l for l in t.splitlines() if "line 1:" in l or "cannot execute binary" in l]
    print("  脏行数：", len(bad))


if __name__ == "__main__":
    main()
