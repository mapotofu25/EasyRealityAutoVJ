# -*- coding: utf-8 -*-
"""第三轮 T01 · 自动保存存储层（B5）：`model/session_store.py`。

判据：原子写（无残留临时文件）/ 读回一致 / `ERCUE_AUTOSAVE_DIR` 隔离 /
      `ERCUE_NO_AUTOSAVE=1` 全部 no-op / **绝不写用户 `.ercue`** / key 消毒 / 坏文件安全。

跑法：<Cue>\\venv\\Scripts\\python.exe tests\\test_session_store.py
"""
from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from ercue.model import session_store as ss                      # noqa: E402

SNAP = json.dumps({"sheet": {"title": "验收码表", "format_version": 1},
                   "ti": 0, "media_paths": {"c1": "D:/a.mp4"}, "track_audio": {}},
                  ensure_ascii=False)


class _Env:
    """临时把它变成测试隔离环境（用后恢复）。"""

    def __init__(self, d, no_autosave=None):
        self.d, self.no_autosave = d, no_autosave
        self._old = (os.environ.get("ERCUE_AUTOSAVE_DIR"),
                     os.environ.get("ERCUE_NO_AUTOSAVE"))

    def __enter__(self):
        os.environ["ERCUE_AUTOSAVE_DIR"] = self.d
        if self.no_autosave is None:
            os.environ.pop("ERCUE_NO_AUTOSAVE", None)
        else:
            os.environ["ERCUE_NO_AUTOSAVE"] = self.no_autosave
        return self

    def __exit__(self, *a):
        old_d, old_n = self._old
        if old_d is None:
            os.environ.pop("ERCUE_AUTOSAVE_DIR", None)
        else:
            os.environ["ERCUE_AUTOSAVE_DIR"] = old_d
        if old_n is None:
            os.environ.pop("ERCUE_NO_AUTOSAVE", None)
        else:
            os.environ["ERCUE_NO_AUTOSAVE"] = old_n
        return False


def _tmp():
    return tempfile.mkdtemp(prefix="ercue_store_t_")


# ---------------------------------------------------------------- 基本读写
def test_atomic_write_and_read():
    d = _tmp()
    with _Env(d):
        assert ss.base_dir() == os.path.abspath(d)
        assert ss.available()
        assert ss.read_workspace() is None and ss.read_session("k") is None
        assert ss.write_workspace({"current": "sheet1", "order": ["sheet1", "sheet2"]})
        assert (ss.read_workspace() or {}).get("current") == "sheet1"
        assert ss.write_session("sheet1", SNAP)
        assert ss.read_session("sheet1") == SNAP
        # 原子写：不留临时文件
        assert not [n for n in os.listdir(d) if n.startswith(".tmp_")], os.listdir(d)
        # 版本号自动补
        assert (ss.read_workspace() or {}).get("version") == ss.WORKSPACE_VERSION
    print("  ✓ 原子写 + 读回一致 + 无残留临时文件 + 版本号自动补")


def test_isolation_env_dir():
    """**隔离**：所有落盘只在 `ERCUE_AUTOSAVE_DIR` 里，且**只在**那里。"""
    d = _tmp()
    with _Env(d):
        ss.write_workspace({"current": "s"})
        ss.write_session("s", SNAP)
        names = sorted(os.listdir(d))
        assert names == ["sheet_s.json", "workspace.json"], names
        # 路径确实都在这个目录下
        assert os.path.dirname(ss.workspace_path()) == os.path.abspath(d)
        assert os.path.dirname(ss.session_path("s")) == os.path.abspath(d)
    print("  ✓ ERCUE_AUTOSAVE_DIR 完全隔离（只在那里落盘）")


def test_no_autosave_is_full_noop():
    """`ERCUE_NO_AUTOSAVE=1` ⇒ **所有** API no-op（读空 / 写 False / 不删）。"""
    d = _tmp()
    with _Env(d):
        ss.write_workspace({"current": "s"})
        ss.write_session("s", SNAP)
    with _Env(d, no_autosave="1"):
        assert ss.disabled() and not ss.available()
        assert ss.read_workspace() is None and ss.read_session("s") is None
        assert ss.write_workspace({"a": 1}) is False
        assert ss.write_session("s", SNAP) is False
        assert ss.remove_session("s") is False
        ss.clear()                                    # 不该删任何东西
    with _Env(d):
        assert ss.read_session("s") == SNAP, "no-op 期间数据不该被动"
    print("  ✓ ERCUE_NO_AUTOSAVE=1 ⇒ 读写删清 全部 no-op（数据完好）")


def test_bad_input_and_bad_file():
    d = _tmp()
    with _Env(d):
        assert ss.write_workspace(["not-a-dict"]) is False
        assert ss.write_session("k", "") is False
        assert ss.write_session("", SNAP) is False
        with open(ss.workspace_path(), "w", encoding="utf-8") as f:
            f.write("{ broken json ")
        assert ss.read_workspace() is None, "坏 JSON ⇒ None（绝不抛）"
    print("  ✓ 非法入参 ⇒ False；坏文件 ⇒ None（不抛）")


def test_key_sanitized():
    """key 消毒：防路径穿越，文件名永远落在 autosave 目录里。"""
    d = _tmp()
    with _Env(d):
        for bad in ("../../evil", "a/b\\c", "..", "", "sheet 1"):
            p = ss.session_path(bad)
            assert os.path.dirname(p) == os.path.abspath(d), (bad, p)
            assert os.sep not in os.path.basename(p)
        assert ss.write_session("../../evil", SNAP)
        assert ss.read_session("../../evil") == SNAP
        assert not [n for n in os.listdir(d) if n not in ("sheet_evil.json",)], os.listdir(d)
    print("  ✓ key 消毒（路径穿越被挡，文件名安全）")


def test_clear_only_own_files():
    d = _tmp()
    with _Env(d):
        keep = os.path.join(d, "keepme.txt")
        with open(keep, "w", encoding="utf-8") as f:
            f.write("x")
        ss.write_workspace({"current": "s"})
        ss.write_session("s", SNAP)
        ss.clear()
        assert ss.read_workspace() is None and ss.read_session("s") is None
        assert os.path.isfile(keep), "clear 不该删无关文件"
    print("  ✓ clear() 只清自己的 workspace/sheet_*/.tmp_，不碰别的文件")


def test_remove_session():
    d = _tmp()
    with _Env(d):
        ss.write_session("s", SNAP)
        assert ss.read_session("s") == SNAP
        assert ss.remove_session("s") is True
        assert ss.read_session("s") is None
        assert ss.remove_session("never") is True, "删不存在的也算成功"
    print("  ✓ remove_session 生效，删不存在的不报错")


def test_snapshot_roundtrip_shape():
    """与真实快照形态（`SheetSession.snapshot()` 产出）做一次往返。"""
    d = _tmp()
    snap = json.loads(SNAP)
    snap.update({"sheet": {"title": "X", "res_w": 1080, "res_h": 1920, "tracks": []}})
    text = json.dumps(snap, ensure_ascii=False)
    with _Env(d):
        assert ss.write_session("sheet9", text)
        back = json.loads(ss.read_session("sheet9"))
    assert back == snap, (back, snap)
    print("  ✓ 快照（秒坐标 + media_paths/track_audio/ti）往返一致")


# ---------------------------------------------------------------- 红线
def test_never_writes_ercue():
    """**红线**：本模块的代码里**没有任何**写用户 `.ercue` 的路径。

    ① AST 检查：不 import `PySide6`，不引用 `ercue_io` / `save_package` / `.ercue` 字面量；
    ② 行为检查：一轮完整写入后，目录里**只有** autosave 自己的文件。
    """
    p = os.path.join(_SRC, "ercue", "model", "session_store.py")
    src = open(p, encoding="utf-8").read()
    tree = ast.parse(src)
    mods, names = set(), set()
    docs = set()
    for node in ast.walk(tree):
        # 收集**文档字符串的原始常量值**（`ast.get_docstring` 会 cleandoc，值对不上）
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docs.add(body[0].value.value)
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)
    assert not (mods & {"PySide6", "PyQt5", "PyQt6", "shiboken6"}), ("零 Qt 铁律", mods)
    assert "ercue_io" not in mods, ("不得依赖 .ercue 的读写模块", mods)
    assert not any(str(_s).startswith("ercue_io") for _s in names), names
    # ⚠ 只扫"代码里的字符串常量"（**排除文档字符串** —— 文档里写明"绝不写 .ercue"是**好事**）
    code_strs = [s for s in names if isinstance(s, str) and s not in docs]
    for s in code_strs:
        for bad in ("ercue_io", "save_package", "write_ercue", "export_ercue", ".ercue"):
            assert bad not in s, ("红线：代码字符串不得出现 %r（%r）" % (bad, s))
    # 行为检查
    d = _tmp()
    with _Env(d):
        ss.write_workspace({"current": "s", "order": ["s"]})
        ss.write_session("s", SNAP)
        got = sorted(os.listdir(d))
    assert got == ["sheet_s.json", "workspace.json"], got
    assert not [n for n in got if n.endswith(".ercue")], got
    print("  ✓ 红线：代码无 .ercue 写路径（AST）+ 落盘无 .ercue 文件（行为）")


def test_default_dir_is_app_autosave():
    """★ 隔离防御：**应用入口**才用 `<app_base>/autosave`；**非入口**（本测试就是）自动落临时目录。"""
    old = os.environ.pop("ERCUE_AUTOSAVE_DIR", None)
    try:
        # 应用真实目录的定义
        appdir = ss._app_dir()
        assert os.path.basename(appdir) == "autosave", appdir
        assert "EasyRealityCue" in appdir, appdir
        # 本进程不是应用入口 ⇒ base_dir() 必须**不是**应用目录，而是进程唯一的临时隔离目录
        if not ss._is_app_entry():
            assert os.path.abspath(ss.base_dir()) != os.path.abspath(appdir), \
                "★ 非应用入口绝不能落到用户真实 autosave 目录"
            assert "nonapp" in ss.base_dir() and str(os.getpid()) in ss.base_dir(), ss.base_dir()
    finally:
        if old is not None:
            os.environ["ERCUE_AUTOSAVE_DIR"] = old
    print("  ✓ 应用入口→<app_base>/autosave；非入口→进程唯一临时目录（不碰用户目录）")


def main() -> int:
    tests = [
        ("原子写与读回", test_atomic_write_and_read),
        ("目录隔离", test_isolation_env_dir),
        ("NO_AUTOSAVE 全 no-op", test_no_autosave_is_full_noop),
        ("非法入参 / 坏文件", test_bad_input_and_bad_file),
        ("key 消毒", test_key_sanitized),
        ("clear 只清自己", test_clear_only_own_files),
        ("remove_session", test_remove_session),
        ("快照往返", test_snapshot_roundtrip_shape),
        ("红线：不写 .ercue", test_never_writes_ercue),
        ("默认目录", test_default_dir_is_app_autosave),
    ]
    ok, passed = True, 0
    for name, fn in tests:
        print("\n--- %s ---" % name)
        try:
            fn()
            passed += 1
        except Exception as e:                              # noqa: BLE001
            ok = False
            print("  ✗ %s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()))
    print("\n" + "=" * 68)
    print("结果：%d 通过 / %d 失败" % (passed, len(tests) - passed))
    print("汇总：%s" % ("全部通过 ✓" if ok else "有失败 ✗"))
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
