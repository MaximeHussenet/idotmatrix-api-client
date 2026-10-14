"""Planes-overhead mode: live info card for aircraft near a point.

Data sources:
- api.adsb.lol /v2/point/{lat}/{lon}/{radius_nm} — live aircraft (callsign,
  ICAO type, altitude, speed, distance + bearing from the point). Community
  API, no key.
- api.adsbdb.com /v0/callsign/{cs} — route lookup (origin/destination
  airports). Cached per callsign; unknowns retried after a while.

Frame layout (64x64, 3x5 font):
  line 1  callsign                  (yellow)
  line 2  ORIGIN>DEST (IATA)        (green)
  line 3  type + altitude           (white)
  line 4  speed + bearing + range   (blue-grey)
  bottom  radar: observer at center, one dot per plane, featured highlighted
"""

import asyncio
import logging
import math
import time

import httpx
from PIL import Image, ImageDraw

from planes.tinyfont import draw_text

log = logging.getLogger("planes")

SIZE = 64
POINT_URL = "https://api.adsb.lol/v2/point/{lat}/{lon}/{radius}"
ROUTE_URL = "https://api.adsbdb.com/v0/callsign/{cs}"
ROUTE_RETRY_S = 900          # retry unknown routes after 15 min
ROUTE_LOOKUPS_PER_REFRESH = 8
MAX_PLANE_AGE_S = 60         # drop aircraft not seen for this long

C_CALLSIGN = (255, 210, 0)
C_ROUTE = (0, 220, 120)
C_INFO = (225, 225, 225)
C_AUX = (130, 150, 255)
C_RADAR = (70, 75, 85)
C_DOT = (150, 150, 150)
C_FEATURED = (255, 210, 0)
C_CENTER = (77, 163, 255)
C_MUTED = (110, 115, 125)

RADAR_CX, RADAR_CY, RADAR_R = 32, 45, 16
WINDS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def _compass(bearing: float) -> str:
    return WINDS[round(bearing / 45) % 8]


class PlanesOverhead:
    def __init__(self, lat: float, lon: float, radius_nm: float = 12) -> None:
        self.lat = lat
        self.lon = lon
        self.radius_nm = radius_nm
        self.planes: list[dict] = []
        self.routes: dict[str, tuple[str | None, float]] = {}  # callsign -> (route, fetched_at)
        self.last_frame: Image.Image | None = None
        self.last_update: float = 0.0
        self.last_error: str | None = None
        self.last_shown: str | None = None

    # ------------------------------------------------------------ data

    async def refresh(self) -> None:
        url = POINT_URL.format(lat=self.lat, lon=self.lon, radius=self.radius_nm)
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers={"User-Agent": "idotmatrix-local-planes/1.0"})
            resp.raise_for_status()
            data = resp.json()
            planes = []
            for ac in data.get("ac", []):
                callsign = (ac.get("flight") or "").strip()
                reg = (ac.get("r") or "").strip()
                if not callsign and not reg:
                    continue  # anonymous TIS-B/MLAT track, nothing to display
                if ac.get("alt_baro") == "ground" or ac.get("seen", 0) > MAX_PLANE_AGE_S:
                    continue
                planes.append({
                    "callsign": callsign or reg,
                    "type": (ac.get("t") or "?").strip(),
                    "alt": ac.get("alt_baro"),
                    "gs": ac.get("gs"),
                    "dst": ac.get("dst", 0.0),
                    "dir": ac.get("dir", 0.0),
                })
            planes.sort(key=lambda p: p["dst"])
            await self._lookup_routes(client, planes[:ROUTE_LOOKUPS_PER_REFRESH])
        self.planes = planes
        self.last_update = time.time()
        self.last_error = None

    async def _lookup_routes(self, client: httpx.AsyncClient, planes: list[dict]) -> None:
        now = time.time()
        todo = []
        for p in planes:
            cached = self.routes.get(p["callsign"])
            if cached is None or (cached[0] is None and now - cached[1] > ROUTE_RETRY_S):
                todo.append(p["callsign"])
        for cs, result in zip(todo, await asyncio.gather(
                *(self._fetch_route(client, cs) for cs in todo), return_exceptions=True)):
            self.routes[cs] = (None if isinstance(result, Exception) else result, now)

    @staticmethod
    async def _fetch_route(client: httpx.AsyncClient, callsign: str) -> str | None:
        resp = await client.get(ROUTE_URL.format(cs=callsign))
        if resp.status_code != 200:
            return None
        fr = resp.json().get("response", {})
        if not isinstance(fr, dict):
            return None  # "unknown callsign"
        fr = fr.get("flightroute", {})
        origin = fr.get("origin", {}).get("iata_code")
        dest = fr.get("destination", {}).get("iata_code")
        return f"{origin}>{dest}" if origin and dest else None

    # ------------------------------------------------------------ rendering

    def _radar(self, img: Image.Image, featured: int) -> None:
        draw = ImageDraw.Draw(img)
        draw.ellipse(
            (RADAR_CX - RADAR_R, RADAR_CY - RADAR_R, RADAR_CX + RADAR_R, RADAR_CY + RADAR_R),
            outline=C_RADAR,
        )
        img.putpixel((RADAR_CX, RADAR_CY), C_CENTER)
        for i, p in enumerate(self.planes):
            frac = min(1.0, p["dst"] / self.radius_nm)
            rad = math.radians(p["dir"])
            x = RADAR_CX + round(math.sin(rad) * frac * (RADAR_R - 1))
            y = RADAR_CY - round(math.cos(rad) * frac * (RADAR_R - 1))
            if i == featured:
                draw.rectangle((x - 1, y - 1, x, y), fill=C_FEATURED)
            else:
                img.putpixel((x, y), C_DOT)
        draw_text(img, 1, 59, f"{len(self.planes)}", C_MUTED)
        rng = f"{self.radius_nm:.0f}NM"
        draw_text(img, SIZE - len(rng) * 4, 59, rng, C_MUTED)

    def render(self, featured: int) -> Image.Image:
        img = Image.new("RGB", (SIZE, SIZE))
        if not self.planes:
            draw_text(img, 13, 8, "NO", C_MUTED)
            draw_text(img, 13, 15, "TRAFFIC", C_MUTED)
            self._radar(img, -1)
            self.last_shown = None
        else:
            featured %= len(self.planes)
            p = self.planes[featured]
            route = self.routes.get(p["callsign"], (None, 0))[0]
            alt = p["alt"]
            alt_s = "" if alt is None else (f"FL{alt // 100}" if alt >= 18000 else f"{alt}FT")
            gs = p["gs"]
            line4 = " ".join(filter(None, [
                f"{gs:.0f}KT" if gs is not None else "",
                _compass(p["dir"]),
                f"{p['dst']:.0f}NM",
            ]))
            draw_text(img, 1, 1, p["callsign"][:15], C_CALLSIGN)
            draw_text(img, 1, 7, route or "ROUTE ?", C_ROUTE)
            draw_text(img, 1, 13, f"{p['type']} {alt_s}"[:16], C_INFO)
            draw_text(img, 1, 19, line4[:16], C_AUX)
            self._radar(img, featured)
            self.last_shown = p["callsign"]
        self.last_frame = img
        return img
