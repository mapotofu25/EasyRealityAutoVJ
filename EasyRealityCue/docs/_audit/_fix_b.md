# 组 B 修复报告（数据安全 + 空头支票接线）

**范围**：只改 `src/ercue/ui/window.py`、`src/ercue/model/ercue_io.py`（本次未改 ercue_io，
`extract_media()` 原样复用）。自测脚本：`tools/_fix_b_verify.py`（离屏 Qt 实测）。
**结论**：4 条 P1 全部修复；`compileall -q src/` 零错误；`tools/selftest.py` 7/7 通过；
`_fix_b_verify.py` **25/25 通过（含 3 条反向验证）**。

跑法：
```
<项目根>\EasyRealityCue\venv\Scripts\python.exe  tools/_fix_b_verify.py
```

---

## B1（P1）关窗 / 关码表静默丢弃未保存编排 —— 已修

**改动**：`window.py`
- 新增 `_confirm_discard(sess_list, context_label)`：三选 **保存并继续 / 不保存 / 取消**
  （非交互模式退回 `"discard"`，无头测试不阻塞）。
- 新增 `_save_session(sess)`：把某张 session 存到它的 `backing_path`（没有则弹「另存为」），
  复用既有 `ercue_io.save` 与 `_embed_pref` / 内嵌体积保护，**不另写一套保存逻辑**。
- 新增 `_confirm_quit()`：关窗前收集所有 `dirty` 的 session 并统一询问。
- 改 `closeEvent`：`_confirm_quit()` 为 False（取消 / 未全保存）时 **`e.ignore()`**，窗口不关。
- 改 `_close_sheet`：当前 session `dirty` 时先走同一三选；取消 / 未保存成功 ⇒ 直接 return。

```python
# window.py:1735
def closeEvent(self, e):
    if not self._confirm_quit():                   # ★ B1：取消时 e.ignore()，窗口不关
        e.ignore()
        return
    self.shutdown()
    super().closeEvent(e)
```
```python
# window.py:1718
def _confirm_quit(self) -> bool:
    dirty = [s for s in self.sessions.values() if s.dirty]
    if not dirty:
        return True
    choice = self._confirm_discard(dirty, "退出前")
    if choice == "cancel":
        self._log("已取消退出。")
        return False
    if choice == "discard":
        return True
    for s in dirty:                                # 全部保存；任一失败即放弃退出（不静默丢）
        if not self._save_session(s):
            self._log("有码表未能保存，已取消退出。")
            return False
    return True
```
```python
# window.py:485 _close_sheet（节选）
if s.dirty:
    choice = self._confirm_discard([s], "当前码表")
    if choice == "cancel":
        self._log("已取消关闭。")
        return
    if choice == "save" and not self._save_session(s):
        self._log("码表未能保存，已取消关闭。")
        return
```

**判据结果**（`_fix_b_verify.py`）：

| 用例 | 结果 |
|---|---|
| 脏数据 + 取消 ⇒ 窗口不关（`e.ignore()`） | PASS |
| 取消后 session 仍在且仍标脏 | PASS |
| 脏数据 + 不保存 ⇒ 正常关窗 | PASS |
| 脏数据 + 保存 ⇒ 文件写盘、`dirty=False`、正常关 | PASS |
| 无脏数据 ⇒ 不弹窗、直接关 | PASS |
| 关闭单张码表 + 取消 ⇒ 码表仍在 | PASS |
| **REVERSE**：把 `closeEvent` 换回旧实现（只 `shutdown`）⇒ "脏数据+取消必须不关窗"断言**失败** | PASS |

**多张脏码表时的交互选择与理由**：采用 **统一询问一次**（"保存并继续 / 不保存 / 取消"），
点「保存并继续」时**逐张**保存（缺 `backing_path` 的逐张弹「另存为」；任一张未保存成功即**取消退出**）。
理由：① 退出时逐个弹窗极易被连点误关，反而更容易丢数据；② 一处选择让用户明确知道"共丢/存几张"；
③ "任一失败即中止退出"保证**绝不出现"部分保存后静默丢弃剩余"**。

---

## B2（P1）内嵌素材导入后读不到 —— 已修

**改动**：`window.py` 新增 `_extract_embedded_media(path)`，并在 `import_sheet` 接线：
导入前先判断包内是否有顶层 `media/`，有则 `extract_media(path, path + ".media")`，再
`load(path, media_dir=embed_dir)`；**非内嵌包仍按旧行为**（`load` 传 `None`），不改动原素材解析路径。
解压**幂等**（目标目录非空则跳过）；解压失败 **`log.exception` + `_log` 明确提示**，不静默继续。

```python
# window.py:550
@staticmethod
def _extract_embedded_media(path):
    import zipfile
    if not path or not os.path.isfile(path) or not zipfile.is_zipfile(path):
        return None
    dest = str(path) + ".media"
    try:
        with zipfile.ZipFile(path) as z:
            has_media = any(n.startswith(ercue_io.MEDIA_DIR + "/") and not n.endswith("/")
                            for n in z.namelist())
    except zipfile.BadZipFile:
        return None
    if not has_media:
        return None
    if os.path.isdir(dest) and os.listdir(dest):
        return dest                            # 已解过 → 幂等跳过
    ercue_io.extract_media(path, dest)         # 失败会抛，交给调用方报错
    return dest
```
```python
# window.py:566 import_sheet（节选）
embed_dir = None
try:
    embed_dir = self._extract_embedded_media(path)
except Exception as e:                             # noqa: BLE001
    log.exception("解压内嵌素材失败: %s", path)
    self._log("⚠ 解压内嵌素材失败：%s: %s（素材将显示为丢失，可用「指定素材目录…」重挂）"
              % (type(e).__name__, e))
try:
    sheet, info = ercue_io.load(path, media_dir=(media_dir or embed_dir))
...
resolved_dir = media_dir or embed_dir or (os.path.dirname(path) or "")
sess = SheetSession(key, sheet, resolved_dir)
```

**判据结果**（造 `embed=True` 的包 → 只把包拷到干净目录、删掉原素材 = 模拟换机）：

| 用例 | 结果 |
|---|---|
| 前置：包内含嵌入素材 / 原素材已删 | PASS |
| 导入 ⇒ 解出 `<包>.media` 目录 | PASS |
| 导入 ⇒ `media_paths` 映射到真实文件 | PASS |
| 导入 ⇒ **无缺失素材** | PASS |
| 重复导入（幂等）⇒ 依然无缺失 | PASS |
| 回归：非内嵌包导入仍能靠 `sess.dir` 扫描重挂，且**不产生多余 `.media` 目录** | PASS（smoke） |
| **REVERSE**：把 `_extract_embedded_media` 换回"永远返回 None"⇒ "导入后素材齐全"断言**失败** | PASS |

---

## B3（P1）撤销 / 重做把当前曲目强制切回第 1 首 —— 已修

**改动**：`window.py`
- `SheetSession` 增 `self.ti`；`snapshot(ti=None)` 除 `sheet` 外**一并快照**
  `ti` / `track_audio` / `media_paths`；`load_snapshot` **兼容旧格式**（裸 `sheet` 快照）。
- `_push_undo` / `undo` / `redo` 传入 `self._ti`。
- `_restore_snapshot` 用快照里的 `ti` 还原并 **clamp 到有效范围**（曲目被删时不会越界）。

```python
# window.py:129
def snapshot(self, ti=None) -> str:
    return json.dumps({
        "sheet": self.sheet.to_state_dict(),
        "ti": int(ti if ti is not None else getattr(self, "ti", 0) or 0),
        "track_audio": {str(k): str(v) for k, v in (self.track_audio or {}).items()},
        "media_paths": {str(k): str(v) for k, v in (self.media_paths or {}).items()},
    }, ensure_ascii=False)
```
```python
# window.py:1536
def _restore_snapshot(self, sess, snap):
    sess.load_snapshot(snap)
    sess.dirty = True
    self.timeline.set_selected("", "")
    self.props_panel.item = None
    self.props_panel.layer = None
    # ★ B3：还原"当前曲目下标"（不再无条件归零），并 clamp 到有效范围（曲目可能被删）
    n = len(sess.sheet.tracks)
    self._ti = max(0, min(int(getattr(sess, "ti", 0) or 0), n - 1)) if n else 0
    self._refresh_all()
```

**判据结果**：

| 用例 | 结果 |
|---|---|
| 切到第 2 首 → 加一层 → 撤销 ⇒ **仍停在第 2 首** | PASS |
| 撤销后确实回滚了那一层 | PASS |
| 重做后仍在第 2 首且层恢复 | PASS |
| 删曲目后撤销 ⇒ `_ti` 在有效范围、不越界 | PASS |
| 回归：第 1 首场景行为不变 | PASS |
| **REVERSE**：把 `_restore_snapshot` 换回无条件 `_ti = 0` ⇒ "撤销后仍停在第 2 首"断言**失败** | PASS |

---

## B4（P1）`probe.probe()` 从未被调用 ⇒ alpha / 时长恒默认 —— 已修

**改动**：`window.py` 在素材**新增**时调用探测（`add_media_paths` → 新增 `_probe_media_ref`），
把 `duration_sec` / `has_alpha` 写进 `MediaRef`；探测失败（`error` 非空 / 抛异常）**容错跳过**，
绝不阻断导入。

```python
# window.py:779
@staticmethod
def _probe_media_ref(ref, path):
    if ref is None or not path or not os.path.isfile(path):
        return
    try:
        from ercue.media.probe import probe
        r = probe(path)
    except Exception:                              # noqa: BLE001
        log.exception("素材探测失败: %s", path)
        return
    if r is None or not getattr(r, "ok", False):
        return
    if float(getattr(r, "duration_sec", 0.0) or 0.0) > 0.0:
        ref.duration_sec = float(r.duration_sec)
    ref.has_alpha = bool(r.has_alpha)
```
```python
# window.py:759 add_media_paths（节选）
new_ref = ref is None
if new_ref:
    cid = "c%d" % _next_num("c", [m.clip_id for m in s.sheet.media])
    ref = MediaRef(clip_id=cid, name=name)
    s.sheet.media.append(ref)
s.media_paths[ref.clip_id] = p
if new_ref:
    self._probe_media_ref(ref, p)          # ★ B4：探测时长 / alpha 写进 MediaRef
```

**判据结果**（用 PyAV 真造一个 3 秒 mp4，放一个 10 秒的片段）：

| 用例 | 结果 |
|---|---|
| 加入素材 ⇒ `probe` 写回 `duration_sec`（≈3s） | PASS |
| 加入素材 ⇒ `has_alpha` 被显式写为 `bool` | PASS |
| 放 10 秒片段 ⇒ `precheck` 报出 **"超出素材本身时长"**（旧实现该分支永久失效） | PASS |

**性能说明（按硬约束"不引入新线程结构"）**：探测是**同步**的，只在**新增素材**时各做一次
（重复加入同名素材不重探）。单素材探测 ≈ 打开容器 + 少量帧；α 素材最深会顺序解 ≤24 帧
（见 `probe.py` 的 `ALPHA_SCAN_FRAMES`），**批量加入几十个 α 素材时会有可感知的一小段停顿**。
本次按要求未引入线程/队列。**建议（后续）**：把探测并入已有的 `ThumbWorker` 后台线程
（`ui/panels.py:78`）逐条回填，或加一个"探测中…"的忙碌指示；届时 `add_media_paths` 只入队不阻塞。

---

## 反向验证汇总（改回旧写法 ⇒ 断言必须失败）

| 编号 | 把什么改回旧写法 | 期望 | 结果 |
|---|---|---|---|
| B1 | `closeEvent` 只 `shutdown()`、不查 `dirty` | 修复断言失败 | PASS（确实失败） |
| B2 | `_extract_embedded_media` 永远返回 `None` | 修复断言失败 | PASS（确实失败） |
| B3 | `_restore_snapshot` 无条件 `self._ti = 0` | 修复断言失败 | PASS（确实失败） |

---

## 附：本次未触碰 / 已知边界
- `ercue_io.py` **未改动**（`extract_media` 原样复用，只是把从没接过的线接上）。
- 旧快照（只含裸 `sheet`）仍能恢复：`load_snapshot` 对无 `"sheet"` 键的 dict 走兼容分支（B3）。
- 关窗时"保存并继续"对内嵌偏好的体积保护：超 500MB 时本次保存**自动退回不内嵌**并记日志
  （与 `export_sheet` 的既有保护一致，避免关窗被超大内嵌卡住）。
- 无关本组的其它 ui 层问题（时间轴点空白不能取消选择、层名点击未接线、视口复位、缩略图跨码表串图、
  spinbox 撤销灌爆等）**仍待修复**，见 `docs/_audit/t2_ui.md`，不在组 B 范围。
