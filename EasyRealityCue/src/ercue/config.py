# -*- coding: utf-8 -*-
"""应用配置（JSON）—— 最近文件 / 上次目录 / 内嵌素材偏好。

落点（架构方案 §6 M5「补齐项」）：
    `%LOCALAPPDATA%\\EasyRealityCue\\config.json`

★ 三件事必须做到（照参考项目 AutoVJ `src/config.py` 的做法，但用**独立目录**，
  本项目对 `%LOCALAPPDATA%\\AutoVJ` 没有任何依赖）：

  1. **原子写**：先写 `.tmp` 再 `os.replace` —— 写一半被强杀也不会留下半截 JSON。
  2. **坏文件不致命**：解析失败 → 退回 `.bak`；再不行 → 用默认值（软件照常能打开），
     并把坏文件改名 `.broken` 留档。
  3. **目录不可写时优雅降级**：任何 IO 异常都被吞掉（记一条日志），
     `Config` 退化为"内存态"（`writable=False`），**绝不把异常抛到 UI**。

本模块**零 Qt**，可离线自检：`python -m ercue.config`
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys

log = logging.getLogger("ercue.config")

APP_DIR_NAME = "EasyRealityCue"
CONFIG_NAME = "config.json"
RECENT_CAP = 10                              # 最近文件最多存几条

# 默认配置（深拷贝给每个实例，绝不共享可变对象）
DEFAULTS = {
    "recent_files": [],     # 打开/导入过的 .ercue 绝对路径（最新在前，≤ RECENT_CAP）
    "last_dir": "",         # 上次打开/导入/导出的目录（对话框默认落点）
    "embed_media": False,   # 导出时"把素材一起打包进 .ercue"的默认勾选（默认关）
    # ★ T02（第三轮）：自动保存开关（**独立目录**，绝不写用户 .ercue；见 `model/session_store`）
    "autosave": True,
    # ★ T02：关窗时的"未导出提醒"（默认开；关掉即完全静默退出）
    "prompt_export_on_close": True,
}


# ==========================================================================
# 路径
# ==========================================================================
def app_base_dir() -> str:
    """配置目录：`%LOCALAPPDATA%\\EasyRealityCue`（拿不到就用用户主目录）。

    ⚠ 本项目**独立目录**，绝不复用 `%LOCALAPPDATA%\\AutoVJ`。
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_DIR_NAME)


def config_path() -> str:
    return os.path.join(app_base_dir(), CONFIG_NAME)


def _deep_defaults() -> dict:
    return json.loads(json.dumps(DEFAULTS))


# ==========================================================================
# 原子写（唯一落盘缝 —— 测试可 monkeypatch 它来模拟"目录不可写"）
# ==========================================================================
def _atomic_write_json(path: str, data: dict) -> None:
    """原子写 JSON：`.tmp` → `os.replace`。**失败就是抛异常**，由调用方吞。"""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    if os.path.exists(path):
        try:
            shutil.copyfile(path, path + ".bak")
        except OSError:
            pass                                  # 备份失败不影响主写入
    os.replace(tmp, path)


# ==========================================================================
# Config
# ==========================================================================
class Config:
    """一个 JSON 配置。**构造绝不抛**（读失败即用默认值）。"""

    def __init__(self, path: str | None = None):
        self.path = path or config_path()
        self.data = _deep_defaults()
        self.error = ""                           # 最近一次 IO 失败原因（"" = 正常）
        self.writable = True                      # 写失败后会置 False（内存态）
        self.load()

    # ---------- 读 ----------
    def load(self) -> bool:
        """依次尝试主文件 → `.bak`；都失败就保持默认。**绝不抛。**"""
        for p in (self.path, self.path + ".bak"):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    stored = json.load(f)
                if not isinstance(stored, dict):
                    raise ValueError("config 顶层不是对象")
                self._merge(stored)
                return True
            except FileNotFoundError:
                continue
            except Exception as e:                 # noqa: BLE001
                log.warning("读取配置失败 %s: %s", p, e)
                if p == self.path:
                    try:
                        os.replace(p, p + ".broken")     # 留档，下次不再读坏文件
                    except OSError:
                        pass
        return False

    def _merge(self, stored: dict) -> None:
        for k in DEFAULTS:
            if k in stored:
                self.data[k] = stored[k]

    # ---------- 写 ----------
    def save(self, force: bool = False) -> bool:
        """落盘。**任何异常都吞掉**：失败置 `writable=False` 并记 `error`，返回 False。"""
        try:
            _atomic_write_json(self.path, self.data)
            self.writable = True
            self.error = ""
            return True
        except Exception as e:                     # noqa: BLE001
            self.writable = False
            self.error = "%s: %s" % (type(e).__name__, e)
            log.warning("写配置失败（已降级为内存态）%s: %s", self.path, e)
            return False

    # ---------- 便捷 ----------
    def get(self, key, default=None):
        return self.data.get(key, DEFAULTS.get(key, default))

    def set(self, key, value, save: bool = True) -> None:
        self.data[key] = value
        if save:
            self.save()

    def recent(self) -> list:
        v = self.data.get("recent_files")
        return list(v) if isinstance(v, list) else []

    def add_recent(self, path) -> list:
        """把一个路径插到最近文件头部（去重、限量、最新在前）并落盘。"""
        p = os.path.abspath(str(path)) if path else ""
        if not p:
            return self.recent()
        lst = [x for x in self.recent() if str(x) and os.path.abspath(str(x)) != p]
        lst.insert(0, p)
        self.data["recent_files"] = lst[:RECENT_CAP]
        self.data["last_dir"] = os.path.dirname(p)
        self.save()
        return self.recent()

    def set_last_dir(self, d) -> None:
        if d:
            self.data["last_dir"] = str(d)
            self.save()


# ==========================================================================
# 模块级单例（UI 层直接用 `config.instance()`）
# ==========================================================================
_INSTANCE: Config | None = None


def instance() -> Config:
    """进程级单例。首次构造即读盘；读失败不抛。"""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = Config()
    return _INSTANCE


# ==========================================================================
# 自检
# ==========================================================================
def selftest() -> bool:
    import tempfile

    # 1) 写 → 读往返
    d = tempfile.mkdtemp(prefix="ercue_cfg_")
    p = os.path.join(d, "sub", "config.json")
    c = Config(p)
    assert c.get("embed_media") is False and c.recent() == []
    c.set("embed_media", True)
    c.add_recent(os.path.join(d, "a.ercue"))
    c.add_recent(os.path.join(d, "b.ercue"))
    c.add_recent(os.path.join(d, "a.ercue"))          # 去重 → a 回到最前
    assert c.recent()[0].endswith("a.ercue") and len(c.recent()) == 2, c.recent()
    c2 = Config(p)
    assert c2.get("embed_media") is True and len(c2.recent()) == 2, c2.data
    assert c2.get("last_dir") == os.path.dirname(os.path.abspath(os.path.join(d, "a.ercue")))

    # 2) 最近文件限量
    for i in range(30):
        c.add_recent(os.path.join(d, "f%02d.ercue" % i))
    assert len(c.recent()) == RECENT_CAP, len(c.recent())

    # 3) 坏文件不致命（退回默认 / .bak）
    bad = os.path.join(d, "bad.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("{ 这不是 json ")
    cb = Config(bad)
    assert cb.get("embed_media") is False, "坏文件应回落默认值"
    assert os.path.isfile(bad + ".broken"), "坏文件应改名留档"

    # 4) 目录不可写 → 优雅降级（不抛）
    cu = Config(os.path.join(d, "u.json"))
    orig = globals()["_atomic_write_json"]

    def _boom(path, data):
        raise PermissionError("目录不可写（模拟）")

    globals()["_atomic_write_json"] = _boom
    try:
        ok = cu.save()
    finally:
        globals()["_atomic_write_json"] = orig
    assert ok is False and cu.writable is False, (ok, cu.writable)
    assert "PermissionError" in cu.error, cu.error

    print("[config] 自检通过 ✓  最近文件上限=%d  坏文件回落默认  不可写优雅降级(不抛)"
          % RECENT_CAP)
    return True


if __name__ == "__main__":
    selftest()
