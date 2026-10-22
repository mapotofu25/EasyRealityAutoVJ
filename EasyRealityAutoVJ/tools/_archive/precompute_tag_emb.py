# -*- coding: utf-8 -*-
"""预计算 Chinese-CLIP 标签向量表（一次性，离线）：
读 cn_clip_text.onnx + vocab.txt，把双语标签词表编码成 512 维向量，
保存到 assets/models/chinese_clip/tag_emb.npy + tags.txt。
运行时只需视觉编码器 + 这两张小文件，无需再带文本编码器。
"""
import os
import numpy as np
import onnxruntime as ort

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_DIR = os.path.join(ROOT, "assets", "models", "chinese_clip")
TMP = os.path.join(ROOT, ".workbuddy", "models_tmp", "chinese_clip")

# 双语标签词表（中文用于编码，英文用于后续与曲风标签匹配）
import sys as _sys
_sys.path.insert(0, os.path.join(ROOT, "src"))
from tags_def import ALL_TAGS
TAGS = [(zh, en) for zh, en, _cat in ALL_TAGS]

# ---------- WordPiece 分词（RoBERTa-wwm，vocab.txt） ----------
vocab = {}
for i, line in enumerate(open(os.path.join(MODEL_DIR, "vocab.txt"), encoding="utf-8").read().split("\n")):
    line = line.strip()
    if line:
        vocab[line] = i
UNK, CLS, SEP, PAD = 100, 101, 102, 0


def _basic(text):
    buf = []
    for ch in text.lower():
        if "\u4e00" <= ch <= "\u9fff":
            buf.append(" " + ch + " ")
        else:
            buf.append(ch)
    return "".join(buf).split()


def wp(token):
    if token in vocab:
        return [token]
    subs, start = [], 0
    while start < len(token):
        end, cur = len(token), None
        while start < end:
            sub = token[start:end]
            if start > 0:
                sub = "##" + sub
            if sub in vocab:
                cur = sub
                break
            end -= 1
        if cur is None:
            return [None]
        subs.append(cur)
        start = end
    return subs


def tokenize(text, max_len=52):
    ids = [CLS]
    for t in _basic(text):
        for s in wp(t):
            ids.append(vocab.get(s, UNK) if s is not None else UNK)
    ids.append(SEP)
    ids = ids[:max_len]
    mask = [1] * len(ids)
    while len(ids) < max_len:
        ids.append(PAD)
        mask.append(0)
    return np.array([ids], dtype=np.int64), np.array([mask], dtype=np.int64)


def main():
    sess = ort.InferenceSession(os.path.join(TMP, "cn_clip_text.onnx"), providers=["CPUExecutionProvider"])
    zh = [t[0] for t in TAGS]
    ids, mask = zip(*[tokenize(t) for t in zh])
    ids = np.vstack(ids)
    mask = np.vstack(mask)
    emb = sess.run(None, {"input_ids": ids, "attention_mask": mask})[0]
    emb = emb / np.linalg.norm(emb, axis=1, keepdims=True)
    np.save(os.path.join(MODEL_DIR, "tag_emb.npy"), emb.astype(np.float32))
    with open(os.path.join(MODEL_DIR, "tags.txt"), "w", encoding="utf-8") as f:
        for z, e in TAGS:
            f.write(f"{z}|{e}\n")
    print(f"已生成 tag_emb.npy {emb.shape} 和 tags.txt（{len(TAGS)} 个双语标签）")


if __name__ == "__main__":
    main()
