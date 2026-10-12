# -*- coding: utf-8 -*-
"""将随源码发布的 ZJ HUB 图标 PNG 转成 Windows ICO。"""
from pathlib import Path
from collections import deque

from PIL import Image


ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def remove_outer_matte(image: Image.Image) -> Image.Image:
    """Turn only the connected white outside matte into real transparency.

    The terminal glyph is enclosed by the dark rounded square, so a border
    flood-fill removes the white corner pixels without touching that glyph.
    Gray edge pixels become partial black alpha, keeping the rounded edge
    smooth when Windows downsamples the icon.
    """
    image = image.convert('RGBA')
    if image.getextrema()[3][0] < 255:
        return image
    width, height = image.size
    pixels = image.load()
    exterior = bytearray(width * height)
    queue = deque()

    def enqueue(x, y):
        index = y * width + x
        if exterior[index]:
            return
        r, g, b, _alpha = pixels[x, y]
        if min(r, g, b) < 12:
            return
        exterior[index] = 1
        queue.append((x, y))

    for x in range(width):
        enqueue(x, 0)
        enqueue(x, height - 1)
    for y in range(1, height - 1):
        enqueue(0, y)
        enqueue(width - 1, y)
    while queue:
        x, y = queue.popleft()
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < width and 0 <= ny < height:
                enqueue(nx, ny)

    for y in range(height):
        for x in range(width):
            index = y * width + x
            if not exterior[index]:
                continue
            r, g, b, _alpha = pixels[x, y]
            luminance = (299 * r + 587 * g + 114 * b) // 1000
            pixels[x, y] = (0, 0, 0, max(0, 255 - luminance))
    return image


def make_ico(path: Path):
    source = path.with_name('zj_hub_icon.png')
    if not source.exists():
        raise FileNotFoundError(f'图标源文件不存在：{source}')
    image = remove_outer_matte(Image.open(source))
    # Keep the source asset itself clean so Qt and Windows previews use the
    # same transparent corners as the generated ICO.
    image.save(source, format='PNG')
    if image.width != image.height:
        edge = max(image.width, image.height)
        canvas = Image.new('RGBA', (edge, edge), (0, 0, 0, 0))
        canvas.paste(image, ((edge - image.width) // 2, (edge - image.height) // 2))
        image = canvas
    image.save(path, format='ICO', sizes=[(size, size) for size in ICON_SIZES])


if __name__ == '__main__':
    make_ico(Path(__file__).with_name('app.ico'))
    print('已生成 ZJ HUB app.ico')
