# -*- coding: utf-8 -*-
"""多屏幕输出配置的数据模型（纯数据，不依赖 Qt）。

设计目标：
  · 老配置零破坏：第一次打开时自动生成「配置1 / 主屏幕」，现有分辨率/Spout/NDI 全部映射过去；
  · 屏幕类型：main（主屏幕）/ independent（独立）/ copy（复制主屏）/ split（切分源屏）；
  · 复制屏幕不保存图层：内容、分辨率、性能、颜色全部跟随主屏幕；
  · 切分屏幕不保存图层：内容跟随源屏幕，只保存 UV 裁切；
  · 布局/UV/mapping 只在 single 模式使用；多路模式只关心每屏自己的分辨率和输出开关。

本模块被 `config.py`、`engine.py`、`ui_main.py` 共同使用，**不要**在这里 import 任何
Qt 或项目内其它模块（避免循环导入）。
"""

import time
import uuid

# 主屏幕、独立屏幕的默认边框颜色（复制/切分继承源颜色）
SCREEN_PALETTE = [
    "#7B2FF7",   # 紫
    "#8B1A1A",   # 深红
    "#1E3CFF",   # 蓝
    "#0E8A5F",   # 绿
    "#B36B00",   # 橙
    "#6A1B9A",   # 深紫
    "#00796B",   # 青绿
    "#455A64",   # 蓝灰
]

SCREEN_TYPES = ("main", "independent", "copy", "split")
TYPE_LABELS = {
    "main": "主屏幕",
    "independent": "独立屏幕",
    "copy": "复制屏幕",
    "split": "切分屏幕",
}


def new_id(prefix="scr"):
    return "%s_%s" % (prefix, uuid.uuid4().hex[:10])


def _as_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return int(default)


def _as_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def sanitize_sender_name(name, fallback="EasyRealityAutoVJ", max_len=64):
    """NDI/Spout 发送端名：去控制字符/文件系统非法字符，保留中文和空格。"""
    s = str(name or "").strip()
    bad = '\\/:*?"<>|\r\n\t'
    s = "".join((" " if c in bad else c) for c in s)
    s = " ".join(s.split()).strip(" .")
    if not s:
        s = str(fallback or "EasyRealityAutoVJ")
    return s[:max_len]


def unique_sender_name(name, used, fallback="EasyRealityAutoVJ"):
    """同一配置内保持发送端名唯一（OBS/NDI 源列表不允许重名）。"""
    base = sanitize_sender_name(name, fallback)
    used = set(used or ())
    if base not in used:
        return base
    for i in range(2, 1000):
        cand = ("%s-%d" % (base, i))[:64]
        if cand not in used:
            return cand
    return ("%s-%s" % (base, uuid.uuid4().hex[:4]))[:64]


def _normalize_uv(uv):
    try:
        if uv is None or len(uv) != 4:
            raise ValueError
        u0, v0, u1, v1 = (float(uv[0]), float(uv[1]), float(uv[2]), float(uv[3]))
    except (TypeError, ValueError):
        u0, v0, u1, v1 = 0.0, 0.0, 1.0, 1.0
    u0 = _clamp(u0, 0.0, 1.0)
    v0 = _clamp(v0, 0.0, 1.0)
    u1 = _clamp(u1, u0 + 1e-6, 1.0)
    v1 = _clamp(v1, v0 + 1e-6, 1.0)
    return [round(u0, 6), round(v0, 6), round(u1, 6), round(v1, 6)]


def _normalize_layout(layout):
    try:
        x, y = int(layout[0]), int(layout[1])
    except (TypeError, ValueError, IndexError):
        x, y = 0, 0
    return [x, y]


def _normalize_preview_rect(rect):
    try:
        if rect is None:
            return None
        if len(rect) != 4:
            return None
        x, y, w, h = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
        if w <= 0 or h <= 0:
            return None
        return [x, y, w, h]
    except (TypeError, ValueError, IndexError):
        return None


def _new_screen(name, stype, width, height, color, source_id=""):
    return {
        "id": new_id("scr"),
        "name": str(name or TYPE_LABELS.get(stype, "屏幕")),
        "type": stype if stype in SCREEN_TYPES else "independent",
        "source_id": str(source_id or ""),
        "width": max(16, _as_int(width, 1280)),
        "height": max(16, _as_int(height, 720)),
        "color": str(color or SCREEN_PALETTE[0]),
        # None = 跟随主屏幕/源屏幕；独立屏幕是 layer id 列表
        "layers": None if stype in ("main", "copy", "split") else [],
        "layout": [0, 0],
        "uv": [0.0, 0.0, 1.0, 1.0],
        "spout": False,
        "ndi": False,
        "follow_main": True,
        # 多路模式上方预览工作区里的位置/大小（仅 UI，不影响输出）
        "preview_rect": None,
        "preview_fit": "fit",     # fit / fill / 1:1
    }


def new_config(name, width=1280, height=720, spout=False, ndi=False,
               spout_name="EasyRealityAutoVJ", ndi_name="EasyRealityAutoVJ"):
    main = _new_screen("主屏幕", "main", width, height, SCREEN_PALETTE[0])
    main["spout"] = bool(spout)
    main["ndi"] = bool(ndi)
    main["follow_main"] = True
    cid = new_id("cfg")
    return {
        "id": cid,
        "name": str(name or "配置1"),
        "mode": "multi",                 # multi / single
        "combined_size": [max(16, _as_int(width, 1280)), max(16, _as_int(height, 720))],
        "screens": [main],
        "spout_name": sanitize_sender_name(spout_name),
        "ndi_name": sanitize_sender_name(ndi_name),
        "created": time.time(),
    }


def _ensure_screen(s, cfg, index, all_layer_ids, used_colors):
    if not isinstance(s, dict):
        s = {}
    stype = s.get("type")
    if stype not in SCREEN_TYPES:
        stype = "main" if index == 0 else "independent"
    if index == 0 and stype != "main":
        # 第一块必须是主屏幕；老数据/脏数据也纠正
        stype = "main"
    sid = str(s.get("id") or new_id("scr"))
    s["id"] = sid
    s["type"] = stype
    s["name"] = str(s.get("name") or TYPE_LABELS.get(stype, "屏幕"))
    s["source_id"] = str(s.get("source_id") or "")

    w = max(16, _as_int(s.get("width"), 1280))
    h = max(16, _as_int(s.get("height"), 720))
    s["width"], s["height"] = w, h
    if not s.get("color"):
        # 独立屏幕默认互不相同；主屏用第一个色
        for c in SCREEN_PALETTE:
            if c not in used_colors:
                s["color"] = c
                break
        else:
            s["color"] = SCREEN_PALETTE[len(used_colors) % len(SCREEN_PALETTE)]
    s["color"] = str(s.get("color") or SCREEN_PALETTE[0])
    used_colors.add(s["color"])

    if stype in ("main", "copy", "split"):
        s["layers"] = None
    else:
        layers = s.get("layers")
        if layers is None or not isinstance(layers, list):
            # 兼容：老数据没有 layers 字段的独立屏，默认包含全部现有图层
            layers = list(all_layer_ids)
        s["layers"] = [str(x) for x in layers]

    s["layout"] = _normalize_layout(s.get("layout"))
    s["uv"] = _normalize_uv(s.get("uv"))
    s["spout"] = bool(s.get("spout", False))
    s["ndi"] = bool(s.get("ndi", False))
    s["follow_main"] = bool(s.get("follow_main", True))
    s["preview_rect"] = _normalize_preview_rect(s.get("preview_rect"))
    s["preview_fit"] = str(s.get("preview_fit") or "fit")
    if s["preview_fit"] not in ("fit", "fill", "1:1"):
        s["preview_fit"] = "fit"
    return s


def _ensure_config(c, index, all_layer_ids):
    if not isinstance(c, dict):
        c = {}
    c["id"] = str(c.get("id") or new_id("cfg"))
    c["name"] = str(c.get("name") or ("配置%d" % (index + 1)))
    mode = str(c.get("mode") or "multi")
    c["mode"] = mode if mode in ("multi", "single") else "multi"
    screens = c.get("screens")
    if not isinstance(screens, list) or not screens:
        screens = [ _new_screen("主屏幕", "main", 1280, 720, SCREEN_PALETTE[0]) ]
    used = set()
    c["screens"] = [_ensure_screen(s, c, i, all_layer_ids, used)
                    for i, s in enumerate(screens)]
    # 主屏幕必须恰好一块，且排第一
    mains = [s for s in c["screens"] if s["type"] == "main"]
    if not mains:
        c["screens"][0]["type"] = "main"
        c["screens"][0]["layers"] = None
    else:
        if c["screens"][0]["type"] != "main":
            first_main = mains[0]
            c["screens"].remove(first_main)
            c["screens"].insert(0, first_main)
        for s in mains[1:]:
            s["type"] = "independent"
            s["layers"] = []
    main = c["screens"][0]
    # 复制屏幕固定跟随主屏幕；切分屏幕的源不能是复制/切分，必须是主屏或独立屏
    by_id = {s["id"]: s for s in c["screens"]}
    for s in c["screens"]:
        if s["type"] == "copy":
            s["source_id"] = main["id"]
            s["layers"] = None
            s["follow_main"] = True
        elif s["type"] == "split":
            src = by_id.get(s.get("source_id"))
            if src is None or src["type"] not in ("main", "independent"):
                s["source_id"] = main["id"]
            s["layers"] = None
            s["follow_main"] = True
        elif s["type"] == "main":
            s["source_id"] = ""
            s["layers"] = None
            s["follow_main"] = True
    # 单路模式的整合画布/布局
    cs = c.get("combined_size")
    try:
        cw, ch = int(cs[0]), int(cs[1])
        if cw <= 0 or ch <= 0:
            raise ValueError
    except (TypeError, ValueError, IndexError):
        cw, ch = combined_size(c)
    c["combined_size"] = [max(16, cw), max(16, ch)]
    c["spout_name"] = sanitize_sender_name(c.get("spout_name") or "EasyRealityAutoVJ")
    c["ndi_name"] = sanitize_sender_name(c.get("ndi_name") or "EasyRealityAutoVJ")
    return c


def ensure_output_configs(data):
    """就地补全 `data["output"]["configs"]`，返回 active config。

    兼容旧配置：旧版只有 output.width/height/screen/spout_enabled/ndi_enabled 等。
    """
    if not isinstance(data, dict):
        return None
    out = data.setdefault("output", {})
    if not isinstance(out, dict):
        out = data["output"] = {}
    all_layer_ids = []
    for ld in (data.get("layers") or []):
        if isinstance(ld, dict) and ld.get("lid"):
            all_layer_ids.append(str(ld["lid"]))
    configs = out.get("configs")
    if not isinstance(configs, list) or not configs:
        cfg = new_config(
            "配置1",
            _as_int(out.get("width"), 1280),
            _as_int(out.get("height"), 720),
            bool(out.get("spout_enabled", False)),
            bool(out.get("ndi_enabled", False)),
            out.get("spout_name") or "EasyRealityAutoVJ",
            out.get("ndi_name") or "EasyRealityAutoVJ",
        )
        # 老配置里“当前输出”就是主屏幕
        main = cfg["screens"][0]
        main["spout"] = bool(out.get("spout_enabled", False))
        main["ndi"] = bool(out.get("ndi_enabled", False))
        main["width"] = max(16, _as_int(out.get("width"), 1280))
        main["height"] = max(16, _as_int(out.get("height"), 720))
        # 老用户 OBS 里已经在用旧源名：迁移时主屏名沿用旧 spout/ndi 名，
        # 避免一升级源名突然从 EasyRealityAutoVJ 变成「主屏幕」。
        _old_name = str(out.get("spout_name") or out.get("ndi_name") or "").strip()
        if _old_name:
            main["name"] = _old_name
        cfg["combined_size"] = [main["width"], main["height"]]
        configs = [cfg]
    out["configs"] = [_ensure_config(c, i, all_layer_ids)
                      for i, c in enumerate(configs)]
    active = str(out.get("active_config") or "")
    ids = [c["id"] for c in out["configs"]]
    if active not in ids:
        active = ids[0]
    out["active_config"] = active
    out["multi_enabled"] = bool(out.get("multi_enabled", False))
    sync_legacy_output(data)
    return out["configs"][ids.index(active)]


def sync_legacy_output(data):
    """把活动配置/主屏幕镜像回旧字段，保证旧代码路径仍能读。

    只镜像“事实字段”，不镜像 configs 本身。
    """
    cfg = active_config(data)
    if not cfg:
        return
    out = data.setdefault("output", {})
    main = main_screen(cfg)
    if main is None:
        return
    w, h = effective_size(cfg, main)
    out["width"], out["height"] = int(w), int(h)
    out["spout_enabled"] = bool(main.get("spout", False))
    out["ndi_enabled"] = bool(main.get("ndi", False))
    out["spout_name"] = main.get("name") if cfg.get("mode") == "multi" else cfg.get("spout_name")
    out["ndi_name"] = main.get("name") if cfg.get("mode") == "multi" else cfg.get("ndi_name")
    out["spout_name"] = sanitize_sender_name(out.get("spout_name") or "EasyRealityAutoVJ")
    out["ndi_name"] = sanitize_sender_name(out.get("ndi_name") or "EasyRealityAutoVJ")


def active_config(data):
    out = (data or {}).get("output") or {}
    configs = out.get("configs") or []
    if not configs:
        return None
    aid = out.get("active_config")
    for c in configs:
        if c.get("id") == aid:
            return c
    return configs[0]


def config_by_id(data, cid):
    for c in ((data or {}).get("output") or {}).get("configs") or []:
        if c.get("id") == cid:
            return c
    return None


def main_screen(cfg):
    for s in (cfg or {}).get("screens") or []:
        if s.get("type") == "main":
            return s
    return None


def screen_by_id(cfg, sid):
    for s in (cfg or {}).get("screens") or []:
        if s.get("id") == sid:
            return s
    return None


def source_screen(cfg, screen):
    sid = (screen or {}).get("source_id")
    if not sid:
        return main_screen(cfg)
    return screen_by_id(cfg, sid) or main_screen(cfg)


def _crop_size(size, uv):
    w, h = size
    u0, v0, u1, v1 = uv
    return (max(1, int(round(w * (u1 - u0)))),
            max(1, int(round(h * (v1 - v0)))))


def effective_size(cfg, screen):
    """屏幕最终输出像素尺寸（复制/切分跟随源，切分=源裁切后的像素尺寸）。"""
    if not screen:
        return (1280, 720)
    t = screen.get("type")
    if t == "copy":
        return effective_size(cfg, main_screen(cfg))
    if t == "split":
        src = source_screen(cfg, screen)
        sw, sh = effective_size(cfg, src)
        return _crop_size((sw, sh), screen.get("uv") or [0, 0, 1, 1])
    return (max(16, _as_int(screen.get("width"), 1280)),
            max(16, _as_int(screen.get("height"), 720)))


def effective_color(cfg, screen):
    if not screen:
        return SCREEN_PALETTE[0]
    if screen.get("type") in ("copy", "split"):
        src = source_screen(cfg, screen)
        return (src or {}).get("color") or SCREEN_PALETTE[0]
    return screen.get("color") or SCREEN_PALETTE[0]


def effective_layers(cfg, screen, all_layer_ids, _seen=None):
    """返回该屏幕使用的图层 id 列表。

    ★ 2026-11-09（用户：「每一个图层、每一个屏幕的图层自己建」）：
      · 主屏：认**自己的** `layers`（老配置没有/空 ⇒ 全量兼容）
      · 独立屏：认自己的 `layers`
      · **复制屏 / 切分屏：跟随源屏**（递归求值，不是快照）——
        复制屏标签写的就是"跟随主屏"，若按自己的 layers 落回"全部"，
        它会把独立屏里新建的层也渲出来，与语义直接冲突。
    """
    if not screen:
        return list(all_layer_ids)
    ids = set(all_layer_ids)
    t = screen.get("type")
    if t == "main":
        lay = screen.get("layers")
        if lay:
            return [x for x in lay if x in ids]
        return list(all_layer_ids)
    if t in ("copy", "split"):
        # 环保护：源屏指向自己 / A→B→A 这种成环，退回"全部"，绝不无限递归
        _seen = set(_seen or ())
        sid = str(screen.get("id") or "")
        if sid and sid in _seen:
            return list(all_layer_ids)
        if sid:
            _seen.add(sid)
        src = source_screen(cfg, screen)
        if not src or (sid and str(src.get("id") or "") == sid):
            return list(all_layer_ids)
        return effective_layers(cfg, src, all_layer_ids, _seen)
    # 独立屏
    layers = screen.get("layers")
    if layers is None:
        return list(all_layer_ids)
    return [x for x in layers if x in ids]


#: U7：可按屏 override 的参数段（性能/屏幕设置不在此列，仍全局）
#: 可以**按屏各存一份**的配置段（其余段如 perf/output 始终全局一份）
PARAM_SECTIONS = ("auto", "postfx", "color")

_MISSING = object()


def effective_params(data, screen):
    """某块屏实际生效的参数：**全局为底 + 该屏 override**（没有 override 就全继承全局）。

    ⚠ `data` 是**根配置**（`cfg.data`，里面才有 color/postfx/auto 三段），
      不是 output config。


    ★ U7（用户第 7 条）：单路模式下"Kv 主视觉 / 演出行为 / 画面效果"要按屏各存一份。
      数据模型是 override 式：`screen["params"][sec][key]` **只存与全局不同的键**，
      没这个键 ⇒ 继承全局 ⇒ **零迁移**、默认行为与改动前完全一致。
    """
    out = {}
    for sec in PARAM_SECTIONS:
        base = dict((data or {}).get(sec) or {})
        ov = ((screen or {}).get("params") or {}).get(sec) or {}
        if ov:
            # ⚠ `postfx.effects` 是**嵌套** dict（effects[key][field]）：必须逐级深合并，
            #   否则一个二级 override 会把整段 effects 顶掉（其它特效全丢）。
            for k, v in ov.items():
                if k == "effects" and isinstance(v, dict):
                    merged = {kk: dict(vv) if isinstance(vv, dict) else vv
                              for kk, vv in (base.get("effects") or {}).items()}
                    for ek, ev in v.items():
                        if isinstance(ev, dict):
                            tgt = dict(merged.get(ek) or {})
                            tgt.update(ev)
                            merged[ek] = tgt
                        else:
                            merged[ek] = ev
                    base["effects"] = merged
                else:
                    base[k] = v
        out[sec] = base
    return out


def set_screen_effect_param(screen, eff, field, val, data=None):
    """按屏 override 一个**二级**特效字段（`postfx.effects[eff][field]`）。

    「与全局同值 ⇒ 删键」对嵌套结构同样成立：比较的是**那个二级字段**，不是整个 effects。
    """
    if not isinstance(screen, dict) or not eff:
        return False
    g_eff = ((((data or {}).get("postfx") or {}).get("effects") or {}).get(eff) or {})
    glob = g_eff.get(field, _MISSING)
    params = screen.get("params") if isinstance(screen.get("params"), dict) else {}
    post = params.get("postfx") if isinstance(params.get("postfx"), dict) else {}
    effs = post.get("effects") if isinstance(post.get("effects"), dict) else {}
    d = effs.get(eff) if isinstance(effs.get(eff), dict) else {}
    if glob is _MISSING:
        if val is None:
            d.pop(field, None)
        else:
            d[field] = val
    elif glob == val:
        d.pop(field, None)
    else:
        d[field] = val
    # 逐级回写 + 清理空壳
    if d:
        effs[eff] = d
    else:
        effs.pop(eff, None)
    if effs:
        post["effects"] = effs
    else:
        post.pop("effects", None)
    if post:
        params["postfx"] = post
    else:
        params.pop("postfx", None)
    if params:
        screen["params"] = params
    else:
        screen.pop("params", None)
    return bool(screen.get("params"))


def screen_params(data, screen, sec):
    """该屏在某个参数段上的 override 字典（没有就返回空 dict）。"""
    return dict((((screen or {}).get("params") or {}).get(sec) or {}))


def set_screen_param(screen, sec, key, val, data=None):
    """写一个按屏 override；**与全局值相同就删键**（自动瘦身），返回是否还有 override。

    `data` = 根配置（用来判断"这个值是不是就等于全局"）。
    """
    if not isinstance(screen, dict) or sec not in PARAM_SECTIONS:
        return False
    glob = ((data or {}).get(sec) or {}).get(key, _MISSING)
    params = screen.get("params")
    if not isinstance(params, dict):
        params = {}
    d = params.get(sec)
    if not isinstance(d, dict):
        d = {}
    if glob is _MISSING:
        # 全局根本没有这个键：写 None 等价于"恢复成全局那种没有的状态" ⇒ 删 override
        if val is None:
            d.pop(key, None)
        else:
            d[key] = val
    elif glob == val:
        d.pop(key, None)               # 与全局同值 ⇒ 自动瘦身
    else:
        d[key] = val
    if d:
        params[sec] = d
    else:
        params.pop(sec, None)
    if params:
        screen["params"] = params
    else:
        screen.pop("params", None)
    return bool(screen.get("params"))


def clear_screen_params(screen, sec=None):
    """「恢复跟随全局」：清掉该屏（某一段）的 override。"""
    if not isinstance(screen, dict):
        return
    if sec is None:
        screen.pop("params", None)
        return
    params = screen.get("params")
    if isinstance(params, dict):
        params.pop(sec, None)
        if not params:
            screen.pop("params", None)


def migrate_copy_screens(data):
    """把老配置里的 `copy` 屏迁移成 `split`（源=主屏、UV 全幅），返回改动数。

    ★ U4（用户第 4 条）：「复制屏幕」这个类型从新建对话框里去掉了——"切分 UV 全幅"
      与它语义完全一致。老的 `copy` 屏必须继续能出画面，所以这里做**一次性迁移**：
      type=copy → split，source_id 缺就补主屏，uv 设成全幅 [0,0,1,1]。
      迁移后 `effective_layers`/`_route_frame` 对 split 的处理与原 copy 等价
      （split 跟随源屏、裁切区域=全幅 ⇒ 帧与源屏一致）。
    """
    n = 0
    for c in ((data or {}).get("output") or {}).get("configs") or []:
        main = main_screen(c) or {}
        main_id = str(main.get("id") or "")
        for s in (c or {}).get("screens") or []:
            if str(s.get("type")) != "copy":
                continue
            s["type"] = "split"
            if not str(s.get("source_id") or ""):
                s["source_id"] = main_id
            s["uv"] = [0.0, 0.0, 1.0, 1.0]
            s["follow_main"] = True
            s["layers"] = None
            n += 1
    return n


def bind_main_layers(cfg, all_layer_ids):
    """一次性：给主屏写入**显式**的 `layers`（老配置兼容），返回是否改动。

    ★ 背景（用户：「每一个图层、每一个屏幕的图层自己建」）：`effective_layers()` 现在
      主屏也认 `layers`；老配置里主屏没有这个键 ⇒ 若不写，主屏仍然"全量"，
      在独立屏里新建的图层会同时冒出到主屏（正是用户否掉的行为）。
      这里在**第一次打开多屏时**把"当时的全部图层"固化成主屏的归属集合，
      并打 `layers_bound=True`；之后只有显式归属的层才进主屏。
    """
    main = main_screen(cfg)
    if not main or main.get("layers_bound"):
        return False
    if main.get("layers") is None:
        main["layers"] = list(all_layer_ids or [])
    main["layers_bound"] = True
    return True


def unbind_layer(lid, cfg=None, data=None):
    """把某个图层 id 从**所有屏幕**的 `layers` 里摘掉（删除图层时调用）。

    留着悬空 id 会让 `effective_layers()` 过滤出一个不存在的层（行数对不上/空白块）。
    """
    lid = str(lid)
    out = 0
    cfgs = []
    if cfg is not None:
        cfgs.append(cfg)
    if data is not None:
        for c in ((data.get("output") or {}).get("configs") or []):
            cfgs.append(c)
    for c in cfgs:
        for s in (c or {}).get("screens") or []:
            lst = s.get("layers")
            if isinstance(lst, list):
                keep = [x for x in lst if str(x) != lid]
                if len(keep) != len(lst):
                    s["layers"] = keep
                    out += 1
    return out


def layout_sources(cfg):
    """单路模式需要摆到整合画布上的源屏幕：主屏 + 独立屏。"""
    return [s for s in (cfg or {}).get("screens") or []
            if s.get("type") in ("main", "independent")]


def mapping_screens(cfg):
    """Mapping 图里要画的**全部**屏（含切分/复制），按"源屏先、切分/复制后"排序。

    ⚠ 与 `layout_sources()` 的区别：那个是"参与整合画布合成的源屏"（只有
      主屏 + 独立屏），用于算画布尺寸；这里是**画图用**的完整列表 ——
      切分屏/复制屏虽然不参与合成，但用户要在图里看到它们各自的输出区域。
    """
    screens = list((cfg or {}).get("screens") or [])
    order = {"main": 0, "independent": 1, "split": 2, "copy": 3}
    # sorted 是稳定排序：同类型内保持配置顺序
    return sorted(screens, key=lambda s: order.get(s.get("type"), 9))


def combined_size(cfg):
    """整合画布尺寸：优先 config["combined_size"]，否则按布局包围盒。"""
    if not cfg:
        return (1280, 720)
    cs = cfg.get("combined_size")
    try:
        w, h = int(cs[0]), int(cs[1])
        if w > 0 and h > 0:
            return (w, h)
    except (TypeError, ValueError, IndexError):
        pass
    x0 = y0 = 10 ** 9
    x1 = y1 = 0
    for s in layout_sources(cfg):
        w, h = effective_size(cfg, s)
        x, y = _normalize_layout(s.get("layout"))
        x0, y0 = min(x0, x), min(y0, y)
        x1, y1 = max(x1, x + w), max(y1, y + h)
    if x1 <= 0 or y1 <= 0:
        return (1280, 720)
    return (max(16, x1), max(16, y1))


def auto_layout(cfg, direction="h"):
    """把主屏 + 独立屏自动排开（边对边），返回 combined size。"""
    srcs = layout_sources(cfg)
    x = y = 0
    for s in srcs:
        w, h = effective_size(cfg, s)
        s["layout"] = [x, y] if direction == "h" else [0, x]
        if direction == "h":
            x += w
        else:
            y += h
    cw, ch = combined_size(cfg) if cfg.get("combined_size") else (0, 0)
    # 自动排完后以包围盒为默认整合尺寸
    bw = bh = 0
    for s in srcs:
        w, h = effective_size(cfg, s)
        lx, ly = s.get("layout") or [0, 0]
        bw, bh = max(bw, lx + w), max(bh, ly + h)
    cfg["combined_size"] = [max(16, bw or cw or 1280), max(16, bh or ch or 720)]
    return tuple(cfg["combined_size"])


def screen_rect(cfg, screen):
    """单路模式：该屏幕在整合画布上的像素矩形；复制/切分返回其裁切区域。"""
    if not screen:
        return (0, 0, 0, 0)
    t = screen.get("type")
    if t in ("main", "independent"):
        w, h = effective_size(cfg, screen)
        x, y = _normalize_layout(screen.get("layout"))
        return (int(x), int(y), int(w), int(h))
    src = source_screen(cfg, screen)
    if t == "copy":
        return screen_rect(cfg, src)
    sx, sy, sw, sh = screen_rect(cfg, src)
    u0, v0, u1, v1 = screen.get("uv") or [0, 0, 1, 1]
    x = sx + int(round(sw * u0))
    y = sy + int(round(sh * v0))
    w = max(1, int(round(sw * (u1 - u0))))
    h = max(1, int(round(sh * (v1 - v0))))
    return (x, y, w, h)


def screen_uv(cfg, screen):
    """单路模式：该屏幕在整合画布上的归一化 UV。"""
    cw, ch = combined_size(cfg)
    if screen and screen.get("type") == "copy":
        src = source_screen(cfg, screen)
        return screen_uv(cfg, src)
    if screen and screen.get("type") == "split":
        src = source_screen(cfg, screen)
        sx, sy, sw, sh = screen_rect(cfg, src)
        u0, v0, u1, v1 = screen.get("uv") or [0, 0, 1, 1]
        return _normalize_uv([(sx + sw * u0) / cw, (sy + sh * v0) / ch,
                              (sx + sw * u1) / cw, (sy + sh * v1) / ch])
    x, y, w, h = screen_rect(cfg, screen)
    return _normalize_uv([x / cw, y / ch, (x + w) / cw, (y + h) / ch])


def screen_output_names(cfg):
    """给每个 screen 分配唯一发送端名，返回 {screen_id: name}。

    ★ T4 #9（用户第 9 条）：多路下**每屏一个输出，名字 = 这块屏的名字**，
      不要再出现固定的「主屏幕」字样。主屏若还叫默认名（"主屏幕"/空），
      用应用默认名（`EasyRealityAutoVJ`）——用户在「屏幕设置」里改过名才用新名字。
    """
    used = set()
    out = {}
    for s in (cfg or {}).get("screens") or []:
        raw = str(s.get("name") or "").strip()
        if str(s.get("type")) == "main" and raw in ("", "主屏幕", "主屏"):
            raw = "EasyRealityAutoVJ"
        nm = unique_sender_name(raw, used)
        used.add(nm)
        out[s["id"]] = nm
    return out


def ensure_layer_ids(data):
    """旧图层没有 lid：补一个稳定 id。必须在引擎创建 Layer 之前调用。"""
    for i, ld in enumerate((data or {}).get("layers") or []):
        if isinstance(ld, dict) and not ld.get("lid"):
            ld["lid"] = "lay_%s_%d" % (uuid.uuid4().hex[:8], i)
