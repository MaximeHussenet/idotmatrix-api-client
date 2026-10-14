"""Test the GIF transport as a full-frame alternative to the broken DIY path.

Sends two uploads via client.gif.upload_gif_file (sniffed-from-app protocol,
16B headers + CRC32 — completely different from the DIY raw-RGB framing):
  1. solid MAGENTA 1-frame 64x64 GIF  (~10s hold)
  2. the real BART basemap.png as a 1-frame GIF (held until script exit)
If the full screen fills magenta, the GIF path works and BART frames can
ship as single-frame GIFs.
"""
import asyncio
import sys
from pathlib import Path

from PIL import Image

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

from idotmatrix.client import IDotMatrixClient
from idotmatrix.screensize import ScreenSize

ADDR = "572D92BB-5306-41F5-C508-0344953BEDE2"
OUT = Path("/private/tmp/claude-501/-Users-maximehussenet-automation/f2c86750-c4de-4806-aebe-7972a8e16a90/scratchpad")
OUT.mkdir(parents=True, exist_ok=True)


def make_solid_gif(rgb, path):
    img = Image.new("RGB", (64, 64), rgb)
    img.save(path, format="GIF")


def make_basemap_gif(path):
    img = Image.open(HERE / "basemap.png").convert("RGB")
    img.save(path, format="GIF")


async def main():
    magenta = OUT / "solid_magenta.gif"
    basemap = OUT / "basemap_frame.gif"
    make_solid_gif((200, 0, 160), magenta)
    make_basemap_gif(basemap)

    c = IDotMatrixClient(screen_size=ScreenSize.SIZE_64x64, mac_address=ADDR)
    await c.connect()

    print("--- 1 MAGENTA solid 1-frame GIF", flush=True)
    await c.gif.upload_gif_file(magenta, palletize=True)
    print("    sent — watch panel for 10s", flush=True)
    await asyncio.sleep(10)

    print("--- 2 BART basemap as 1-frame GIF", flush=True)
    await c.gif.upload_gif_file(basemap, palletize=True)
    print("    sent — holding connection 45s, watch panel", flush=True)
    await asyncio.sleep(45)

    await c.disconnect()


asyncio.run(main())
