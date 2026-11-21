# -*- coding: utf-8 -*-
"""配置与项目保存/加载（JSON）"""
import json
import os
import shutil
import sys
import threading
import time
from contextlib import contextmanager


def exe_dir():
    """程序所在目录：打包 exe=exe 同级；源码=项目根（便携模式素材库默认放这里的子文件夹）"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def app_base_dir():
    """数据目录：源码运行=项目根；打包 exe=%LOCALAPPDATA%/AutoVJ（可写、随用户环境）"""
    if getattr(sys, "frozen", False):
        base = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "AutoVJ")
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.makedirs(base, exist_ok=True)
    return base


CONFIG_FILE = os.path.join(app_base_dir(), "config.json")

# 两个输出的**固定默认名**（用户 2026-10-30：「两个输出名字改成固定的」）。
# ⚠ 「本地 / 局域网」的括号说明加在**复选框标签**上（见 `ui_main.OutputPanel`），
#   不加在名字里 —— 用户明确纠正过：「不是 是加到前面的选项上」。
#   名字保持裸的 `EasyRealityAutoVJ`：NDI 接收端本来就会自动加「机器名 (源名)」前缀，
#   名字里再塞中文括号只会让 OBS 里更难认。
DEFAULT_SPOUT_NAME = "EasyRealityAutoVJ"
DEFAULT_NDI_NAME = "EasyRealityAutoVJ"

DEFAULT_HOTKEYS = {
    # ★★ 2026-11-06（用户：「键里面所有的操作都改成组合键，不然太容易误触了」）：
    #   这几个原来是**裸键**（空格/B/F/P/C），演出时手一碰就触发（黑场、冻结、暂停自动）。
    #   全部改成 Ctrl/Shift 组合；旧的裸键配置由 `_migrate_hotkeys()` 自动升级。
    "toggle_run": {"key": "Ctrl+Space", "global": False, "enabled": True},
    "blackout": {"key": "Ctrl+B", "global": False, "enabled": True},
    "freeze": {"key": "Ctrl+Shift+F", "global": False, "enabled": True},
    "pause_auto": {"key": "Ctrl+P", "global": False, "enabled": True},
    # 「下一素材」默认 = **Ctrl+Right + 全局**（2026-10-18 用户要求）。
    # 演出时主窗口常常没有焦点（VDJ / 播放器在前台），必须走全局 RegisterHotKey；
    # 用 Ctrl+Right 而不是裸 Right —— 全局注册会整个系统吞掉该键，
    # 裸 Right 会让浏览器 / 编辑器 / VDJ 里翻不了页。
    # 需要改回只在本窗口生效，可在「快捷键设置」里把「范围」改成仅窗口。
    "next_scene": {"key": "Ctrl+Right", "global": True, "enabled": True},
    # 注：「上一素材（Left）」已移除——它的实现与「下一素材」完全相同（都是随机切下一个），
    # 属于死键，留着只会让人以为能往回切。旧配置里的 prev_scene 项会被自动忽略。
    "toggle_order_mode": {"key": "Ctrl+R", "global": False, "enabled": True},
    "toggle_beat_mode": {"key": "Ctrl+T", "global": False, "enabled": True},
    "intensity_up": {"key": "Ctrl+Up", "global": False, "enabled": True},
    "intensity_down": {"key": "Ctrl+Down", "global": False, "enabled": True},
    "manual_transition": {"key": "Ctrl+Return", "global": False, "enabled": True},
    "lock_clip": {"key": "Ctrl+L", "global": False, "enabled": True},
    "output_fullscreen": {"key": "Ctrl+F", "global": False, "enabled": True},
    "toggle_ui": {"key": "Ctrl+H", "global": False, "enabled": True},
    "color_bypass": {"key": "Ctrl+Shift+C", "global": False, "enabled": True},
    "panic_reset": {"key": "Ctrl+Shift+R", "global": False, "enabled": True},
}

#: 2026-11-06 快捷键改组合键时的「旧默认 → 新默认」对照。
#  只替换**仍然是旧默认值**的项：用户自己改过的一律不动（否则等于替人做主）。
#  裸键默认：Space / B / F / P / C。
_HOTKEY_V2 = {
    "toggle_run": ("Space", "Ctrl+Space"),
    "blackout": ("B", "Ctrl+B"),
    "freeze": ("F", "Ctrl+Shift+F"),
    "pause_auto": ("P", "Ctrl+P"),
    "color_bypass": ("C", "Ctrl+Shift+C"),
}
#: 迁移版本号（写在 `data["_hotkey_ver"]`；到 2 之后再启动就不再动）
HOTKEY_VER = 2


def _migrate_hotkeys(data):
    """把旧的**裸键**默认值升级成组合键（用户改过的不动；幂等）。"""
    if not isinstance(data, dict):
        return False
    hk = data.get("hotkeys")
    if not isinstance(hk, dict):
        return False
    try:
        if int(data.get("_hotkey_ver", 0) or 0) >= HOTKEY_VER:
            return False
    except (TypeError, ValueError):
        pass
    n = 0
    for act, (old, new) in _HOTKEY_V2.items():
        conf = hk.get(act)
        if not isinstance(conf, dict):
            continue
        if str(conf.get("key") or "").strip() == old:
            conf["key"] = new
            n += 1
    data["_hotkey_ver"] = HOTKEY_VER
    return n > 0


DEFAULTS = {
    "lang": "zh",
    "theme": "dark",              # 亮暗主题：dark / light
    "perf": {
        # 渲染帧率。素材大多是 25~30fps，而引擎默认按 60fps 合成 —— 其中相当一部分
        # 是把同一帧重复合成一遍。在 4 核老机器上这部分是**纯浪费**（要把 CPU 让给 VDJ/直播）。
        #   60 = 最顺（默认）
        #   30 = 省掉约一半渲染 CPU，画面仍跟得上 25/30fps 的素材
        #   20 = 极省，观感会明显变卡，只在机器实在带不动时用
        "render_fps": 60,
        # ★ 优化版：素材 25~30fps 时不要按 60fps 重复合成同一帧。开启后引擎每 2 秒
        #   看一次可见视频源的最高帧率，把有效合成帧率压到源帧率（最低 24fps）。
        #   用户仍可在「渲染帧率」里设更低的上限；关闭此项则完全按 render_fps 跑。
        "fps_follow_source": True,
        # ★ 2026-11-12：停止 VJ / 冻结 / 黑场时的空闲帧率。
        #   这些状态下输出窗口已隐藏、Spout/NDI 也被 `running` 门控，只有主界面
        #   预览还需要更新。默认 15fps：预览仍然实时，但解码/合成/GUI 缩放开销
        #   约为 30fps 时的一半。设 30 可恢复原来的“停止也全速预览”。
        "idle_fps": 15,
        # ★ 2026-11-12：运行中但长时间没有音频（还没放歌/放完歌）时的「静音待机」。
        #   画面停在上一次合成帧，播放器全部挂起，引擎/GUI 按 silent_standby_fps 跑。
        #   音频一回来下一 tick 自动恢复，不影响演出。
        "silent_standby_sec": 8.0,
        "silent_standby_fps": 2.0,
        "silent_standby_level": 0.002,
        # GPU 合成（QPainter on QOpenGLPaintDevice）：像素已验证与 CPU 一致。
        # ⚠ 默认关闭：实测引擎定时器下只能跑 ~24fps（GPU 回读/上下文同步），
        #   而 CPU 路径可稳定 30fps。需要“省 CPU 优先于帧率”的静态图场景可手动开。
        "gpu_compose": False,
        # 视频图层是否也走 GPU 合成。实测 720p 下 Qt GL 的逐帧纹理上传比 CPU
        # QPainter 慢，默认只让静态图/GIF 走 GPU；4K 或特殊素材可自行打开。
        "gpu_compose_video": False,
        # GPU 后处理（预留；当前 CPU 后处理已做专项优化，未启用时为 False）
        "gpu_postfx": False,
        # GPU 解码：把 DXV3 帧内的 DXT 压缩块直接交给显卡硬件解压
        # （BC 纹理），CPU 只做 LZ 解包 + 回读。对 DXT5/大尺寸 DXT1 有明显收益；
        # 失败一律回退软解，不影响播放。2026-11-08 优化版默认开启（自动回退）。
        "gpu_decode": True,
        # ★ 2026-11-09 用户要求：**去掉 `safe_decode` 开关**（原来默认让普通视频走 PyAV
        #   以规避 cv2 的 0xc0000005，代价 3 路约 +1.8 核）。现在普通视频固定走 OpenCV；
        #   老配置里残留的 `safe_decode` 键会被忽略，不需要迁移。
    },
    "audio": {
        "source_type": "system",      # system / mic / linein
        "device_name": "",            # 与 source_type 配对的设备名（仅当前音源类型）
        # 按音源类型分别记住上次用的设备：{"system": "...", "mic": "..."}
        # 必须分开存 —— 设备名是分类型的（扬声器 vs 麦克风），混在一起会出现
        # 「切到麦克风时拿扬声器的名字去比对」→ 误报「设备已不可用」。
        "device_by_source": {},
        "mono": True,
        "sample_rate": 0,              # 0=自动（跟随设备原生采样率，避免重采样；推荐）
        "gain": 1.0,                # 已废弃（2026-09-25 移除增益滑块）：保留只为兼容旧配置
        # ASIO 用哪两路**输入通道**（0 基）。⚠ ASIO 抓不到别的程序播出来的声音
        # （没有 loopback 概念），只能选硬件输入；有些驱动的输入自带 Loopback 通道，
        # 名字会在「音源 → 输入通道」里列出来供选（见 panels.AudioSourceDialog）。
        "asio_ch0": 0,
        "asio_ch1": 1,
    },
    "auto": {
        "intensity": 1,               # -1 关闭 / 0 低 / 1 中 / 2 高 / 3 极高（画面振幅强度，影响脉冲缩放）
        "energy_map": True,
        "energy_scale": 1.0,          # 能量伽马校正（>1 压低人声段、鼓点段基本不动）：现场系统低频增强时用
        "bpm_speed_sync": True,
        # 离线节拍网格（八拍乐句）驱动全局开关（默认开）：
        #   开 = 认出的歌有网格且识别已确认时，拍位/切换对齐改由离线网格推算（八拍乐句）；
        #   关 = 行为与本特性引入前**完全一致**（退回现有实时拍钟 + 4 拍小节对齐）。
        # ⚠ 性能/节奏类开关一律全局，不为任何图层（含 Kv 待机层）做豁免。
        "beat_grid": True,
        "cooldown_sec": 20,
        "seed": 0,                    # 0 = random each run
        "switch_on_beat": True,       # 【已废弃】早期「必须检测到鼓点才切换」，引擎不再读取（拍位有自己的回退时钟）
    },
    "mode": {
        "auto": True,                 # 自动模式：软件自动判断切换节奏
        "manual": "auto",             # auto / normal / fast / beat（slow 已删除，旧值自动回退 auto）
        "freq_beats": 0,              # 【已废弃】「切换频率」覆盖选项已删除，引擎不再读取
        "transition": "",             # 过渡方式覆盖：""=跟随模式，fade/cut/slide/zoom/glitch
        "sensitivity": 1,             # 0 低 / 1 中 / 2 高
        "min_switch_beats": 1,        # 【已废弃】「素材最小切换间隔」选项已删除，间隔固定（常规16拍/快切8拍）
        "min_stay_migrated": True,    # 旧版"素材最短停留"已并入此项
    },
    "kv": {                           # Kv 主视觉图层（待机层）：无声音时切入、有声音时切出
        "db_threshold": -50.0,        # 静音判定阈值（dBFS）：-60 ~ -20
        "silent_delay": 2.0,          # 静音触发延迟（秒，防抖）：持续静音多久才切入
        "resume_delay": 2.5,          # 声音恢复切出延迟（秒，防抖）：持续有声多久才切出
        "in_trans": "fade",           # 切入过渡：fade / cut / glitch
        "in_dur": 1.5,                # 切入过渡时长（秒）：0.1 ~ 5
        "out_trans": "fade",          # 切出过渡：fade / cut / zoom
        "out_dur": 1.5,               # 切出过渡时长（秒）：0.1 ~ 3
        "switch_sec": 0.0,            # 素材切换速度（秒）：Kv 显示期间每隔 N 秒轮换素材，0=不轮换
    },
    "beat": {                         # 逐拍交替（独立开关，仅前景层 · 仅 drop 段）
        "enabled": False,             # 总开关（默认关闭；老「模式=逐拍交替」迁移时自动打开）
        "pick_mode": "seq",           # seq / rand
        "interval_beats": 1,          # A↔B 交替间隔（拍）：1/4 1/2 1 2 4 8 16
        "bars_per_pair": 2,           # 旧版换对周期（仅兼容保留；现在换对跟段落走）
        "end_action": "loop",         # loop / reverse
    },
    "postfx": {                       # 后处理特效链（合成后、输出前）
        "enabled": True,
        "mode": "auto",               # auto=自动挑选效果/强度；manual=手动逐个配置
        "global_level": 60,           # 全局强度：自动模式=整体效果强度；手动=总乘数
        "auto": True,                 # 自动联动总开关：关掉则所有特效退化为常驻纯手动
        # 自动模式下「一组效果保持多少拍」再重新挑选：
        #   0 = 实时跟随能量（旧行为，能量抖动时效果会忽有忽无）
        #   >0 = 选中一组效果后保持 N 拍，到点或曲风变化才换下一组（有节奏感）
        "auto_hold_beats": 0,
        "effects": {
            "chromatic":  {"on": False, "level": 40, "trigger": "constant", "genres": "", "decay": 1.5, "drive": "off"},
            "bloom":      {"on": False, "level": 30, "trigger": "constant", "genres": "", "decay": 1.5, "drive": "off"},
            "glitch":     {"on": False, "level": 50, "trigger": "constant", "genres": "", "decay": 1.5, "drive": "off"},
            # exposure 的 level 原来是 0：手动模式下 `level>0` 是硬性过滤条件
            # ⇒ 勾上「曝光脉冲」永远没有效果（自动模式因为 override 成 100 才正常）。
            # 2026-10-23 改成 30，与其它效果同量级。
            "exposure":   {"on": False, "level": 30, "trigger": "constant", "genres": "", "decay": 1.5, "drive": "off"},
            "saturation": {"on": False, "level": 30, "trigger": "constant", "genres": "", "decay": 1.5, "drive": "off"},
            "softfocus":  {"on": False, "level": 30, "trigger": "constant", "genres": "", "decay": 1.5, "drive": "off"},
            "trails":     {"on": False, "level": 30, "trigger": "constant", "genres": "", "decay": 1.5, "drive": "off"},
            "grain":      {"on": False, "level": 25, "trigger": "constant", "genres": "", "decay": 1.5, "drive": "off"},
            "pixelate":   {"on": False, "level": 35, "trigger": "constant", "genres": "", "decay": 1.5, "drive": "off"},
            "spectrum":   {"on": False, "level": 50, "trigger": "constant", "genres": "", "decay": 1.5, "drive": "off"},
            "deform":     {"on": False, "level": 40, "trigger": "constant", "genres": "", "decay": 1.5, "drive": "off"},
        },
    },
    "color": {                        # 颜色渲染器（一键调色：降低对素材数量的依赖）
        "on": True,                   # 总开关
        "auto": True,                 # 自动模式：跟随能量自动变色（关掉=纯手动点色卡）
        "mode": "lut",                # lut=调色（保留素材色彩）/ duotone=双色调（最像换素材）
        "strength": 60,               # 染色整体浓淡 0~100
        "rotate_sec": 45,             # 高能量染色下每隔多少秒轮换颜色（30~120）
        "burst": True,                # 能量突变时立刻刷新颜色
        # 频闪素材处理：on=遇频闪就休眠 / off=不休眠照常染色 / random=每次换素材随机决定
        "flicker_mode": "on",
        "skip_flicker": True,         # 旧字段（仅用于兼容老配置，新代码读 flicker_mode）
        "bypass": False,              # 一键旁路：原片 / 调色 闪切
        "manual_color": "",           # 手动锁定的色卡名（空=未锁定）
        "manual_t": 0.0,              # 锁定时刻（**墙钟 time.time()**；老的 perf_counter 值会被判为已过期）
        "manual_hold": 10.0,          # 手动锁定保持秒数，之后回到自动
    },
    "ui": {
        "thumb_size": 1,              # 素材库预览大小：0 小 / 1 中 / 2 大
        # 曲库列表每行的歌曲封面边长（px）：右键「封面大小」里改，见 panels.COVER_SIZES
        "music_cover_size": 64,
        "hud": True,                  # 预览区 HUD 浮层
        "next_preview": True,         # 预览区"下一个素材"预看
        # 主界面预览画面（**只影响主界面**，输出窗口 / Spout / NDI 完全不受影响）
        # on=False 时跳过每帧的 fromImage+缩放（约 1.5ms/帧 ≈ 0.09 个核 @60fps）
        "preview": {"on": True, "res": 1.0, "fit": "keep", "fps": 60},
        # ★ 2026-11-08 多屏输出：素材库浮动窗口 + 收藏夹 + 输出预览工作区状态
        "library_float": None,        # [x, y, w, h]；None=默认位置
        "library_float_visible": False,
        "favorites": [],              # [{"id","name","clips":[...]}]
        "active_favorite": "",
        "output_preview": {},         # 每个配置的预览工作区缩放/平移/屏幕框位置
    },
    "output": {
        # 输出到显示器：**-2 = 关闭（不弹出输出窗口，默认）** / -1 = 窗口模式（可拖动）/
        # 0..n = 对应显示器序号（全屏）。
        # ★ S2 #14（用户第 14 条）：「默认打开软件时不要弹输出窗口」⇒ 默认值改成 -2。
        #   老配置里存的 -1 语义不变（窗口模式），不会被这次改动影响。
        "screen": -2,
        "width": 1280,
        "height": 720,
        "aspect_lock": True,
        # 输出窗口优先用 QOpenGLWidget 做 GPU 缩放（4K 屏上 CPU 放大 720p/1080p
        # 单帧可达 13ms/34ms，是输出卡顿的主因）。失败自动回退 CPU。
        "gpu_display": True,
        "borderless": False,
        "always_top": False,
        "geometry": None,             # [x, y, w, h]
        "library_dir": "",            # 素材库目录：空=exe 同级/素材库；否则用此路径
        "import_strategy": "",        # 记住的导入策略：copy/move/link；空=每次询问
        "spout_enabled": False,       # Spout 输出：把画面发给本机 OBS/Resolume 等
        # Spout/NDI 是否发送 Alpha（透明通道）。默认关＝原来的不透明 BGRA；
        # 开启后画布改用 ARGB32（直通 alpha），显示器窗口仍按黑底显示。
        "alpha_enabled": False,
        # ★★ 2026-10-30（用户要求）：两个输出的默认名**固定**，并各自带括号说明。
        #   原来两个都叫 `EasyRealityAutoVJ` —— 在 OBS 的源列表里**完全分不出来**，
        #   用户不知道该选哪个。它们的本质差异就一条：
        #     · Spout = 本机进程间共享内存（同一台电脑才看得到）
        #     · NDI   = 走局域网（别的电脑/手机也能收）
        "spout_name": DEFAULT_SPOUT_NAME,
        "ndi_enabled": False,         # NDI 输出（音画同步；运行时随包自带，用户无需安装）
        "ndi_name": DEFAULT_NDI_NAME,
        "ndi_audio": False,           # NDI 是否随画面一起发声音（默认关：怕双声/回声）
        # ★ 2026-11-08 多屏输出：每套配置独立保存模式/屏幕/布局/UV。
        #   老配置第一次打开时由 output_model.ensure_output_configs() 自动生成配置1。
        "configs": [],
        "active_config": "",
        # ★ 2026-11-08：演出台的「多屏输出」总开关。关闭时完全走原来的
        #   单画布/单路输出逻辑，演出台 UI 与旧版一字不差。
        "multi_enabled": False,
    },
    "hotkeys": DEFAULT_HOTKEYS,
    "library": [],
    "clip_adj": {},                # 每素材独立的大小/位置：path -> {scale, x, y}
    # ★★ 2026-10-30 用户「大砍刀」：`custom_tags` / `genre_visual_map` / `custom_genres`
    #   三个键删除（CLIP 内容词表、曲风映射、自定义曲风全部砍掉）。
    #   `clip_tags` / `clip_roles` / `clip_excluded` / `dyn_tag_ver` **保留**：
    #   素材标签现在只剩「动态/频闪 + 铺满/局部」，仍然存在同一批键里（数据格式不变，
    #   老配置里的旧词条读进来也不会崩，只是不再产出）。
    "clip_tags": {},              # 每素材自动 tag（动态/频闪/铺满度）：path -> [tag, ...]
    "clip_tags_manual": {},      # 预留：手动 tag（2026-10-30 手动标签 UI 已删，字段保留兼容）
    "clip_tags_disabled": {},    # 每素材被取消的自动 tag（重扫不复活）：path -> [tag, ...]
    # ★ 2026-10-30 用户「给素材打分 0-1」：帧差细分（见 `tagger.dynamic_score`）。
    #   与 `clip_tags` 的三档并行存在：档位决定"进哪个池"，分决定"池里挑谁"。
    "clip_score": {},             # 每素材动态分 0~1：path -> float
    "clip_roles": {},             # 每素材角色：path -> {"fg":bool,"bg":bool}
    "clip_excluded": {},          # 排除自动匹配的素材（logo 等固定素材）：path -> True
    "dyn_tag_ver": 0,             # 动态标签版本：动态检测算法升级时 +1，启动时 <当前版本 触发一次重扫
    "music_library": [],          # 音乐曲库：音频文件路径列表（演出前分析用）
    "music_meta": {},             # 每首歌的曲风：path -> {"artist","title","genre","zh":[],"source"}
    "layers": [
        {"name": "图层 1", "visible": True, "opacity": 1.0, "fixed": -1, "clips": [],
         "hide_silent": False, "blend": "normal", "solo": False,
         "speed": 1.0, "pause_silent": False},
    ],
}


def deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


SAVE_COALESCE_SEC = 0.40   # 合并窗口：0.4 秒内的多次 save 只真写一次


class Config:
    def __init__(self):
        self.data = json.loads(json.dumps(DEFAULTS))  # deep copy
        self._batch = 0        # batch() 嵌套层数（>0 时 save() 只标脏）
        self._dirty = False    # 批量期间有未落盘的改动
        # ★ 进程内写锁：串行化多线程 save()（后台扫描线程 + GUI 线程会并发调）。
        #   必须在 load()/`_migrate()` 之前创建 —— `_migrate()` 里就会调 save()。
        self._save_lock = threading.Lock()
        self._timer_lock = threading.Lock()
        self._pending_timer = None
        self.load()
        self._migrate()

    def _migrate(self):
        """一次性迁移：只在缺标记位时改，**不覆盖用户手动改过的值**。"""
        # ★ 2026-11-08 优化版：旧配置里 gpu_decode 默认是 False；迁移一次到 True。
        #   用户之后在界面里手动关掉，会写入 _gpu_decode_auto 标记，不再覆盖。
        try:
            if not self.data.get("_gpu_decode_auto"):
                perf = self.data.setdefault("perf", {})
                if perf.get("gpu_decode") is False:
                    perf["gpu_decode"] = True
                self.data["_gpu_decode_auto"] = True
                self.save()
        except Exception:
            pass
        hk = self.data.setdefault("hotkeys", {})
        # 「下一素材」默认值升级（2026-10-18 用户要求）：Right/仅窗口 → Ctrl+Right/全局。
        # ⚠ **只改仍是旧默认值的配置**（key 恰好是 "Right" 且 global 为 False）：
        #   用户自己改过键位或范围，就一个字都不动（不强行覆盖已保存配置）。
        #   用一次性标记位，之后用户手动改回"仅窗口"也不会被再改回来。
        if not self.data.get("_hk_next_ctrl_right"):
            spec = hk.get("next_scene")
            if isinstance(spec, dict) and spec.get("key") == "Right" and not spec.get("global"):
                spec["key"] = "Ctrl+Right"
                spec["global"] = True
            self.data["_hk_next_ctrl_right"] = True
            self.save()
        # ★ 2026-11-09（S2 #14 的配套，必须做否则"改了没生效"）：
        #   `output.screen` 的默认值改成了 -2（关闭：不弹输出窗口），但那**只对新建配置生效**；
        #   老配置里存的是 -1（窗口模式）⇒ 用户升级后照样弹输出窗口，改动等于看不见。
        #   这里做**一次性**迁移：
        #     · 没有 `output.screen_closed_migrated` 标记 ⇒ screen 置 -2 并记标记、落盘；
        #     · 之后用户自己在界面里选回「窗口模式」，标记已在 ⇒ **绝不再覆盖**。
        try:
            out = self.data.setdefault("output", {})
            if not out.get("screen_closed_migrated"):
                try:
                    cur = int(out.get("screen", -1))
                except (TypeError, ValueError):
                    cur = -1
                # ⚠ 只在"**仍是旧默认值**"时改：-1 = 窗口模式（旧默认，所有人升级后都是它）。
                #   0..n = 用户自己明确选了某台显示器（双屏演出常用）⇒ 一个字都不动，
                #   否则一次升级就把人家的全屏输出吃掉，比不改更糟。
                if cur == -1:
                    out["screen"] = -2
                out["screen_closed_migrated"] = True
                self.save()
        except Exception:                                         # noqa: BLE001
            pass
        # ★ 2026-11-08 多屏输出：图层稳定 id + 输出配置迁移。
        #   只做一次；之后用户增删屏幕/配置不再被覆盖。
        try:
            # ★ U4：`copy` 屏幕类型已从对话框移除 ⇒ 老配置里的 copy 屏一次性迁移成
            #   split（源=主屏、UV 全幅，语义等价），否则它们会变成"没有类型"的砖。
            if not self.data.get("_copy_screen_migrated"):
                try:
                    import output_model as _omc
                    if _omc.migrate_copy_screens(self.data):
                        pass
                except Exception:                                 # noqa: BLE001
                    pass
                self.data["_copy_screen_migrated"] = True
                self.save()
            if not self.data.get("_output_configs_v1"):
                import output_model
                output_model.ensure_layer_ids(self.data)
                output_model.ensure_output_configs(self.data)
                self.data["_output_configs_v1"] = True
                self.save()
        except Exception:
            pass

        # ★ 2026-11-21：逐拍交替从「素材切换节奏」里独立出来。
        #   老配置 mode.manual == "beat" ⇒ 改成 normal 并打开独立开关；
        #   每块屏的 params.mode.manual == "beat" 同样迁移，开关写到该屏 params.beat。
        try:
            _b = self.data.setdefault("beat", {})
            if "enabled" not in _b:
                _b["enabled"] = False
            _md = self.data.setdefault("mode", {})
            if str(_md.get("manual") or "") == "beat":
                _md["manual"] = "normal"
                _b["enabled"] = True
                self.save()
            for _c in ((self.data.get("output") or {}).get("configs") or []):
                for _s in (_c.get("screens") or []):
                    _pm = (_s.get("params") or {}).get("mode") or {}
                    if str(_pm.get("manual") or "") == "beat":
                        _pm["manual"] = "normal"
                        _pb = (_s.setdefault("params", {})).setdefault("beat", {})
                        _pb["enabled"] = True
                        self.save()
        except Exception:
            pass

    # ---------- IO ----------
    def load(self):
        """读配置。**坏文件不能让软件打不开**：解析失败时退回 .bak，再不行就用默认值，
        并把坏文件改名留档（用户导入大量素材时崩溃过一次，若正好写坏配置文件，
        下次启动就会「有进程没窗口」）。"""
        for path in (CONFIG_FILE, CONFIG_FILE + ".bak"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    stored = json.load(f)
                if not isinstance(stored, dict):
                    raise ValueError("config 顶层不是对象")
                self.data = deep_merge(self.data, stored)
                try:
                    _migrate_hotkeys(self.data)      # 裸键 → 组合键（见函数注释）
                except Exception:                    # noqa: BLE001
                    pass
                return
            except FileNotFoundError:
                continue
            except Exception as e:
                print("config load failed:", path, e)
                if path == CONFIG_FILE:
                    try:
                        os.replace(path, path + ".broken")
                    except Exception:
                        pass
        # 全部失败：保持默认值（软件照常能打开）

    def flush(self):
        """把合并窗口里还没落盘的改动立刻写掉（退出/导出/切页前调用）。

        没有脏改动时是**零成本**的（不碰磁盘），可以随手调。
        """
        if getattr(self, "_dirty", False):
            self._dirty = False
            self.save(force=True)

    def flush_if_dirty(self):
        """别名：语义上更明确（供定时器调用）。"""
        self.flush()

    def _schedule_flush(self):
        """合并窗口到期后补写一次：避免“最后一次修改后没有下一次 save”丢盘。"""
        try:
            with self._timer_lock:
                t = self._pending_timer
                if t is not None and t.is_alive():
                    return
                t = threading.Timer(SAVE_COALESCE_SEC + 0.05, self._flush_from_timer)
                t.daemon = True
                self._pending_timer = t
                t.start()
        except Exception:                                      # noqa: BLE001
            pass

    def _flush_from_timer(self):
        try:
            with self._timer_lock:
                self._pending_timer = None
            if getattr(self, "_dirty", False):
                self.save(force=True)
        except Exception:                                      # noqa: BLE001
            pass

    def save(self, force: bool = False):
        """**原子写 + 多线程安全**：先写临时文件再替换，避免写一半被强杀 → 配置文件损坏。

        `batch()` 批量模式内（`_batch > 0`）只标脏不落盘，退出批量时统一写一次 ——
        启动时 UI 初始化会连着触发几十次 save（每次 json.dump + fsync + 备份 170KB
        ≈ 45ms），实测占主窗口构造时间的 43%，而且用户机器上还有杀毒软件在中间插队。

        ⚠ 2026-10-10 审计修复（多线程）：后台扫描线程（TagScan/DynTagScan/MusicScan…）
          与 GUI 线程都会并发调 `save()`。旧实现临时文件名是**固定的** `CONFIG_FILE + ".tmp"`
          ⇒ 两个线程互相截断同一个临时文件，极端情况把半截 JSON 落到 config.json。
          现在：① 进程内 `_save_lock` 串行化；② 临时文件名带 pid + 线程 id 唯一化
          （即使将来去掉锁也不会撞名）；③ 异常时清理自己的临时文件。
        """
        if not force:
            if getattr(self, "_batch", 0) > 0:
                self._dirty = True
                return
            # ★★ 2026-10-19 审计修复：**合并写盘**。
            #   滑条每动一格都调 save()，而单次真写=229KB dump+fsync+备份 ≈ 24~28ms
            #   ⇒ 拖 2 秒卡 1.5~3 秒（现场调参一顿一顿）。
            #   现在 0.4 秒窗口内的多次调用只标脏、不落盘；退出/导出前 flush() 保证不丢。
            _now = time.monotonic()
            if _now - float(getattr(self, "_last_write_t", 0.0)) < SAVE_COALESCE_SEC:
                self._dirty = True
                self._schedule_flush()
                return
        with self._save_lock:
            self._last_write_t = time.monotonic()
            self._write_count = int(getattr(self, "_write_count", 0)) + 1
            tmp = "%s.tmp.%d.%d" % (CONFIG_FILE, os.getpid(), threading.get_ident())
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                if os.path.exists(CONFIG_FILE):
                    try:
                        shutil.copyfile(CONFIG_FILE, CONFIG_FILE + ".bak")
                    except Exception:
                        pass
                os.replace(tmp, CONFIG_FILE)
                self._dirty = False
            except Exception as e:
                print("config save failed:", e)
                try:
                    os.remove(tmp)
                except Exception:
                    pass

    @contextmanager
    def batch(self):
        """批量模式上下文：期间的 `save()` 只标脏，退出时统一落盘一次。

        用法：`with cfg.batch(): ...一堆会各自 save 的初始化...`
        嵌套安全；异常也会在 finally 里补一次保存。
        """
        self._batch = getattr(self, "_batch", 0) + 1
        try:
            yield self
        finally:
            self._batch = max(0, self._batch - 1)
            if self._batch == 0 and getattr(self, "_dirty", False):
                self.save(force=True)

    def export_to(self, path: str) -> bool:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

    def import_from(self, path: str) -> bool:
        try:
            with open(path, "r", encoding="utf-8") as f:
                stored = json.load(f)
            self.data = deep_merge(DEFAULTS, stored)
            self.save()
            return True
        except Exception:
            return False

    def reset(self):
        self.data = json.loads(json.dumps(DEFAULTS))
        self.save()

    # ---------- helpers ----------
    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def set(self, section, key, value):
        self.data.setdefault(section, {})[key] = value
