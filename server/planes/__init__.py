"""Planes-overhead mode: live info card for aircraft near a point.

Data sources:
- api.adsb.lol /v2/point/{lat}/{lon}/{radius_nm} — live aircraft (callsign,
  ICAO type, altitude, speed, distance + bearing from the point). Community
  API, no key.
- api.adsbdb.com /v0/callsign/{cs} — route lookup (origin/destination
  airports). Cached per callsign; unknowns retried after a while.

Frame layout (64x64, 3x5 font):
  line 1  callsign (yellow, 1px taller) | aircraft model right-aligned (white)
  line 2  origin city (IATA) TO        (green)
  line 3  destination city (IATA)      (green)
  middle  compass rose, arrow = the plane's current track
  bottom  aircraft within ~5mi (left) | ground speed in KT (right, pale blue)
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

NEAR_NM = 4.34  # "within vicinity" = 5 statute miles

C_CALLSIGN = (255, 210, 0)
C_ROUTE = (0, 220, 120)
C_INFO = (225, 225, 225)
C_SPEED = (150, 200, 255)
C_COMPASS = (70, 75, 85)
C_ARROW = (255, 210, 0)
C_MUTED = (110, 115, 125)

CMP_CX, CMP_CY, CMP_R = 32, 40, 15


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
                    "gs": ac.get("gs"),
                    "track": ac.get("track", ac.get("calc_track")),
                    "dst": ac.get("dst", 0.0),
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
    async def _fetch_route(client: httpx.AsyncClient, callsign: str) -> dict | None:
        resp = await client.get(ROUTE_URL.format(cs=callsign))
        if resp.status_code != 200:
            return None
        fr = resp.json().get("response", {})
        if not isinstance(fr, dict):
            return None  # "unknown callsign"
        fr = fr.get("flightroute", {})
        origin, dest = fr.get("origin", {}), fr.get("destination", {})
        if not (origin.get("iata_code") and dest.get("iata_code")):
            return None
        return {
            "o_city": origin.get("municipality") or origin.get("name") or "",
            "o_iata": origin["iata_code"],
            "d_city": dest.get("municipality") or dest.get("name") or "",
            "d_iata": dest["iata_code"],
        }

    # ------------------------------------------------------------ rendering

    @staticmethod
    def _airport_line(city: str, iata: str, suffix: str = "") -> str:
        """'San Francisco', 'SFO' -> 'SAN FRAN (SFO)' truncated to 16 chars."""
        tail = f" ({iata}){suffix}"
        return city[: 16 - len(tail)].rstrip() + tail

    def _compass(self, img: Image.Image, track: float | None) -> None:
        draw = ImageDraw.Draw(img)
        # cardinal + intercardinal tick marks
        for deg in range(0, 360, 45):
            rad = math.radians(deg)
            sin, cos = math.sin(rad), math.cos(rad)
            outer = CMP_R if deg % 90 == 0 else CMP_R - 1
            draw.line(
                (CMP_CX + round(sin * (outer - 2)), CMP_CY - round(cos * (outer - 2)),
                 CMP_CX + round(sin * outer), CMP_CY - round(cos * outer)),
                fill=C_COMPASS,
            )
        draw_text(img, CMP_CX - 1, CMP_CY - CMP_R + 3, "N", C_MUTED)
        if track is None:
            draw_text(img, CMP_CX - 1, CMP_CY - 2, "?", C_MUTED)
            return
        rad = math.radians(track)
        sin, cos = math.sin(rad), math.cos(rad)
        tip = (CMP_CX + sin * (CMP_R - 3), CMP_CY - cos * (CMP_R - 3))
        draw.line((CMP_CX - round(sin * 3), CMP_CY + round(cos * 3),
                   round(tip[0]), round(tip[1])), fill=C_ARROW)
        for side in (150, -150):  # arrowhead barbs
            brad = math.radians(track + side)
            draw.line((round(tip[0]), round(tip[1]),
                       round(tip[0] + math.sin(brad) * 4), round(tip[1] - math.cos(brad) * 4)),
                      fill=C_ARROW)

    def render(self, featured: int) -> Image.Image:
        img = Image.new("RGB", (SIZE, SIZE))
        if not self.planes:
            draw_text(img, 1, 1, "NO TRAFFIC", C_MUTED, tall=True)
            self._compass(img, None)
            draw_text(img, 1, 59, "0", C_MUTED)
            self.last_shown = None
        else:
            featured %= len(self.planes)
            p = self.planes[featured]
            route = self.routes.get(p["callsign"], (None, 0))[0]
            draw_text(img, 1, 1, p["callsign"][:11], C_CALLSIGN, tall=True)
            model = p["type"][:4]
            draw_text(img, SIZE - len(model) * 4, 2, model, C_INFO)
            if route:
                draw_text(img, 1, 10, self._airport_line(route["o_city"], route["o_iata"], " TO"), C_ROUTE)
                draw_text(img, 1, 16, self._airport_line(route["d_city"], route["d_iata"]), C_ROUTE)
            else:
                draw_text(img, 1, 10, "ROUTE ?", C_ROUTE)
            self._compass(img, p["track"])
            near = sum(1 for q in self.planes if q["dst"] <= NEAR_NM)
            draw_text(img, 1, 59, f"{near} ACFT", C_MUTED)
            if p["gs"] is not None:
                speed = f"{p['gs']:.0f}KT"
                draw_text(img, SIZE - len(speed) * 4, 59, speed, C_SPEED)
            self.last_shown = p["callsign"]
        self.last_frame = img
        return img
