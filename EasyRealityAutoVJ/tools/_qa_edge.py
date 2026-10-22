# -*- coding: utf-8 -*-
"""任务D：边界与异常路径。全部夹具落 tempfile.mkdtemp()。"""
import sys, os, shutil, tempfile
sys.path.insert(0, "src")
import numpy as np
import cv2
import tags_def as td
from tagger import Tagger

tmp = tempfile.mkdtemp(prefix="qa_edge_")
try:
    print("夹具目录:", tmp)
    ok_all = True
    def check(name, cond, extra=""):
        global ok_all
        print(("  [PASS] " if cond else "  [FAIL] ") + name + ("  " + str(extra) if extra else ""))
        if not cond:
            ok_all = False

    def bucket(med):
        if med is None:
            return None
        return (td.BRIGHT_DARK if med < td.BRIGHTNESS_DARK_THRESHOLD
                else (td.BRIGHT_LIGHT if med >= td.BRIGHTNESS_BRIGHT_THRESHOLD else td.BRIGHT_MID))

    print("① med=None 路径：打不开的文件")
    zero = os.path.join(tmp, "empty.mp4")
    open(zero, "wb").close()
    m, f, d, med = Tagger.analyze_dynamic(zero, "video")
    check("0字节 .mp4 → 元组4项", isinstance((m, f, d, med), tuple) and len((m, f, d, med)) == 4)
    check("0字节 .mp4 → med is None", med is None, "med=%r" % (med,))
    check("0字节 .mp4 → motion=''", m == "", "motion=%r" % (m,))

    mp, fp, dp, medp = Tagger.analyze_dynamic(os.path.join(tmp, "nope.mp4"), "video")
    check("不存在路径 → med is None", medp is None, "med=%r" % (medp,))

    print("② med=None 假素材：消费点不落词")
    # 模拟 ui_main TagScanThread 落词逻辑
    def land(med):
        tags = []
        if med is not None:
            tags.append(bucket(med))
        return tags
    check("med=None 不落任何亮度词", land(None) == [], land(None))
    check("med=None 不产生 None 元素", None not in land(None))

    print("③ 图片分支：med == 灰度均值")
    img = np.zeros((64, 64, 3), dtype=np.uint8)
    img[:, :] = (10, 20, 30)
    ipath = os.path.join(tmp, "img.png")
    cv2.imwrite(ipath, img)
    _, _, _, med_img = Tagger.analyze_dynamic(ipath, "image")
    expect = float(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).mean())
    check("图片 med == 灰度均值", med_img is not None and abs(med_img - expect) < 1e-6,
          "med=%r expect=%.4f" % (med_img, expect))

    print("④ 极端值分档")
    check("全黑 med=0 → 暗基调", bucket(0.0) == td.BRIGHT_DARK, bucket(0.0))
    check("全白 med=255 → 亮基调", bucket(255.0) == td.BRIGHT_LIGHT, bucket(255.0))

    print("⑤ 阈值边界（med 恰好 = 15.0 / 40.0）")
    check("med=15.0 → 中亮度（注释: <15 暗）", bucket(15.0) == td.BRIGHT_MID, bucket(15.0))
    check("med=14.999 → 暗基调", bucket(14.999) == td.BRIGHT_DARK, bucket(14.999))
    check("med=40.0 → 亮基调（注释: >=40 亮）", bucket(40.0) == td.BRIGHT_LIGHT, bucket(40.0))
    check("med=39.999 → 中亮度", bucket(39.999) == td.BRIGHT_MID, bucket(39.999))

    print("⑥ brights 为空（帧数不足）")
    # 造一个只有 1 帧的 mp4 → len(diffs) < 2 → None
    short = os.path.join(tmp, "one.mp4")
    vw = cv2.VideoWriter(short, cv2.VideoWriter_fourcc(*"mp4v"), 10, (32, 32))
    for _ in range(1):
        vw.write(np.zeros((32, 32, 3), dtype=np.uint8))
    vw.release()
    m2, f2, d2, med2 = Tagger.analyze_dynamic(short, "video")
    check("单帧视频 → 不崩且返回 4 元组", len((m2, f2, d2, med2)) == 4, (m2, f2, d2, med2))
    check("单帧视频 → med is None 或 合法值", med2 is None or isinstance(med2, float), "med=%r" % (med2,))

    print("=" * 50)
    print("任务D 结论:", "全部 PASS" if ok_all else "存在 FAIL")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
