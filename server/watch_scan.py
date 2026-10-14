"""Continuous BLE watch: flag devices that START advertising mid-scan.

Run it, then power-cycle the panel ~15s in. Whatever appears = the panel,
whatever its name is.
"""
import asyncio
import time

from bleak import BleakScanner

seen: dict[str, float] = {}
start = time.monotonic()


def on_adv(device, adv):
    t = time.monotonic() - start
    if device.address not in seen:
        seen[device.address] = t
        name = adv.local_name or device.name or "(no name)"
        mfr = ",".join(hex(k) for k in (adv.manufacturer_data or {}))
        flag = "  <<< NEW MID-SCAN" if t > 15 else ""
        print(f"[{t:6.1f}s] {adv.rssi:>4}dBm  {name:<28} {device.address}  mfr={mfr or '-'}{flag}", flush=True)


async def main():
    scanner = BleakScanner(detection_callback=on_adv)
    await scanner.start()
    print("watching for 120s — power-cycle the panel now...", flush=True)
    await asyncio.sleep(120)
    await scanner.stop()
    late = {a: t for a, t in seen.items() if t > 15}
    print(f"\ndone. {len(seen)} devices total, {len(late)} appeared after the 15s baseline.", flush=True)


asyncio.run(main())
