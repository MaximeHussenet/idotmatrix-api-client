"""One-shot generator for basemap.png (64x64) from OSM carto tiles.

Fetches z11 tiles covering the frame defined in geo.py, classifies every
source pixel into water / vegetation / city / other by OSM carto palette
heuristics, then majority-votes each 64x64 cell. Prints an ASCII preview.

Run from repo root:  .venv/bin/python server/bartmap/generate_basemap.py
"""

import io
import math
import sys
from collections import Counter
from colorsys import rgb_to_hsv
from pathlib import Path

import httpx
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent))
from bartmap import geo

ZOOM = 11
TILE = 256
N = 2 ** ZOOM

# panel palette
WATER = (20, 60, 180)
VEG = (25, 110, 35)
CITY = (95, 95, 100)
OTHER = (110, 80, 45)
PALETTE = {0: WATER, 1: VEG, 2: CITY, 3: OTHER}


def lonlat_to_global_px(lat: float, lon: float) -> tuple[float, float]:
    x = (lon + 180) / 360 * N * TILE
    lat_r = math.radians(lat)
    y = (1 - math.asinh(math.tan(lat_r)) / math.pi) / 2 * N * TILE
    return x, y


def classify(r: int, g: int, b: int) -> int:
    h, s, v = rgb_to_hsv(r / 255, g / 255, b / 255)
    hue = h * 360
    if 175 <= hue <= 250 and s > 0.06:
        return 0  # water
    if 65 <= hue <= 170 and s > 0.08:
        return 1  # vegetation
    if s < 0.025:
        return 2  # greys/whites = built-up & roads
    return 3  # everything else (bare land, farmland, beige base)


def main() -> None:
    x0, y0 = lonlat_to_global_px(geo.LAT_MAX, geo.LON_MIN)  # NW
    x1, y1 = lonlat_to_global_px(geo.LAT_MIN, geo.LON_MAX)  # SE
    tx0, tx1 = int(x0 // TILE), int(x1 // TILE)
    ty0, ty1 = int(y0 // TILE), int(y1 // TILE)
    w, h = (tx1 - tx0 + 1) * TILE, (ty1 - ty0 + 1) * TILE
    print(f"frame lon {geo.LON_MIN:.4f}..{geo.LON_MAX:.4f} lat {geo.LAT_MIN}..{geo.LAT_MAX}")
    print(f"tiles z{ZOOM}: x {tx0}..{tx1}, y {ty0}..{ty1} ({(tx1-tx0+1)*(ty1-ty0+1)} tiles)")

    mosaic = Image.new("RGB", (w, h))
    headers = {"User-Agent": "idotmatrix-local-bartmap/1.0 (one-shot basemap generation)"}
    with httpx.Client(headers=headers, timeout=30) as client:
        for tx in range(tx0, tx1 + 1):
            for ty in range(ty0, ty1 + 1):
                url = f"https://tile.openstreetmap.org/{ZOOM}/{tx}/{ty}.png"
                resp = client.get(url)
                resp.raise_for_status()
                tile = Image.open(io.BytesIO(resp.content)).convert("RGB")
                mosaic.paste(tile, ((tx - tx0) * TILE, (ty - ty0) * TILE))

    crop = mosaic.crop((round(x0 - tx0 * TILE), round(y0 - ty0 * TILE),
                        round(x1 - tx0 * TILE), round(y1 - ty0 * TILE)))
    cw, ch = crop.size
    src = crop.load()
    out = Image.new("RGB", (geo.SIZE, geo.SIZE))
    ascii_rows = []
    chars = {0: "~", 1: '"', 2: "#", 3: "."}
    for py in range(geo.SIZE):
        row = []
        for px in range(geo.SIZE):
            xa, xb = cw * px // geo.SIZE, cw * (px + 1) // geo.SIZE
            ya, yb = ch * py // geo.SIZE, ch * (py + 1) // geo.SIZE
            votes = Counter()
            for sy in range(ya, yb, 2):  # sample every other px, plenty
                for sx in range(xa, xb, 2):
                    votes[classify(*src[sx, sy])] += 1
            cls = votes.most_common(1)[0][0]
            out.putpixel((px, py), PALETTE[cls])
            row.append(chars[cls])
        ascii_rows.append("".join(row))
    print("\n".join(ascii_rows))

    dest = Path(__file__).parent / "basemap.png"
    out.save(dest)
    print(f"\nsaved {dest}")


if __name__ == "__main__":
    main()
