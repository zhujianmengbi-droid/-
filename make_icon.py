# -*- coding: utf-8 -*-
"""将随源码发布的 ZJ HUB 图标 PNG 转成 Windows ICO。"""
from pathlib import Path

from PIL import Image


ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def make_ico(path: Path):
    source = path.with_name('zj_hub_icon.png')
    if not source.exists():
        raise FileNotFoundError(f'图标源文件不存在：{source}')
    image = Image.open(source).convert('RGBA')
    if image.width != image.height:
        edge = max(image.width, image.height)
        canvas = Image.new('RGBA', (edge, edge), (0, 0, 0, 255))
        canvas.paste(image, ((edge - image.width) // 2, (edge - image.height) // 2))
        image = canvas
    image.save(path, format='ICO', sizes=[(size, size) for size in ICON_SIZES])


if __name__ == '__main__':
    make_ico(Path(__file__).with_name('app.ico'))
    print('已生成 ZJ HUB app.ico')
