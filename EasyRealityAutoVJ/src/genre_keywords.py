# -*- coding: utf-8 -*-
"""曲风细分关键词匹配（源自 genre-police-visualizer 的 genre-classifier，MIT 许可）。

原理：电子音乐等曲风有大量细分（frenchcore/rawstyle/happy-hardcore…），
这些细分不是 AI 能听出来的，而是靠「曲名/艺人/专辑/标签文本里出现的关键词」判定。
本模块把它的「细分曲风 → 关键词正则」表搬来（英文原词），供 music_meta 细化大类曲风。

is_major=True 表示"大类兜底"（electronic/house/trance/techno…），这类太宽泛，
命中时不作为细分信号（交给 Discogs 大类），只有细分命中才采纳。
"""
import re

# (中文名, 正则, 是否大类兜底)
GENRE_KEYWORDS = [
    # ---- 硬核 / 硬派系 ----
    ("Hard Dance", r"\bhard dance\b", False),
    ("Puzzycore", r"\b(puzzycore|puzzy core)\b", False),
    ("Uptempo Hardcore", r"\b(uptempo(?: hardcore)?|terrorcore|terror core|speedcore)\b", False),
    ("UK Hardcore", r"\b(uk hardcore|ukcore|freeform hardcore)\b", False),
    ("Happy Hardcore", r"\b(happy hardcore)\b", False),
    ("Industrial Hardcore", r"\b(industrial hardcore|darkcore|doomcore)\b", False),
    ("Frenchcore", r"\b(frenchcore)\b", False),
    ("Gabber", r"\b(gabber|mainstream hardcore|early hardcore)\b", False),
    ("Hardcore", r"\b(hardcore techno|hardcore edm|j[\s-]?core|kawaii hardcore|hardcore)\b", False),
    ("Rawstyle", r"\b(rawstyle|raw hardstyle|xtra raw|rawphoric)\b", False),
    ("Euphoric Hardstyle", r"\b(euphoric hardstyle|euphoric)\b", False),
    ("Hardstyle", r"\b(hardstyle|reverse bass)\b", False),

    # ---- 回响 / 贝斯系 ----
    ("Future Riddim", r"\b(future riddim)\b", False),
    ("Colour Bass", r"\b(colour bass|color bass)\b", False),
    ("Deathstep", r"\b(deathstep|minatory)\b", False),
    ("Melodic Dubstep", r"\b(melodic dubstep|chillstep|lovestep)\b", False),
    ("Riddim", r"\b(riddim|briddim)\b", False),
    ("Brostep", r"\b(brostep)\b", False),
    ("Bass Music", r"\b(bass music)\b", False),
    ("Dubstep", r"\b(dubstep|tearout)\b", False),
    ("Moombahcore", r"\b(moombahcore|moombah core)\b", False),
    ("Kawaii Bass", r"\b(kawaii(?: future)? bass|cute future bass)\b", False),
    ("Future Bass", r"\b(future bass|wave music)\b", False),
    ("Hard Trap", r"\b(hard trap)\b", False),
    ("Hybrid Trap", r"\b(hybrid trap)\b", False),
    ("EDM Trap", r"\b(trap edm|edm trap|electronic trap)\b", False),
    ("Midtempo Bass", r"\b(midtempo bass|midtempo edm|midtempo)\b", False),
    ("Glitch Hop", r"\b(glitch hop(?: edm)?|neurohop)\b", False),
    ("Moombahton", r"\b(moombahton|moombahcore)\b", False),

    # ---- 鼓打贝斯系 ----
    ("Neurofunk", r"\b(neurofunk|neuro drum)\b", False),
    ("Liquid DnB", r"\b(liquid (?:drum and bass|drum & bass|dnb|funk)|liquid dnb)\b", False),
    ("Dancefloor DnB", r"\b(dancefloor (?:drum and bass|drum & bass|dnb)|dancefloor dnb)\b", False),
    ("Jump Up DnB", r"\b(jump[\s-]?up (?:drum and bass|drum & bass|dnb)|jump[\s-]?up dnb)\b", False),
    ("Drumstep", r"\b(drumstep)\b", False),
    ("Jungle", r"\b(jungle|ragga jungle)\b", False),
    ("Breakcore", r"\b(breakcore|lolicore)\b", False),
    ("Drum & Bass", r"\b(drum\s*(?:and|&)\s*bass|drum n bass|dnb)\b", False),

    # ---- 浩室系 ----
    ("Complextro", r"\b(complextro)\b", False),
    ("Big Room House", r"\b(big room(?: house)?|festival house|festival progressive house)\b", False),
    ("Dutch House", r"\b(dutch house|dirty dutch)\b", False),
    ("Melbourne Bounce", r"\b(melbourne bounce)\b", False),
    ("Electro House", r"\b(electro house)\b", False),
    ("Acid House", r"\b(acid house)\b", False),
    ("Tropical House", r"\b(tropical house|trop house)\b", False),
    ("French House", r"\b(french house|filter house|french touch)\b", False),
    ("Disco House", r"\b(disco house|funky house)\b", False),
    ("Hard House", r"\b(?:uk )?hard house\b", False),
    ("Bass House", r"\b(bass house|g house)\b", False),
    ("Future House", r"\b(future house|future bounce|slap house)\b", False),
    ("Tech House", r"\b(tech house|minimal deep tech)\b", False),
    ("Deep House", r"\b(deep house|lo[\s-]?fi house)\b", False),
    ("Progressive House", r"\b(progressive house|mainstage progressive)\b", False),
    ("Amapiano", r"\b(amapiano)\b", False),
    ("Afro House", r"\b(afro house)\b", False),
    ("Melodic House", r"\b(melodic house|melodic house & techno|organic house)\b", False),
    ("House", r"\b(house music|house)\b", True),

    # ---- 迷幻 / 科技舞曲 ----
    ("Psytrance", r"\b(psy[\s-]?trance|goa trance|full[\s-]?on psytrance)\b", False),
    ("Uplifting Trance", r"\b(uplifting trance|anthem trance)\b", False),
    ("Progressive Trance", r"\b(progressive trance|trance 2\.0)\b", False),
    ("Tech Trance", r"\b(tech trance)\b", False),
    ("Hard Trance", r"\b(hard trance|acid trance)\b", False),
    ("Trance", r"\b(trance)\b", True),
    ("Hard Techno", r"\b(hard techno|schranz|hardgroove techno|peak time techno)\b", False),
    ("Acid Techno", r"\b(acid techno)\b", False),
    ("Melodic Techno", r"\b(melodic techno)\b", False),
    ("Industrial Techno", r"\b(industrial techno)\b", False),
    ("Minimal Techno", r"\b(minimal techno|deep techno|detroit techno)\b", False),
    ("Techno", r"\b(techno)\b", True),

    # ---- 车库 / 碎拍 / 迪斯科 ----
    ("Future Garage", r"\b(future garage)\b", False),
    ("Speed Garage", r"\b(speed garage)\b", False),
    ("2-Step Garage", r"\b(2[\s-]?step(?: garage)?|two[\s-]?step garage)\b", False),
    ("Bassline", r"\b(4x4[\s-]?bassline|niche[\s-]?bassline|(?:uk )?bassline(?: house)?)\b", False),
    ("UK Garage", r"\b(uk garage|ukg|dark garage|4x4 garage|garage 4x4)\b", False),
    ("Big Beat", r"\b(big beat)\b", False),
    ("Breakbeat", r"\b(breakbeat|breaks|nu skool breaks|progressive breaks)\b", False),
    ("Nu-Disco", r"\b(nu[\s-]?disco|future funk)\b", False),
    ("Electro Swing", r"\b(electro swing)\b", False),
    ("Synthwave", r"\b(synthwave|darksynth|retrowave|vaporwave)\b", False),

    # ---- 金属 / 朋克 ----
    ("Deathcore", r"\b(deathcore)\b", False),
    ("Metalcore", r"\b(metalcore|post[\s-]?hardcore)\b", False),
    ("Industrial Metal", r"\b(industrial metal|neue deutsche härte)\b", False),
    ("Progressive Metal", r"\b(progressive metal|djent)\b", False),
    ("Death Metal", r"\b(death metal|melodic death metal|technical death metal)\b", False),
    ("Black Metal", r"\b(black metal|blackgaze)\b", False),
    ("Nu Metal", r"\b(nu[\s-]?metal|rap metal)\b", False),
    ("Metal", r"(?:\b(?:thrash metal|heavy metal|power metal|doom metal|metal)\b|メタル)", True),
    ("Punk", r"(?:\b(?:pop punk|punk rock|punk)\b|パンク)", False),

    # ---- 嘻哈 / R&B / 灵魂 / 放克 ----
    ("Drift Phonk", r"\b(drift[\s-]?phonk|cowbell phonk|street phonk)\b", False),
    ("Phonk", r"\b(phonk|rare phonk|memphis phonk|cloud phonk)\b", False),
    ("Lo-Fi Hip Hop", r"\b(?:lo[\s-]?fi hip[\s-]?hop|lofi hip[\s-]?hop|lo[\s-]?fi beats?|chillhop)\b", False),
    ("Instrumental Hip Hop", r"\binstrumental hip[\s-]?hop\b", False),
    ("Experimental Hip Hop", r"\b(?:experimental[\s-]+(?:hip[\s-]?hop|rap)|abstract[\s-]+hip[\s-]?hop|avant[\s-]?garde)", False),
    ("Hip Hop", r"(?:\b(?:hip[\s-]?hop|rap|trap|grime|g[\s-]?funk)\b|ヒップホップ|ラップ)", True),
    ("Alternative R&B", r"\b(?:alternative|alt|experimental)[\s-]?(?:r&b|rnb)\b", False),
    ("Contemporary R&B", r"\b(?:contemporary|modern)[\s-]?(?:r&b|rnb)\b", False),
    ("New Jack Swing", r"\b(?:new jack swing|swingbeat|rnb[\s/-]?swing|r&b[\s/-]?swing)\b", False),
    ("Neo Soul", r"\bneo[\s-]?soul\b", False),
    ("Gospel", r"\b(?:gospel|contemporary christian)\b", False),
    ("Soul", r"\b(?:soul|uk street soul|northern soul|psychedelic soul)\b", False),
    ("R&B", r"\b(?:r&b|rnb|rhythm (?:and|&) blues)\b", True),
    ("Funk", r"(?:^funk$|\b(?:p[.\s-]?funk|free funk)\b)", False),

    # ---- 流行 / 摇滚 ----
    ("City Pop", r"\bcity[\s-]?pop\b", False),
    ("Vocaloid", r"\bvocaloid\b", False),
    ("Anime", r"\b(?:anime|anison)\b", False),
    ("J-Pop", r"\b(?:j[\s-]?pop|japanese pop)\b", False),
    ("K-Pop", r"\b(?:k[\s-]?pop|korean pop)\b", False),
    ("Dance Pop", r"\b(?:dance[\s-]?pop|electropop|synthpop|europop)\b", False),
    ("Indie Pop", r"\b(?:indie pop|bedroom pop|dream pop)\b", False),
    ("Pop Rock", r"\b(?:pop rock|piano rock|power pop)\b", False),
    ("Pop", r"(?:\bpop\b|ポップ)", True),
    ("Alternative Rock", r"\b(?:alternative rock|indie rock|garage rock|alternative|indie)\b", False),
    ("Rock", r"(?:\b(?:hard rock|classic rock|punk rock|rock)\b|ロック)", True),

    # ---- 爵士 / 古典 / 世界音乐 ----
    ("Jazz Fusion", r"\b(?:jazz[\s-]?(?:fusion|funk|rock)|fusion jazz)\b", False),
    ("Bossa Nova", r"\bbossa[\s-]?nova\b", False),
    ("Bebop", r"\b(?:be[\s-]?bop|hard bop|post bop)\b", False),
    ("Swing Jazz", r"\b(?:swing jazz|big band|swing music)\b", False),
    ("Jazz", r"\b(?:jazz|cool jazz|free jazz|modal jazz|smooth jazz)\b", True),
    ("Baroque", r"\bbaroque\b", False),
    ("Romantic Classical", r"\b(?:romantic classical|romantic era|neo-romantic)\b", False),
    ("Opera", r"\bopera\b", False),
    ("Modern Classical", r"\b(?:modern classical|contemporary classical|neo[\s-]?classical|post[\s-]?modern)", False),
    ("Classical", r"\b(?:classical|orchestral|chamber music|piano)\b", True),
    ("Soundtrack", r"\b(?:soundtrack|film score|video game music|original score)\b", False),
    ("Latin", r"\b(?:latin(?: pop| urban| dance| music)?|reggaeton|salsa|bachata|merengue|cumbia|mambo)\b", True),
    ("Reggae", r"\b(?:reggae|dancehall|dub music)\b", True),

    # ---- 氛围 / 慢节奏 ----
    ("Chillout", r"\b(?:chillout|chill-out)\b", False),
    ("Downtempo", r"\b(?:downtempo|down-tempo|downbeat|trip[\s-]?hop)\b", False),
    ("Ambient", r"(?:^(?:ambient|ambient music|dark ambient|space ambient|drone|drone ambient)$)", False),
    ("IDM", r"\b(?:idm|intelligent dance music|braindance|drill and bass)\b", False),
    ("Glitch", r"(?:^(?:glitch|microsound|lowercase|clicks and cuts)$)", False),
    ("Electronic", r"(?:\b(?:electronic|electronica|dance|edm|electro)\b|クラブ|ダンス|エレクトロ(?:ニック)?)", True),
]


def match_genre_keywords(artist="", title="", album="", genre=""):
    """用艺人/曲名/专辑/标签文本匹配细分曲风，返回 [英文曲风名]。
    细分度 = 关键词长度（越具体越长）；只返回非大类命中的细分，大类忽略。"""
    text = " ".join(x for x in (artist, title, album, genre) if x)
    if not text:
        return []
    hits = []
    for zh, rx, is_major in GENRE_KEYWORDS:
        if is_major:
            continue  # 大类交给 Discogs/在线查询，不用关键词
        try:
            if re.search(rx, text, re.I):
                hits.append(zh)
        except re.error:
            continue
    return hits


# 本地AI(EffNet 400类)输出的细碎英文词 → 中等大类（归并，避免标签太杂）
GENRE_SIMPLIFY = {
    "goa trance": "Trance", "psy-trance": "Trance", "psytrance": "Trance",
    "acid trance": "Trance", "hard trance": "Trance", "tech trance": "Trance",
    "progressive trance": "Trance", "uplifting trance": "Trance", "trance": "Trance",
    "deep house": "House", "tech house": "House", "progressive house": "House",
    "acid house": "House", "disco house": "House", "french house": "House",
    "funky house": "House", "minimal house": "House", "house": "House",
    "electro": "Electro", "electroclash": "Electro", "electro house": "Electro",
    "hard techno": "Techno", "minimal techno": "Techno", "acid techno": "Techno",
    "industrial techno": "Techno", "melodic techno": "Techno", "techno": "Techno",
    "happy hardcore": "Hardcore", "gabber": "Hardcore", "breakcore": "Hardcore",
    "speedcore": "Hardcore", "j-core": "Hardcore", "hardcore": "Hardcore",
    "rawstyle": "Hardstyle", "euphoric hardstyle": "Hardstyle", "hardstyle": "Hardstyle",
    "liquid funk": "Drum & Bass", "neurofunk": "Drum & Bass", "jungle": "Drum & Bass",
    "drum and bass": "Drum & Bass", "drum & bass": "Drum & Bass", "dnb": "Drum & Bass",
    "brostep": "Dubstep", "dubstep": "Dubstep",
    "grime": "UK Garage", "bassline": "UK Garage", "uk garage": "UK Garage", "garage": "UK Garage",
    "drone": "Ambient", "dark ambient": "Ambient", "ambient": "Ambient",
    "trip hop": "Downtempo", "chillout": "Downtempo", "downtempo": "Downtempo",
    "idm": "Experimental", "glitch": "Experimental", "abstract": "Experimental",
    "musique concrète": "Experimental", "musique concrete": "Experimental",
    "noise": "Experimental", "experimental": "Experimental",
    "synthwave": "Synthwave", "synth-pop": "Synthpop", "synthpop": "Synthpop",
    "eurodance": "Eurodance", "eurobeat": "Eurobeat",
    "big beat": "Breakbeat", "breaks": "Breakbeat", "breakbeat": "Breakbeat",
    "disco": "Disco", "nu-disco": "Disco", "funk": "Funk", "soul": "Soul",
    "r&b": "R&B", "rnb": "R&B", "hip hop": "Hip Hop", "rap": "Hip Hop",
    "rock": "Rock", "metal": "Metal", "punk": "Punk", "pop": "Pop",
    "jazz": "Jazz", "classical": "Classical", "folk": "Folk",
    "reggae": "Reggae", "latin": "Latin", "world music": "World",
    "electronic": "Electronic", "electronica": "Electronic", "edm": "Electronic",
    "dance": "Dance", "vocal": "Vocal", "soundtrack": "Soundtrack",
}


def simplify_genres(genres):
    """把细碎英文词归并到中等大类（精确优先，子串兜底：大类键是词的子串）；
    无法归类的保留原词。"""
    out = []
    for g in (genres or []):
        gl = str(g).lower()
        s = GENRE_SIMPLIFY.get(gl)
        if s is None:
            for k, v in GENRE_SIMPLIFY.items():
                if k in gl:      # 只做「键是词的子串」，避免短词误伤长词
                    s = v
                    break
        target = s if s else g
        if target and target not in out:
            out.append(target)
    return out


# 曲风大类分组（纠正曲风编辑器折叠展示用；未列入的词自动进「其他」）
GENRE_GROUPS = [
    ("硬核系", ["Hard Dance", "Puzzycore", "Uptempo Hardcore", "UK Hardcore",
              "Happy Hardcore", "Industrial Hardcore", "Frenchcore", "Gabber", "Hardcore"]),
    ("Hardstyle系", ["Hardstyle", "Rawstyle", "Euphoric Hardstyle"]),
    ("浩室系", ["House", "Deep House", "Tech House", "Progressive House", "Acid House",
              "Tropical House", "French House", "Disco House", "Hard House", "Bass House",
              "Future House", "Electro House", "Melodic House", "Afro House",
              "Big Room House", "Melbourne Bounce", "Amapiano", "Dutch House"]),
    ("Trance系", ["Trance", "Hard Trance", "Psytrance", "Uplifting Trance",
                "Progressive Trance", "Tech Trance"]),
    ("Techno系", ["Techno", "Hard Techno", "Acid Techno", "Melodic Techno",
                "Industrial Techno", "Minimal Techno"]),
    ("贝斯/回响系", ["Dubstep", "Brostep", "Riddim", "Future Riddim", "Deathstep",
                 "Melodic Dubstep", "Colour Bass", "Bass Music", "Kawaii Bass",
                 "Future Bass", "Midtempo Bass", "Moombahcore"]),
    ("Drum & Bass系", ["Drum & Bass", "Liquid DnB", "Dancefloor DnB", "Jump Up DnB",
                  "Neurofunk", "Jungle", "Drumstep", "Breakcore"]),
    ("陷阱系", ["EDM Trap", "Hard Trap", "Hybrid Trap", "Glitch Hop", "Moombahton"]),
    ("车库/碎拍系", ["UK Garage", "2-Step Garage", "Speed Garage", "Future Garage",
                 "Bassline", "Breakbeat", "Big Beat", "Nu-Disco", "Electro Swing"]),
    ("氛围/慢节奏", ["Ambient", "Chillout", "Downtempo", "IDM", "Glitch", "Experimental"]),
    ("电子其他", ["Electronic", "Electro", "Synthwave", "Disco", "Complextro"]),
    ("嘻哈/R&B/灵魂", ["Hip Hop", "Lo-Fi Hip Hop", "Instrumental Hip Hop",
                  "Experimental Hip Hop", "Phonk", "Drift Phonk",
                  "R&B", "Alternative R&B", "Contemporary R&B", "New Jack Swing",
                  "Neo Soul", "Soul", "Gospel", "Funk"]),
    ("流行/摇滚", ["Pop", "J-Pop", "K-Pop", "City Pop", "Dance Pop", "Indie Pop",
               "Vocaloid", "Anime", "Rock", "Alternative Rock", "Pop Rock", "Punk"]),
    ("金属系", ["Metal", "Metalcore", "Deathcore", "Death Metal", "Black Metal",
             "Nu Metal", "Industrial Metal", "Progressive Metal"]),
    ("爵士/古典/世界", ["Jazz", "Jazz Fusion", "Bossa Nova", "Bebop", "Swing Jazz",
                  "Classical", "Baroque", "Romantic Classical", "Opera",
                  "Modern Classical", "Soundtrack", "Latin", "Reggae",
                  "Country", "Folk", "Blues", "Singer-Songwriter"]),
]


def _build_groups():
    """校验分组覆盖；词表里没分组的词自动补进「其他」"""
    grouped = {n for _, names in GENRE_GROUPS for n in names}
    other = sorted(set(n for n, _, _ in GENRE_KEYWORDS) - grouped)
    if other:
        GENRE_GROUPS.append(("其他", other))
    return GENRE_GROUPS


GENRE_GROUPS = _build_groups()


def grouped_genres(custom=None):
    """曲风大类分组（含用户自定义曲风），供「曲风映射」「纠正曲风」两个编辑器共用。

    custom：`{大类名: [曲风名, ...]}`（存在 cfg["custom_genres"]）。
    自定义曲风并入同名大类末尾；大类名不在内置列表里则新建一个大类追加到最后。
    返回 [(大类名, [曲风名, ...]), ...]——都是新列表，调用方随便改，不会污染内置表。
    """
    custom = {k: [c for c in (v or []) if c] for k, v in (custom or {}).items()}
    base_names = {n for n, _ in GENRE_GROUPS}
    out = []
    for name, names in GENRE_GROUPS:
        lst = list(names)
        for c in custom.get(name, []):
            if c not in lst:
                lst.append(c)
        out.append((name, lst))
    for g, names in custom.items():
        if g in base_names or not names:
            continue
        out.append((g, list(names)))
    return out
