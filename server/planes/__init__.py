"""Planes-overhead mode: live info card for aircraft near a point.

Data sources:
- api.adsb.lol /v2/point/{lat}/{lon}/{radius_nm} — live aircraft (callsign,
  ICAO type, altitude, speed, distance + bearing from the point). Community
  API, no key.
- api.adsbdb.com /v0/callsign/{cs} — route lookup (origin/destination
  airports). Cached per callsign; unknowns retried after a while.

Frame layout (64x64):
  line 1  callsign (yellow) | aircraft model right-aligned (white) — 4x6 font
  line 2  SFO > CDG centered (green) — classic 5x7 LED font
  middle  compass rose with outer N/E/S/W, arrow = the plane's current track
  bottom  aircraft in the search radius (left) | speed (right, pale blue) — 3x5
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
C_SPEED = (150, 200, 255)
C_COMPASS = (70, 75, 85)
C_ARROW = (255, 210, 0)
C_MUTED = (110, 115, 125)

CMP_CX, CMP_CY, CMP_R = 32, 38, 12


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
        # cardinal letters on the outer layer
        draw_text(img, CMP_CX - 1, CMP_CY - CMP_R - 7, "N", C_MUTED)
        draw_text(img, CMP_CX - 1, CMP_CY + CMP_R + 3, "S", C_MUTED)
        draw_text(img, CMP_CX + CMP_R + 3, CMP_CY - 2, "E", C_MUTED)
        draw_text(img, CMP_CX - CMP_R - 6, CMP_CY - 2, "W", C_MUTED)
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
            draw_text(img, 2, 1, "NO", C_MUTED, font="4x6")
            draw_text(img, 2, 9, "TRAFFIC", C_MUTED, font="5x7")
            self._compass(img, None)
            draw_text(img, 1, 59, "0 AIRCRAFT", C_MUTED)
            self.last_shown = None
        else:
            featured %= len(self.planes)
            p = self.planes[featured]
            route = self.routes.get(p["callsign"], (None, 0))[0]
            model = p["type"][:4]
            draw_text(img, 1, 1, p["callsign"][:7], C_CALLSIGN, font="4x6")
            draw_text(img, SIZE - len(model) * 5 + 1, 1, model, C_INFO, font="4x6")
            line2 = f"{route['o_iata']} > {route['d_iata']}" if route else "ROUTE ?"
            draw_text(img, (SIZE - (len(line2) * 6 - 1)) // 2, 9, line2, C_ROUTE, font="5x7")
            self._compass(img, p["track"])
            # count everything being cycled (the 5mi-only subset read as a
            # frozen 0 — downtown SF often has zero planes that close)
            draw_text(img, 1, 59, f"{len(self.planes)} AIRCRAFT", C_MUTED)
            if p["gs"] is not None:
                speed = f"{p['gs']:.0f}KT"
                draw_text(img, SIZE - len(speed) * 4, 59, speed, C_SPEED)
            self.last_shown = p["callsign"]
        self.last_frame = img
        return img
