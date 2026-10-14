"""Screensaver mode: Win98-style animated loops rendered as multi-frame GIFs.

The trick that makes this work on BLE: the whole animation is uploaded ONCE
as a looping GIF and the panel plays it forever by itself — zero radio
traffic between refreshes. A fresh animation is generated and pushed every
cycle (default every few minutes) so it never gets stale.

Styles:
  pipes    — the classic: colored pipes random-walking across the screen
  life     — Conway's Game of Life from a random soup, cells colored by age
  mystify  — bouncing polygons with fading trails (Win98 "Mystify Your Mind")
"""

import colorsys
import io
import logging
import random
import time

from PIL import Image, ImageDraw

log = logging.getLogger("saver")

SIZE = 64
STYLES = ("pipes", "life", "mystify")

PIPE_COLORS = [
    (230, 60, 50), (60, 190, 70), (70, 120, 240), (240, 200, 40),
    (200, 80, 220), (60, 210, 210), (240, 140, 40),
]


def _gif_bytes(frames: list[Image.Image], frame_ms: int) -> bytes:
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:],
                   duration=frame_ms, loop=0, optimize=True)
    return buf.getvalue()


# ------------------------------------------------------------------ styles

def _pipes_frames(n: int) -> list[Image.Image]:
    canvas = Image.new("RGB", (SIZE, SIZE))
    draw = ImageDraw.Draw(canvas)
    dirs = [(0, -1), (0, 1), (-1, 0), (1, 0)]
    x, y = random.randrange(SIZE), random.randrange(SIZE)
    dx, dy = random.choice(dirs)
    color = random.choice(PIPE_COLORS)
    frames = []
    for _ in range(n):
        for _ in range(10):  # steps per frame
            if random.random() < 0.22:  # elbow: turn 90°, mark the joint
                draw.point((x, y), fill=tuple(min(255, c + 70) for c in color))
                dx, dy = random.choice([(dy, dx), (-dy, -dx)])
            nx, ny = x + dx, y + dy
            if not (0 <= nx < SIZE and 0 <= ny < SIZE):  # hit a wall: new pipe
                x, y = random.randrange(SIZE), random.randrange(SIZE)
                dx, dy = random.choice(dirs)
                color = random.choice(PIPE_COLORS)
                continue
            x, y = nx, ny
            draw.point((x, y), fill=color)
        frames.append(canvas.copy())
    return frames


def _life_frames(n: int) -> list[Image.Image]:
    cells = {(x, y) for x in range(SIZE) for y in range(SIZE) if random.random() < 0.33}
    age: dict[tuple[int, int], int] = {c: 0 for c in cells}
    frames = []
    for _ in range(n):
        img = Image.new("RGB", (SIZE, SIZE))
        for (x, y), a in age.items():
            if a == 0:
                color = (140, 255, 160)        # newborn: bright green
            elif a < 4:
                color = (40, 190, 120)
            elif a < 10:
                color = (30, 120, 160)
            else:
                color = (40, 60, 140)          # ancient: deep blue
            img.putpixel((x, y), color)
        frames.append(img)
        # next generation (torus wrap)
        neigh: dict[tuple[int, int], int] = {}
        for x, y in cells:
            for ddx in (-1, 0, 1):
                for ddy in (-1, 0, 1):
                    if ddx or ddy:
                        k = ((x + ddx) % SIZE, (y + ddy) % SIZE)
                        neigh[k] = neigh.get(k, 0) + 1
        new = {c for c, cnt in neigh.items() if cnt == 3 or (cnt == 2 and c in cells)}
        age = {c: (age.get(c, -1) + 1) for c in new}
        cells = new
    return frames


def _mystify_frames(n: int) -> list[Image.Image]:
    class Poly:
        def __init__(self, hue: float):
            self.pts = [[random.uniform(2, SIZE - 3), random.uniform(2, SIZE - 3)] for _ in range(4)]
            self.vel = [[random.choice((-1, 1)) * random.uniform(0.7, 1.6) for _ in range(2)] for _ in range(4)]
            self.hue = hue
            self.trail: list[list[list[float]]] = []

        def step(self):
            self.trail.append([p[:] for p in self.pts])
            if len(self.trail) > 6:
                self.trail.pop(0)
            for p, v in zip(self.pts, self.vel):
                for i in (0, 1):
                    p[i] += v[i]
                    if not 0 <= p[i] <= SIZE - 1:
                        v[i] = -v[i]
                        p[i] += v[i]
            self.hue = (self.hue + 0.008) % 1.0

    polys = [Poly(random.random()), Poly(random.random())]
    frames = []
    for _ in range(n):
        img = Image.new("RGB", (SIZE, SIZE))
        draw = ImageDraw.Draw(img)
        for poly in polys:
            poly.step()
            layers = poly.trail + [poly.pts]
            for depth, pts in enumerate(layers):
                dim = (depth + 1) / len(layers)
                rgb = colorsys.hsv_to_rgb(poly.hue, 0.9, dim)
                color = tuple(round(c * 255) for c in rgb)
                for i in range(4):
                    a, b = pts[i], pts[(i + 1) % 4]
                    draw.line((round(a[0]), round(a[1]), round(b[0]), round(b[1])), fill=color)
        frames.append(img)
    return frames


_GENERATORS = {"pipes": _pipes_frames, "life": _life_frames, "mystify": _mystify_frames}


class Saver:
    def __init__(self, style: str = "pipes", frames: int = 40, frame_ms: int = 150) -> None:
        if style not in STYLES:
            raise ValueError(f"style must be one of {STYLES}")
        self.style = style
        self.frames = frames
        self.frame_ms = frame_ms
        self.last_gif: bytes | None = None
        self.last_frame: Image.Image | None = None
        self.last_update: float = 0.0
        self.last_error: str | None = None

    def generate(self) -> bytes:
        frames = _GENERATORS[self.style](self.frames)
        gif = _gif_bytes(frames, self.frame_ms)
        self.last_gif = gif
        self.last_frame = frames[-1]
        self.last_update = time.time()
        self.last_error = None
        log.info("saver '%s': %d frames, %.1f KB gif", self.style, len(frames), len(gif) / 1024)
        return gif
