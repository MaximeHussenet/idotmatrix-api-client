"""Brute-force test of DIY image continuation framings against the panel.

Each candidate framing paints the whole panel one solid color for ~7s:
  1. GREEN  — single 9-byte header + full 12288B payload (no per-chunk headers)
  2. RED    — per-4K headers, continuation flag = 1 (instead of 2)
  3. BLUE   — per-4K headers, flag = 2, len field = REMAINING bytes
  4. YELLOW — per-4K headers like stock, but byte[2] = 1 (GIF-style)
Whichever color fills the ENTIRE screen = the framing this firmware wants.
"""
import asyncio
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from idotmatrix.client import IDotMatrixClient
from idotmatrix.modules.image import ImageMode
from idotmatrix.screensize import ScreenSize

ADDR = "572D92BB-5306-41F5-C508-0344953BEDE2"
WRITE = "0000fa02-0000-1000-8000-00805f9b34fb"


def solid(rgb):
    data = bytearray()
    for _ in range(4096):
        data.extend(rgb)
    return data


def h16(v): return struct.pack("<h", v)
def h32(v): return struct.pack("<i", v)


def framing_single_header(data):
    return [bytes(h16(len(data) + 9)) + bytes([0, 0, 0]) + bytes(h32(len(data))) + data]


def framing_flag1(data):
    out = []
    for i in range(0, len(data), 4096):
        chunk = data[i:i + 4096]
        flag = 0 if i == 0 else 1
        out.append(bytes(h16(len(chunk) + 9)) + bytes([0, 0, flag]) + bytes(h32(len(data))) + chunk)
    return out


def framing_remaining(data):
    out = []
    for i in range(0, len(data), 4096):
        chunk = data[i:i + 4096]
        flag = 0 if i == 0 else 2
        remaining = len(data) - i
        out.append(bytes(h16(len(chunk) + 9)) + bytes([0, 0, flag]) + bytes(h32(remaining)) + chunk)
    return out


def framing_gifstyle(data):
    out = []
    for i in range(0, len(data), 4096):
        chunk = data[i:i + 4096]
        flag = 0 if i == 0 else 2
        out.append(bytes(h16(len(chunk) + 9)) + bytes([1, 0, flag]) + bytes(h32(len(data))) + chunk)
    return out


CANDIDATES = [
    ("1 GREEN  single-header", framing_single_header, (0, 170, 0)),
    ("2 RED    flag=1", framing_flag1, (200, 0, 0)),
    ("3 BLUE   remaining-len", framing_remaining, (0, 60, 220)),
    ("4 YELLOW gif-style byte2", framing_gifstyle, (220, 200, 0)),
]


async def main():
    c = IDotMatrixClient(screen_size=ScreenSize.SIZE_64x64, mac_address=ADDR)
    await c.connect()
    cm = c._connection_manager
    for label, framing, rgb in CANDIDATES:
        print(f"--- {label}", flush=True)
        await c.image.set_mode(ImageMode.EnableDIY)
        await asyncio.sleep(0.3)
        for payload in framing(solid(rgb)):
            for j in range(0, len(payload), 509):
                await cm.client.write_gatt_char(WRITE, payload[j:j + 509], response=True)
            await asyncio.sleep(0.4)
        print(f"    sent — watch panel for 7s", flush=True)
        await asyncio.sleep(7)
    print("test over — holding connection 30s", flush=True)
    await asyncio.sleep(30)
    await c.disconnect()


asyncio.run(main())
