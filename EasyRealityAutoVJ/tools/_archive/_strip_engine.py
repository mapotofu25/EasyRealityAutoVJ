# -*- coding: utf-8 -*-
"""摘除码表（第 1 步）：src/engine.py

原则：只删码表分支，通用逻辑（solo / subtract_jobs / crop 之外的一切）保持原样。
每个补丁都要求锚点**恰好命中一次**，否则整批不写。
"""
import io
import os
import sys

ROOT = r"P:\AutoVJ"
P = os.path.join(ROOT, "src", "engine.py")

PAT = []


def rep(old, new):
    PAT.append(("rep", old, new, None))


def cut(start, end):
    """删掉 start..end 之间（含 start，不含 end）。start/end 都必须唯一。"""
    PAT.append(("cut", start, end, None))


# --- 1) Layer.__init__ 的码表属性 -------------------------------------------
rep("""        self._sub_tmp = None     # "减少"混合用的暂存画布（按当前分辨率复用，别每帧新建）
        # ---- 码表图层（**容器**：按绑定的码表在内部渲染多层）----
        #   见 _码表功能设计.md §7 与 src/cuesheet_runtime.py
        self.is_cuesheet = False
        self.cuesheet_id = ""      # 绑定的码表 id（存 cfg["cuesheets"]）
        self.cs_states = []        # [LayerPlayState]，由 _cuesheet_tick 每帧填
        self.cs_active = False     # 本帧是否接管（未激活 ⇒ 退回普通图层行为）
        self._cue_sub_tmps = {}    # 子层 subtract 用的暂存图（按 lyr_id 缓存）
""",
    """        self._sub_tmp = None     # "减少"混合用的暂存画布（按当前分辨率复用，别每帧新建）
""")

# --- 2) to_dict：不再写码表字段 ---------------------------------------------
rep("""                "role": self.role, "auto_mode": self.auto_mode, "is_kv": self.is_kv,
                "is_cuesheet": self.is_cuesheet, "cuesheet_id": self.cuesheet_id,
                "auto_clips": [c.path for c in self.auto_clips],
""",
    """                "role": self.role, "auto_mode": self.auto_mode, "is_kv": self.is_kv,
                "auto_clips": [c.path for c in self.auto_clips],
""")

# --- 3) _CueSubLayer 类 + CUE_FP_* 常量整块删 -------------------------------
cut("class _CueSubLayer:", "class AutoVJEngine(QObject):")

# --- 4) 引擎初始化的码表实例属性 -------------------------------------------
rep("""        # ---- 码表（时间码 / 码表图层）----
        # ⚠ 位置快照由 UI 侧的位置源回调写入 —— **只存一份引用、不做任何重活**
        #   （位置源的 beat 消息可能 10~100Hz，绝不能把高频回调接进 Qt 信号）
        self._deck_state = None
        self._cue_sheet = None
        self._cue_track = None
        self._cue_rt = None
        self._cue_media = {}          # clip_id -> 磁盘路径
        self._cue_pos_beats = None
        self._cue_pos_src = ""       # "" / "deck" / "fingerprint"（位置是谁给的）
        self._cue_sid = -1           # 上一次见到的识别曲目 id（认歌切曲目用）
        self._cue_switch = None      # 最近一次「按认歌切曲目」的信息（界面显示用）
        self._cue_level = ""         # 上次匹配命中的级别（fingerprint/meta/title+artist/file）
        self._cue_jumped = False
""", "")

# --- 5) 恢复配置时不再读码表字段 -------------------------------------------
rep("""            # ⚠ 新增字段必须在恢复路径里也读一次，否则重启后码表图层退化成普通图层
            lay.is_cuesheet = bool(ld.get("is_cuesheet", False))
            lay.cuesheet_id = str(ld.get("cuesheet_id", "") or "")
""", "")

# --- 6) 主循环不再调 _cuesheet_tick ----------------------------------------
rep("""        # 合成
        self._cuesheet_tick(snap)      # 码表：先算"每层该显示什么"，再合成
        self._compose(snap)
""",
    """        # 合成
        self._compose(snap)
""")

# --- 7) _get_player 里的容器分支 -------------------------------------------
cut("""        # ★★ 码表图层（容器层）：**自己一份播放器缓存**。""",
    """        # 缩略图缺失时 alpha 是**后台**探测的：探测回来发现确实带 alpha → 丢掉当时按""")

# --- 8) _apply_color：不再因码表接管而把颜色归零 ---------------------------
rep("""        # ★ 2026-09-28 用户要求：「码表图层和 Kv 图层一样，不受画面效果的影响」。
        #   码表里放的是 DJ 做好的成片，再叠一层调色就不是原片了 ⇒ 接管期间硬归零，
        #   复用 Kv 已有的 kv_on=1 通路（ColorRenderer 会把强度直接归零）。
        if self._cuesheet_on() > 0.0:
            kv_on = max(kv_on, 1.0)
""", "")

# --- 9) _apply_postfx：删掉码表 bypass 块 ----------------------------------
rep("""        # ★ 码表图层接管：整条后处理链同样跳过（理由见 _cuesheet_on）
        if self._cuesheet_on() > 0.0:
            try:
                from postprocess import fx_trails
                fx_trails(None, 0.0, reset=True)
            except Exception:
                pass
            return
""", "")

# --- 10) 整个码表方法区块（set_cue_sheet .. _draw_cuesheet_layer）----------
cut("""    # ==================================================================
    # 码表（时间码 / 码表图层）—— 见 _码表功能设计.md §7""",
    """    def _compose(self, snap):""")

# --- 11) _compose 里的容器拦截分支 -----------------------------------------
rep("""            # ★ 码表图层是**容器**：它没有单一 player，由内部子层各自取帧 ⇒
            #   必须在 `not lay.player` 之前拦下，否则会被当成"空图层"跳过。
            if getattr(lay, "is_cuesheet", False):
                # ★ 2026-09-28：**Solo 也要对码表图层生效**。旧写法在 solo 判断**之前**就
                #   continue 了，于是"别的图层按了 S"时码表图层依然盖在最上面 —— 与其它图层
                #   的行为不一致（图层头上那个 S 按钮是给用户点的，必须真的管用）。
                if solo_on and not getattr(lay, "solo", False):
                    continue
                self._draw_cuesheet_layer(p, lay, W, H, snap, subtract_jobs)
                continue
""", "")


def main():
    s = io.open(P, encoding="utf-8").read()
    orig = s
    for i, (kind, a, b, _) in enumerate(PAT, 1):
        if kind == "rep":
            n = s.count(a)
            if n != 1:
                print("✗ 补丁 %d（rep）锚点命中 %d 次，整批不写" % (i, n))
                print("   锚点首行：", a.splitlines()[0][:90])
                return 1
            s = s.replace(a, b)
        else:
            if s.count(a) != 1 or s.count(b) != 1:
                print("✗ 补丁 %d（cut）边界不唯一：start=%d end=%d"
                      % (i, s.count(a), s.count(b)))
                print("   start：", a.splitlines()[-1][:90])
                return 1
            ia = s.index(a)
            ib = s.index(b)
            if ib <= ia:
                print("✗ 补丁 %d（cut）end 在 start 之前" % i)
                return 1
            s = s[:ia] + s[ib:]
    io.open(P, "w", encoding="utf-8", newline="\n").write(s)
    print("engine.py：%d → %d 行（删了 %d 行）"
          % (orig.count("\n") + 1, s.count("\n") + 1,
             (orig.count("\n") - s.count("\n"))))

    # 复查残留
    import re
    left = [(i + 1, l) for i, l in enumerate(s.splitlines())
            if re.search(r"cue|cuesheet|码表|_CueSubLayer|cs_states|cs_active", l, re.I)]
    print("残留命中 %d 处：" % len(left))
    for ln, l in left:
        print("   %5d| %s" % (ln, l.strip()[:110]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
