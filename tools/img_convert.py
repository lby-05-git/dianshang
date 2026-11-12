#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图片转 JPEG 小图，便于 Claude 视觉读取。用法: python tools/img_convert.py <输入图> <输出图> [max_宽]"""
import sys
from PIL import Image

src, dst = sys.argv[1], sys.argv[2]
max_w = int(sys.argv[3]) if len(sys.argv) > 3 else 1280

im = Image.open(src).convert("RGB")
if im.width > max_w:
    im = im.resize((max_w, int(im.height * max_w / im.width)), Image.LANCZOS)
im.save(dst, "JPEG", quality=80)
print(f"ok -> {dst} ({im.width}x{im.height})")
