# -*- coding: utf-8 -*-
"""配置与项目保存/加载（JSON）"""
import json
import os
import shutil
import sys
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

DEFAULT_HOTKEYS = {
    "toggle_run": {"key": "Space", "global": False, "enabled": True},
    "blackout": {"key": "B", "global": False, "enabled": True},
    "freeze": {"key": "F", "global": False, "enabled": True},
    "pause_auto": {"key": "P", "global": False, "enabled": True},
    # 「下一素材」默认 = 仅窗口内（2026-09-24 用户改回）。
    # 曾一度默认全局，但全局 RegisterHotKey 会**整个系统地吞掉 Right 键**
    # （浏览器/编辑器/VDJ 里都翻不了页），代价太大。
    # 需要演出时跨窗口触发，可在「快捷键设置」里把「范围」改成全局。
    "next_scene": {"key": "Right", "global": False, "enabled": True},
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
    "color_bypass": {"key": "C", "global": False, "enabled": True},
    "panic_reset": {"key": "Ctrl+Shift+R", "global": False, "enabled": True},
}

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
        # GPU 解码（实验，默认关）：把 DXV3 帧内的 DXT 压缩块直接交给显卡硬件解压
        # （BC 纹理），CPU 只做 LZ 解包 + 回读。实测 DXT5-1080p 省 58% 解码 CPU、
        # DXT5 超宽省 73%；**DXT1-1080p 反而略亏**（回读成本 > 解压收益）所以只有
        # 大尺寸 DXT1 才启用。失败一律回退软解，不影响播放。
        # ⚠ 只作用于普通图层，不影响 Kv 待机层（铁律）。
        "gpu_decode": False,
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
        "intensity": 1,               # 0 低 / 1 中 / 2 高 / 3 极高（画面振幅强度，影响脉冲缩放）
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
    "beat": {                         # 逐拍交替参数（模式为"逐拍交替"时生效）
        "pick_mode": "seq",           # seq / rand
        "interval_beats": 1,          # A↔B 交替间隔（拍）：1/4 1/2 1 2 4 8 16
        "bars_per_pair": 2,
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
            "exposure":   {"on": False, "level": 0,  "trigger": "constant", "genres": "", "decay": 1.5, "drive": "off"},
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
        "manual_t": 0.0,              # 锁定时刻（perf_counter，跨次启动自动失效）
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
    },
    "output": {
        "screen": -1,                 # 输出到显示器：-1 窗口模式 / 0..n 显示器序号
        "width": 1280,
        "height": 720,
        "aspect_lock": True,
        "borderless": False,
        "always_top": False,
        "geometry": None,             # [x, y, w, h]
        "library_dir": "",            # 素材库目录：空=exe 同级/素材库；否则用此路径
        "import_strategy": "",        # 记住的导入策略：copy/move/link；空=每次询问
        "spout_enabled": False,       # Spout 输出：把画面发给本机 OBS/Resolume 等
        "spout_name": "EasyRealityAutoVJ",
        "ndi_enabled": False,         # NDI 输出（音画同步；运行时随包自带，用户无需安装）
        "ndi_name": "EasyRealityAutoVJ",
        "ndi_audio": False,           # NDI 是否随画面一起发声音（默认关：怕双声/回声）
    },
    "hotkeys": DEFAULT_HOTKEYS,
    "library": [],
    "clip_adj": {},                # 每素材独立的大小/位置：path -> {scale, x, y}
    "clip_tags": {},              # 每素材自动 tag（视觉扫描）：path -> [tag, ...]
    "clip_tags_manual": {},      # 每素材手动 tag（永不覆盖）：path -> [tag, ...]
    "clip_tags_disabled": {},    # 每素材被取消的自动 tag（重扫不复活）：path -> [tag, ...]
    "custom_tags": {},           # 用户新建的标签词条：分类 -> [tag, ...]
    "clip_roles": {},             # 每素材多重角色：path -> {"fg":bool,"mg":bool,"bg":bool}                    # media paths
    "clip_excluded": {},          # 排除自动打标/匹配的素材（logo 等固定素材）：path -> True
    "dyn_tag_ver": 0,             # 动态标签版本：动态检测算法升级时 +1，启动时 dyn_tag_ver<当前版本 触发一次快速重扫
    "genre_visual_map": {},       # 用户自定义的曲风→画面标签映射：曲风(英文) -> [画面标签中文]
    "custom_genres": {},          # 用户新建的曲风：大类名 -> [曲风名]（曲风映射里「+ 新建曲风」写入；纠正曲风也显示）
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


class Config:
    def __init__(self):
        self.data = json.loads(json.dumps(DEFAULTS))  # deep copy
        self._batch = 0        # batch() 嵌套层数（>0 时 save() 只标脏）
        self._dirty = False    # 批量期间有未落盘的改动
        self.load()
        self._migrate()

    def _migrate(self):
        """一次性迁移：只在缺标记位时改，**不覆盖用户手动改过的值**。"""
        hk = self.data.setdefault("hotkeys", {})
        # 「下一素材」默认范围 局部 → 全局（2026-09-24 用户要求）。
        # 用标记位而不是每次启动都写 True：用户如果手动改回"仅窗口"，不该被改回来。
        if not self.data.get("_hk_next_global_migrated"):
            spec = hk.get("next_scene")
            if isinstance(spec, dict):
                spec["global"] = True
            self.data["_hk_next_global_migrated"] = True
            self.save()
        # 回退：「下一素材」范围 全局 → 仅窗口（2026-09-24 用户改回；全局会吞掉 Right 键）。
        # 同样用一次性标记：之后他在「快捷键设置」里把范围改成全局，不会再被改回来。
        if not self.data.get("_hk_next_global_reverted"):
            spec = hk.get("next_scene")
            if isinstance(spec, dict):
                spec["global"] = False
            self.data["_hk_next_global_reverted"] = True
            self.save()

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

    def save(self, force: bool = False):
        """**原子写**：先写临时文件再替换，避免写一半被强杀 → 配置文件损坏。

        `batch()` 批量模式内（`_batch > 0`）只标脏不落盘，退出批量时统一写一次 ——
        启动时 UI 初始化会连着触发几十次 save（每次 json.dump + fsync + 备份 170KB
        ≈ 45ms），实测占主窗口构造时间的 43%，而且用户机器上还有杀毒软件在中间插队。
        """
        if not force and getattr(self, "_batch", 0) > 0:
            self._dirty = True
            return
        try:
            tmp = CONFIG_FILE + ".tmp"
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
