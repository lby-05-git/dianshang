# -*- coding: utf-8 -*-
"""生成联调用中文投诉测试图片（PIL + 微软雅黑字体）：
- damage_invoice.png：破损商品发票（场景一，350 元）
- clear_product.png：清晰商品图（场景二，128 元）
"""
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT = r"C:\Windows\Fonts\msyh.ttc"
OUT = Path(__file__).resolve().parents[1] / "backend" / "uploads"
OUT.mkdir(parents=True, exist_ok=True)


def make(path: str, bg: tuple, lines: list[str], size=(640, 480)):
    """生成带随机噪点的测试图。

    注意：MD5 必须唯一！若所有测试图内容一致，会命中 Fraud 规则
    "凭证复用 +30"（D4 裁决）导致低风险案件也转人工——真实用户上传的照片
    天然不同，这里用随机噪点模拟（同时可当"照片感"纹理）。
    """
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT, 34)
    y = 60
    for line in lines:
        d.text((50, y), line, fill=(30, 30, 30), font=font)
        y += 70
    # 随机噪点（保证每次生成 MD5 不同）
    rnd = random.Random()
    for _ in range(400):
        x, y0 = rnd.randrange(size[0]), rnd.randrange(size[1])
        d.point((x, y0), fill=(rnd.randrange(80, 180),) * 3)
    # 边框模拟破损
    d.rectangle([20, 20, size[0] - 20, size[1] - 20], outline=(120, 120, 120), width=4)
    img.save(str(OUT / path))
    print("生成:", OUT / path)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    make("damage_invoice.png", (245, 240, 230), [
        "破损商品发票",
        "商品：蓝牙耳机",
        "金额：350.00 元",
        "订单号：ORD-350001",
        "购买日期：2026-08-10",
    ])
    make("clear_product.png", (235, 245, 235), [
        "商品完好图",
        "商品：数据线",
        "金额：128.00 元",
        "订单号：ORD-128001",
    ])
