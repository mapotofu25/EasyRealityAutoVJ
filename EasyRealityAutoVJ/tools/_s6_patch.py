# -*- coding: utf-8 -*-
"""S6 一次性补丁：`_cue_frame` 接 `play_sec` / `src_in` / `src_ended`，**判断全部走 `CueSeekPolicy`**。

设计纪律（团队裁定）：
  · `_cue_frame` 只做"**取数据 → 问 policy → 执行**"三段；
  · **"该不该 seek / 阈值多少 / 有没有落地 / speed 取几" 全部由 `cue_seek_policy` 决定**，
    这里一行判断都不写（否则 S0 的 26 用例 + 5 变异就白做了，两套逻辑迟早分叉）。
跑完即删。
"""
import os

NL = chr(10)
P = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "src", "engine.py")
text = open(P, encoding="utf-8").read()
orig = len(text)
edits = []


def sub(old, new, tag):
    global text
    assert text.count(old) == 1, "锚点命中 %d 次：%s" % (text.count(old), tag)
    text = text.replace(old, new, 1)
    edits.append(tag)


# ---- ① 顶部导入（纯逻辑、零 Qt、零 player 依赖）----
sub(
    "from tags_def import (DYNAMIC_LOW, DYNAMIC_MID, DYNAMIC_HIGH,",
    "from cue_seek_policy import CueSeekPolicy      # 码表 seek 策略（纯逻辑，见 tools/cue_should_seek_test.py）"
    + NL + "from tags_def import (DYNAMIC_LOW, DYNAMIC_MID, DYNAMIC_HIGH,",
    "顶部 import CueSeekPolicy",
)

# ---- ② __init__：建策略实例 ----
sub(
    "        self._cue_closed_last = 0   # 上次 clear 关掉的解码器数（自检/排障用）",
    "        self._cue_closed_last = 0   # 上次 clear 关掉的解码器数（自检/排障用）"
    + NL + "        _cue_cfg = (self.cfg.get(\"cue\") if isinstance(self.cfg, dict) else None) or {}"
    + NL + "        # ★ 所有「该不该 seek / 阈值 / 落地 / 计速」的判断都在这个对象里（见 cue_seek_policy）"
    + NL + "        self._cue_policy = CueSeekPolicy(cfg=_cue_cfg)",
    "__init__ 建 CueSeekPolicy",
)

# ---- ③ clear_cue_sheet：重置策略状态（换歌/卸表不能带着旧 in-flight）----
sub(
    "        self._cue_closed_last = closed" + NL + "        return True",
    "        self._cue_closed_last = closed"
    + NL + "        try:"
    + NL + "            if getattr(self, \"_cue_policy\", None) is not None:"
    + NL + "                self._cue_policy.reset()   # 卸表/换歌：清 in-flight / degraded / target"
    + NL + "        except Exception:                                     # noqa: BLE001"
    + NL + "            pass"
    + NL + "        return True",
    "clear_cue_sheet 重置策略状态",
)

# ---- ④ _cue_frame：三段式（取数据 → 问 policy → 执行）----
old_frame = text[text.index("    def _cue_frame(self, lay, ps):"):
                 text.index("    def _draw_cuesheet_layer(self, p, lay, W, H, snap, subtract_jobs):")]
new_frame = '''    def _cue_frame(self, lay, ps):
        """取该子层当前应显示的帧。

        ★★ 三段式（团队裁定，**别改**）：
          ① **取数据**：item / player / `position()`（实际显示帧的素材内秒）；
          ② **问 policy**：`settle()` 结算上次 seek、`should_seek()` 决定是否要 seek、
             `on_seek()` 记账、`desired_speed()` 取该设的倍速 —— **判断一行都不在这里**；
          ③ **执行**：`player.seek(play_sec)` / `player.set_speed(want)` / 取帧。
        为什么必须这样：`tools/cue_should_seek_test.py` 用 26 个用例 + 5 个源码级变异
        验的就是 `cue_seek_policy` 那套判断。若在这里另写一遍，两套逻辑迟早分叉。
        """
        try:
            item = self._cue_item(lay, ps.clip_path, ps.xform)
            player = self._get_player(lay, item)
        except Exception:                                         # noqa: BLE001
            return None
        if player is None:
            return None
        try:
            player.set_active(True)      # ⚠ 不激活时 current() 恒 None（历史坑：码表层一片黑）
        except Exception:                                         # noqa: BLE001
            pass

        pol = getattr(self, "_cue_policy", None)
        st = None
        if pol is not None:
            try:
                st = pol.state_for(ps)
                pol.settle(ps, player, st)                     # ②-a 结算上次 seek（落地/超时）
                if pol.should_seek(ps, item, player, st):      # ②-b 判定（全部在 policy 里）
                    ok = False
                    try:
                        ok = bool(player.seek(ps.play_sec))    # ③ 只投递，不等待（解码线程执行）
                    except Exception:                          # noqa: BLE001
                        ok = False
                    pol.on_seek(ps, st, ok)                    # ②-c 记账
                    if not ok:
                        self._cue_health("码表 seek 失败，本片段降级为自由播放：%s"
                                         % ps.clip_path)
                want = pol.desired_speed(ps)                   # ②-d 计速取值（恒 ps.speed）
                cur = float(getattr(player, "speed", 1.0) or 1.0)
                if abs(cur - want) > 1e-6:
                    player.set_speed(want)                     # ③ 执行
            except Exception as e:                             # noqa: BLE001
                # policy 自身出问题也不许把渲染线程带走
                self._cue_health("码表 seek 策略异常（已跳过本帧的定位）：%s: %s"
                                 % (type(e).__name__, e))

        if not getattr(player, "_cue_warmed", False):
            try:
                player._cue_warmed = True
                player.warm_up()
            except Exception:                                     # noqa: BLE001
                pass

        cache = getattr(lay, "_cue_last_frame", None)
        if not isinstance(cache, dict):
            cache = {}
            lay._cue_last_frame = cache

        # ★ 到出点：**持最后一帧**（不 seek、不越过、不黑）—— 与 Cue 端 timeline A2 一致
        if getattr(ps, "src_ended", False):
            last = cache.get(ps.lyr_id)
            if last is not None and not last.isNull():
                return last
        try:
            img = player.current()
        except Exception:                                         # noqa: BLE001
            img = None
        if img is None or img.isNull():
            return cache.get(ps.lyr_id)
        cache[ps.lyr_id] = img            # 只存引用，不拷贝（解码线程每帧产出新 QImage）
        return img

'''
text = text.replace(old_frame, new_frame, 1)
edits.append("_cue_frame 三段式（settle→should_seek→seek/set_speed）+ src_ended 持帧")

open(P, "w", encoding="utf-8").write(text)
print("engine.py 已改 %d 处；字符 %d → %d" % (len(edits), orig, len(text)))
for e in edits:
    print("  ·", e)
