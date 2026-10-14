"""Shared map frame: bounding box + Web-Mercator projection to panel pixels.

The frame covers the whole BART network (Daly City/Millbrae to Antioch,
Richmond to Berryessa) with extra width on the west so the Pacific shows.
Both the base map generator and the live renderer MUST use this module so
geography and train dots line up.
"""

import math

SIZE = 64

# Latitude bounds chosen around the station extremes (37.368..38.019).
LAT_MIN = 37.32
LAT_MAX = 38.06
# Longitude bounds are derived so pixels are square in Web-Mercator,
# centered to fit stations (-122.469..-121.780) with the surplus pushed west
# into the Pacific/ocean side.
LON_MAX = -121.70


def _merc_y(lat_deg: float) -> float:
    lat = math.radians(lat_deg)
    return math.log(math.tan(math.pi / 4 + lat / 2))


_Y_MIN = _merc_y(LAT_MIN)
_Y_MAX = _merc_y(LAT_MAX)
_SPAN = _Y_MAX - _Y_MIN  # mercator radians, == lon span in radians for square px
LON_MIN = LON_MAX - math.degrees(_SPAN)


def latlon_to_xy(lat: float, lon: float) -> tuple[float, float]:
    """Project to fractional pixel coords; (0,0) = top-left (NW corner)."""
    x = (lon - LON_MIN) / (LON_MAX - LON_MIN) * SIZE
    y = (_Y_MAX - _merc_y(lat)) / _SPAN * SIZE
    return x, y


def latlon_to_pixel(lat: float, lon: float) -> tuple[int, int]:
    x, y = latlon_to_xy(lat, lon)
    return min(SIZE - 1, max(0, int(x))), min(SIZE - 1, max(0, int(y)))
