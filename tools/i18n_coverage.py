"""UI 文案中英覆盖度检查（切英文界面有没有残留中文）

用 AST 扫源码里「用户可见的中文文案」—— 控件文本 / 提示 / 标题 / setText / T() / Tf() /
下拉项 / 设置区块标题 —— 逐条查 `i18n_map.ZH2EN`，报出**漏翻**的条目。

为什么需要：切英文后残留的中文只能靠肉眼发现（历史上就漏过）。把「新增文案必须
跑覆盖度验证」变成一条命令。

用 AST（不是正则）的原因：多行长文案在源码里是相邻字面量隐式拼接的
（`"...前半句，"` 换行 `"后半句。"`），正则只能抓到半句 → 满屏假阳性。
AST 能把它们拼回完整字符串，报告里就只剩真正缺的。

白名单：语言下拉里的「中文」是故意的（语言名用母语显示），不算缺。

用法：venv\\Scripts\\python.exe tools\\i18n_coverage.py
"""

import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from i18n_map import ZH2EN  # noqa: E402

CJK = re.compile(r"[\u4e00-\u9fff]")
SRC = os.path.join(ROOT, "src")
SKIP_FILES = {"i18n_map.py", "i18n.py"}          # 词表本身与机制文件不算
IGNORE = {"中文"}                                 # 语言下拉：语言名用自己的语言写

# 只检查这些调用里的字符串 —— 它们进的都是界面
CALL_NAMES = {
    "QLabel", "QCheckBox", "QPushButton", "QGroupBox", "QRadioButton", "QToolButton",
    "QAction", "setToolTip", "setStatusTip", "setWindowTitle", "setText",
    "T", "Tf", "_add_section", "addItem", "addItems",
}


def _call_name(node):
    fn = node.func
    if isinstance(fn, ast.Attribute):
        return fn.attr
    if isinstance(fn, ast.Name):
        return fn.id
    return ""


def _strings_in(call):
    """调用参数里的字符串字面量 → [(文本, 是否动态拼装), ...]。

    动态拼装（f-string 的片段、`+` 拼接）按项目约定**不进词表** ——
    它们含运行期数值，代码里应该用 `Tf("…{}…", v)` 组装或标 `_i18nDynamic`
    （见 MEMORY：动态文本必须让 retranslate 跳过）。所以这里直接排除。
    """
    out = []

    def walk(n, dyn=False):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append((n.value, dyn))
        elif isinstance(n, (ast.List, ast.Tuple, ast.Set)):
            for e in n.elts:
                walk(e, dyn)
        elif isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
            walk(n.left, True)
            walk(n.right, True)
        elif isinstance(n, ast.JoinedStr):
            for v in n.values:
                if isinstance(v, ast.FormattedValue):
                    for sub in ast.walk(v.value):
                        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                            out.append((sub.value, True))
                elif isinstance(v, ast.Constant) and isinstance(v.value, str):
                    out.append((v.value, True))
        elif isinstance(n, ast.IfExp):
            walk(n.body, dyn)
            walk(n.orelse, dyn)

    for a in list(call.args) + [k.value for k in call.keywords]:
        walk(a)
    return out


def scan():
    miss, total = {}, 0
    for fn in sorted(os.listdir(SRC)):
        if not fn.endswith(".py") or fn in SKIP_FILES:
            continue
        path = os.path.join(SRC, fn)
        try:
            src = open(path, encoding="utf-8").read()
            tree = ast.parse(src)
        except Exception as e:                                        # noqa: BLE001
            print("  （跳过 %s：%s）" % (fn, e))
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) not in CALL_NAMES:
                continue
            for s, dyn in _strings_in(node):
                if dyn:                       # 动态拼装：不进词表（见 _strings_in 说明）
                    continue
                if not s.strip() or not CJK.search(s) or s in IGNORE:
                    continue
                total += 1
                if s not in ZH2EN:
                    miss.setdefault(s, []).append((fn, node.lineno, _call_name(node)))
    return total, miss


def main():
    total, miss = scan()
    print("=" * 90)
    print("UI 文案中英覆盖度检查")
    print("=" * 90)
    print("扫到中文文案 %d 处；词表 ZH2EN 共 %d 条" % (total, len(ZH2EN)))
    print()
    if not miss:
        print("✅ 全部覆盖，无残留中文")
    else:
        print("⚠ 有 %d 条中文文案在词表里找不到英文：" % len(miss))
        for text, where in sorted(miss.items(), key=lambda kv: (kv[1][0][0], kv[1][0][1])):
            f, ln, name = where[0]
            shown = text.replace("\n", "\\n")
            if len(shown) > 60:
                shown = shown[:60] + "…"
            print("  %s:%d  (%s)  %s" % (f, ln, name, shown))
    with open(os.path.join(ROOT, "_i18n_coverage.txt"), "w", encoding="utf-8") as fh:
        fh.write("扫到 %d 处；缺 %d 条\n\n" % (total, len(miss)))
        for text, where in sorted(miss.items()):
            f, ln, name = where[0]
            fh.write("%s:%d\t%s\t%s\n" % (f, ln, name, text.replace("\n", "\\n")))
    print("\n→ 已写入 _i18n_coverage.txt")
    return 1 if miss else 0


if __name__ == "__main__":
    sys.exit(main())
