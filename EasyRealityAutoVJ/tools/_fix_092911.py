# -*- coding: utf-8 -*-
"""092911 修复补丁：三个现场问题

① 演出台「正在使用的素材」底色不显示 —— theme 的基态 `QListWidget::item` 规则
   让 item.setBackground() 全部失效（tools/_hl_qss_test.py 已实测）。
② CPU 50%+ / 磁盘 300MB/s —— 播放器建出来那一刻 GL 工作线程没就绪就**永久软解**
   （`_soft_frames` 一次性选择、永不重探）；另外 gpu_status() 会把瞬时失败写进
   全局 `_GPU_DISABLED`，等于给整个进程判了 GPU 死刑。
③ 码表编排加不进素材 —— `_append_cue` 里 `it.at_sec = at_sec` 用了未定义变量
   （NameError 半路炸掉，cue 根本没进层）；外加素材列表 UserRole 存的是路径而不是
   clip_id、秒/拍单位混用。

用法：venv/Scripts/python.exe -u tools/_fix_092911.py
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATCHES = []


def patch(rel, old, new):
    PATCHES.append((rel, old, new))


# ==========================================================================
# ① theme.py：去掉基态 QListWidget::item 规则
# ==========================================================================
patch("src/theme.py",
      "QListWidget::item {{ padding: 3px 4px; border-radius: 4px; }}\n",
      """/* 这里**不能**写基态 `QListWidget::item` 规则：只要存在任何 ::item 规则，
   Qt 就改用样式表画列表项背景，`item.setBackground()` 会被整个忽略
   ⇒ 演出台「正在使用的素材」金黄打底、码表徽标底色统统消失。
   实测见 tools/_hl_qss_test.py（带这条规则 #2e2e2e，删掉就恢复金黄）。
   代价只是列表项少了 3px/4px 内边距；:hover / :selected 仍然保留。 */
""")

# ==========================================================================
# ② media_manager.py
# ==========================================================================
# ②-a 别再让全局 _GPU_DISABLED 一票否决（它可能只是上一次的瞬时失败）
patch("src/media_manager.py",
      """            if _GPU_DISABLED:
                self.reason = _GPU_DISABLED
                return False
""",
      """            # ⚠ 不再因为全局 `_GPU_DISABLED` 就一票否决：它可能只是**上一次**的
            #   瞬时失败（GL 工作线程起得慢 / 正在冷却）。真正的硬失败（dxvnative
            #   缺失、依赖导入失败）下面各有独立判据，照样会退回软解。
""")

# ②-b 工作线程暂不可用 ⇒ 标记 gpu_want，交给 _gpu_frames 的自愈路径
patch("src/media_manager.py",
      """                if "已达上限" in err:
                    self.reason = err
                else:
                    _GPU_DISABLED = "GL 工作线程不可用：%s" % err
                    self.reason = _GPU_DISABLED
                return False
""",
      """                # ⚠⚠ 这里**绝不能就这么 return False 完事**（2026-09-29 现场
                #   CPU 50%+ / 磁盘 300MB/s 的根因）：软解路径 `_soft_frames` 是
                #   **建播放器那一刻定一次、永不重探**的 —— 只要此刻 GL 工作线程
                #   还没就绪（或正在冷却期），这个播放器就**一辈子软解**，
                #   每个 DXV-1080p 烧 0.6~0.95 核 + 一路磁盘读。
                #   正确做法：标记 `gpu_want` 后照常进 `_gpu_frames()` —— 它开头就
                #   `yield from _soft_until_recover()`，**每 3 秒重探**，工作线程一
                #   恢复就自动切回 GPU（这条自愈路径本来就有，只是没人走进去）。
                #   「配额达上限」同理：那是这一个素材的事，不是全局判死刑。
                self.gpu_want = True
                self.reason = ("GL 工作线程暂不可用（%s）—— 先软解，"
                               "恢复后自动切回 GPU" % err)
                return False
""")

# ②-c 新增 gpu_want 字段
patch("src/media_manager.py",
      """        self.gpu = False           # 诊断：是否走 GPU
        self.reason = "未探测"      # 诊断：走 / 不走 GPU 的原因
""",
      """        self.gpu = False           # 诊断：**当前**是否真的走 GPU
        self.gpu_want = False      # ★ 素材允许走 GPU（即使此刻工作线程没就绪）
                                   #   ⇒ 用 `_gpu_frames()` 进自愈路径，别用 `_soft_frames`
        self.reason = "未探测"      # 诊断：走 / 不走 GPU 的原因
""")

# ②-d _loop：按 gpu_want 选生成器，并允许中途跟着开关换
patch("src/media_manager.py",
      """        interval = 1.0 / max(self.fps, 1.0)
        frames = self._gpu_frames() if self.gpu else self._soft_frames()
        while not self._dead:
            if not self._active:
""",
      """        interval = 1.0 / max(self.fps, 1.0)
        gen_gpu = (self.gpu or self.gpu_want) and gpu_decode_enabled()
        frames = self._gpu_frames() if gen_gpu else self._soft_frames()
        while not self._dead:
            # ★ 只有"该不该走 GPU"这一件事允许中途换生成器：设置里开关 GPU 解码
            #   立刻生效（以前只在建播放器那一刻定一次 ⇒ 改完开关得重开软件才行）。
            want = (self.gpu or self.gpu_want) and gpu_decode_enabled()
            if want != gen_gpu:
                gen_gpu = want
                frames = self._gpu_frames() if want else self._soft_frames()
            if not self._active:
""")

# ②-e gpu_status() 不许再写全局 `_GPU_DISABLED`（那等于给进程判 GPU 死刑）
patch("src/media_manager.py",
      """        worker = glctx.GLWorker.get()
        if not worker.ensure():
            _GPU_DISABLED = "GL 工作线程不可用：%s" % (worker.last_error or "未知")
            return _fail(_GPU_DISABLED)
    except Exception as e:                                            # noqa: BLE001
        _GPU_DISABLED = "GL 初始化异常：%s" % str(e)[:80]
        return _fail(_GPU_DISABLED)
""",
      """        worker = glctx.GLWorker.get()
        if not worker.ensure():
            # ⚠ **不要**把瞬时状态写进全局 `_GPU_DISABLED`：那会自动堵住后面所有
            #   播放器的 GPU 路径（用户就是在设置里开关一次 GPU 之后再也回不到 GPU）。
            return _fail("GL 工作线程暂不可用：%s" % (worker.last_error or "未知"))
    except Exception as e:                                            # noqa: BLE001
        # 同上：初始化异常也只作显示，不做全局封杀（下次探测可能就好了）
        return _fail("GL 初始化异常：%s" % str(e)[:80])
""")

# ==========================================================================
# ③ cue_editor.py
# ==========================================================================
# ③-a _append_cue：未定义变量 at_sec（NameError 真凶）
patch("src/cue_editor.py",
      """        it.at_beats = at
        it.at_sec = at_sec          # ★ 冗余字段一起维护（审查 M11，导出要用）
""",
      """        it.at_beats = at
        # ★ 冗余字段一起维护（审查 M11，导出要用）。
        # ⚠⚠ 这里原来写的是**未定义**的 `at_sec` ⇒ NameError ⇒ `_append_cue` 半路
        #   炸掉、cue 根本没 append 进层 —— 用户 2026-09-29 报「码表编排无法添加
        #   素材到时间轴」就是它（拖放 / 双击 / ＋ 三条路都走这个函数）。
        it.at_sec = float(at) * max(0.02, float(getattr(self, "_bl", 0.5) or 0.5))
""")

# ③-b 素材列表 UserRole 存 clip_id（路径挪到 UserRole+1）
patch("src/cue_editor.py",
      """                it.setData(Qt.UserRole, p)
                if not ok:
                    it.setForeground(QColor("#e06666"))
""",
      """                # ⚠ UserRole **必须是 clip_id**：拖到时间轴、双击加 cue、右键删除
                #   都按 clip_id 认素材。以前存的是文件路径 ⇒ 把路径当 clip_id 用，
                #   加 cue 必然失败（「无法添加素材到时间轴」的另一半原因）。
                it.setData(Qt.UserRole, m.clip_id)
                it.setData(Qt.UserRole + 1, p)      # 路径单独一个 role
                if not ok:
                    it.setForeground(QColor("#e06666"))
""")

# ③-c _reveal_media 改读路径 role
patch("src/cue_editor.py",
      """        p = item.data(Qt.UserRole) if item is not None else ""
        if not p or not os.path.isfile(p):
            self._log(T("这个素材没找到文件，用「指定素材目录…」或重新「＋ 素材…」。"))
""",
      """        p = (item.data(Qt.UserRole + 1) or "") if item is not None else ""
        if not p or not os.path.isfile(p):
            self._log(T("这个素材没找到文件，用「指定素材目录…」或重新「＋ 素材…」。"))
""")

# ③-d _add_media_to_layer：clip_id 兜底 + 秒→拍
patch("src/cue_editor.py",
      """        if not cid:
            cid = self.cb_clip.currentData() or ""
        if not cid:
            self._log(T("先在素材列表里选一个素材（「3 素材」里「＋ 素材…」加进来）。"))
            return
""",
      """        # 兜底：万一拿到的不是本码表里的 clip_id（旧数据 / 拿成了路径），退回下拉框
        _ids = {m.clip_id for m in (self._cur.media or [])}
        if cid not in _ids:
            cid = self.cb_clip.currentData() or ""
        if not cid:
            self._log(T("先在素材列表里选一个素材（「3 素材」里「＋ 素材…」加进来）。"))
            return
""")
patch("src/cue_editor.py",
      """        sec = float(self._pos if at_beats is None else at_beats)
        self._append_cue(ly, cid, sec)
""",
      """        # ⚠ 单位：`self._pos` 是**秒**，而 `_append_cue` 第三个参数是**拍**
        #   （CueItem 的主坐标就是拍）⇒ 必须除以「秒/拍」。以前直接传秒，
        #   落点整体偏 1/beat_len 倍（120BPM 下偏 2 倍）。
        bl = max(0.02, float(getattr(self, "_bl", 0.5) or 0.5))
        self._append_cue(ly, cid, float(self._pos if at_beats is None else at_beats) / bl)
""")

# ③-e _add_item 同样是秒→拍
patch("src/cue_editor.py",
      """        at = float(self.sld.value()) if self.sld else 0.0
        n = len(ly.items) + 1
""",
      """        # ⚠ 滑杆是**秒**，cue 坐标是**拍** ⇒ 换算（同 _add_media_to_layer）
        _bl = max(0.02, float(getattr(self, "_bl", 0.5) or 0.5))
        at = (float(self.sld.value()) if self.sld else 0.0) / _bl
        n = len(ly.items) + 1
""")

# ③-f _clip_from_event：修掉 staticmethod 里的 `self.` 递归调用
patch("src/cue_editor.py",
      """        try:
            if self._clip_from_event(e):
                cid = bytes(e.mimeData().data(CLIP_MIME)).decode("utf-8", "replace")
                if cid:
                    return cid
        except Exception:                                          # noqa: BLE001
            pass
""",
      """        try:
            # ⚠ 原写法 `if self._clip_from_event(e):` 是在 **staticmethod** 里用
            #   `self` ⇒ NameError 被下面的 except 吞掉 ⇒ mime 分支等于不存在，
            #   只剩 `e.source()` 兜底（source 一旦为 None 整条拖放就废）。
            _md = e.mimeData() if e is not None else None
            if _md is not None and _md.hasFormat(CLIP_MIME):
                cid = bytes(_md.data(CLIP_MIME)).decode("utf-8", "replace")
                if cid:
                    return cid
        except Exception:                                          # noqa: BLE001
            pass
""")


def main():
    changed = 0
    for rel, old, new in PATCHES:
        path = os.path.join(ROOT, rel)
        s = io.open(path, encoding="utf-8").read()
        n = s.count(old)
        if n != 1:
            print("✗ %s：锚点命中 %d 次（应为 1），本次**不写**" % (rel, n))
            print("   锚点首行：", old.splitlines()[0][:100])
            return 1
        s = s.replace(old, new)
        io.open(path, "w", encoding="utf-8", newline="\n").write(s)
        changed += 1
        print("✓ %s" % rel)
    print("\n共 %d 处补丁全部落地" % changed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
