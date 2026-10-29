"""把贴纸整图按网格切成单张：白底抠透明，出 PNG + 带序号的预览图。

贴纸挨得太紧时连通块会粘在一起，所以不靠连通块分割，而是：
  1. 整行接近纯白的位置切出两行；
  2. 每行内部找「墨水最少」的列当缝；
  3. 边界再往外推一点把字幅收全，但碰到「整列都是画」就停（那是隔壁角色身体）；
  4. 最后清掉贴着边缘的细碎残片。

ROW_BANDS / COL_BOUNDS / TILE_NUDGE 是按具体图调出来的，换图要重新量。
用法：python slice_emoji.py <源图> <输出目录>
"""

import os
import sys
from collections import deque

from PIL import Image, ImageDraw

# 两行的高度范围（y0, y1），来自整行白带检测
ROW_BANDS = [(1, 95), (99, 193)]
# 每行的列边界（x），来自每行内部竖白缝检测 + 等距补齐
COL_BOUNDS = [
    [0, 97, 190, 283, 375, 465, 562, 656, 752, 840, 940, 1029],
    [0, 94, 188, 282, 376, 470, 564, 658, 752, 846],
]
WHITE = 236
# 个别贴纸的字紧贴邻居身体，自动扩展收不全，手工补几像素：{序号: (左, 右)}
TILE_NUDGE = {16: (0, 9)}        # 三通道都 >= 这个值算白
PAD = 2


def knock_out_white(tile):
    """把从四边连通的白色区域抠成透明（角色身上的白色高光不受影响）。"""
    tile = tile.convert("RGBA")
    width, height = tile.size
    pixels = tile.load()

    def pale(x, y):
        r, g, b, a = pixels[x, y]
        return a > 0 and r >= WHITE and g >= WHITE and b >= WHITE

    seen = [[False] * height for _ in range(width)]
    queue = deque()
    for x in range(width):
        for y in (0, height - 1):
            if pale(x, y) and not seen[x][y]:
                seen[x][y] = True
                queue.append((x, y))
    for y in range(height):
        for x in (0, width - 1):
            if pale(x, y) and not seen[x][y]:
                seen[x][y] = True
                queue.append((x, y))
    while queue:
        cx, cy = queue.popleft()
        pixels[cx, cy] = (255, 255, 255, 0)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = cx + dx, cy + dy
            if 0 <= nx < width and 0 <= ny < height and not seen[nx][ny] and pale(nx, ny):
                seen[nx][ny] = True
                queue.append((nx, ny))
    return tile


def drop_edge_slivers(tile, maxWidth=22, maxArea=0.08):
    """去掉贴着左右边、又细又小的碎片——那是隔壁贴纸被切开的一角。"""
    tile = tile.convert("RGBA")
    width, height = tile.size
    pixels = tile.load()
    seen = [[False] * height for _ in range(width)]
    for x in range(width):
        for y in range(height):
            if seen[x][y] or pixels[x, y][3] == 0:
                continue
            queue = deque([(x, y)])
            seen[x][y] = True
            cells, minx, maxx, miny, maxy = [], x, x, y, y
            while queue:
                cx, cy = queue.popleft()
                cells.append((cx, cy))
                minx, maxx = min(minx, cx), max(maxx, cx)
                miny, maxy = min(miny, cy), max(maxy, cy)
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < width and 0 <= ny < height and not seen[nx][ny] and pixels[nx, ny][3] > 0:
                        seen[nx][ny] = True
                        queue.append((nx, ny))
            touchesEdge = minx <= 1 or maxx >= width - 2
            if not touchesEdge:
                continue
            thin = maxx - minx + 1 <= maxWidth
            small = len(cells) < width * height * maxArea
            if thin and small:
                for cx, cy in cells:
                    pixels[cx, cy] = (255, 255, 255, 0)
    return tile


def trim(tile, margin=1):
    box = tile.getbbox()
    if not box:
        return tile
    left, top, right, bottom = box
    return tile.crop((max(0, left - margin), max(0, top - margin),
                      min(tile.width, right + margin), min(tile.height, bottom + margin)))


def main():
    source, outDir = sys.argv[1], sys.argv[2]
    os.makedirs(outDir, exist_ok=True)
    image = Image.open(source).convert("RGBA")
    pixels = image.load()

    def inkCount(x, y0, y1):
        """这一列有多少非白像素（用来找真正的缝：字幅会盖进白缝里）。"""
        total = 0
        for y in range(y0, y1):
            r, g, b, a = pixels[x, y]
            if a > 20 and not (r >= WHITE and g >= WHITE and b >= WHITE):
                total += 1
        return total

    tiles = []
    for (y0, y1), bounds in zip(ROW_BANDS, COL_BOUNDS):
        profile = [inkCount(x, y0, y1) for x in range(image.width)]
        height = y1 - y0
        # 每个内部边界在 ±14 像素里挪到墨水最少的那一列，别切到字
        fixed = [bounds[0]]
        for cut in bounds[1:-1]:
            lo, hi = max(1, cut - 14), min(image.width - 1, cut + 14)
            best = min(range(lo, hi), key=lambda x: (profile[x], abs(x - cut)))
            fixed.append(best)
        fixed.append(bounds[-1])

        def widen(cut, step):
            """把边界往外推，只要新列不是「整列都是画」——这样字幅能收全，
            相邻贴纸的身体（几乎整列有墨）收不进来。"""
            x = cut
            for _ in range(30):
                nxt = x + step
                if nxt <= 0 or nxt >= image.width - 1:
                    break
                if profile[nxt] > height * 0.6:
                    break
                x = nxt
            return x

        cells = []
        for index, (left, right) in enumerate(zip(fixed, fixed[1:])):
            newLeft = widen(left, -1) if index else left
            newRight = widen(right, 1) if index < len(fixed) - 2 else right
            cells.append((newLeft, newRight))

        for left, right in cells:
            adjust = TILE_NUDGE.get(len(tiles) + 1)
            if adjust:
                left += adjust[0]
                right += adjust[1]
            tile = image.crop((left, max(0, y0 - PAD), right, min(image.height, y1 + PAD)))
            tile = drop_edge_slivers(trim(knock_out_white(tile)))
            tiles.append(tile)

    for index, tile in enumerate(tiles, 1):
        tile.save(os.path.join(outDir, f"{index:02d}.png"))
    print(f"切出 {len(tiles)} 张，尺寸:", [f"{t.width}x{t.height}" for t in tiles])

    # 预览：等比到高 150，排成一行，上面标序号
    scaled = []
    for tile in tiles:
        ratio = 150 / tile.height
        scaled.append(tile.resize((max(1, int(tile.width * ratio)), 150), Image.LANCZOS))
    gap = 10
    width = sum(t.width for t in scaled) + gap * (len(scaled) + 1)
    sheet = Image.new("RGBA", (width, 200), (245, 245, 250, 255))
    draw = ImageDraw.Draw(sheet)
    offset = gap
    for index, tile in enumerate(scaled, 1):
        sheet.paste(tile, (offset, 40), tile)
        draw.text((offset + 4, 12), str(index), fill=(20, 20, 30, 255))
        offset += tile.width + gap
    sheet.convert("RGB").save(os.path.join(outDir, "contact.png"))
    print("预览:", os.path.join(outDir, "contact.png"))


if __name__ == "__main__":
    main()
