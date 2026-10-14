"""Now Playing mode: Spotify album art + track info, no API keys needed.

The Spotify desktop app exposes player state over AppleScript, including the
album artwork URL — so everything is local except fetching the cover image.

Frame: album art full-bleed, dimmed band at the bottom with track (white) and
artist (grey) in 3x5, and a 1px Spotify-green progress bar on the last row.
Paused shows a PAUSED badge over the art; idle (no music) shows a placeholder.
"""

import logging
import subprocess
import time
import unicodedata

import httpx
from PIL import Image, ImageDraw, ImageEnhance, ImageOps

from planes.tinyfont import draw_text

log = logging.getLogger("nowplaying")

SIZE = 64
GREEN = (30, 215, 96)
C_TRACK = (240, 240, 240)
C_ARTIST = (150, 155, 160)
C_BAR_BG = (45, 45, 50)

SCRIPT = '''
if application "Spotify" is running then
  tell application "Spotify"
    set s to player state as string
    if s is not "stopped" then
      set t to current track
      return s & linefeed & (player position as string) & linefeed & \
             (duration of t as string) & linefeed & (artwork url of t) & \
             linefeed & (name of t) & linefeed & (artist of t)
    end if
    return s
  end tell
else
  return "not_running"
end if
'''


def _ascii(s: str) -> str:
    """Fold accents etc. into the tiny font's ASCII repertoire."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def _num(s: str) -> float:
    return float(s.replace(",", "."))  # AppleScript uses the locale decimal separator


class NowPlaying:
    def __init__(self) -> None:
        self.state = "unknown"
        self.track: str | None = None
        self.artist: str | None = None
        self.art_url: str | None = None
        self.position = 0.0
        self.duration = 0.0
        self._art_cache: dict[str, Image.Image] = {}
        self.last_frame: Image.Image | None = None
        self.last_update: float = 0.0
        self.last_error: str | None = None

    def poll(self) -> bool:
        """Refresh player state; True when something display-worthy changed."""
        out = subprocess.run(["osascript", "-e", SCRIPT],
                             capture_output=True, text=True, timeout=10)
        lines = out.stdout.strip().split("\n")
        prev = (self.state, self.track, self.artist)
        if len(lines) < 6:
            self.state = "idle"
            self.track = self.artist = self.art_url = None
        else:
            self.state = lines[0]  # playing | paused
            self.position = _num(lines[1])
            self.duration = _num(lines[2]) / 1000.0
            self.art_url, self.track, self.artist = lines[3], lines[4], lines[5]
        return (self.state, self.track, self.artist) != prev

    def _art(self) -> Image.Image:
        if self.art_url in self._art_cache:
            return self._art_cache[self.art_url]
        resp = httpx.get(self.art_url, timeout=20, follow_redirects=True)
        resp.raise_for_status()
        import io

        art = Image.open(io.BytesIO(resp.content)).convert("RGB")
        art = ImageOps.fit(art, (SIZE, SIZE), method=Image.Resampling.LANCZOS)
        if len(self._art_cache) > 20:
            self._art_cache.clear()
        self._art_cache[self.art_url] = art
        return art

    def render(self) -> Image.Image:
        img = Image.new("RGB", (SIZE, SIZE), (8, 8, 10))
        draw = ImageDraw.Draw(img)
        if self.state in ("playing", "paused") and self.track:
            try:
                img.paste(self._art(), (0, 0))
            except Exception as exc:
                log.warning("artwork fetch failed: %s", exc)
            band = ImageEnhance.Brightness(img.crop((0, 49, SIZE, SIZE))).enhance(0.25)
            img.paste(band, (0, 49))
            draw = ImageDraw.Draw(img)
            draw_text(img, 1, 51, _ascii(self.track)[:15], C_TRACK)
            draw_text(img, 1, 57, _ascii(self.artist)[:15], C_ARTIST)
            draw.line((0, 63, SIZE - 1, 63), fill=C_BAR_BG)
            if self.duration > 0:
                frac = max(0.0, min(1.0, self.position / self.duration))
                draw.line((0, 63, round(frac * (SIZE - 1)), 63), fill=GREEN)
            if self.state == "paused":
                draw.rectangle((15, 20, 48, 30), fill=(10, 10, 12))
                draw_text(img, 18, 22, "PAUSED", (255, 210, 0), font="4x6")
        else:
            draw.ellipse((24, 14, 40, 30), outline=GREEN)
            for i, w in enumerate((8, 6, 4)):
                y = 19 + i * 4
                draw.line((32 - w // 2, y, 32 + w // 2, y), fill=GREEN)
            draw_text(img, 19, 40, "SPOTIFY", C_ARTIST)
            draw_text(img, 13, 48, "NOT PLAYING", C_ARTIST)
        self.last_frame = img
        self.last_update = time.time()
        return img
