"""
HeatGuard Naic — Rolling Weather Data Updater
Runs inside GitHub Actions every 5 minutes.

Fetches the weather station API, parses the nested Ecowitt payload,
appends a new { ts, hi, wbgt } reading to data/rolling_data.json,
and trims anything older than 24 hours.

The GITHUB_TOKEN used to commit is the automatic Actions token —
it is never stored in code or shared anywhere.
"""

import json
import math
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ── Config ───────────────────────────────────────────────────────────────────
STATION_URL  = os.environ["STATION_URL"]
OUTPUT_FILE  = Path("data/rolling_data.json")
WINDOW_HOURS = 24   # keep last 24 hours of readings


# ── Helpers ───────────────────────────────────────────────────────────────────
def f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9

def val(obj):
    """Parse a nested Ecowitt {value, unit} dict to float, or None."""
    if obj and obj.get("value") not in (None, ""):
        try:
            return float(obj["value"])
        except (ValueError, TypeError):
            return None
    return None

def heat_index_rothfusz(temp_c: float, rh: float) -> float:
    """NOAA Rothfusz Heat Index formula. Input °C + %, output °C."""
    tf = temp_c * 9 / 5 + 32
    if tf < 80:
        return temp_c
    hi = (-42.379
          + 2.04901523 * tf
          + 10.14333127 * rh
          - 0.22475541 * tf * rh
          - 0.00683783 * tf * tf
          - 0.05481717 * rh * rh
          + 0.00122874 * tf * tf * rh
          + 0.00085282 * tf * rh * rh
          - 0.00000199 * tf * tf * rh * rh)
    return (hi - 32) * 5 / 9

def wbgt_liljegren(temp_c: float, rh: float) -> float:
    """Liljegren simplified WBGT approximation (used only if sensor unavailable)."""
    vp = (rh / 100) * 6.105 * math.exp(17.27 * temp_c / (237.3 + temp_c))
    return 0.567 * temp_c + 0.393 * vp + 3.94


# ── Fetch station data ────────────────────────────────────────────────────────
print("[HeatGuard] Fetching station data…")
try:
    resp = requests.get(STATION_URL, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
except Exception as e:
    print(f"[HeatGuard] ERROR fetching station: {e}", file=sys.stderr)
    sys.exit(1)

row = payload[-1] if isinstance(payload, list) else payload

try:
    data    = row["raw_data"]["data"]
    outdoor = data["outdoor"]
    bgt     = data.get("black_globe_temperature", {})
except (KeyError, TypeError) as e:
    print(f"[HeatGuard] ERROR parsing payload structure: {e}", file=sys.stderr)
    print(f"  Top-level keys: {list(row.keys())}", file=sys.stderr)
    sys.exit(1)

temp_f    = val(outdoor.get("temperature"))
humidity  = val(outdoor.get("humidity"))
feels_f   = val(outdoor.get("feels_like")) or val(outdoor.get("app_temp"))
wbgt_f    = val(bgt.get("wbgt"))

if temp_f is None or humidity is None:
    print("[HeatGuard] ERROR: outdoor.temperature or outdoor.humidity missing", file=sys.stderr)
    sys.exit(1)

temp_c = f_to_c(temp_f)
hi_c   = f_to_c(feels_f) if feels_f is not None else heat_index_rothfusz(temp_c, humidity)
wbgt_c = f_to_c(wbgt_f)  if wbgt_f  is not None else wbgt_liljegren(temp_c, humidity)
wbgt_src = "Black Globe sensor" if wbgt_f is not None else "estimated"

print(f"[HeatGuard] Parsed — Temp: {temp_c:.1f}°C | RH: {humidity:.0f}% "
      f"| HI: {hi_c:.1f}°C | WBGT: {wbgt_c:.1f}°C ({wbgt_src})")


# ── Load existing rolling data ────────────────────────────────────────────────
OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

if OUTPUT_FILE.exists():
    try:
        readings = json.loads(OUTPUT_FILE.read_text())
        if not isinstance(readings, list):
            readings = []
    except json.JSONDecodeError:
        readings = []
else:
    readings = []

print(f"[HeatGuard] Existing readings in pool: {len(readings)}")


# ── Append new reading ────────────────────────────────────────────────────────
now = datetime.now(timezone.utc)
readings.append({
    "ts":   now.isoformat().replace("+00:00", "Z"),
    "hi":   round(hi_c,   2),
    "wbgt": round(wbgt_c, 2),
    "temp": round(temp_c, 2),
    "rh":   round(humidity, 1),
})


# ── Trim to 24-hour window ────────────────────────────────────────────────────
cutoff = now - timedelta(hours=WINDOW_HOURS)
before = len(readings)
readings = [
    r for r in readings
    if datetime.fromisoformat(r["ts"].replace("Z", "+00:00")) >= cutoff
]
trimmed = before - len(readings)

avg_hi   = sum(r["hi"]   for r in readings) / len(readings)
avg_wbgt = sum(r["wbgt"] for r in readings) / len(readings)

print(f"[HeatGuard] Pool after trim: {len(readings)} readings "
      f"(removed {trimmed} older than {WINDOW_HOURS}h)")
print(f"[HeatGuard] 24h avg — HI: {avg_hi:.1f}°C | WBGT: {avg_wbgt:.1f}°C")


# ── Write updated file ────────────────────────────────────────────────────────
OUTPUT_FILE.write_text(json.dumps(readings, indent=2))
print(f"[HeatGuard] Wrote {len(readings)} readings to {OUTPUT_FILE}")
