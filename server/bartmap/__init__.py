"""Live BART map mode: base map + route lines + one colored dot per train.

Data sources:
- static GTFS (bart.gov) — stations, trip stop sequences, route colors, track
  shapes; cached in server/bartmap/cache/, refreshed when older than 30 days.
- GTFS-Realtime TripUpdates (api.bart.gov/gtfsrt) — per-train predicted stop
  times. A train's position is interpolated between its previous and next
  station by prediction time; at 64x64 (~1.3 km/pixel) that is plenty.
"""

import csv
import io
import logging
import time
import zipfile
from pathlib import Path

import httpx
from PIL import Image, ImageDraw
from google.transit import gtfs_realtime_pb2

from bartmap import geo

log = logging.getLogger("bartmap")

CACHE_DIR = Path(__file__).parent / "cache"
STATIC_ZIP = CACHE_DIR / "gtfs_static.zip"
STATIC_URL = "https://www.bart.gov/dev/schedules/google_transit.zip"
RT_URL = "https://api.bart.gov/gtfsrt/tripupdate.aspx"
STATIC_MAX_AGE_S = 30 * 86400
BASEMAP = Path(__file__).parent / "basemap.png"
LINE_COLOR = (0, 0, 0)


def _hms_to_s(hms: str) -> int:
    h, m, s = hms.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


class BartMap:
    def __init__(self) -> None:
        self._load_static()
        self._background = self._render_background()
        self.last_frame: Image.Image | None = None
        self.last_update: float = 0.0
        self.last_train_count: int = 0
        self.last_error: str | None = None

    # ------------------------------------------------------------ static

    def _ensure_static_zip(self) -> None:
        CACHE_DIR.mkdir(exist_ok=True)
        if STATIC_ZIP.exists() and time.time() - STATIC_ZIP.stat().st_mtime < STATIC_MAX_AGE_S:
            return
        log.info("downloading BART static GTFS...")
        resp = httpx.get(STATIC_URL, follow_redirects=True, timeout=120,
                         headers={"User-Agent": "idotmatrix-local-bartmap/1.0"})
        resp.raise_for_status()
        STATIC_ZIP.write_bytes(resp.content)

    def _load_static(self) -> None:
        self._ensure_static_zip()
        z = zipfile.ZipFile(STATIC_ZIP)

        def rows(name: str):
            return csv.DictReader(io.TextIOWrapper(z.open(name), "utf-8-sig"))

        # route -> color; bus bridges excluded entirely
        self.route_color: dict[str, tuple[int, int, int]] = {}
        for r in rows("routes.txt"):
            if r["route_id"].startswith("BB"):
                continue
            c = r.get("route_color") or "FFFFFF"
            self.route_color[r["route_id"]] = tuple(int(c[i : i + 2], 16) for i in (0, 2, 4))

        self.stop_pos: dict[str, tuple[float, float]] = {
            s["stop_id"]: (float(s["stop_lat"]), float(s["stop_lon"]))
            for s in rows("stops.txt")
            if s.get("stop_lat") and s.get("stop_lon")
        }

        self.trip_route: dict[str, str] = {}
        route_shapes: set[str] = set()
        for t in rows("trips.txt"):
            if t["route_id"] in self.route_color:
                self.trip_route[t["trip_id"]] = t["route_id"]
                if t.get("shape_id"):
                    route_shapes.add(t["shape_id"])

        # trip -> ordered stops with scheduled times (for prev-stop lookup and
        # scheduled segment durations)
        self.trip_stops: dict[str, list[tuple[str, int, int]]] = {}
        for st in rows("stop_times.txt"):
            trip_id = st["trip_id"]
            if trip_id not in self.trip_route:
                continue
            self.trip_stops.setdefault(trip_id, []).append(
                (st["stop_id"], _hms_to_s(st["arrival_time"]), _hms_to_s(st["departure_time"]))
            )
        self.trip_stop_index: dict[str, dict[str, int]] = {
            trip: {stop_id: i for i, (stop_id, _, _) in enumerate(stops)}
            for trip, stops in self.trip_stops.items()
        }

        self.shapes: list[list[tuple[float, float]]] = []
        shape_pts: dict[str, list[tuple[int, float, float]]] = {}
        for sh in rows("shapes.txt"):
            sid = sh["shape_id"]
            if sid in route_shapes:
                shape_pts.setdefault(sid, []).append(
                    (int(sh["shape_pt_sequence"]), float(sh["shape_pt_lat"]), float(sh["shape_pt_lon"]))
                )
        for pts in shape_pts.values():
            pts.sort()
            self.shapes.append([(lat, lon) for _, lat, lon in pts])
        log.info(
            "static GTFS loaded: %d routes, %d stops, %d trips, %d shapes",
            len(self.route_color), len(self.stop_pos), len(self.trip_stops), len(self.shapes),
        )

    # ------------------------------------------------------------ rendering

    def _render_background(self) -> Image.Image:
        img = Image.open(BASEMAP).convert("RGB").copy()
        draw = ImageDraw.Draw(img)
        for shape in self.shapes:
            xy = [geo.latlon_to_xy(lat, lon) for lat, lon in shape]
            draw.line(xy, fill=LINE_COLOR, width=1)
        return img

    # ------------------------------------------------------------ live

    async def _fetch_rt(self) -> gtfs_realtime_pb2.FeedMessage:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(RT_URL, follow_redirects=True)
            resp.raise_for_status()
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(resp.content)
        return feed

    def _train_positions(self, feed) -> list[tuple[float, float, tuple[int, int, int]]]:
        """Returns (lat, lon, color) per active train."""
        now = time.time()
        trains = []
        skipped = 0
        for entity in feed.entity:
            if not entity.HasField("trip_update"):
                continue
            tu = entity.trip_update
            trip_id = tu.trip.trip_id
            route_id = self.trip_route.get(trip_id)
            if route_id is None:
                skipped += 1
                continue
            stus = [
                (stu.stop_id,
                 stu.arrival.time if stu.HasField("arrival") else stu.departure.time,
                 stu.departure.time if stu.HasField("departure") else stu.arrival.time)
                for stu in tu.stop_time_update
            ]
            stus = [s for s in stus if s[1]]
            nxt = next((s for s in stus if s[1] > now), None)
            if nxt is None:
                continue  # all predictions in the past; trip ending
            stop_id, arr_t, _dep_t = nxt
            idx = self.trip_stop_index.get(trip_id, {}).get(stop_id)
            if idx is None or stop_id not in self.stop_pos:
                continue
            if idx == 0:
                continue  # hasn't departed its origin yet
            prev_id, _pa, prev_dep_sched = self.trip_stops[trip_id][idx - 1]
            if prev_id not in self.stop_pos:
                continue
            sched_arr = self.trip_stops[trip_id][idx][1]
            seg = max(60, sched_arr - prev_dep_sched)  # scheduled segment seconds
            frac = min(1.0, max(0.0, 1.0 - (arr_t - now) / seg))
            lat1, lon1 = self.stop_pos[prev_id]
            lat2, lon2 = self.stop_pos[stop_id]
            lat = lat1 + (lat2 - lat1) * frac
            lon = lon1 + (lon2 - lon1) * frac
            trains.append((lat, lon, self.route_color[route_id]))
        if skipped:
            log.debug("skipped %d RT trips with no static match", skipped)
        return trains

    async def compute_frame(self) -> Image.Image:
        feed = await self._fetch_rt()
        trains = self._train_positions(feed)
        frame = self._background.copy()
        for lat, lon, color in trains:
            frame.putpixel(geo.latlon_to_pixel(lat, lon), color)
        self.last_frame = frame
        self.last_update = time.time()
        self.last_train_count = len(trains)
        self.last_error = None
        return frame

    def frame_pixels(self, frame: Image.Image) -> list[tuple[int, int, int]]:
        return list(frame.getdata())
