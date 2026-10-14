"""Photo slideshow mode: pixelized random pictures from a local folder.

Point it at any directory of images — e.g. an Instagram data export
(Settings -> Your activity -> Download your information) — and it shuffles
through them. Each picture is EXIF-rotated, center-cropped square and
downscaled to the panel size (which is what makes it pixel art), with a mild
auto-contrast + saturation boost so photos read well on an LED matrix.
Optional palette quantization (colors=N) for a harder pixel-art look.
"""

import logging
import random
import time
from pathlib import Path

from PIL import Image, ImageEnhance, ImageOps

log = logging.getLogger("photos")

SIZE = 64
EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}


class PhotoShow:
    def __init__(self, folder: str, colors: int = 0) -> None:
        self.folder = Path(folder).expanduser()
        self.colors = colors
        self.playlist: list[Path] = []
        self.total = 0
        self.last_frame: Image.Image | None = None
        self.last_update: float = 0.0
        self.last_error: str | None = None
        self.last_shown: str | None = None

    def _scan(self) -> None:
        files = [p for p in self.folder.rglob("*")
                 if p.is_file() and p.suffix.lower() in EXTS]
        random.shuffle(files)
        self.playlist = files
        self.total = len(files)
        log.info("photo library scanned: %d images under %s", self.total, self.folder)

    def _pixelize(self, path: Path) -> Image.Image:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im = ImageOps.fit(im, (SIZE, SIZE), method=Image.Resampling.LANCZOS)
        im = ImageOps.autocontrast(im, cutoff=1)
        im = ImageEnhance.Color(im).enhance(1.25)
        if self.colors:
            im = im.quantize(self.colors, dither=Image.Dither.FLOYDSTEINBERG).convert("RGB")
        return im

    def next_frame(self) -> Image.Image:
        """Random image, no repeats until the whole library has been shown."""
        if not self.playlist:
            self._scan()
            if not self.playlist:
                raise RuntimeError(f"no images found under {self.folder}")
        # skip unreadable files without dying (corrupt/truncated exports happen)
        while self.playlist:
            path = self.playlist.pop()
            try:
                frame = self._pixelize(path)
            except Exception as exc:
                log.warning("skipping unreadable image %s: %s", path, exc)
                continue
            self.last_frame = frame
            self.last_shown = path.name
            self.last_update = time.time()
            self.last_error = None
            return frame
        raise RuntimeError(f"no readable images under {self.folder}")
