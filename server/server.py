"""Local HTTP API for iDotMatrix BLE pixel displays (64x64 by default).

Wraps markusressel/idotmatrix-api-client behind FastAPI so anything on your
machine (curl, scripts, Shortcuts, Home Assistant, …) can drive the panel
without the iDotMatrix iOS app.

Run:  ./run.sh   (then open http://127.0.0.1:8588/ for the control panel)

Env:
  IDM_SIZE            16 | 32 | 64   (default 64)
  IDM_ADDRESS         BLE address/UUID (default: last connected, else discovery)
  IDM_PORT            HTTP port      (default 8588, used by run.sh)
  IDM_BART_AUTOSTART  "1" = start the BART live map on boot (turn on and forget)
"""

import asyncio
import io
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Tuple, Union

from PIL import Image

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from idotmatrix.client import IDotMatrixClient
from idotmatrix.connection_manager import ConnectionManager
from idotmatrix.modules.clock import ClockStyle
from idotmatrix.modules.effect import EffectStyle
from idotmatrix.modules.text import TextColorMode, TextMode
from idotmatrix.screensize import ScreenSize
from idotmatrix.util.image_utils import ResizeMode

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("idotmatrix-local")

BASE_DIR = Path(__file__).resolve().parent
ADDRESS_FILE = BASE_DIR / ".device_address"

_SIZES = {16: ScreenSize.SIZE_16x16, 32: ScreenSize.SIZE_32x32, 64: ScreenSize.SIZE_64x64}
SIZE = int(os.environ.get("IDM_SIZE", "64"))
if SIZE not in _SIZES:
    raise SystemExit(f"IDM_SIZE must be one of {sorted(_SIZES)}, got {SIZE}")


def _saved_address() -> Optional[str]:
    addr = os.environ.get("IDM_ADDRESS")
    if addr:
        return addr
    if ADDRESS_FILE.exists():
        return ADDRESS_FILE.read_text().strip() or None
    return None


client = IDotMatrixClient(screen_size=_SIZES[SIZE], mac_address=_saved_address())
client.set_auto_reconnect(True)

# BLE is a single serial channel — serialize every device operation.
device_lock = asyncio.Lock()

# Server-side view of what the panel is showing (the panel can't be queried).
# "bart" is the only continuous mode: claiming the display for anything else
# stops its loop, otherwise the next refresh would overwrite the new content.
display = {"mode": None, "detail": None}


def claim_display(mode: Optional[str], detail: Optional[str] = None) -> None:
    for name, state in (("bart", _bart), ("planes", _planes), ("photos", _photos),
                        ("spotify", _spotify), ("saver", _saver)):
        if name == mode:
            continue
        task = state["task"]
        if task is not None and not task.done():
            task.cancel()
            state["task"] = None
            log.info("%s loop stopped: display claimed by %s", name, mode)
    display["mode"] = mode
    display["detail"] = detail

app = FastAPI(title="iDotMatrix Local API", version="1.0")

Color = Union[str, Tuple[int, int, int], List[int]]


def parse_color(value: Color) -> Tuple[int, int, int]:
    """Accept '#rrggbb', 'rrggbb', 'r,g,b' or [r,g,b]."""
    if isinstance(value, (tuple, list)):
        if len(value) != 3:
            raise HTTPException(422, f"color needs 3 components, got {value!r}")
        return tuple(int(c) for c in value)
    s = value.strip().lstrip("#")
    if "," in s:
        parts = [p.strip() for p in s.split(",")]
        if len(parts) != 3:
            raise HTTPException(422, f"color needs 3 components, got {value!r}")
        return tuple(int(p) for p in parts)
    if len(s) != 6:
        raise HTTPException(422, f"bad hex color {value!r}")
    return tuple(int(s[i : i + 2], 16) for i in (0, 2, 4))


async def run_on_device(coro_factory, *, timeout: float = 90.0):
    """Connect if needed and run one device operation under the lock."""
    async with device_lock:
        try:
            await asyncio.wait_for(client.connect(), timeout=30)
            return await asyncio.wait_for(coro_factory(), timeout=timeout)
        except asyncio.TimeoutError:
            raise HTTPException(504, "device operation timed out")
        except HTTPException:
            raise
        except Exception as exc:  # bleak errors vary by backend
            log.exception("device operation failed")
            raise HTTPException(502, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- meta

@app.get("/health")
async def health():
    return {"ok": True, "size": SIZE}


@app.get("/status")
async def status():
    return {
        "connected": client._connection_manager.is_connected(),
        "address": client.mac_address,
        "size": SIZE,
        "mode": display["mode"],
        "mode_detail": display["detail"],
    }


@app.get("/enums")
async def enums():
    return {
        "text_modes": {m.name: m.value for m in TextMode},
        "text_color_modes": {m.name: m.value for m in TextColorMode},
        "clock_styles": {m.name: m.value for m in ClockStyle},
        "effect_styles": {m.name: m.value for m in EffectStyle},
        "resize_modes": [m.value for m in ResizeMode],
    }


@app.post("/scan")
async def scan():
    async with device_lock:
        try:
            devices = await asyncio.wait_for(ConnectionManager.discover_devices(), timeout=30)
        except Exception as exc:
            raise HTTPException(502, f"scan failed: {type(exc).__name__}: {exc}")
    return {"devices": devices}


class ConnectBody(BaseModel):
    address: Optional[str] = None


@app.post("/connect")
async def connect(body: ConnectBody = Body(default=ConnectBody())):
    if body.address:
        client.mac_address = body.address
        client._connection_manager.set_address(body.address)
    await run_on_device(lambda: asyncio.sleep(0))  # connect happens inside
    if client.mac_address:
        ADDRESS_FILE.write_text(client.mac_address)
    return await status()


@app.post("/disconnect")
async def disconnect():
    async with device_lock:
        await client.disconnect()
    return {"connected": False}


# ---------------------------------------------------------------- basics

class BrightnessBody(BaseModel):
    percent: int


@app.post("/brightness")
async def brightness(body: BrightnessBody):
    if not 5 <= body.percent <= 100:
        raise HTTPException(422, "percent must be 5-100")
    await run_on_device(lambda: client.set_brightness(body.percent))
    return {"brightness": body.percent}


class ScreenBody(BaseModel):
    on: bool


@app.post("/screen")
async def screen(body: ScreenBody):
    await run_on_device(lambda: client.common.set_screen_state(body.on))
    return {"on": body.on}


@app.post("/screen/flip")
async def screen_flip(flip: bool = Body(True, embed=True)):
    await run_on_device(lambda: client.common.set_screen_flipped(flip))
    return {"flipped": flip}


@app.post("/freeze")
async def freeze():
    await run_on_device(lambda: client.common.freeze_screen())
    return {"frozen": True}


@app.post("/time/sync")
async def time_sync():
    from datetime import datetime

    now = datetime.now()
    await run_on_device(lambda: client.common.set_time(now))
    return {"time": now.isoformat()}


@app.post("/reset")
async def reset():
    claim_display(None)
    await run_on_device(lambda: client.reset())
    return {"reset": True}


# ---------------------------------------------------------------- content

class ColorBody(BaseModel):
    color: Color


@app.post("/color")
async def fullscreen_color(body: ColorBody):
    rgb = parse_color(body.color)
    claim_display("color", "#%02x%02x%02x" % rgb)
    await run_on_device(lambda: client.color.show_color(rgb))
    return {"color": rgb}


class TextBody(BaseModel):
    text: str
    font_size: int = 16
    mode: Union[str, int] = "MARQUEE"
    speed: int = 95
    color: Optional[Color] = None          # sets RGB mode when given
    color_mode: Optional[Union[str, int]] = None  # overrides (e.g. RAINBOW_1)
    bg_color: Optional[Color] = None
    font_path: Optional[str] = None


def _enum_lookup(enum_cls, value, what):
    if isinstance(value, int):
        return enum_cls(value)
    try:
        return enum_cls[str(value).upper()]
    except KeyError:
        raise HTTPException(422, f"unknown {what} {value!r}; one of {[m.name for m in enum_cls]}")


@app.post("/text")
async def text(body: TextBody):
    mode = _enum_lookup(TextMode, body.mode, "text mode")
    rgb = parse_color(body.color) if body.color is not None else None
    if body.color_mode is not None:
        color_mode = _enum_lookup(TextColorMode, body.color_mode, "text color mode")
    else:
        color_mode = TextColorMode.RGB if rgb else TextColorMode.WHITE
    bg = parse_color(body.bg_color) if body.bg_color is not None else None
    claim_display("text", body.text[:32])
    await run_on_device(
        lambda: client.text.show_text(
            body.text,
            font_size=body.font_size,
            font_path=body.font_path,
            text_mode=mode,
            speed=body.speed,
            text_color_mode=color_mode,
            text_color=rgb,
            text_bg_color=bg,
        ),
        timeout=120,
    )
    return {"text": body.text, "mode": mode.name, "color_mode": color_mode.name}


class ClockBody(BaseModel):
    style: Union[str, int] = "Color"
    show_date: bool = True
    hour24: bool = True
    color: Optional[Color] = None


@app.post("/clock")
async def clock(body: ClockBody):
    style = body.style
    if isinstance(style, str):
        match = [m for m in ClockStyle if m.name.lower() == style.lower()]
        if not match:
            raise HTTPException(422, f"unknown clock style {style!r}; one of {[m.name for m in ClockStyle]}")
        style = match[0]
    else:
        style = ClockStyle(style)
    rgb = parse_color(body.color) if body.color is not None else None
    claim_display("clock", style.name)
    await run_on_device(lambda: client.clock.show(style=style, show_date=body.show_date, hour24=body.hour24, color=rgb))
    return {"clock": style.name}


class EffectBody(BaseModel):
    style: Union[str, int]
    colors: List[Color] = []


@app.post("/effect")
async def effect(body: EffectBody):
    style = _enum_lookup(EffectStyle, body.style, "effect style")
    colors = [parse_color(c) for c in body.colors] or [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    claim_display("effect", style.name)
    await run_on_device(lambda: client.effect.show(style, colors))
    return {"effect": style.name, "colors": colors}


class PixelsBody(BaseModel):
    color: Color
    pixels: List[Tuple[int, int]]


@app.post("/pixels")
async def pixels(body: PixelsBody):
    rgb = parse_color(body.color)
    for x, y in body.pixels:
        if not (0 <= x < SIZE and 0 <= y < SIZE):
            raise HTTPException(422, f"pixel ({x},{y}) outside {SIZE}x{SIZE}")
    claim_display("pixels", f"{len(body.pixels)} px")
    await run_on_device(lambda: client.graffiti.set_pixels(rgb, [tuple(p) for p in body.pixels]))
    return {"color": rgb, "count": len(body.pixels)}


class CanvasBody(BaseModel):
    # Flat row-major list of SIZE*SIZE colors ('#rrggbb' or [r,g,b]).
    pixels: List[Color]


async def _push_pil_image(img: Image.Image, timeout: float = 180) -> None:
    """Send a PIL image as a single-frame GIF.

    The GIF transport (sniffed-from-app protocol, CRC32 headers) is the only
    multi-chunk upload this panel renders completely: both DIY framings (raw
    RGB and PNG) drop everything past the first 4096-byte chunk."""
    with tempfile.NamedTemporaryFile(suffix=".gif", delete=False) as f:
        img.convert("RGB").save(f, format="GIF")
        path = f.name
    try:
        try:
            await run_on_device(lambda: client.gif.upload_gif_file(path, palletize=True), timeout=timeout)
        except HTTPException as exc:
            # The panel's weak BLE link occasionally drops mid-upload. One
            # quick retry repaints the frame instead of leaving it broken
            # until the next loop iteration.
            if exc.status_code != 502:
                raise
            await asyncio.sleep(3)
            await run_on_device(lambda: client.gif.upload_gif_file(path, palletize=True), timeout=timeout)
    finally:
        os.unlink(path)


@app.post("/canvas")
async def canvas(body: CanvasBody):
    if len(body.pixels) != SIZE * SIZE:
        raise HTTPException(422, f"need exactly {SIZE * SIZE} pixels, got {len(body.pixels)}")
    data = [parse_color(c) for c in body.pixels]
    img = Image.new("RGB", (SIZE, SIZE))
    img.putdata(data)
    claim_display("canvas")
    await _push_pil_image(img)
    return {"uploaded": len(data)}


class ScoreboardBody(BaseModel):
    home: int
    away: int


@app.post("/scoreboard")
async def scoreboard(body: ScoreboardBody):
    claim_display("scoreboard", f"{body.home}:{body.away}")
    await run_on_device(lambda: client.scoreboard.show(body.home, body.away))
    return {"home": body.home, "away": body.away}


class CountdownBody(BaseModel):
    minutes: int
    seconds: int = 0


@app.post("/countdown/start")
async def countdown_start(body: CountdownBody):
    claim_display("countdown", f"{body.minutes}:{body.seconds:02d}")
    await run_on_device(lambda: client.countdown.start(body.minutes, body.seconds))
    return {"countdown": f"{body.minutes}:{body.seconds:02d}"}


@app.post("/countdown/stop")
async def countdown_stop():
    await run_on_device(lambda: client.countdown.stop())
    return {"countdown": "stopped"}


# ---------------------------------------------------------------- files

def _resize_mode(value: str) -> ResizeMode:
    try:
        return ResizeMode(value)
    except ValueError:
        raise HTTPException(422, f"resize must be one of {[m.value for m in ResizeMode]}")


async def _save_upload(file: UploadFile, suffix: str) -> Path:
    data = await file.read()
    if not data:
        raise HTTPException(422, "empty upload")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=tempfile.gettempdir())
    tmp.write(data)
    tmp.close()
    return Path(tmp.name)


@app.post("/image")
async def image(
    file: Optional[UploadFile] = File(None),
    path: Optional[str] = None,
    resize: str = "fit",
    palletize: bool = False,
):
    resize_mode = _resize_mode(resize)
    if file is not None:
        src = await _save_upload(file, Path(file.filename or "img.png").suffix or ".png")
    elif path:
        src = Path(path).expanduser()
        if not src.exists():
            raise HTTPException(404, f"no such file: {src}")
    else:
        raise HTTPException(422, "provide a multipart 'file' or a 'path' query param")
    claim_display("image", file.filename if file else src.name)
    try:
        from PIL import ImageOps
        from idotmatrix.util import image_utils

        with Image.open(src) as img:
            img = ImageOps.exif_transpose(img)
            resample = Image.Resampling.NEAREST if palletize else Image.Resampling.LANCZOS
            img = image_utils.resize_image(
                image=img, canvas_size=SIZE, resize_mode=resize_mode,
                resample_mode=resample, mode="RGB",
            )
            if palletize:
                img = image_utils.palettize(img)
            await _push_pil_image(img)
    finally:
        if file is not None:
            src.unlink(missing_ok=True)
    return {"image": file.filename if file else str(src), "resize": resize}


@app.post("/gif")
async def gif(
    file: Optional[UploadFile] = File(None),
    path: Optional[str] = None,
    resize: str = "fit",
    frame_ms: Optional[int] = None,
):
    resize_mode = _resize_mode(resize)
    if file is not None:
        src = await _save_upload(file, ".gif")
    elif path:
        src = Path(path).expanduser()
        if not src.exists():
            raise HTTPException(404, f"no such file: {src}")
    else:
        raise HTTPException(422, "provide a multipart 'file' or a 'path' query param")
    claim_display("gif", file.filename if file else src.name)
    try:
        await run_on_device(
            lambda: client.gif.upload_gif_file(src, resize_mode=resize_mode, duration_per_frame_in_ms=frame_ms),
            timeout=300,
        )
    finally:
        if file is not None:
            src.unlink(missing_ok=True)
    return {"gif": file.filename if file else str(src), "resize": resize}


# ---------------------------------------------------------------- BART mode

_bart = {"mode": None, "task": None, "interval": 60}


async def _get_bart_mode():
    if _bart["mode"] is None:
        from bartmap import BartMap

        # first init downloads/parses GTFS — keep it off the event loop
        _bart["mode"] = await asyncio.to_thread(BartMap)
    return _bart["mode"]


async def _bart_loop():
    mode = _bart["mode"]
    while True:
        try:
            frame = await mode.compute_frame()
            await _push_pil_image(frame, timeout=120)
            log.info("bart frame pushed: %d trains", mode.last_train_count)
            delay = _bart["interval"]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            mode.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("bart loop iteration failed: %s", mode.last_error)
            delay = max(30, _bart["interval"] // 2)
        await asyncio.sleep(delay)


@app.on_event("startup")
async def _bart_autostart():
    if os.environ.get("IDM_BART_AUTOSTART") == "1":
        log.info("IDM_BART_AUTOSTART=1 — starting BART live map")
        await bart_start(BartStartBody())


class BartStartBody(BaseModel):
    interval_s: int = 60


@app.post("/bart/start")
async def bart_start(body: BartStartBody = Body(default=BartStartBody())):
    if not 15 <= body.interval_s <= 3600:
        raise HTTPException(422, "interval_s must be 15-3600")
    await _get_bart_mode()
    _bart["interval"] = body.interval_s
    claim_display("bart", f"every {body.interval_s}s")
    if _bart["task"] is None or _bart["task"].done():
        _bart["task"] = asyncio.create_task(_bart_loop())
    return await bart_status()


@app.post("/bart/stop")
async def bart_stop():
    task = _bart["task"]
    if task is not None and not task.done():
        task.cancel()
    _bart["task"] = None
    if display["mode"] == "bart":
        display["mode"] = display["detail"] = None
    return {"running": False}


@app.get("/bart/status")
async def bart_status():
    mode = _bart["mode"]
    running = _bart["task"] is not None and not _bart["task"].done()
    return {
        "running": running,
        "interval_s": _bart["interval"],
        "trains": mode.last_train_count if mode else None,
        "last_update": mode.last_update if mode else None,
        "last_error": mode.last_error if mode else None,
    }


@app.get("/bart/frame.png")
async def bart_frame_png():
    mode = await _get_bart_mode()
    frame = mode.last_frame
    if frame is None:
        frame = await mode.compute_frame()
    big = frame.resize((SIZE * 6, SIZE * 6), resample=Image.NEAREST)
    buf = io.BytesIO()
    big.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- planes mode

_planes = {"mode": None, "task": None, "interval": 30, "rotate": 8}
PLANES_CONFIG_FILE = BASE_DIR / ".planes_config"


def _planes_defaults() -> dict:
    cfg = {
        "lat": float(os.environ.get("IDM_LAT", "37.7749")),
        "lon": float(os.environ.get("IDM_LON", "-122.4194")),
        "radius_nm": 12.0,
        "interval_s": 30,
        "rotate_s": 8,
    }
    if PLANES_CONFIG_FILE.exists():
        try:
            import json

            cfg.update(json.loads(PLANES_CONFIG_FILE.read_text()))
        except Exception:
            log.warning("ignoring unreadable %s", PLANES_CONFIG_FILE)
    return cfg


class PlanesStartBody(BaseModel):
    lat: Optional[float] = None
    lon: Optional[float] = None
    radius_nm: Optional[float] = None
    interval_s: Optional[int] = None   # data refresh
    rotate_s: Optional[int] = None     # featured-plane rotation / push cadence


async def _planes_loop():
    mode = _planes["mode"]
    last_fetch = 0.0
    featured = -1
    idle = False  # "NO TRAFFIC" already on the panel
    while True:
        try:
            if time.time() - last_fetch >= _planes["interval"]:
                await mode.refresh()
                last_fetch = time.time()
            if mode.planes:
                featured += 1
                idle = False
                frame = mode.render(featured)
                await _push_pil_image(frame, timeout=120)
                log.info("planes frame pushed: %s (%d overhead)", mode.last_shown, len(mode.planes))
            elif not idle:
                await _push_pil_image(mode.render(0), timeout=120)
                idle = True
                log.info("planes frame pushed: no traffic")
            else:
                mode.render(0)  # keep the preview fresh
            delay = _planes["rotate"]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            mode.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("planes loop iteration failed: %s", mode.last_error)
            delay = 5
        await asyncio.sleep(delay)


@app.post("/planes/start")
async def planes_start(body: PlanesStartBody = Body(default=PlanesStartBody())):
    from planes import PlanesOverhead

    cfg = _planes_defaults()
    for key in cfg:
        value = getattr(body, key)
        if value is not None:
            cfg[key] = value
    if not 1 <= cfg["radius_nm"] <= 250:
        raise HTTPException(422, "radius_nm must be 1-250")
    if not 10 <= cfg["interval_s"] <= 3600 or not 3 <= cfg["rotate_s"] <= 3600:
        raise HTTPException(422, "interval_s must be 10-3600, rotate_s 3-3600")

    import json

    PLANES_CONFIG_FILE.write_text(json.dumps(cfg))
    task = _planes["task"]
    if task is not None and not task.done():
        task.cancel()
    _planes["mode"] = PlanesOverhead(cfg["lat"], cfg["lon"], cfg["radius_nm"])
    _planes["interval"] = cfg["interval_s"]
    _planes["rotate"] = cfg["rotate_s"]
    claim_display("planes", f"r{cfg['radius_nm']:.0f}nm every {cfg['rotate_s']}s")
    _planes["task"] = asyncio.create_task(_planes_loop())
    return await planes_status()


@app.post("/planes/stop")
async def planes_stop():
    task = _planes["task"]
    if task is not None and not task.done():
        task.cancel()
    _planes["task"] = None
    if display["mode"] == "planes":
        display["mode"] = display["detail"] = None
    return {"running": False}


@app.get("/planes/status")
async def planes_status():
    mode = _planes["mode"]
    running = _planes["task"] is not None and not _planes["task"].done()
    return {
        "running": running,
        "config": _planes_defaults(),
        "planes": len(mode.planes) if mode else None,
        "featured": mode.last_shown if mode else None,
        "last_update": mode.last_update if mode else None,
        "last_error": mode.last_error if mode else None,
    }


@app.get("/planes/frame.png")
async def planes_frame_png():
    mode = _planes["mode"]
    if mode is None or mode.last_frame is None:
        raise HTTPException(404, "planes mode has not rendered yet")
    big = mode.last_frame.resize((SIZE * 6, SIZE * 6), resample=Image.NEAREST)
    buf = io.BytesIO()
    big.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.on_event("startup")
async def _planes_autostart():
    if os.environ.get("IDM_PLANES_AUTOSTART") == "1":
        log.info("IDM_PLANES_AUTOSTART=1 — starting planes overhead")
        await planes_start(PlanesStartBody())


# ---------------------------------------------------------------- photos mode

_photos = {"mode": None, "task": None, "rotate": 30}
PHOTOS_CONFIG_FILE = BASE_DIR / ".photos_config"


def _photos_defaults() -> dict:
    cfg = {"dir": "~/Pictures/instagram", "rotate_s": 30, "colors": 0}
    if PHOTOS_CONFIG_FILE.exists():
        try:
            import json

            cfg.update(json.loads(PHOTOS_CONFIG_FILE.read_text()))
        except Exception:
            log.warning("ignoring unreadable %s", PHOTOS_CONFIG_FILE)
    return cfg


class PhotosStartBody(BaseModel):
    dir: Optional[str] = None
    rotate_s: Optional[int] = None
    colors: Optional[int] = None  # 0 = full color, else quantize palette


async def _photos_push_next():
    mode = _photos["mode"]
    frame = await asyncio.to_thread(mode.next_frame)
    await _push_pil_image(frame, timeout=120)
    log.info("photo pushed: %s (%d in library)", mode.last_shown, mode.total)


async def _photos_loop():
    mode = _photos["mode"]
    while True:
        try:
            await _photos_push_next()
            delay = _photos["rotate"]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            mode.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("photos loop iteration failed: %s", mode.last_error)
            delay = 15
        await asyncio.sleep(delay)


@app.post("/photos/start")
async def photos_start(body: PhotosStartBody = Body(default=PhotosStartBody())):
    from photos import PhotoShow

    cfg = _photos_defaults()
    for key in cfg:
        value = getattr(body, key)
        if value is not None:
            cfg[key] = value
    if not 5 <= cfg["rotate_s"] <= 86400:
        raise HTTPException(422, "rotate_s must be 5-86400")
    if not 0 <= cfg["colors"] <= 256:
        raise HTTPException(422, "colors must be 0 (full) or 2-256")
    folder = Path(cfg["dir"]).expanduser()
    if not folder.is_dir():
        raise HTTPException(404, f"no such folder: {folder}")

    import json

    PHOTOS_CONFIG_FILE.write_text(json.dumps(cfg))
    task = _photos["task"]
    if task is not None and not task.done():
        task.cancel()
    _photos["mode"] = PhotoShow(cfg["dir"], cfg["colors"])
    _photos["rotate"] = cfg["rotate_s"]
    claim_display("photos", f"every {cfg['rotate_s']}s")
    _photos["task"] = asyncio.create_task(_photos_loop())
    return await photos_status()


@app.post("/photos/next")
async def photos_next():
    if _photos["mode"] is None or _photos["task"] is None or _photos["task"].done():
        raise HTTPException(409, "photos mode is not running")
    await _photos_push_next()
    return await photos_status()


@app.post("/photos/stop")
async def photos_stop():
    task = _photos["task"]
    if task is not None and not task.done():
        task.cancel()
    _photos["task"] = None
    if display["mode"] == "photos":
        display["mode"] = display["detail"] = None
    return {"running": False}


@app.get("/photos/status")
async def photos_status():
    mode = _photos["mode"]
    running = _photos["task"] is not None and not _photos["task"].done()
    return {
        "running": running,
        "config": _photos_defaults(),
        "library": mode.total if mode else None,
        "showing": mode.last_shown if mode else None,
        "last_update": mode.last_update if mode else None,
        "last_error": mode.last_error if mode else None,
    }


@app.get("/photos/frame.png")
async def photos_frame_png():
    mode = _photos["mode"]
    if mode is None or mode.last_frame is None:
        raise HTTPException(404, "photos mode has not rendered yet")
    big = mode.last_frame.resize((SIZE * 6, SIZE * 6), resample=Image.NEAREST)
    buf = io.BytesIO()
    big.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.on_event("startup")
async def _photos_autostart():
    if os.environ.get("IDM_PHOTOS_AUTOSTART") == "1":
        log.info("IDM_PHOTOS_AUTOSTART=1 — starting photo slideshow")
        await photos_start(PhotosStartBody())


# ---------------------------------------------------------------- spotify mode

_spotify = {"mode": None, "task": None, "poll": 3, "repush": 5}


class SpotifyStartBody(BaseModel):
    poll_s: Optional[int] = None    # how often to ask Spotify for state
    repush_s: Optional[int] = None  # progress-bar refresh while playing


async def _spotify_loop():
    mode = _spotify["mode"]
    last_push = 0.0
    while True:
        try:
            changed = await asyncio.to_thread(mode.poll)
            # The panel occasionally swallows an upload (BLE transfer succeeds
            # but it keeps showing the previous GIF), so never push a state
            # exactly once: playing refreshes the progress bar every repush_s,
            # paused/idle re-push slowly so a dropped frame self-heals.
            interval = {"playing": _spotify["repush"], "paused": 20}.get(mode.state, 60)
            if changed or time.time() - last_push >= interval:
                frame = await asyncio.to_thread(mode.render)
                await _push_pil_image(frame, timeout=120)
                last_push = time.time()
                log.info("spotify frame pushed: %s — %s (%s)",
                         mode.track or "-", mode.artist or "-", mode.state)
            delay = _spotify["poll"]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            mode.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("spotify loop iteration failed: %s", mode.last_error)
            delay = 10
        await asyncio.sleep(delay)


@app.post("/spotify/start")
async def spotify_start(body: SpotifyStartBody = Body(default=SpotifyStartBody())):
    from nowplaying import NowPlaying

    poll_s = body.poll_s if body.poll_s is not None else 3
    repush_s = body.repush_s if body.repush_s is not None else 5
    if not 1 <= poll_s <= 60 or not 5 <= repush_s <= 300:
        raise HTTPException(422, "poll_s must be 1-60, repush_s 5-300")
    task = _spotify["task"]
    if task is not None and not task.done():
        task.cancel()
    _spotify["mode"] = NowPlaying()
    _spotify["poll"] = poll_s
    _spotify["repush"] = repush_s
    claim_display("spotify")
    _spotify["task"] = asyncio.create_task(_spotify_loop())
    return await spotify_status()


@app.post("/spotify/stop")
async def spotify_stop():
    task = _spotify["task"]
    if task is not None and not task.done():
        task.cancel()
    _spotify["task"] = None
    if display["mode"] == "spotify":
        display["mode"] = display["detail"] = None
    return {"running": False}


@app.get("/spotify/status")
async def spotify_status():
    mode = _spotify["mode"]
    running = _spotify["task"] is not None and not _spotify["task"].done()
    return {
        "running": running,
        "state": mode.state if mode else None,
        "track": mode.track if mode else None,
        "artist": mode.artist if mode else None,
        "position": mode.position if mode else None,
        "duration": mode.duration if mode else None,
        "last_update": mode.last_update if mode else None,
        "last_error": mode.last_error if mode else None,
    }


@app.get("/spotify/frame.png")
async def spotify_frame_png():
    mode = _spotify["mode"]
    if mode is None or mode.last_frame is None:
        raise HTTPException(404, "spotify mode has not rendered yet")
    big = mode.last_frame.resize((SIZE * 6, SIZE * 6), resample=Image.NEAREST)
    buf = io.BytesIO()
    big.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.on_event("startup")
async def _spotify_autostart():
    if os.environ.get("IDM_SPOTIFY_AUTOSTART") == "1":
        log.info("IDM_SPOTIFY_AUTOSTART=1 — starting now-playing mode")
        await spotify_start(SpotifyStartBody())


# ---------------------------------------------------------------- saver mode

_saver = {"mode": None, "task": None, "cycle": 240}


class SaverStartBody(BaseModel):
    style: Optional[str] = None     # pipes | life | mystify
    cycle_s: Optional[int] = None   # new animation every N seconds
    frames: Optional[int] = None
    frame_ms: Optional[int] = None


async def _saver_loop():
    mode = _saver["mode"]
    while True:
        try:
            gif = await asyncio.to_thread(mode.generate)
            await run_on_device(lambda: client.gif.upload_gif_data(gif), timeout=300)
            log.info("saver animation pushed: %s (%.1f KB)", mode.style, len(gif) / 1024)
            delay = _saver["cycle"]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            mode.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("saver loop iteration failed: %s", mode.last_error)
            delay = 30
        await asyncio.sleep(delay)


@app.post("/saver/start")
async def saver_start(body: SaverStartBody = Body(default=SaverStartBody())):
    from saver import STYLES, Saver

    style = body.style or "snake"
    cycle_s = body.cycle_s if body.cycle_s is not None else 240
    frames = body.frames if body.frames is not None else 40
    frame_ms = body.frame_ms if body.frame_ms is not None else 150
    if style not in STYLES:
        raise HTTPException(422, f"style must be one of {STYLES}")
    if not 30 <= cycle_s <= 86400 or not 4 <= frames <= 64 or not 40 <= frame_ms <= 2000:
        raise HTTPException(422, "cycle_s 30-86400, frames 4-64, frame_ms 40-2000")
    task = _saver["task"]
    if task is not None and not task.done():
        task.cancel()
    _saver["mode"] = Saver(style, frames, frame_ms)
    _saver["cycle"] = cycle_s
    claim_display("saver", style)
    _saver["task"] = asyncio.create_task(_saver_loop())
    return await saver_status()


@app.post("/saver/stop")
async def saver_stop():
    task = _saver["task"]
    if task is not None and not task.done():
        task.cancel()
    _saver["task"] = None
    if display["mode"] == "saver":
        display["mode"] = display["detail"] = None
    return {"running": False}


@app.get("/saver/status")
async def saver_status():
    mode = _saver["mode"]
    running = _saver["task"] is not None and not _saver["task"].done()
    return {
        "running": running,
        "style": mode.style if mode else None,
        "cycle_s": _saver["cycle"],
        "last_update": mode.last_update if mode else None,
        "last_error": mode.last_error if mode else None,
    }


@app.get("/saver/preview.gif")
async def saver_preview_gif():
    mode = _saver["mode"]
    if mode is None or mode.last_gif is None:
        raise HTTPException(404, "saver mode has not generated yet")
    return Response(mode.last_gif, media_type="image/gif",
                    headers={"Cache-Control": "no-store"})


@app.on_event("startup")
async def _saver_autostart():
    if os.environ.get("IDM_SAVER_AUTOSTART") == "1":
        log.info("IDM_SAVER_AUTOSTART=1 — starting screensaver mode")
        await saver_start(SaverStartBody(style=os.environ.get("IDM_SAVER_STYLE")))


# ---------------------------------------------------------------- UI

@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "static" / "index.html")
