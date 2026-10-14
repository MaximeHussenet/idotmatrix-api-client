# Local HTTP API + web control panel

FastAPI server wrapping this library so anything that speaks HTTP (curl,
scripts, Shortcuts, Home Assistant, …) can drive an iDotMatrix panel —
no iOS app needed.

## Run

```bash
# one-time setup, from the repo root
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e . fastapi 'uvicorn[standard]' pillow python-multipart

./run.sh                 # http://127.0.0.1:8588  (web UI at /)
IDM_PORT=9000 ./run.sh   # custom port
```

The library is installed editable (`-e .`), so library changes in this repo
apply on the next server restart.

Panel size defaults to 64x64; set `IDM_SIZE=16|32` for other panels. The last
connected address is persisted in `server/.device_address` (`IDM_ADDRESS` env
var overrides).

## macOS notes

- First BLE use triggers a Bluetooth permission prompt for your terminal app —
  allow it, then restart the server (a denial is cached per-process). If the
  prompt never shows: System Settings → Privacy & Security → Bluetooth.
- Device "addresses" are CoreBluetooth UUIDs, not MACs.
- The panel accepts ONE BLE connection: force-quit the iDotMatrix iOS app
  before connecting, and expect scans to miss it while anything is connected.
- Panel BLE TX is weak (~-77 dBm at 2 m observed on a 64x64 unit) — keep it
  in the same room as the machine running this server.
- `server/watch_scan.py` is a 120s continuous diff-scan that flags devices
  appearing mid-scan — power-cycle the panel while it runs to find a panel
  regardless of its advertised name.

## API

Interactive docs: `http://127.0.0.1:8588/docs` (FastAPI/OpenAPI).

```bash
curl -X POST localhost:8588/scan                                   # find IDM-* devices
curl -X POST localhost:8588/connect -d '{"address":"<uuid>"}' -H 'Content-Type: application/json'
curl -X POST localhost:8588/text -d '{"text":"Hello!","color":"#00ff88","mode":"MARQUEE"}' -H 'Content-Type: application/json'
curl -X POST localhost:8588/color -d '{"color":"#2266ff"}' -H 'Content-Type: application/json'
curl -X POST localhost:8588/brightness -d '{"percent":60}' -H 'Content-Type: application/json'
curl -X POST localhost:8588/clock -d '{"style":"Color","color":"#ffffff"}' -H 'Content-Type: application/json'
curl -X POST 'localhost:8588/image?resize=fit' -F file=@pic.png    # auto-scales
curl -X POST 'localhost:8588/gif?resize=fill'  -F file=@anim.gif
curl -X POST 'localhost:8588/image?path=/Users/me/pic.png'         # local path, no upload
curl -X POST localhost:8588/screen -d '{"on":false}' -H 'Content-Type: application/json'
curl -X POST localhost:8588/pixels -d '{"color":"#ff0000","pixels":[[0,0],[1,1]]}' -H 'Content-Type: application/json'
```

`/canvas` takes a flat row-major list of 4096 colors (hex or `[r,g,b]`) and
pushes a full-color DIY frame — that's what the web UI's pixel canvas uses,
and the easiest way to render arbitrary frames programmatically.

Enum values for text modes / clock styles / effects: `GET /enums`.

## Keep it running (optional)

```bash
cat > ~/Library/LaunchAgents/io.local.idotmatrix.plist <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>io.local.idotmatrix</string>
  <key>ProgramArguments</key><array><string>/Users/maximehussenet/automation/idotmatrix-api-client/run.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
EOF
launchctl load ~/Library/LaunchAgents/io.local.idotmatrix.plist
```

The launchd-run process needs Bluetooth permission too (System Settings →
Privacy & Security → Bluetooth).

## Gotchas

- BLE is single-channel: the server serializes all device ops behind a lock;
  concurrent HTTP calls queue up rather than corrupt uploads.
- Large/many-frame GIF uploads are slow over BLE (tens of seconds) — the
  endpoint allows up to 5 min.
- The panel stores uploaded DIY images/GIFs; `/reset` clears weird states.
