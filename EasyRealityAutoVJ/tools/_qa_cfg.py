import sys, json
sys.path.insert(0, "src")
import config
print("CONFIG_FILE =", config.CONFIG_FILE)
c = config.Config()
print("library count =", len(c["library"]))
print("dyn_tag_ver =", c["dyn_tag_ver"])
print("clip_tags_manual nonempty =", sum(1 for v in (c["clip_tags_manual"] or {}).values() if v))
print("clip_tags_disabled nonempty =", sum(1 for v in (c["clip_tags_disabled"] or {}).values() if v))
# 亮度词现有落档统计
from collections import Counter
cnt = Counter()
for p, tags in (c["clip_tags"] or {}).items():
    for t in (tags or []):
        if t in ("暗基调","中亮度","亮基调","明亮","昏暗","中等"):
            cnt[t] += 1
print("现有标签里的亮度词计数:", dict(cnt))
