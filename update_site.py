#!/usr/bin/env python3
"""
Refresh Mississippi Lake cottager briefing for GitHub Pages.

Fetches WSC + KiWIS gauges, rebuilds projection + chart, writes index.html.
"""

from __future__ import annotations

import hashlib
import json
import math
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo


def _asset_v(path: Path) -> str:
    """Short content hash so browsers fetch updated chart PNGs."""
    if not path.exists():
        return "0"
    return hashlib.md5(path.read_bytes()).hexdigest()[:8]

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CHART_PNG = ROOT / "chart.png"
YTD_CHART_PNG = ROOT / "ytd_chart.png"
WATERSHED_PNG = ROOT / "watershed_profile.png"
WATERSHED_FULL_PNG = ROOT / "watershed_full_profile.png"
INDEX = ROOT / "index.html"
CACHE = DATA / "chart_series.json"
HISTORIC_DOY = DATA / "historic_doy_means.json"

AREA_M2 = 25e6  # Mississippi Lake ~25 km²
CM_PER_M3S_DAY = 86400 / AREA_M2 * 100
LAKE_TS = 1404042
CROTCH_TS = 16468042  # Crotch GOES (MVCA20) — more reliable than main dam gauge
DALHOUSIE_TS = 54708042  # Dalhousie outlet Public stage (MASL)
EARLY_JULY_LEVEL = 134.10

# Full-system lake stage series (KiWIS Stage CGVD28, typically 102.Edited)
FULL_LEVEL_TS: dict[str, tuple[str, int]] = {
    "shabomeka": ("Shabomeka Lake", 1395042),
    "mazinaw": ("Mazinaw Lake", 1399042),
    "kashwakamak": ("Kashwakamak Lake", 1358042),
    "mississagagon": ("Mississagagon Lake", 2511042),
    "big_gull": ("Big Gull Lake", 1400042),
    "pine": ("Pine Lake", 49830042),
    "malcolm": ("Malcolm Lake", 49818042),
    "farm": ("Farm Lake", 32304042),
    "crotch": ("Crotch Lake", CROTCH_TS),
    "palmerston": ("Palmerston Lake", 1402042),
    "canonto": ("Canonto Lake", 36589042),
    "summit": ("Summit Lake", 52426042),
    "mosque": ("Mosque Lake", 39481042),
    "stump": ("Stump Lake", 39476042),
    "widow": ("Widow Lake", 37160042),
    "dalhousie": ("Dalhousie Lake", DALHOUSIE_TS),
    "clayton": ("Clayton Lake", 91719042),
    "lanark": ("Lanark", 80686042),
    "mississippi": ("Mississippi Lake", LAKE_TS),
    "carleton": ("Carleton Place", 1405042),
}
FULL_FLOW_WSC: dict[str, tuple[str, str]] = {
    "marble": ("Marble Lake outflow", "02KF016"),
    "dalhousie_out": ("Dalhousie outlet", "02KF019"),
    "ferguson": ("Ferguson’s Falls", "02KF001"),
    "appleton": ("Appleton", "02KF006"),
    "galetta": ("Galetta", "02KF002"),
}

ctx = ssl.create_default_context()
try:
    import certifi

    ctx = ssl.create_default_context(cafile=certifi.where())
except Exception:
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE


def fetch_json(url: str, *, attempts: int = 4, timeout: float = 45) -> dict | list:
    """GET JSON with retries — WSC / KiWIS are occasionally slow or unreachable from Actions."""
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "mississippi-lake-levels/1.0 (+https://github.com/colinpjack/mississippi-lake-levels)",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return json.load(resp)
        except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError) as e:
            last_err = e
            if i + 1 >= attempts:
                break
            wait = min(30, 2**i)
            print(f"  fetch retry {i + 1}/{attempts} after {type(e).__name__}: {e}; sleep {wait}s")
            time.sleep(wait)
    raise urllib.error.URLError(f"Failed after {attempts} attempts: {last_err}")


def _parse_obs_time(raw: str) -> datetime | None:
    """Parse gauge timestamps into timezone-aware UTC datetimes."""
    s = (raw or "").strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            # KiWIS lake stamps are Eastern local without always sending offset
            dt = dt.replace(tzinfo=ZoneInfo("America/Toronto"))
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def fetch_wsc_daily(stn: str, start: str, end: str) -> tuple[dict[str, float], datetime | None]:
    url = (
        "https://api.weather.gc.ca/collections/hydrometric-realtime/items"
        f"?STATION_NUMBER={stn}&datetime={start}/{end}&limit=10000&f=json"
    )
    by: dict[str, list[float]] = defaultdict(list)
    latest: datetime | None = None
    for f in fetch_json(url).get("features", []):
        p = f["properties"]
        if p.get("DISCHARGE") is not None:
            by[p["DATETIME"][:10]].append(float(p["DISCHARGE"]))
            ts = _parse_obs_time(p.get("DATETIME", ""))
            if ts is not None and (latest is None or ts > latest):
                latest = ts
    return {d: mean(v) for d, v in sorted(by.items())}, latest


def fetch_lake_daily(
    frm: str, to: str, *, ts_id: int = LAKE_TS
) -> tuple[dict[str, float], datetime | None]:
    params = {
        "service": "kisters",
        "type": "queryServices",
        "request": "getTimeseriesValues",
        "datasource": "0",
        "format": "json",
        "ts_id": str(ts_id),
        "from": frm,
        "to": to,
    }
    url = "https://waterdata.quinteconservation.ca/KiWIS/KiWIS?" + urllib.parse.urlencode(params)
    data = fetch_json(url)
    by: dict[str, list[float]] = defaultdict(list)
    latest: datetime | None = None
    if isinstance(data, list) and data:
        for row in data[0].get("data") or []:
            if isinstance(row, list) and len(row) >= 2 and row[1] is not None:
                by[str(row[0])[:10]].append(float(row[1]))
                ts = _parse_obs_time(str(row[0]))
                if ts is not None and (latest is None or ts > latest):
                    latest = ts
    return {d: mean(v) for d, v in sorted(by.items())}, latest


def _series_trend(daily: dict[str, float], *, days: int = 7) -> tuple[float | None, float | None]:
    """Return (latest, change over ~days) from a daily mean series."""
    if not daily:
        return None, None
    keys = sorted(daily)
    latest = daily[keys[-1]]
    target = (date.fromisoformat(keys[-1]) - timedelta(days=days)).isoformat()
    prior_keys = [k for k in keys if k <= target]
    if not prior_keys:
        prior_keys = keys[:1]
    prior = daily[prior_keys[-1]]
    return latest, latest - prior


def fetch_watershed_core(frm: str, to: str, ff: dict[str, float], ap: dict[str, float], lake: dict[str, float]) -> dict:
    """Core chain snapshots + ~7-day trends for the profile diagram."""
    nodes: dict[str, dict] = {}

    def add_level(key: str, label: str, daily: dict[str, float], ts: datetime | None) -> None:
        latest, delta = _series_trend(daily)
        nodes[key] = {
            "label": label,
            "kind": "level",
            "value": latest,
            "delta_7d": delta * 100 if delta is not None else None,  # cm
            "unit": "m",
            "as_of_iso": ts.isoformat() if ts else "",
        }

    def add_flow(key: str, label: str, daily: dict[str, float], ts: datetime | None) -> None:
        latest, delta = _series_trend(daily)
        nodes[key] = {
            "label": label,
            "kind": "flow",
            "value": latest,
            "delta_7d": delta,  # m³/s
            "unit": "m3s",
            "as_of_iso": ts.isoformat() if ts else "",
        }

    try:
        crotch, crotch_ts = fetch_lake_daily(frm, to, ts_id=CROTCH_TS)
        add_level("crotch", "Crotch Lake", crotch, crotch_ts)
    except Exception as e:
        print(f"  Crotch Lake fetch failed: {e}")
        nodes["crotch"] = {"label": "Crotch Lake", "kind": "level", "value": None, "delta_7d": None, "unit": "m", "as_of_iso": ""}

    try:
        dal, dal_ts = fetch_lake_daily(frm, to, ts_id=DALHOUSIE_TS)
        add_level("dalhousie", "Dalhousie Lake", dal, dal_ts)
    except Exception as e:
        print(f"  Dalhousie Lake fetch failed: {e}")
        nodes["dalhousie"] = {"label": "Dalhousie Lake", "kind": "level", "value": None, "delta_7d": None, "unit": "m", "as_of_iso": ""}

    add_level("mississippi", "Mississippi Lake", lake, None)
    add_flow("ferguson", "Ferguson’s Falls", ff, None)
    add_flow("appleton", "Appleton", ap, None)
    return {"watershed": nodes}


def fetch_watershed_full(
    frm: str,
    to: str,
    *,
    ff: dict[str, float],
    ap: dict[str, float],
    lake: dict[str, float],
    core: dict[str, dict] | None = None,
) -> dict:
    """Full Mississippi system snapshots for the extended profile diagram."""
    nodes: dict[str, dict] = {}
    core = core or {}
    frm_long = (date.fromisoformat(frm[:10]) - timedelta(days=20)).isoformat()

    def add_level(key: str, label: str, daily: dict[str, float], ts: datetime | None) -> None:
        latest, delta = _series_trend(daily)
        nodes[key] = {
            "label": label,
            "kind": "level",
            "value": latest,
            "delta_7d": delta * 100 if delta is not None else None,
            "unit": "m",
            "as_of_iso": ts.isoformat() if ts else "",
        }

    def add_flow(key: str, label: str, daily: dict[str, float], ts: datetime | None) -> None:
        latest, delta = _series_trend(daily)
        nodes[key] = {
            "label": label,
            "kind": "flow",
            "value": latest,
            "delta_7d": delta,
            "unit": "m3s",
            "as_of_iso": ts.isoformat() if ts else "",
        }

    # Prefer already-fetched core nodes where available
    for key in ("crotch", "dalhousie", "mississippi", "ferguson", "appleton"):
        if key in core:
            nodes[key] = dict(core[key])

    if "mississippi" not in nodes:
        add_level("mississippi", "Mississippi Lake", lake, None)
    if "ferguson" not in nodes:
        add_flow("ferguson", "Ferguson’s Falls", ff, None)
    if "appleton" not in nodes:
        add_flow("appleton", "Appleton", ap, None)

    for key, (label, ts_id) in FULL_LEVEL_TS.items():
        if key in nodes and nodes[key].get("value") is not None:
            continue
        try:
            # Some upper-lake gauges lag; use a longer window for trends/latest.
            daily, ts = fetch_lake_daily(frm_long, to, ts_id=ts_id)
            add_level(key, label, daily, ts)
        except Exception as e:
            print(f"  Full watershed level {label} failed: {e}")
            nodes[key] = {
                "label": label,
                "kind": "level",
                "value": None,
                "delta_7d": None,
                "unit": "m",
                "as_of_iso": "",
            }

    for key, (label, stn) in FULL_FLOW_WSC.items():
        if key in nodes and nodes[key].get("value") is not None:
            continue
        if key == "ferguson":
            add_flow(key, label, ff, None)
            continue
        if key == "appleton":
            add_flow(key, label, ap, None)
            continue
        try:
            daily, ts = fetch_wsc_daily(stn, frm, to)
            add_flow(key, label, daily, ts)
        except Exception as e:
            print(f"  Full watershed flow {label} ({stn}) failed: {e}")
            nodes[key] = {
                "label": label,
                "kind": "flow",
                "value": None,
                "delta_7d": None,
                "unit": "m3s",
                "as_of_iso": "",
            }

    return {"watershed_full": nodes}


def fetch_open_meteo_wed_rain(lat=45.14, lon=-76.15) -> dict[str, float]:
    """Return daily precip mm for next ~7 days; used to scale Wed bump."""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&daily=precipitation_sum&timezone=America%2FToronto&forecast_days=8"
    )
    try:
        d = fetch_json(url)["daily"]
        return {day: float(p) for day, p in zip(d["time"], d["precipitation_sum"])}
    except Exception:
        return {}


def historic_mean_for_date(d: date | str) -> float | None:
    """Look up multi-year mean lake level for this calendar day (MASL)."""
    if isinstance(d, str):
        md = d[5:10] if len(d) >= 10 else d
    else:
        md = f"{d.month:02d}-{d.day:02d}"
    if not HISTORIC_DOY.exists():
        return None
    try:
        means = json.loads(HISTORIC_DOY.read_text()).get("means", {})
        val = means.get(md)
        return float(val) if val is not None else None
    except Exception:
        return None


def historic_monthly_means() -> dict[int, float]:
    """Average of day-of-year historic means for each calendar month (1–12)."""
    if not HISTORIC_DOY.exists():
        return {}
    try:
        means = json.loads(HISTORIC_DOY.read_text()).get("means", {})
    except Exception:
        return {}
    by_month: dict[int, list[float]] = defaultdict(list)
    for md, val in means.items():
        try:
            month = int(md[:2])
            by_month[month].append(float(val))
        except Exception:
            continue
    return {m: mean(vals) for m, vals in by_month.items() if vals}


def fetch_ytd_monthly(year: int, today: date) -> dict:
    """Year-to-date monthly mean lake levels (MASL) from KiWIS."""
    frm = f"{year}-01-01T00:00:00-0500"
    to = today.strftime("%Y-%m-%dT23:59:59-0400")
    try:
        daily, _ = fetch_lake_daily(frm, to)
    except Exception as e:
        print(f"  YTD lake fetch failed: {e}")
        return {"ytd_months": [], "ytd_lake": [], "ytd_historic": []}

    by_month: dict[str, list[float]] = defaultdict(list)
    for d, v in daily.items():
        if d.startswith(str(year)):
            by_month[d[:7]].append(v)
    months = sorted(by_month)
    lake_means = [mean(by_month[m]) for m in months]
    hist_by_m = historic_monthly_means()
    hist_means = []
    for m in months:
        month_num = int(m[5:7])
        hist_means.append(hist_by_m.get(month_num))
    return {
        "ytd_months": months,
        "ytd_lake": lake_means,
        "ytd_historic": hist_means,
        "ytd_year": year,
    }


def load_cached_series() -> dict | None:
    if not CACHE.exists():
        return None
    try:
        return json.loads(CACHE.read_text())
    except Exception:
        return None


def day_delta(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    return values[-1] - values[-2]


def ticker_markup(delta: float | None, *, flat: float, digits: int = 1, unit: str = "", invert: bool = False) -> str:
    """Stock-ticker style ▲ / ▼ / – chip for change vs previous reading."""
    if delta is None or (isinstance(delta, float) and math.isnan(delta)):
        return '<span class="ticker flat" title="No prior reading">–</span>'
    if abs(delta) < flat:
        return '<span class="ticker flat" title="Little change vs prior day">–</span>'
    up = delta > 0
    if invert:
        up = not up
    arrow = "▲" if delta > 0 else "▼"
    cls = "up" if up else "down"
    # Always show signed magnitude in the natural up/down sense of the value
    return (
        f'<span class="ticker {cls}" title="Change vs prior day">'
        f"{arrow} {delta:+.{digits}f}{unit}</span>"
    )


def build_series() -> dict:
    now = datetime.now(timezone.utc)
    tor = now.astimezone(ZoneInfo("America/Toronto"))
    today = tor.date()
    edt = tor
    end = (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    start = (now - timedelta(days=10)).strftime("%Y-%m-%dT00:00:00Z")
    frm = (now - timedelta(days=10)).strftime("%Y-%m-%dT00:00:00-0400")
    to = (now + timedelta(days=1)).strftime("%Y-%m-%dT23:59:59-0400")

    ff, ff_ts = fetch_wsc_daily("02KF001", start, end)
    ap, ap_ts = fetch_wsc_daily("02KF006", start, end)
    lake, lake_ts = fetch_lake_daily(frm, to)

    # Flow gauges often publish today's partial day before KiWIS has a lake day.
    # Only chart days that have both lake level and Ferguson inflow.
    overlap = sorted(set(ff) & set(lake))
    cutoff = (today - timedelta(days=7)).isoformat()
    days = [d for d in overlap if d >= cutoff][-7:]
    if len(days) < 3:
        days = overlap[-7:]
    if len(days) < 3:
        raise RuntimeError(
            f"Not enough overlapping gauge days (ff={len(ff)}, lake={len(lake)}, overlap={len(overlap)})"
        )

    hist_ff = [ff[d] for d in days]
    hist_ap = [ap.get(d, ff[d]) for d in days]
    hist_lake = [lake[d] for d in days]

    # Calibrate k from observed gap vs rise
    obs = []
    ds = sorted(lake)
    for i in range(1, len(ds)):
        d0, d1 = ds[i - 1], ds[i]
        if d0 in ff and d0 in ap:
            gap = ff[d0] - ap[d0]
            dL = (lake[d1] - lake[d0]) * 100
            obs.append((gap, dL))
    ks = [dL / (gap * CM_PER_M3S_DAY) for gap, dL in obs if abs(gap) > 2]
    k = max(0.4, min(1.6, mean(ks))) if ks else 1.2
    biases = []
    for gap, dL in obs[-4:]:
        biases.append(dL - k * CM_PER_M3S_DAY * gap)
    bias = mean(biases) if biases else 0.0

    ff_vals = [(d, ff[d]) for d in days if d in ff]
    peak_d, peak_q = max(ff_vals, key=lambda x: x[1])
    last_d, last_q = ff_vals[-1]
    Qbase = 8.0
    days_since_peak = max(0.5, (date.fromisoformat(last_d) - date.fromisoformat(peak_d)).days + 0.5)
    if last_q > Qbase and peak_q > Qbase:
        a = -math.log((last_q - Qbase) / (peak_q - Qbase)) / days_since_peak
    else:
        a = 0.08
    a = max(0.03, min(0.25, a))

    precip = fetch_open_meteo_wed_rain()
    rain_bump: dict[str, float] = {}
    for d, mm in precip.items():
        if mm >= 5:
            # Scale bump roughly with precip amount
            rain_bump[d] = min(20.0, 4.0 + mm * 0.6)

    ap_last = ap.get(last_d, last_q)
    proj_start = date.fromisoformat(last_d)
    proj_days, proj_ff, proj_ap, proj_lake, proj_lo, proj_hi = [], [], [], [], [], []
    level = lake[last_d]
    level_lo = level
    level_hi = level

    for i in range(1, 8):
        d = (proj_start + timedelta(days=i)).isoformat()
        t_from_peak = (date.fromisoformat(d) - date.fromisoformat(peak_d)).days
        qff = Qbase + (peak_q - Qbase) * math.exp(-a * t_from_peak)
        qff += rain_bump.get(d, 0.0)
        if i == 1:
            qap = 0.55 * ap_last + 0.45 * qff
        else:
            qap = 0.35 * proj_ap[-1] + 0.65 * qff
        if i <= 2:
            qap = max(qap, qff - 2)
        gap = qff - qap
        dL = k * CM_PER_M3S_DAY * gap + bias * math.exp(-0.35 * i)
        dL_lo = k * 0.7 * CM_PER_M3S_DAY * gap + (bias - 0.8) * math.exp(-0.35 * i) - 0.3
        dL_hi = (
            k * 1.3 * CM_PER_M3S_DAY * gap
            + (bias + 1.0) * math.exp(-0.35 * i)
            + (2.0 if d in rain_bump else 0.4)
        )
        level += dL / 100
        level_lo += dL_lo / 100
        level_hi += dL_hi / 100
        proj_days.append(d)
        proj_ff.append(qff)
        proj_ap.append(qap)
        proj_lake.append(level)
        proj_lo.append(level_lo)
        proj_hi.append(level_hi)

    # Latest instantaneous-ish values from last daily means
    gap_now = hist_ff[-1] - hist_ap[-1]
    hist_avg = historic_mean_for_date(days[-1])
    vs_hist = (hist_lake[-1] - hist_avg) * 100 if hist_avg is not None else None
    prev = load_cached_series()
    prev_hist_avg = historic_mean_for_date(days[-2]) if len(days) >= 2 else None
    outlook_delta = None
    if prev and prev.get("proj_end_lake") is not None:
        outlook_delta = proj_lake[-1] - float(prev["proj_end_lake"])
    deltas = {
        "lake_m": day_delta(hist_lake),
        "ff": day_delta(hist_ff),
        "ap": day_delta(hist_ap),
        "gap": day_delta([a - b for a, b in zip(hist_ff, hist_ap)]),
        "historic_avg_m": (hist_avg - prev_hist_avg) if hist_avg is not None and prev_hist_avg is not None else None,
        "outlook_m": outlook_delta,
    }
    ytd = fetch_ytd_monthly(today.year, today)

    def _gauge_stamp(ts: datetime | None) -> tuple[str, str]:
        if ts is None:
            return "", "unavailable"
        local = ts.astimezone(ZoneInfo("America/Toronto"))
        label = f"{local.strftime('%b')} {local.day}, {local.strftime('%I:%M %p').lstrip('0')} EDT"
        return ts.isoformat(), label

    lake_iso, lake_edt = _gauge_stamp(lake_ts)
    ff_iso, ff_edt = _gauge_stamp(ff_ts)
    ap_iso, ap_edt = _gauge_stamp(ap_ts)

    watershed = fetch_watershed_core(frm, to, ff, ap, lake)
    # Attach the same as-of stamps we already computed for header freshness
    ws = watershed["watershed"]
    if "mississippi" in ws:
        ws["mississippi"]["as_of_iso"] = lake_iso
    if "ferguson" in ws:
        ws["ferguson"]["as_of_iso"] = ff_iso
    if "appleton" in ws:
        ws["appleton"]["as_of_iso"] = ap_iso

    watershed_full = fetch_watershed_full(frm, to, ff=ff, ap=ap, lake=lake, core=ws)
    wsf = watershed_full["watershed_full"]
    if "mississippi" in wsf:
        wsf["mississippi"]["as_of_iso"] = lake_iso
    if "ferguson" in wsf:
        wsf["ferguson"]["as_of_iso"] = ff_iso
    if "appleton" in wsf:
        wsf["appleton"]["as_of_iso"] = ap_iso

    return {
        "generated_edt": edt.strftime("%Y-%m-%d %H:%M"),
        "lake_as_of_iso": lake_iso,
        "lake_as_of_edt": lake_edt,
        "ff_as_of_iso": ff_iso,
        "ff_as_of_edt": ff_edt,
        "ap_as_of_iso": ap_iso,
        "ap_as_of_edt": ap_edt,
        "hist_days": days,
        "hist_ff": hist_ff,
        "hist_ap": hist_ap,
        "hist_lake": hist_lake,
        "proj_days": proj_days,
        "proj_ff": proj_ff,
        "proj_ap": proj_ap,
        "proj_lake": proj_lake,
        "proj_lo": proj_lo,
        "proj_hi": proj_hi,
        "latest_lake": hist_lake[-1],
        "latest_ff": hist_ff[-1],
        "latest_ap": hist_ap[-1],
        "gap_now": gap_now,
        "historic_avg": hist_avg,
        "vs_historic_cm": vs_hist,
        "vs_early_july_cm": (hist_lake[-1] - EARLY_JULY_LEVEL) * 100,
        "proj_end_lake": proj_lake[-1],
        "proj_change_cm": (proj_lake[-1] - hist_lake[-1]) * 100,
        "deltas": deltas,
        "rain_bump": rain_bump,
        **ytd,
        **watershed,
        **watershed_full,
        "params": {
            "k_calibrated": k,
            "effective_cm_per_m3s_day": k * CM_PER_M3S_DAY,
            "routing_bias_cm_day": bias,
            "recession_a": a,
            "peak_ff": peak_q,
            "peak_day": peak_d,
        },
    }


def render_chart(series: dict) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    hist_days = [datetime.fromisoformat(d) for d in series["hist_days"]]
    proj_days = [datetime.fromisoformat(d) for d in series["proj_days"]]
    hist_ff, hist_ap, hist_lake = series["hist_ff"], series["hist_ap"], series["hist_lake"]
    proj_ff, proj_ap = series["proj_ff"], series["proj_ap"]
    proj_lake, proj_lo, proj_hi = series["proj_lake"], series["proj_lo"], series["proj_hi"]

    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "axes.facecolor": "#f7fafb",
            "figure.facecolor": "#ffffff",
            "axes.edgecolor": "#c5d3d9",
            "grid.color": "#dce6ea",
        }
    )
    fig, axes = plt.subplots(
        2, 1, figsize=(10.2, 7.2), sharex=True, gridspec_kw={"height_ratios": [1.05, 1.15], "hspace": 0.18}
    )
    ax = axes[0]
    ax.plot(hist_days, hist_ff, color="#0b6e4f", lw=2.4, marker="o", ms=5, label="Inflow (Ferguson's Falls)")
    ax.plot(hist_days, hist_ap, color="#c45c26", lw=2.4, marker="o", ms=5, label="Outflow (Appleton)")
    ax.plot([hist_days[-1]] + proj_days, [hist_ff[-1]] + proj_ff, color="#0b6e4f", lw=2.0, ls="--", marker="o", ms=4, alpha=0.85)
    ax.plot([hist_days[-1]] + proj_days, [hist_ap[-1]] + proj_ap, color="#c45c26", lw=2.0, ls="--", marker="o", ms=4, alpha=0.85)
    for i in range(len(hist_days) - 1):
        if hist_ff[i] > hist_ap[i]:
            ax.fill_between(
                [hist_days[i], hist_days[i + 1]],
                [hist_ap[i], hist_ap[i + 1]],
                [hist_ff[i], hist_ff[i + 1]],
                color="#0b6e4f",
                alpha=0.12,
            )
    # shade rain bump days
    for d in series.get("rain_bump", {}):
        dt = datetime.fromisoformat(d)
        ax.axvspan(dt, dt + timedelta(days=1), color="#5b8def", alpha=0.12)
    ax.set_ylabel("Flow (m³/s)")
    ax.set_title("Mississippi Lake water balance — last 7 days + 7-day outlook", loc="left", color="#1a3a4a")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(True, axis="y")
    ax.set_ylim(0, max(95, max(hist_ff + hist_ap + proj_ff + proj_ap) * 1.15))

    ax2 = axes[1]
    ax2.plot(hist_days, hist_lake, color="#1f4e79", lw=2.6, marker="o", ms=5, label="Lake level (observed)")
    ax2.fill_between(proj_days, proj_lo, proj_hi, color="#1f4e79", alpha=0.15, label="Projection range")
    ax2.plot(
        [hist_days[-1]] + proj_days,
        [hist_lake[-1]] + proj_lake,
        color="#1f4e79",
        lw=2.2,
        ls="--",
        marker="o",
        ms=4,
        label="Lake level (projected)",
    )
    ax2.axhline(EARLY_JULY_LEVEL, color="#6b8f71", ls=":", lw=1.3, label=f"Early July ~{EARLY_JULY_LEVEL:.2f} m")
    hist_avg = series.get("historic_avg")
    if hist_avg is not None:
        ax2.axhline(hist_avg, color="#8a6d3b", ls=":", lw=1.4, label=f"Historic avg ~{hist_avg:.2f} m")
    for d in series.get("rain_bump", {}):
        dt = datetime.fromisoformat(d)
        ax2.axvspan(dt, dt + timedelta(days=1), color="#5b8def", alpha=0.12)
    ax2.set_ylabel("Level (m MASL)")
    ax2.set_xlabel("Date")
    ax2.legend(loc="lower right", fontsize=9)
    ax2.grid(True, axis="y")
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    fig.text(
        0.01,
        0.01,
        "Model: storage routing Δh ≈ k·(Qin−Qout)/A with A≈25 km²; k calibrated to recent rise. Not an official forecast.",
        fontsize=7.5,
        color="#6a7c84",
    )
    fig.savefig(CHART_PNG, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close()


def render_ytd_chart(series: dict) -> bool:
    """Render year-to-date monthly lake level chart. Returns False if no data."""
    months = series.get("ytd_months") or []
    lake = series.get("ytd_lake") or []
    hist = series.get("ytd_historic") or []
    year = series.get("ytd_year") or date.today().year
    if len(months) < 1 or len(lake) != len(months):
        return False

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "axes.facecolor": "#f7fafb",
            "figure.facecolor": "#ffffff",
            "axes.edgecolor": "#c5d3d9",
            "grid.color": "#dce6ea",
        }
    )
    labels = [datetime.fromisoformat(f"{m}-01").strftime("%b") for m in months]
    x = list(range(len(months)))

    fig, ax = plt.subplots(figsize=(10.2, 4.2))
    ax.bar(x, lake, width=0.62, color="#2f6f7e", edgecolor="#1a3a4a", linewidth=0.6, label=f"{year} monthly mean")
    for xi, yi in zip(x, lake):
        ax.text(xi, yi + 0.015, f"{yi:.2f}", ha="center", va="bottom", fontsize=8, color="#1a3a4a")

    hist_x, hist_y = [], []
    for i, hv in enumerate(hist):
        if hv is not None:
            hist_x.append(x[i])
            hist_y.append(hv)
    if hist_x:
        ax.plot(
            hist_x,
            hist_y,
            color="#8a6d3b",
            lw=2.0,
            marker="D",
            ms=5,
            ls="--",
            label="Historic monthly avg (2014–2025)",
        )

    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_ylabel("Level (m MASL)")
    ax.set_xlabel("Month")
    ax.set_title(f"Mississippi Lake level — {year} year to date (monthly means)", loc="left", color="#1a3a4a")
    ax.legend(loc="best", fontsize=9)
    ax.grid(True, axis="y")
    ymin = min(lake + [v for v in hist if v is not None] or lake) - 0.15
    ymax = max(lake + [v for v in hist if v is not None] or lake) + 0.25
    ax.set_ylim(ymin, ymax)
    fig.text(
        0.01,
        0.01,
        "Monthly mean of daily KiWIS lake levels. Historic line = average of day-of-year means for that month.",
        fontsize=7.5,
        color="#6a7c84",
    )
    fig.savefig(YTD_CHART_PNG, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close()
    return True


def _trend_arrow(delta: float | None, *, flat: float) -> tuple[str, str]:
    if delta is None:
        return "–", "#5a7a86"
    if abs(delta) < flat:
        return "–", "#5a7a86"
    if delta > 0:
        return "▲", "#c45c26"
    return "▼", "#0b6e4f"


def render_watershed_chart(series: dict) -> bool:
    """Great-Lakes-style elevation profile for the core Mississippi watershed chain."""
    nodes = series.get("watershed") or {}
    if not nodes:
        return False

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.patches import Patch, Rectangle

    crotch = nodes.get("crotch") or {}
    dal = nodes.get("dalhousie") or {}
    miss = nodes.get("mississippi") or {}
    ff = nodes.get("ferguson") or {}
    ap = nodes.get("appleton") or {}

    z_crotch = float(crotch.get("value") or 239.7)
    z_dal = float(dal.get("value") or 156.9)
    z_miss = float(miss.get("value") or 134.4)
    z_ap = z_miss - 1.2

    fig, ax = plt.subplots(figsize=(11.8, 6.6))
    ax.set_xlim(0, 12)
    ax.set_ylim(108, 292)
    ax.set_facecolor("#d9eaf3")
    fig.patch.set_facecolor("white")
    ax.axhspan(108, 292, color="#d0e6f1", zorder=0)

    def _ease(t):
        t = np.clip(t, 0, 1)
        return t * t * (3 - 2 * t)

    def _bowl_depth(t, depth):
        # t in [-1,1] across a lake span
        return depth * (1.0 - 0.9 * t * t)

    # Key x stations for a continuous profile
    # Crotch lake, dam, drop, Dalhousie, river, Miss lake, dam, Appleton reach
    xs = np.linspace(0.2, 11.6, 900)
    surface = np.zeros_like(xs)
    floor = np.zeros_like(xs)
    kind = np.full(xs.shape, "river", dtype=object)  # lake | river

    def set_span(x0, x1, fn_surface, fn_floor, k):
        m = (xs >= x0) & (xs <= x1)
        if not np.any(m):
            return
        t = (xs[m] - x0) / max(x1 - x0, 1e-9)
        surface[m] = fn_surface(t)
        floor[m] = fn_floor(t)
        kind[m] = k

    # Crotch Lake bowl
    set_span(
        0.35,
        2.15,
        lambda t: np.full_like(t, z_crotch),
        lambda t: z_crotch - _bowl_depth(2 * t - 1, 17),
        "lake",
    )
    # Drop after Crotch Dam
    set_span(
        2.15,
        3.40,
        lambda t: z_crotch + (z_dal - z_crotch) * _ease(t),
        lambda t: (z_crotch + (z_dal - z_crotch) * _ease(t)) - (10 - 2 * _ease(t)),
        "river",
    )
    # Dalhousie Lake
    set_span(
        3.40,
        5.20,
        lambda t: np.full_like(t, z_dal),
        lambda t: z_dal - _bowl_depth(2 * t - 1, 13),
        "lake",
    )
    # River past Ferguson’s Falls into Mississippi Lake
    set_span(
        5.20,
        6.55,
        lambda t: z_dal + (z_miss - z_dal) * _ease(t),
        lambda t: (z_dal + (z_miss - z_dal) * _ease(t)) - 8,
        "river",
    )
    # Mississippi Lake
    set_span(
        6.55,
        8.95,
        lambda t: np.full_like(t, z_miss),
        lambda t: z_miss - _bowl_depth(2 * t - 1, 11),
        "lake",
    )
    # Downstream of Carleton Place Dam toward Appleton
    set_span(
        8.95,
        11.50,
        lambda t: z_miss + (z_ap - z_miss) * _ease(t),
        lambda t: (z_miss + (z_ap - z_miss) * _ease(t)) - 7,
        "river",
    )

    # Smooth tiny kinks at segment joins with a light rolling average
    kernel = np.array([1, 2, 3, 2, 1], dtype=float)
    kernel /= kernel.sum()
    surface = np.convolve(surface, kernel, mode="same")
    floor = np.convolve(floor, kernel, mode="same")
    # keep a minimum water column
    floor = np.minimum(floor, surface - 4.5)

    # Continuous land mass under the whole profile
    land_x = np.concatenate([[xs[0]], xs, [xs[-1]], [xs[0]]])
    land_y = np.concatenate([[106], floor, [106], [106]])
    ax.fill(land_x, land_y, color="#cbb79f", zorder=1)
    ax.plot(xs, floor, color="#9a8874", lw=0.9, alpha=0.65, solid_capstyle="round", zorder=2)

    # Water fills by kind (lake vs river colors) but still curved
    def fill_kind(target, color, alpha=0.95):
        m = kind == target
        if not np.any(m):
            return
        # split into contiguous runs
        idx = np.where(m)[0]
        cuts = np.where(np.diff(idx) > 1)[0]
        starts = np.r_[idx[0], idx[cuts + 1]]
        ends = np.r_[idx[cuts], idx[-1]]
        for a, b in zip(starts, ends):
            seg_x = xs[a : b + 1]
            seg_s = surface[a : b + 1]
            seg_f = floor[a : b + 1]
            wx = np.concatenate([seg_x, seg_x[::-1]])
            wy = np.concatenate([seg_s, seg_f[::-1]])
            ax.fill(wx, wy, color=color, alpha=alpha, zorder=3)
            ax.plot(seg_x, seg_s, color="#16384a", lw=1.35, solid_capstyle="round", zorder=4)

    fill_kind("lake", "#2f6f7e")
    fill_kind("river", "#5ba3c9", alpha=0.9)

    def draw_dam(x, z_top, height=16, label="", label_side=1):
        ax.add_patch(
            Rectangle(
                (x - 0.06, z_top - height),
                0.12,
                height + 2.5,
                facecolor="#2b2b2b",
                edgecolor="#111",
                lw=0.35,
                zorder=5,
                clip_on=False,
            )
        )
        if label:
            ax.text(
                x + 0.18 * label_side,
                z_top - height * 0.35,
                label,
                ha="left" if label_side > 0 else "right",
                va="center",
                fontsize=7,
                color="#3a4a52",
                zorder=6,
                bbox=dict(boxstyle="round,pad=0.2", facecolor="white", edgecolor="#d5dde3", alpha=0.9),
            )

    draw_dam(2.15, z_crotch, height=17, label="Crotch Dam", label_side=1)
    draw_dam(8.95, z_miss, height=13, label="Carleton Place Dam", label_side=1)

    def label_box(x, z, node, name, kind_label, x_text):
        val = node.get("value")
        d7 = node.get("delta_7d")
        arrow, tcolor = _trend_arrow(d7, flat=0.8)
        if kind_label == "level":
            if val is None:
                body = f"{name}\n— m MASL"
            else:
                trend = "—" if d7 is None else f"{arrow} {d7:+.0f} cm / 7d"
                body = f"{name}\n{val:.2f} m MASL\n{trend}"
            face, edge, marker = "#ffffff", "#cfd8dd", "o"
        else:
            if val is None:
                body = f"{name}\n— m³/s"
            else:
                trend = "—" if d7 is None else f"{arrow} {d7:+.1f} m³/s / 7d"
                body = f"{name}\n{val:.1f} m³/s\n{trend}"
            face, edge, marker = "#fff8e8", "#ead9a8", "s"
        ax.annotate(
            body,
            xy=(x, z),
            xytext=(x_text, 272),
            ha="center",
            va="top",
            fontsize=8,
            color="#1a3a4a",
            linespacing=1.45,
            arrowprops=dict(arrowstyle="-", color="#7a8f99", lw=0.8, shrinkA=4, shrinkB=5),
            bbox=dict(boxstyle="round,pad=0.45", facecolor=face, edgecolor=edge, alpha=0.97),
            zorder=7,
            clip_on=False,
        )
        ax.plot(
            [x],
            [z + 1.2],
            marker=marker,
            color=tcolor,
            ms=5.5,
            zorder=8,
            markeredgecolor="white",
            markeredgewidth=0.7,
        )

    # Even label columns with extra horizontal breathing room
    label_box(1.25, z_crotch, crotch, "Crotch Lake", "level", 1.15)
    label_box(4.30, z_dal, dal, "Dalhousie Lake", "level", 3.55)
    label_box(5.85, (z_dal + z_miss) / 2, ff, "Ferguson’s Falls\ninflow", "flow", 5.85)
    label_box(7.75, z_miss, miss, "Mississippi Lake", "level", 8.05)
    label_box(10.20, z_ap, ap, "Appleton\noutflow", "flow", 10.45)

    ax.annotate(
        "",
        xy=(11.55, 118),
        xytext=(0.25, 118),
        arrowprops=dict(arrowstyle="->", color="#5a7a86", lw=1.2),
    )
    ax.text(5.9, 114.5, "Flow direction → Ottawa River", ha="center", va="top", fontsize=8, color="#5a7a86")

    legend_handles = [
        Patch(facecolor="#2f6f7e", edgecolor="#1f4e79", label="Lake"),
        Patch(facecolor="#5ba3c9", edgecolor="#1f4e79", label="River / channel"),
        Patch(facecolor="#2b2b2b", edgecolor="#111111", label="Dam"),
    ]
    leg = ax.legend(
        handles=legend_handles,
        loc="lower right",
        bbox_to_anchor=(0.995, 0.145),
        fontsize=8.5,
        frameon=True,
        fancybox=True,
        framealpha=0.96,
        edgecolor="#d5dde3",
        title="Legend",
    )
    leg.get_title().set_fontsize(8.5)
    leg.get_title().set_color("#1a3a4a")

    ax.set_title(
        "Mississippi watershed profile (core chain) — levels, flows & 7-day trends",
        loc="left",
        color="#1a3a4a",
        fontsize=12,
        pad=14,
    )
    ax.set_ylabel("Elevation (m MASL, exaggerated)")
    ax.set_xticks([])
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["bottom"].set_visible(False)
    ax.grid(True, axis="y", alpha=0.28)
    fig.text(
        0.01,
        0.01,
        "NOT TO SCALE · Schematic only. Levels from MVCA/KiWIS; flows from WSC. Trends ≈ change vs ~7 days earlier. ▲ rising · ▼ falling.",
        fontsize=7.5,
        color="#6a7c84",
    )
    fig.savefig(WATERSHED_PNG, dpi=170, bbox_inches="tight", facecolor="white")
    plt.close()
    return True


def render_watershed_full_chart(series: dict) -> bool:
    """Full Mississippi system elevation profile — headwaters through Ottawa River."""
    nodes = series.get("watershed_full") or {}
    if not nodes:
        return False

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.patches import Patch, Rectangle

    def z_of(key: str, fallback: float) -> float:
        val = (nodes.get(key) or {}).get("value")
        try:
            return float(val) if val is not None else fallback
        except (TypeError, ValueError):
            return fallback

    # Schematic elevations (MASL) with live overrides where available
    z_mosque = z_of("mosque", 319.0)
    z_summit = z_of("summit", 281.0)
    z_shab = z_of("shabomeka", 271.0)
    z_palm = z_of("palmerston", 272.0)
    z_maz = z_of("mazinaw", 268.0)
    z_can = z_of("canonto", 268.0)
    z_missag = z_of("mississagagon", 268.0)
    z_kash = z_of("kashwakamak", 261.0)
    z_pine = z_of("pine", 255.0)
    z_gull = z_of("big_gull", 253.0)
    z_malc = z_of("malcolm", 253.0)
    z_farm = z_of("farm", 248.0)
    z_crotch = z_of("crotch", 239.7)
    z_stump = z_of("stump", 187.4)
    z_widow = z_of("widow", 184.0)
    z_dal = z_of("dalhousie", 156.9)
    z_clay = z_of("clayton", 161.8)
    z_lanark = z_of("lanark", 144.2)
    z_miss = z_of("mississippi", 134.4)
    z_cp = z_of("carleton", z_miss - 0.15)
    z_ap = z_miss - 1.5
    z_almonte = z_miss - 8.0
    z_galetta = z_miss - 40.0
    z_ottawa = 70.0

    fig, ax = plt.subplots(figsize=(15.5, 7.8))
    ax.set_xlim(0, 22.2)
    ax.set_ylim(18, 355)
    ax.set_facecolor("#d9eaf3")
    fig.patch.set_facecolor("white")
    ax.axhspan(18, 355, color="#d0e6f1", zorder=0)

    def _ease(t):
        t = np.clip(t, 0, 1)
        return t * t * (3 - 2 * t)

    def _bowl_depth(t, depth):
        return depth * (1.0 - 0.9 * t * t)

    xs = np.linspace(0.15, 21.9, 1400)
    surface = np.full_like(xs, z_ottawa)
    floor = np.full_like(xs, z_ottawa - 6.0)
    kind = np.full(xs.shape, "river", dtype=object)

    def set_span(x0, x1, fn_surface, fn_floor, k):
        m = (xs >= x0) & (xs <= x1)
        if not np.any(m):
            return
        t = (xs[m] - x0) / max(x1 - x0, 1e-9)
        surface[m] = fn_surface(t)
        floor[m] = fn_floor(t)
        kind[m] = k

    # Main-stem continuous profile (schematic x stations)
    # Shabomeka → Mazinaw → Kash → Gull/Farm → Crotch → Stump → Dalhousie → Miss → Appleton → Almonte → Galetta → Ottawa
    segs = [
        (0.25, 1.35, z_shab, 14, "lake"),
        (1.35, 2.15, z_maz, None, "river"),  # drop to Mazinaw
        (2.15, 3.35, z_maz, 12, "lake"),
        (3.35, 4.25, z_kash, None, "river"),
        (4.25, 5.45, z_kash, 11, "lake"),
        (5.45, 6.35, z_gull, None, "river"),
        (6.35, 7.45, z_gull, 10, "lake"),
        (7.45, 8.20, z_farm, None, "river"),
        (8.20, 9.55, z_crotch, 15, "lake"),
        (9.55, 10.55, z_stump, None, "river"),
        (10.55, 11.45, z_stump, 9, "lake"),
        (11.45, 12.45, z_dal, None, "river"),
        (12.45, 13.55, z_dal, 10, "lake"),
        (13.55, 14.55, z_miss, None, "river"),
        (14.55, 16.15, z_miss, 10, "lake"),
        (16.15, 17.35, z_ap, None, "river"),
        (17.35, 18.55, z_almonte, None, "river"),
        (18.55, 20.00, z_galetta, None, "river"),
        (20.00, 21.70, z_ottawa, None, "river"),
    ]

    for i, (x0, x1, z, depth, k) in enumerate(segs):
        z_prev = segs[i - 1][2] if i else z
        if k == "lake":
            set_span(
                x0,
                x1,
                lambda t, zz=z: np.full_like(t, zz),
                lambda t, zz=z, d=depth: zz - _bowl_depth(2 * t - 1, d),
                "lake",
            )
        else:
            set_span(
                x0,
                x1,
                lambda t, za=z_prev, zb=z: za + (zb - za) * _ease(t),
                lambda t, za=z_prev, zb=z: (za + (zb - za) * _ease(t)) - (9 - 2 * _ease(t)),
                "river",
            )

    kernel = np.array([1, 2, 3, 2, 1], dtype=float)
    kernel /= kernel.sum()
    surface = np.convolve(surface, kernel, mode="same")
    floor = np.convolve(floor, kernel, mode="same")
    floor = np.minimum(floor, surface - 4.0)

    land_x = np.concatenate([[xs[0]], xs, [xs[-1]], [xs[0]]])
    land_y = np.concatenate([[50], floor, [50], [50]])
    ax.fill(land_x, land_y, color="#cbb79f", zorder=1)
    ax.plot(xs, floor, color="#9a8874", lw=0.8, alpha=0.65, solid_capstyle="round", zorder=2)

    def fill_kind(target, color, alpha=0.95):
        m = kind == target
        if not np.any(m):
            return
        idx = np.where(m)[0]
        cuts = np.where(np.diff(idx) > 1)[0]
        starts = np.r_[idx[0], idx[cuts + 1]]
        ends = np.r_[idx[cuts], idx[-1]]
        for a, b in zip(starts, ends):
            seg_x = xs[a : b + 1]
            seg_s = surface[a : b + 1]
            seg_f = floor[a : b + 1]
            ax.fill(
                np.concatenate([seg_x, seg_x[::-1]]),
                np.concatenate([seg_s, seg_f[::-1]]),
                color=color,
                alpha=alpha,
                zorder=3,
            )
            ax.plot(seg_x, seg_s, color="#16384a", lw=1.15, solid_capstyle="round", zorder=4)

    fill_kind("lake", "#2f6f7e")
    fill_kind("river", "#5ba3c9", alpha=0.9)

    def draw_dam(x, z_top, height=12):
        ax.add_patch(
            Rectangle(
                (x - 0.045, z_top - height),
                0.09,
                height + 2.0,
                facecolor="#2b2b2b",
                edgecolor="#111",
                lw=0.3,
                zorder=5,
                clip_on=False,
            )
        )
        # Anchor leaders on the dam crest (top of bar)
        return x, z_top

    def dam_lead(x_dam, z_dam, text, x_text, y_text, *, fontsize=5.6, ha="left", va="bottom"):
        """Angled leader from dam crest to label (usually above-right, lead down-left)."""
        ax.annotate(
            text,
            xy=(x_dam, z_dam),
            xytext=(x_text, y_text),
            ha=ha,
            va=va,
            fontsize=fontsize,
            color="#3a4a52",
            arrowprops=dict(
                arrowstyle="-",
                color="#8a9aa3",
                lw=0.65,
                shrinkA=2,
                shrinkB=2,
                connectionstyle="arc3,rad=0",
            ),
            bbox=dict(boxstyle="round,pad=0.12", facecolor="white", edgecolor="#d5dde3", alpha=0.95),
            zorder=6,
            clip_on=False,
        )

    # Main-stem dam bars
    d_shab = draw_dam(1.35, z_shab, height=12)
    d_maz = draw_dam(3.35, z_maz, height=12)
    d_kash = draw_dam(5.45, z_kash, height=11)
    d_gull = draw_dam(7.45, z_gull, height=10)
    d_farm = draw_dam(8.20, z_farm, height=9)
    d_crotch = draw_dam(9.55, z_crotch, height=15)
    z_hf = (z_crotch + z_stump) / 2
    d_hf = draw_dam(10.55, z_hf, height=10)
    d_cp = draw_dam(16.15, z_miss, height=11)
    d_apgs = draw_dam(17.05, z_ap, height=8)
    d_enerdu = draw_dam(17.85, (z_ap + z_almonte) / 2, height=7)
    d_almonte = draw_dam(18.55, z_almonte, height=8)
    d_galetta = draw_dam(20.00, z_galetta, height=9)

    # Dam labels — all lean right (ha=left); staggered so boxes never overlap ovals
    dam_lead(*d_shab, "Shabomeka Dam", 1.60, 324)
    dam_lead(*d_maz, "Mazinaw Dam", 3.90, 306)
    dam_lead(*d_kash, "Kashwakamak Dam", 5.80, 262)
    dam_lead(*d_gull, "Big Gull Dam", 8.00, 252)
    dam_lead(*d_farm, "Farm Dam", 8.55, 276, fontsize=5.3)
    dam_lead(*d_crotch, "Crotch Dam (OPG)", 9.90, 255)
    dam_lead(*d_hf, "High Falls GS", 11.00, 238)
    dam_lead(*d_cp, "Carleton Place Dam", 16.50, 192, fontsize=5.3)
    dam_lead(*d_apgs, "Appleton GS", 17.45, 145, fontsize=5.3)
    dam_lead(*d_enerdu, "Enerdu GS", 18.25, 180, fontsize=5.3)
    dam_lead(*d_almonte, "Almonte GS", 19.00, 158, fontsize=5.3)
    dam_lead(*d_galetta, "Galetta GS", 20.30, 170, fontsize=5.3)

    # Side dams — same right-lean, vertical stagger, clear of flow ovals
    side_dams = [
        (0.50, z_mosque, "Mosque Dam", 0.70, 338, "left"),
        (0.90, z_summit, "Summit Dam", 1.20, 304, "left"),
        (1.70, z_palm, "Palmerston Dam", 2.05, 292, "left"),
        (2.50, z_can, "Canonto Dam", 2.85, 266, "left"),
        (4.80, z_missag, "Mississagagon Dam", 5.15, 292, "left"),
        (6.80, z_pine, "Pine Dam", 7.55, 286, "left"),
        (7.20, z_malc, "Malcolm Dam", 7.60, 314, "left"),
        (12.00, z_widow, "Widow Dam", 12.35, 228, "left"),
        (12.75, (z_widow + z_lanark) / 2, "Bennett Dam", 13.20, 200, "left"),
        (13.15, z_lanark, "Lanark Dam", 13.40, 158, "left"),
        (14.15, z_clay, "Clayton Dam", 14.80, 192, "left"),
    ]
    for x, z, name, xt, yt, ha in side_dams:
        ax.add_patch(
            Rectangle(
                (x - 0.035, z - 7),
                0.07,
                9,
                facecolor="#2b2b2b",
                edgecolor="#111",
                lw=0.25,
                zorder=5,
                clip_on=False,
            )
        )
        dam_lead(x, z, name, xt, yt, fontsize=5.2, ha=ha)

    def label_box(x, z, node, name, kind_label, x_text, y_text, *, below=False):
        val = (node or {}).get("value")
        d7 = (node or {}).get("delta_7d")
        arrow, tcolor = _trend_arrow(d7, flat=0.8)
        if kind_label == "level":
            if val is None:
                body = f"{name}\n— m MASL"
            else:
                trend = "—" if d7 is None else f"{arrow} {d7:+.0f} cm / 7d"
                body = f"{name}\n{val:.2f} m\n{trend}"
            face, edge, marker = "#ffffff", "#cfd8dd", "o"
            box = dict(boxstyle="square,pad=0.30", facecolor=face, edgecolor=edge, alpha=0.97)
        else:
            if val is None:
                body = f"{name}\n— m³/s"
            else:
                trend = "—" if d7 is None else f"{arrow} {d7:+.1f} m³/s / 7d"
                body = f"{name}\n{val:.1f} m³/s\n{trend}"
            face, edge, marker = "#fff8e8", "#ead9a8", "s"
            box = dict(boxstyle="ellipse,pad=0.38", facecolor=face, edgecolor=edge, alpha=0.97)
        ax.annotate(
            body,
            xy=(x, z),
            xytext=(x_text, y_text),
            ha="center",
            va="bottom" if below else "top",
            fontsize=7.2,
            color="#1a3a4a",
            linespacing=1.35,
            arrowprops=dict(arrowstyle="-", color="#7a8f99", lw=0.7, shrinkA=3, shrinkB=4),
            bbox=box,
            zorder=7,
            clip_on=False,
        )
        ax.plot(
            [x],
            [z + (1.0 if not below else -1.0)],
            marker=marker,
            color=tcolor,
            ms=4.5,
            zorder=8,
            markeredgecolor="white",
            markeredgewidth=0.6,
        )

    # Flows ABOVE — Marble in the marked sky oval; others stay in the upper band
    y_lvl = 78
    label_box(3.70, (z_maz + z_kash) / 2, nodes.get("marble"), "Marble outflow", "flow", 6.15, 346)
    label_box(12.90, z_dal - 2, nodes.get("dalhousie_out"), "Dalhousie outlet", "flow", 13.10, 305)
    label_box(13.95, (z_dal + z_miss) / 2, nodes.get("ferguson"), "Ferguson’s Falls", "flow", 15.40, 278)
    label_box(16.85, z_ap, nodes.get("appleton"), "Appleton", "flow", 19.40, 268)
    label_box(19.20, z_galetta, nodes.get("galetta"), "Galetta", "flow", 20.65, 145)

    # Lake levels BELOW — unchanged except Ottawa lower
    label_box(0.80, z_shab, nodes.get("shabomeka"), "Shabomeka", "level", 1.05, y_lvl, below=True)
    label_box(2.75, z_maz, nodes.get("mazinaw"), "Mazinaw", "level", 2.65, y_lvl, below=True)
    label_box(4.85, z_kash, nodes.get("kashwakamak"), "Kashwakamak", "level", 4.55, y_lvl, below=True)
    label_box(6.90, z_gull, nodes.get("big_gull"), "Big Gull", "level", 6.85, y_lvl, below=True)
    label_box(8.85, z_crotch, nodes.get("crotch"), "Crotch Lake", "level", 8.95, y_lvl, below=True)
    label_box(11.00, z_stump, nodes.get("stump"), "Stump Lake", "level", 10.95, y_lvl, below=True)
    label_box(13.00, z_dal, nodes.get("dalhousie"), "Dalhousie", "level", 12.70, y_lvl, below=True)
    label_box(15.35, z_miss, nodes.get("mississippi"), "Mississippi Lake", "level", 15.45, y_lvl, below=True)
    ax.annotate(
        "Ottawa River\nconfluence\n~70 m MASL",
        xy=(21.1, z_ottawa),
        xytext=(19.70, 22),
        ha="center",
        va="bottom",
        fontsize=7.2,
        color="#1a3a4a",
        linespacing=1.35,
        arrowprops=dict(arrowstyle="-", color="#7a8f99", lw=0.7, shrinkA=3, shrinkB=4),
        bbox=dict(boxstyle="square,pad=0.30", facecolor="#eef6f0", edgecolor="#b7d0c0", alpha=0.97),
        zorder=7,
        clip_on=False,
    )
    ax.plot([21.1], [z_ottawa - 1.0], marker="D", color="#2f6f7e", ms=4.5, zorder=8, markeredgecolor="white", markeredgewidth=0.6)

    ax.annotate(
        "",
        xy=(17.4, 20),
        xytext=(0.2, 20),
        arrowprops=dict(arrowstyle="->", color="#5a7a86", lw=1.15),
    )
    ax.text(8.6, 18.5, "Flow direction → Ottawa River near Fitzroy Harbour", ha="center", va="top", fontsize=8, color="#5a7a86")

    legend_handles = [
        Patch(facecolor="#2f6f7e", edgecolor="#1f4e79", label="Lake"),
        Patch(facecolor="#5ba3c9", edgecolor="#1f4e79", label="River / channel"),
        Patch(facecolor="#2b2b2b", edgecolor="#111111", label="Dam / generating station"),
    ]
    leg = ax.legend(
        handles=legend_handles,
        loc="upper right",
        bbox_to_anchor=(0.995, 0.92),
        fontsize=8,
        frameon=True,
        fancybox=True,
        framealpha=0.96,
        edgecolor="#d5dde3",
        title="Legend",
    )
    leg.get_title().set_fontsize(8)
    leg.get_title().set_color("#1a3a4a")

    ax.set_title(
        "Mississippi watershed profile (full system) — flows above · lake levels below",
        loc="left",
        color="#1a3a4a",
        fontsize=11.5,
        pad=12,
    )
    ax.set_ylabel("Elevation (m MASL, exaggerated)")
    ax.set_xticks([])
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["bottom"].set_visible(False)
    ax.grid(True, axis="y", alpha=0.28)
    fig.text(
        0.01,
        0.005,
        "NOT TO SCALE · Schematic main stem + side dams. White boxes = lake levels (m MASL); ovals = flows. Dam names are structures only (no MASL). Levels MVCA/KiWIS; flows WSC. Trends ≈ vs ~7 days earlier.",
        fontsize=7.2,
        color="#6a7c84",
    )
    fig.savefig(WATERSHED_FULL_PNG, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close()
    return True



def render_html(series: dict) -> None:
    lake = series["latest_lake"]
    ff = series["latest_ff"]
    ap = series["latest_ap"]
    gap = series["gap_now"]
    vs = series["vs_early_july_cm"]
    hist_avg = series.get("historic_avg")
    vs_hist = series.get("vs_historic_cm")
    proj = series["proj_end_lake"]
    dcm = series["proj_change_cm"]
    when = series["generated_edt"]
    chart_src = f"chart.png?v={_asset_v(CHART_PNG)}"
    ytd_src = f"ytd_chart.png?v={_asset_v(YTD_CHART_PNG)}"
    watershed_src = f"watershed_profile.png?v={_asset_v(WATERSHED_PNG)}"
    watershed_full_src = f"watershed_full_profile.png?v={_asset_v(WATERSHED_FULL_PNG)}"
    lake_as_of_iso = series.get("lake_as_of_iso") or ""
    lake_as_of_edt = series.get("lake_as_of_edt") or "unavailable"
    ff_as_of_iso = series.get("ff_as_of_iso") or ""
    ff_as_of_edt = series.get("ff_as_of_edt") or "unavailable"
    ap_as_of_iso = series.get("ap_as_of_iso") or ""
    ap_as_of_edt = series.get("ap_as_of_edt") or "unavailable"
    deltas = series.get("deltas") or {}
    # Date label for the glance header — prefer latest observed gauge day
    try:
        glance_day = date.fromisoformat(series["hist_days"][-1])
    except Exception:
        glance_day = datetime.strptime(when[:10], "%Y-%m-%d").date()
    glance_label = f"{glance_day.strftime('%B')} {glance_day.day}"
    status = "Near crest" if abs(gap) < 5 else ("Filling" if gap > 5 else "Falling / draining")
    rain_note = (
        ", ".join(f"{d} (~bump)" for d in sorted(series.get("rain_bump", {})))
        or "no significant rain bump in current forecast"
    )
    gap_color = "#0b6e4f" if gap < -0.5 else ("#c45c26" if gap > 0.5 else "#5a7a86")
    gap_label = "Draining" if gap < -0.5 else ("Filling" if gap > 0.5 else "Balanced")
    outlook_color = "#0b6e4f" if dcm < -0.5 else ("#c45c26" if dcm > 0.5 else "#5a7a86")
    if hist_avg is not None and vs_hist is not None:
        hist_avg_html = f"{hist_avg:.2f}"
        hist_sub = f"for this date · 2014–2025<br>current is {vs_hist:+.0f} cm vs avg"
        lake_sub = f"{vs_hist:+.0f} cm vs historic avg<br>{vs:+.0f} cm vs early July"
    else:
        hist_avg_html = "—"
        hist_sub = "for this date · unavailable<br>see early July baseline"
        lake_sub = f"{vs:+.0f} cm vs early July<br>~{EARLY_JULY_LEVEL:.2f} m MASL"

    # Ticker chips: lake/outlook in cm; flows in m³/s
    lake_delta = deltas.get("lake_m")
    lake_tick = ticker_markup(
        None if lake_delta is None else lake_delta * 100, flat=0.3, digits=1, unit=" cm"
    )
    outlook_delta = deltas.get("outlook_m")
    outlook_tick = ticker_markup(
        None if outlook_delta is None else outlook_delta * 100, flat=0.3, digits=1, unit=" cm"
    )
    ff_tick = ticker_markup(deltas.get("ff"), flat=0.4, digits=1)
    ap_tick = ticker_markup(deltas.get("ap"), flat=0.4, digits=1)
    gap_tick = ticker_markup(deltas.get("gap"), flat=0.4, digits=1)

    def _fmt_day(iso: str) -> str:
        d = date.fromisoformat(iso)
        return f"{d.strftime('%b')} {d.day}"

    table_rows: list[str] = []
    for d, lake_v, ff_v, ap_v in zip(
        series["hist_days"], series["hist_lake"], series["hist_ff"], series["hist_ap"]
    ):
        table_rows.append(
            "<tr>"
            f'<td>{_fmt_day(d)}</td>'
            f'<td class="num">{lake_v:.2f}</td>'
            f'<td class="num">{ff_v:.1f}</td>'
            f'<td class="num">{ap_v:.1f}</td>'
            f'<td class="tag">Observed</td>'
            "</tr>"
        )
    for d, lake_v, ff_v, ap_v in zip(
        series["proj_days"], series["proj_lake"], series["proj_ff"], series["proj_ap"]
    ):
        table_rows.append(
            '<tr class="proj">'
            f'<td>{_fmt_day(d)}</td>'
            f'<td class="num">{lake_v:.2f}</td>'
            f'<td class="num">{ff_v:.1f}</td>'
            f'<td class="num">{ap_v:.1f}</td>'
            f'<td class="tag">Modeled</td>'
            "</tr>"
        )
    data_table_body = "\n                ".join(table_rows)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="1800">
  <title>Mississippi Lake High Water Update</title>
  <meta name="description" content="People of the Lake — Mississippi Lake Ontario water level, inflow/outflow, and dock guidance. Updated several times daily.">
  <link rel="icon" href="favicon.svg" type="image/svg+xml">
  <link rel="icon" href="favicon.png" type="image/png" sizes="32x32">
  <link rel="apple-touch-icon" href="apple-touch-icon.png">
  <style>
    .kpi-grid {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:12px; }}
    .kpi {{ background:#f4f8f9; border:1px solid #d7e4e8; border-radius:10px; padding:14px 12px; text-align:center; }}
    .kpi-label {{ margin:0 0 8px 0; font-family:Arial,Helvetica,sans-serif; font-size:10px; letter-spacing:0.08em; text-transform:uppercase; color:#5a7a86; }}
    .kpi-value {{ margin:0; font-family:Arial,Helvetica,sans-serif; font-size:24px; font-weight:700; color:#1a3a4a; line-height:1.1; }}
    .kpi-sub {{ margin:8px 0 0 0; font-family:Arial,Helvetica,sans-serif; font-size:11px; color:#6a7c84; line-height:1.35; }}
    .ticker {{ display:inline-block; margin-top:6px; font-family:Arial,Helvetica,sans-serif; font-size:12px; font-weight:700; letter-spacing:0.02em; }}
    .ticker.up {{ color:#0b6e4f; }}
    .ticker.down {{ color:#c45c26; }}
    .ticker.flat {{ color:#5a7a86; font-size:14px; }}
    .data-fresh {{ font-family:Arial,Helvetica,sans-serif; text-align:right; min-width:168px; }}
    .data-fresh-heading {{ margin:0 0 8px 0; font-size:10px; letter-spacing:0.1em; text-transform:uppercase; color:#8eb8c8; }}
    .gauge-fresh {{ margin:0 0 8px 0; }}
    .gauge-fresh:last-child {{ margin-bottom:0; }}
    .gauge-fresh-name {{ margin:0; font-size:10px; letter-spacing:0.04em; text-transform:uppercase; color:#8eb8c8; }}
    .gauge-fresh-age {{ margin:2px 0 0 0; font-size:13px; font-weight:700; line-height:1.2; }}
    .gauge-fresh-age.fresh-ok {{ color:#7dcea0; }}
    .gauge-fresh-age.fresh-warn {{ color:#f4d35e; }}
    .gauge-fresh-age.fresh-stale {{ color:#e07a5f; }}
    .gauge-fresh-age.fresh-unknown {{ color:#b7d0da; }}
    .gauge-fresh-when {{ margin:2px 0 0 0; font-size:10px; color:#7a96a3; line-height:1.25; }}
    .data-table {{ width:100%; border-collapse:collapse; font-family:Arial,Helvetica,sans-serif; font-size:13px; color:#243036; }}
    .data-table th {{ text-align:left; padding:8px 6px; border-bottom:2px solid #d5dde3; color:#5a7a86; font-size:11px; letter-spacing:0.06em; text-transform:uppercase; font-weight:700; }}
    .data-table td {{ padding:7px 6px; border-bottom:1px solid #e4ebef; }}
    .data-table td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
    .data-table th.num {{ text-align:right; }}
    .data-table td.tag {{ color:#6a7c84; font-size:12px; }}
    .data-table tr.proj td {{ color:#5a7078; font-style:italic; }}
    .data-table tr:last-child td {{ border-bottom:0; }}
    .chart-thumb {{ cursor:zoom-in; max-width:100%; height:auto; border:1px solid #d5dde3; border-radius:6px; display:block; transition:opacity .15s ease; }}
    .chart-thumb:hover {{ opacity:0.92; }}
    .lightbox {{ display:none; position:fixed; inset:0; z-index:1000; background:rgba(10,20,28,0.92); align-items:center; justify-content:center; padding:24px; box-sizing:border-box; }}
    .lightbox.open {{ display:flex; }}
    .lightbox img {{ max-width:min(1200px,96vw); max-height:92vh; width:auto; height:auto; border-radius:6px; box-shadow:0 12px 40px rgba(0,0,0,0.45); background:#fff; }}
    .lightbox-close {{ position:fixed; top:16px; right:20px; border:0; background:rgba(255,255,255,0.12); color:#fff; font:600 14px/1 Arial,Helvetica,sans-serif; padding:10px 14px; border-radius:8px; cursor:pointer; }}
    .lightbox-hint {{ position:fixed; bottom:16px; left:50%; transform:translateX(-50%); color:rgba(255,255,255,0.7); font:12px/1.4 Arial,Helvetica,sans-serif; }}
    @media (max-width:640px) {{
      .kpi-grid {{ grid-template-columns:1fr 1fr; }}
    }}
    @media (max-width:420px) {{
      .kpi-grid {{ grid-template-columns:1fr; }}
      .kpi-value {{ font-size:22px; }}
    }}
  </style>
</head>
<body style="margin:0;padding:0;background:#eef2f4;font-family:Georgia,'Times New Roman',serif;">
  <div id="chart-lightbox" class="lightbox" role="dialog" aria-modal="true" aria-label="Full screen chart" onclick="if(event.target===this)closeChart()">
    <button type="button" class="lightbox-close" onclick="closeChart()" aria-label="Close">Close ✕</button>
    <img id="lightbox-img" src="{chart_src}" alt="Full screen chart">
    <div class="lightbox-hint">Click outside or press Esc to close</div>
  </div>
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#eef2f4;padding:24px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" width="640" cellspacing="0" cellpadding="0" style="max-width:640px;width:100%;background:#ffffff;border-radius:8px;overflow:hidden;border:1px solid #d5dde3;">
          <tr>
            <td style="background:#1a3a4a;padding:28px 32px 24px 32px;">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                <tr>
                  <td style="vertical-align:top;padding-right:16px;">
                    <p style="margin:0 0 6px 0;font-family:Arial,Helvetica,sans-serif;font-size:12px;letter-spacing:0.12em;text-transform:uppercase;color:#8eb8c8;">Mississippi Lake · Ontario</p>
                    <h1 style="margin:0;font-family:Georgia,serif;font-size:28px;line-height:1.25;font-weight:normal;color:#ffffff;">Water Update for Mississippi Cottagers</h1>
                    <p style="margin:10px 0 0 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#b7d0da;">Updated {when} EDT · auto-refreshes hourly, but dependent on MVCA data updates</p>
                  </td>
                  <td style="vertical-align:middle;width:1%;">
                    <div class="data-fresh" title="How fresh each gauge reading is">
                      <p class="data-fresh-heading">Gauge freshness</p>
                      <div class="gauge-fresh" data-as-of="{lake_as_of_iso}">
                        <p class="gauge-fresh-name">Lake level</p>
                        <p class="gauge-fresh-age fresh-unknown">Checking…</p>
                        <p class="gauge-fresh-when">{lake_as_of_edt}</p>
                      </div>
                      <div class="gauge-fresh" data-as-of="{ff_as_of_iso}">
                        <p class="gauge-fresh-name">Ferguson’s Falls</p>
                        <p class="gauge-fresh-age fresh-unknown">Checking…</p>
                        <p class="gauge-fresh-when">{ff_as_of_edt}</p>
                      </div>
                      <div class="gauge-fresh" data-as-of="{ap_as_of_iso}">
                        <p class="gauge-fresh-name">Appleton</p>
                        <p class="gauge-fresh-age fresh-unknown">Checking…</p>
                        <p class="gauge-fresh-when">{ap_as_of_edt}</p>
                      </div>
                    </div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="background:#2f6f7e;padding:14px 32px;font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#ffffff;">
              <strong>Status:</strong> {status} · MVCA Water Safety — check <a href="https://mvc.on.ca/flood-status/" style="color:#ffffff;">mvc.on.ca/flood-status</a> · Flooding not expected unless status changes
            </td>
          </tr>
          <tr>
            <td style="padding:28px 32px 8px 32px;font-family:Georgia,serif;font-size:16px;line-height:1.55;color:#243036;">
              <p style="margin:0 0 14px 0;">People of the Lake,</p>
              <p style="margin:0 0 14px 0;">Live briefing for Mississippi Lake. Gauges below are from MVCA / Water Survey of Canada. The chart includes a <strong>7-day model outlook</strong> (not an official forecast).</p>
            </td>
          </tr>
          <tr>
            <td style="padding:12px 32px 8px 32px;">
              <p style="margin:0 0 12px 0;font-family:Arial,Helvetica,sans-serif;font-size:12px;letter-spacing:0.1em;text-transform:uppercase;color:#5a7a86;">{glance_label} - At a glance <span style="letter-spacing:0;text-transform:none;color:#8a9aa2;">· vs prior day</span></p>
              <div class="kpi-grid">
                <div class="kpi">
                  <p class="kpi-label">Current lake level</p>
                  <p class="kpi-value">{lake:.2f}<span style="font-size:13px;font-weight:600;color:#5a7a86;"> m</span></p>
                  <div>{lake_tick}</div>
                  <p class="kpi-sub">{lake_sub}</p>
                </div>
                <div class="kpi">
                  <p class="kpi-label">Historic average</p>
                  <p class="kpi-value">{hist_avg_html}<span style="font-size:13px;font-weight:600;color:#5a7a86;"> m</span></p>
                  <p class="kpi-sub">{hist_sub}</p>
                </div>
                <div class="kpi">
                  <p class="kpi-label">7-day outlook</p>
                  <p class="kpi-value" style="color:{outlook_color};">{proj:.2f}<span style="font-size:13px;font-weight:600;color:#5a7a86;"> m</span></p>
                  <div>{outlook_tick}</div>
                  <p class="kpi-sub">{dcm:+.0f} cm modeled change<br>not an official forecast</p>
                </div>
                <div class="kpi">
                  <p class="kpi-label">Inflow · Ferguson’s Falls</p>
                  <p class="kpi-value">{ff:.1f}</p>
                  <div>{ff_tick}</div>
                  <p class="kpi-sub">m³/s<br>into Mississippi Lake</p>
                </div>
                <div class="kpi">
                  <p class="kpi-label">Outflow · Appleton</p>
                  <p class="kpi-value">{ap:.1f}</p>
                  <div>{ap_tick}</div>
                  <p class="kpi-sub">m³/s<br>downstream of the lake</p>
                </div>
                <div class="kpi">
                  <p class="kpi-label">In − out gap</p>
                  <p class="kpi-value" style="color:{gap_color};">{gap:+.1f}</p>
                  <div>{gap_tick}</div>
                  <p class="kpi-sub">m³/s · {gap_label}<br>positive = lake filling</p>
                </div>
              </div>
            </td>
          </tr>
          <tr>
            <td style="padding:16px 32px 4px 32px;font-family:Georgia,serif;font-size:16px;color:#243036;">
              <h2 style="margin:0 0 8px 0;font-size:20px;color:#1a3a4a;">Inflow, outflow &amp; Mississippi lake level</h2>
              <p style="margin:0 0 12px 0;font-size:15px;">Solid = last 7 days observed. Dashed = next 7 days modeled. Rain-sensitive days in forecast: {rain_note}. <span style="color:#5a7a86;">Click the chart for full screen.</span></p>
            </td>
          </tr>
          <tr>
            <td style="padding:0 24px 8px 24px;" align="center">
              <img src="{chart_src}" width="592" class="chart-thumb" alt="Inflow, outflow, and lake level chart with projection — click to enlarge" onclick="openChart('{chart_src}')" title="Click to view full screen">
            </td>
          </tr>
          <tr>
            <td style="padding:4px 32px 16px 32px;font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:1.45;color:#5a7078;">
              <strong>Model:</strong> Δh ≈ k · (Q<sub>in</sub> − Q<sub>out</sub>) / A with A ≈ 25 km²; k calibrated to recent observed rise; Qin recession from peak + forecast rain bumps.
            </td>
          </tr>
          <tr>
            <td style="padding:8px 32px;font-family:Georgia,serif;font-size:16px;line-height:1.55;color:#243036;">
              <h2 style="margin:0 0 10px 0;font-size:20px;color:#1a3a4a;">Dock guidance</h2>
              <ul style="margin:0;padding-left:20px;">
                <li>Expect little or no freeboard on many docks while levels stay elevated.</li>
                <li>Secure floatables; check lines, chains, and shore power.</li>
                <li>Wait for a sustained multi-day drop before major dock reconfiguration.</li>
              </ul>
            </td>
          </tr>
          <tr>
            <td style="padding:16px 32px 8px 32px;font-family:Georgia,serif;font-size:16px;color:#243036;">
              <h2 style="margin:0 0 8px 0;font-size:20px;color:#1a3a4a;">Chart data</h2>
              <p style="margin:0 0 12px 0;font-size:14px;color:#5a7078;font-family:Arial,Helvetica,sans-serif;">Daily values from the chart above. Inflow = Ferguson’s Falls; outflow = Appleton. Modeled rows are the 7-day outlook (not an official forecast).</p>
              <table class="data-table" role="table">
                <thead>
                  <tr>
                    <th scope="col">Date</th>
                    <th class="num" scope="col">Lake (m MASL)</th>
                    <th class="num" scope="col">Inflow (m³/s)</th>
                    <th class="num" scope="col">Outflow (m³/s)</th>
                    <th scope="col">Source</th>
                  </tr>
                </thead>
                <tbody>
                {data_table_body}
                </tbody>
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:20px 32px 4px 32px;font-family:Georgia,serif;font-size:16px;color:#243036;">
              <h2 style="margin:0 0 8px 0;font-size:20px;color:#1a3a4a;">Year-to-date Mississippi lake level</h2>
              <p style="margin:0 0 12px 0;font-size:15px;">Monthly mean lake level for {series.get("ytd_year") or ""} so far, compared with the long-term monthly average. <span style="color:#5a7a86;">Click the chart for full screen.</span></p>
            </td>
          </tr>
          <tr>
            <td style="padding:0 24px 16px 24px;" align="center">
              <img src="{ytd_src}" width="592" class="chart-thumb" alt="Year-to-date monthly lake level chart — click to enlarge" onclick="openChart('{ytd_src}')" title="Click to view full screen">
            </td>
          </tr>
          <tr>
            <td style="padding:12px 32px 4px 32px;font-family:Georgia,serif;font-size:16px;color:#243036;">
              <h2 style="margin:0 0 8px 0;font-size:20px;color:#1a3a4a;">Watershed profile</h2>
              <p style="margin:0 0 12px 0;font-size:15px;">Core chain from Crotch Lake through Mississippi Lake to Appleton, with current levels/flows and ~7-day trends. <span style="color:#5a7a86;">Click for full screen. Not to scale.</span></p>
            </td>
          </tr>
          <tr>
            <td style="padding:0 24px 8px 24px;" align="center">
              <img src="{watershed_src}" width="592" class="chart-thumb" alt="Mississippi watershed elevation profile with levels, flows, and 7-day trends — click to enlarge" onclick="openChart('{watershed_src}')" title="Click to view full screen">
            </td>
          </tr>
          <tr>
            <td style="padding:12px 32px 4px 32px;font-family:Georgia,serif;font-size:16px;color:#243036;">
              <h2 style="margin:0 0 8px 0;font-size:20px;color:#1a3a4a;">Full watershed system</h2>
              <p style="margin:0 0 12px 0;font-size:15px;">Headwaters (Shabomeka / Mazinaw) through all major dams and generating stations to the Ottawa River confluence, including side-reservoir levels where gauges report. <span style="color:#5a7a86;">Click for full screen. Not to scale.</span></p>
            </td>
          </tr>
          <tr>
            <td style="padding:0 24px 16px 24px;" align="center">
              <img src="{watershed_full_src}" width="592" class="chart-thumb" alt="Full Mississippi watershed elevation profile with dams, lake levels, flows, and 7-day trends — click to enlarge" onclick="openChart('{watershed_full_src}')" title="Click to view full screen">
            </td>
          </tr>
          <tr>
            <td style="padding:16px 32px 28px 32px;font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#243036;">
              <p style="margin:0 0 6px 0;">• MVCA levels: <a href="https://www.mvc.on.ca/water-levels" style="color:#2f6f7e;">mvc.on.ca/water-levels</a></p>
              <p style="margin:0 0 14px 0;">• MVCA status: <a href="https://mvc.on.ca/flood-status/" style="color:#2f6f7e;">mvc.on.ca/flood-status</a> · 613-253-0006 ext. 248</p>
              <p style="margin:0;padding:14px 16px;background:#fff8e8;border:1px solid #ead9a8;border-radius:6px;font-size:13px;color:#5a4a20;">
                <strong>Disclaimer:</strong> Provisional gauges + simplified model for cottager awareness only. Not an official flood forecast.
              </p>
            </td>
          </tr>
          <tr>
            <td style="background:#f0f4f6;padding:16px 32px;font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#6a7c84;border-top:1px solid #d5dde3;">
              People of the Lake · Source on GitHub · Updated automatically ~8:00, 13:00, 18:00 America/Toronto
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
  <script>
    function openChart(src) {{
      var lb = document.getElementById('chart-lightbox');
      var img = document.getElementById('lightbox-img');
      if (img && src) img.src = src;
      lb.classList.add('open');
      document.body.style.overflow = 'hidden';
    }}
    function closeChart() {{
      var lb = document.getElementById('chart-lightbox');
      lb.classList.remove('open');
      document.body.style.overflow = '';
    }}
    document.addEventListener('keydown', function(e) {{
      if (e.key === 'Escape') closeChart();
    }});
    (function updateGaugeFreshness() {{
      function setAge(el, iso) {{
        if (!iso) {{
          el.textContent = 'Unavailable';
          el.className = 'gauge-fresh-age fresh-unknown';
          return;
        }}
        var then = new Date(iso);
        if (isNaN(then.getTime())) {{
          el.textContent = 'Unavailable';
          el.className = 'gauge-fresh-age fresh-unknown';
          return;
        }}
        var mins = Math.max(0, Math.round((Date.now() - then.getTime()) / 60000));
        var hours = mins / 60;
        var label;
        if (mins < 1) label = 'Just now';
        else if (mins < 60) label = mins + ' min ago';
        else if (mins < 120) label = '1 hour ago';
        else label = Math.floor(hours) + ' hours ago';
        var cls = hours <= 1 ? 'fresh-ok' : (hours <= 4 ? 'fresh-warn' : 'fresh-stale');
        el.textContent = label;
        el.className = 'gauge-fresh-age ' + cls;
      }}
      var nodes = document.querySelectorAll('.gauge-fresh');
      for (var i = 0; i < nodes.length; i++) {{
        var wrap = nodes[i];
        var ageEl = wrap.querySelector('.gauge-fresh-age');
        if (!ageEl) continue;
        setAge(ageEl, wrap.getAttribute('data-as-of') || '');
      }}
    }})();
  </script>
</body>
</html>
"""
    INDEX.write_text(html)


def main() -> None:
    DATA.mkdir(exist_ok=True)
    print("Fetching gauges…")
    try:
        series = build_series()
    except Exception as e:
        print(f"Live gauge refresh failed: {e}")
        if CACHE.exists():
            print(f"Keeping previous site files from {CACHE.name} (workflow continues).")
            return
        raise
    CACHE.write_text(json.dumps(series, indent=2))
    print("Rendering chart…")
    render_chart(series)
    print("Rendering YTD chart…")
    if not render_ytd_chart(series):
        print("  (skipped YTD chart — no monthly data)")
    print("Rendering watershed profile…")
    if not render_watershed_chart(series):
        print("  (skipped watershed profile — no node data)")
    print("Rendering full watershed profile…")
    if not render_watershed_full_chart(series):
        print("  (skipped full watershed profile — no node data)")
    print("Writing index.html…")
    render_html(series)
    print(f"Done. Lake={series['latest_lake']:.3f} FF={series['latest_ff']:.1f} gap={series['gap_now']:+.1f}")
    print(f"Wrote {INDEX}, {CHART_PNG}, {YTD_CHART_PNG}, {WATERSHED_PNG}, and {WATERSHED_FULL_PNG}")


if __name__ == "__main__":
    main()
