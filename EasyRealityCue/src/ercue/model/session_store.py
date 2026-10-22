# -*- coding: utf-8 -*-
"""★ 自动保存的**存储层**（B5）—— 零 Qt，纯 IO。

**红线（架构师裁定）**：本模块**绝不写用户的 `.ercue`**。autosave 落到**独立目录**，
与用户主动"导出/另存为"完全分离 —— 用户没点导出，就绝不在他的文件上留痕。

**位置**：`<app_base>/autosave/`；环境变量 **`ERCUE_AUTOSAVE_DIR`** 可覆盖（**测试隔离用**）。
**形态**：
  · `workspace.json`      —— 索引（有哪些码表、当前是哪张、顺序）
  · `sheet_<key>.json`    —— 每张码表的快照（**复用 `SheetSession.snapshot()` 的 JSON 形态**：
                             秒坐标、含 `media_paths`/`track_audio`/`ti`、**不含素材二进制**）
**原子写**：先写同目录临时文件 → `flush+fsync` → `os.replace`（同盘替换是原子的）⇒
  半路崩溃也只会留下一个完整的旧文件，**不会留下写坏的文件**。

**测试隔离（P0，两层保险）**：
  ① `ERCUE_NO_AUTOSAVE=1` ⇒ 本模块**所有** API 变 no-op（读返回空 / 写返回 False）；
  ② **非应用入口**（`sys.argv[0]` 不是 `ercue/main.py` / 打包 exe）⇒ `base_dir()` **自动落临时目录**
     （`<temp>/ercue_autosave_nonapp_<pid>`，**按进程唯一**）⇒ 诊断脚本/测试**永远碰不到用户真实目录**
     （见 `_is_app_entry` 的说明：这是"一处生效"的根因修复）。
  显式设置 `ERCUE_AUTOSAVE_DIR` 仍然**最优先**（验收脚本要"同进程跨窗口恢复"就靠它）。

自检：`python -m ercue.model.session_store`
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile

log = logging.getLogger("ercue.model.session_store")

WORKSPACE_PATH_NAME = "workspace.json"
SESSION_FILE_FMT = "sheet_%s.json"
WORKSPACE_VERSION = 1

#: **应用入口白名单**（`sys.argv[0]` 的基名，小写）。只有从这些入口启动的进程才算"真·应用"，
#: 才会使用**用户真实的** autosave 目录；其余（测试 / 诊断脚本 / `-c`）一律自动落临时目录。
APP_ENTRY_NAMES = ("main.py", "easyrealitycue.exe", "easyrealitycue")

_SAFE_KEY = re.compile(r"[^A-Za-z0-9_.\-]")


# ==========================================================================
# 开关 / 目录
# ==========================================================================
def disabled() -> bool:
    """是否被环境变量禁用（`ERCUE_NO_AUTOSAVE=1`）。"""
    v = str(os.environ.get("ERCUE_NO_AUTOSAVE", "") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _is_app_entry() -> bool:
    """当前进程是否由**应用入口**启动（`ercue/main.py` 或打包后的 `EasyRealityCue.exe`）。

    ★ 为什么需要（根因修复）：`MainWindow()` 现在"启动即恢复 + 改动即落盘"，而 autosave 目录是
      **整个用户级共享**的。于是**任何诊断/测试脚本**只要 new 一个窗口，就会去读写**用户真实的**
      autosave 目录 —— 既污染用户下次启动会恢复的内容，又让并发跑的多个诊断**互相看到对方的码表**
      （实测：清空真实目录后立刻被别的诊断进程重新写入）。
      ⇒ 判定"不是应用入口"就自动落**临时目录**，一处生效，不必逐个脚本去加 env。
    """
    try:
        argv = list(getattr(sys, "argv", None) or [])
    except Exception:                                     # noqa: BLE001
        return False
    a0 = os.path.basename(str(argv[0] if argv else "")).lower()
    if not a0 or a0 in ("-c", "-m", "-"):
        return False
    return a0 in APP_ENTRY_NAMES


def _app_dir() -> str:
    """**应用真实**的 autosave 目录（`<app_base>/autosave`）。"""
    try:
        from ..config import app_base_dir                     # 零 Qt，安全
        return os.path.join(app_base_dir(), "autosave")
    except Exception:                                         # noqa: BLE001
        return os.path.join(tempfile.gettempdir(), "ercue_autosave")


def nonapp_dir() -> str:
    """**非应用入口**（测试 / 诊断脚本）用的隔离目录：`<temp>/ercue_autosave_nonapp_<pid>`。

    ★ **按进程唯一**：这样每个诊断进程都从"空目录"启动（确定性、互不干扰）；
      而同一进程内多次 new 窗口（例如验收脚本）仍共享同一目录（才能验证"恢复"）。
    """
    return os.path.join(tempfile.gettempdir(), "ercue_autosave_nonapp_%d" % os.getpid())


def base_dir() -> str:
    """自动保存目录。优先级：`ERCUE_AUTOSAVE_DIR` → **非应用入口 ⇒ 临时目录** → `<app_base>/autosave`。

    只算路径，**不创建**（创建的副作用留给 `available()/write_*`）。
    """
    env = str(os.environ.get("ERCUE_AUTOSAVE_DIR", "") or "").strip()
    if env:
        return os.path.abspath(env)
    if not _is_app_entry():                                   # ★ 非应用入口 ⇒ 绝不碰用户目录
        return nonapp_dir()
    return _app_dir()


def _safe_key(key) -> str:
    """把码表 key 变成安全文件名（防路径穿越；非法字符 → `_`；首尾的 `.`/`_` 去掉）。

    ⚠ 只影响**文件名**，不影响逻辑：空 key 由 `write_session` 直接拒绝（见那里）。
    """
    k = _SAFE_KEY.sub("_", str(key or "").strip()).strip("._")
    return (k or "sheet")[:80]


def workspace_path() -> str:
    return os.path.join(base_dir(), WORKSPACE_PATH_NAME)


def session_path(key) -> str:
    return os.path.join(base_dir(), SESSION_FILE_FMT % _safe_key(key))


def available() -> bool:
    """目录可写且未被禁用。**绝不抛**。"""
    if disabled():
        return False
    d = base_dir()
    try:
        os.makedirs(d, exist_ok=True)
        return os.access(d, os.W_OK)
    except OSError:
        return False


# ==========================================================================
# 原子写 / 读
# ==========================================================================
def _atomic_write_text(path: str, text: str) -> bool:
    """原子写文本：同目录临时文件 → fsync → `os.replace`。失败返回 False（**绝不抛**）。"""
    tmp = ""
    try:
        d = os.path.dirname(path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=d)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())                          # 尽量落盘（崩溃窗口最小）
            except OSError:
                pass
        os.replace(tmp, path)                                 # ★ 原子替换
        return True
    except OSError:
        log.exception("自动保存写盘失败：%s", path)
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False
    except Exception:                                         # noqa: BLE001
        log.exception("自动保存写盘异常：%s", path)
        return False


def _read_text(path: str):
    """读文本；不存在/读失败返回 None（**绝不抛**）。"""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None
    except Exception:                                         # noqa: BLE001
        log.exception("自动保存读盘异常：%s", path)
        return None


# ==========================================================================
# workspace 索引
# ==========================================================================
def read_workspace():
    """读索引（dict）；缺失 / 坏 / 被禁用 ⇒ `None`。**绝不抛**。"""
    if disabled():
        return None
    raw = _read_text(workspace_path())
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        log.warning("autosave 索引损坏，忽略：%s", workspace_path())
        return None
    return d if isinstance(d, dict) else None


def write_workspace(index) -> bool:
    """写索引。**被禁用 / 非法入参 ⇒ False**。"""
    if disabled():
        return False
    if not isinstance(index, dict):
        return False
    d = dict(index)
    d.setdefault("version", WORKSPACE_VERSION)
    try:
        text = json.dumps(d, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        log.exception("autosave 索引无法序列化")
        return False
    return _atomic_write_text(workspace_path(), text)


# ==========================================================================
# 每码表快照
# ==========================================================================
def write_session(key, snapshot_json) -> bool:
    """写一张码表的快照（**字符串**，即 `SheetSession.snapshot()` 的产出）。

    ⚠ **空 key / 空快照一律拒绝**（返回 False）—— 绝不写"无名"文件。
    """
    if disabled():
        return False
    if not str(key or "").strip():
        return False
    k = _safe_key(key)
    if not k:
        return False
    text = snapshot_json if isinstance(snapshot_json, str) else str(snapshot_json or "")
    if not text:
        return False
    return _atomic_write_text(session_path(k), text)


def read_session(key):
    """读一张码表的快照**字符串**；缺失 / 被禁用 ⇒ `None`。**绝不抛**。"""
    if disabled():
        return None
    return _read_text(session_path(key))


def remove_session(key) -> bool:
    """删掉一张码表的快照（不存在也算成功）。"""
    if disabled():
        return False
    try:
        os.remove(session_path(key))
        return True
    except OSError:
        return True
    except Exception:                                         # noqa: BLE001
        log.exception("autosave 删除失败：%s", key)
        return False


def clear() -> None:
    """清空自动保存目录里的 workspace + 所有 `sheet_*.json`（**绝不抛**）。"""
    if disabled():
        return
    d = base_dir()
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        if n == WORKSPACE_PATH_NAME or (n.startswith("sheet_") and n.endswith(".json")) \
                or n.startswith(".tmp_"):
            try:
                os.remove(os.path.join(d, n))
            except OSError:
                pass


# ==========================================================================
# 别名（lead 建议名 → 上面的主名；两套都可用，避免上下游命名分歧）
# ==========================================================================
autosave_dir = base_dir
save_workspace = write_workspace
load_workspace = read_workspace
save_sheet = write_session
load_sheet = read_session
remove_sheet = remove_session


# ==========================================================================
# 自检
# ==========================================================================
def selftest() -> bool:
    """零依赖自检：原子写 / 读回 / key 消毒 / `ERCUE_NO_AUTOSAVE=1` 全 no-op /
    坏文件不抛 / `clear()` 只清自己的文件。**跑在临时目录里，绝不动真实 autosave。**"""
    d = tempfile.mkdtemp(prefix="ercue_sess_")
    old_dir, old_no = os.environ.get("ERCUE_AUTOSAVE_DIR"), os.environ.get("ERCUE_NO_AUTOSAVE")
    os.environ["ERCUE_AUTOSAVE_DIR"] = d
    os.environ.pop("ERCUE_NO_AUTOSAVE", None)
    try:
        assert base_dir() == os.path.abspath(d), base_dir()
        assert available(), "临时目录应可写"
        assert read_workspace() is None, "空目录应返回 None"
        assert read_session("k") is None

        # 原子写 + 读回 + 无残留临时文件
        assert write_workspace({"version": 1, "current": "sheet1", "order": ["sheet1"]})
        assert (read_workspace() or {}).get("current") == "sheet1"
        snap = json.dumps({"sheet": {"title": "T"}, "ti": 0}, ensure_ascii=False)
        assert write_session("sheet1", snap)
        assert read_session("sheet1") == snap
        leftovers = [n for n in os.listdir(d) if n.startswith(".tmp_")]
        assert not leftovers, ("不应残留临时文件", leftovers)

        # key 消毒（防路径穿越）
        assert "/" not in os.path.basename(session_path("../../evil")), session_path("../../evil")
        assert os.path.dirname(session_path("../../evil")) == os.path.abspath(d)

        # ★ 隔离防御（P0）：**非应用入口**时，即便没设 env 也**绝不**用用户真实目录
        saved_env = os.environ.pop("ERCUE_AUTOSAVE_DIR", None)
        try:
            if not _is_app_entry():
                assert "nonapp" in base_dir(), ("非入口应落隔离目录", base_dir())
                assert os.path.abspath(base_dir()) != os.path.abspath(_app_dir()), \
                    "★ 非应用入口绝不能碰用户真实 autosave 目录"
                assert os.path.abspath(base_dir()) == os.path.abspath(nonapp_dir())
        finally:
            os.environ["ERCUE_AUTOSAVE_DIR"] = d
        assert _app_dir().endswith("autosave"), _app_dir()

        # 坏文件不抛
        with open(workspace_path(), "w", encoding="utf-8") as f:
            f.write("{ not json ")
        assert read_workspace() is None, "坏 JSON 应返回 None"

        # 非 dict 索引 / 空快照 ⇒ False
        assert write_workspace(["nope"]) is False
        assert write_session("k", "") is False

        # ERCUE_NO_AUTOSAVE=1 ⇒ 全 no-op
        os.environ["ERCUE_NO_AUTOSAVE"] = "1"
        assert disabled() and not available()
        assert read_workspace() is None and read_session("sheet1") is None
        assert write_workspace({"a": 1}) is False
        assert write_session("sheet1", snap) is False
        assert remove_session("sheet1") is False
        clear()                                            # 也不该删东西
        os.environ.pop("ERCUE_NO_AUTOSAVE", None)
        assert read_session("sheet1") == snap, "no-op 期间不该被删"

        # clear 只清自己的
        keep = os.path.join(d, "keepme.txt")
        with open(keep, "w", encoding="utf-8") as f:
            f.write("x")
        clear()
        assert read_workspace() is None and read_session("sheet1") is None
        assert os.path.isfile(keep), "clear 不该删无关文件"
    finally:
        if old_dir is None:
            os.environ.pop("ERCUE_AUTOSAVE_DIR", None)
        else:
            os.environ["ERCUE_AUTOSAVE_DIR"] = old_dir
        if old_no is None:
            os.environ.pop("ERCUE_NO_AUTOSAVE", None)
        else:
            os.environ["ERCUE_NO_AUTOSAVE"] = old_no
    print("[session_store] 自检通过 ✓  原子写/读回/key消毒/坏文件安全/NO_AUTOSAVE 全 no-op/"
          "非应用入口自动隔离/clear 只清自己")
    return True


if __name__ == "__main__":
    selftest()
