# -*- coding: utf-8 -*-
"""端到端自检：造一张英文字幕图 → OCR → 翻译，结果写到 test_out.txt"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "vendor"))

from PIL import Image, ImageDraw, ImageFont
import numpy as np

OUT = os.path.join(BASE, "test_out.txt")
log = []


def w(s):
    log.append(str(s))
    print(s)


# 1. 造图
img = Image.new("RGB", (900, 120), (12, 14, 18))
d = ImageDraw.Draw(img)
try:
    font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 34)
except Exception:
    font = ImageFont.load_default()
d.text((20, 20), "Use the force, Luke. We should head home", fill="white", font=font)
d.text((20, 66), "before dark. The road is dangerous.", fill="white", font=font)
sample = os.path.join(BASE, "_sample.png")
img.save(sample)
w("样例图: %s" % sample)

# 2. OCR
try:
    from rapidocr_onnxruntime import RapidOCR
    ocr = RapidOCR()
    res, elapse = ocr(np.array(img)[:, :, ::-1])
    w("OCR 用时: %s" % (elapse,))
    texts = [(r[1], round(float(r[2]), 3)) for r in (res or [])]
    w("OCR 结果: %s" % texts)
    joined = " ".join(t for t, s in texts)
except Exception as e:
    import traceback
    w("OCR 失败: %s" % traceback.format_exc())
    joined = ""

# 3. 翻译
try:
    from screen_translate import Translator
    tr = Translator()
    zh = tr.translate(joined or "Use the force, Luke.")
    w("译文: %s" % zh)
except Exception:
    import traceback
    w("翻译失败: %s" % traceback.format_exc())

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(log))
print("written", OUT)
