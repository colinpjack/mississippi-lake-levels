#!/usr/bin/env python3
"""
Refresh Mississippi Lake cottager briefing for GitHub Pages.

Fetches WSC + KiWIS gauges, rebuilds projection + chart, writes index.html.
"""

from __future__ import annotations

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

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CHART_PNG = ROOT / "chart.png"
YTD_CHART_PNG = ROOT / "ytd_chart.png"
INDEX = ROOT / "index.html"
CACHE = DATA / "chart_series.json"
HISTORIC_DOY = DATA / "historic_doy_means.json"

AREA_M2 = 25e6  # Mississippi Lake ~25 km²
CM_PER_M3S_DAY = 86400 / AREA_M2 * 100
LAKE_TS = 1404042
EARLY_JULY_LEVEL = 134.10

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


def fetch_lake_daily(frm: str, to: str) -> tuple[dict[str, float], datetime | None]:
    params = {
        "service": "kisters",
        "type": "queryServices",
        "request": "getTimeseriesValues",
        "datasource": "0",
        "format": "json",
        "ts_id": str(LAKE_TS),
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
    gauge_times = [t for t in (lake_ts, ff_ts, ap_ts) if t is not None]
    data_as_of = max(gauge_times) if gauge_times else None
    data_as_of_iso = data_as_of.isoformat() if data_as_of else ""
    if data_as_of:
        local = data_as_of.astimezone(ZoneInfo("America/Toronto"))
        data_as_of_edt = f"{local.strftime('%b')} {local.day}, {local.strftime('%I:%M %p').lstrip('0')} EDT"
    else:
        data_as_of_edt = "unavailable"

    return {
        "generated_edt": edt.strftime("%Y-%m-%d %H:%M"),
        "data_as_of_iso": data_as_of_iso,
        "data_as_of_edt": data_as_of_edt,
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
    data_as_of_iso = series.get("data_as_of_iso") or ""
    data_as_of_edt = series.get("data_as_of_edt") or "unavailable"
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
    .data-fresh {{ font-family:Arial,Helvetica,sans-serif; text-align:right; min-width:140px; }}
    .data-fresh-label {{ margin:0 0 4px 0; font-size:10px; letter-spacing:0.1em; text-transform:uppercase; color:#8eb8c8; }}
    .data-fresh-age {{ margin:0; font-size:15px; font-weight:700; line-height:1.2; }}
    .data-fresh-age.fresh-ok {{ color:#7dcea0; }}
    .data-fresh-age.fresh-warn {{ color:#f4d35e; }}
    .data-fresh-age.fresh-stale {{ color:#e07a5f; }}
    .data-fresh-age.fresh-unknown {{ color:#b7d0da; }}
    .data-fresh-when {{ margin:4px 0 0 0; font-size:11px; color:#8eb8c8; line-height:1.3; }}
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
    <img id="lightbox-img" src="chart.png" alt="Full screen chart">
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
                    <h1 style="margin:0;font-family:Georgia,serif;font-size:28px;line-height:1.25;font-weight:normal;color:#ffffff;">High Water Update for Cottagers</h1>
                    <p style="margin:10px 0 0 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#b7d0da;">Updated {when} EDT · auto-refreshes hourly, but dependent on MVCA data updates</p>
                  </td>
                  <td style="vertical-align:middle;width:1%;">
                    <div class="data-fresh" id="data-freshness" data-as-of="{data_as_of_iso}" title="Newest reading from lake or flow gauges">
                      <p class="data-fresh-label">Data source</p>
                      <p class="data-fresh-age fresh-unknown" id="data-freshness-age">Checking…</p>
                      <p class="data-fresh-when">{data_as_of_edt}</p>
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
              <p style="margin:0 0 14px 0;">Live briefing for Mississippi Lake after the recent high-water pulse. Gauges below are from MVCA / Water Survey of Canada. The chart includes a <strong>7-day model outlook</strong> (not an official forecast).</p>
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
              <h2 style="margin:0 0 8px 0;font-size:20px;color:#1a3a4a;">Inflow, outflow &amp; lake level</h2>
              <p style="margin:0 0 12px 0;font-size:15px;">Solid = last 7 days observed. Dashed = next 7 days modeled. Rain-sensitive days in forecast: {rain_note}. <span style="color:#5a7a86;">Click the chart for full screen.</span></p>
            </td>
          </tr>
          <tr>
            <td style="padding:0 24px 8px 24px;" align="center">
              <img src="chart.png" width="592" class="chart-thumb" alt="Inflow, outflow, and lake level chart with projection — click to enlarge" onclick="openChart('chart.png')" title="Click to view full screen">
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
              <h2 style="margin:0 0 8px 0;font-size:20px;color:#1a3a4a;">Year-to-date lake level</h2>
              <p style="margin:0 0 12px 0;font-size:15px;">Monthly mean lake level for {series.get("ytd_year") or ""} so far, compared with the long-term monthly average. <span style="color:#5a7a86;">Click the chart for full screen.</span></p>
            </td>
          </tr>
          <tr>
            <td style="padding:0 24px 16px 24px;" align="center">
              <img src="ytd_chart.png" width="592" class="chart-thumb" alt="Year-to-date monthly lake level chart — click to enlarge" onclick="openChart('ytd_chart.png')" title="Click to view full screen">
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
    (function updateDataFreshness() {{
      var wrap = document.getElementById('data-freshness');
      var ageEl = document.getElementById('data-freshness-age');
      if (!wrap || !ageEl) return;
      var iso = wrap.getAttribute('data-as-of') || '';
      if (!iso) {{
        ageEl.textContent = 'Unavailable';
        ageEl.className = 'data-fresh-age fresh-unknown';
        return;
      }}
      var then = new Date(iso);
      if (isNaN(then.getTime())) {{
        ageEl.textContent = 'Unavailable';
        ageEl.className = 'data-fresh-age fresh-unknown';
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
      ageEl.textContent = label;
      ageEl.className = 'data-fresh-age ' + cls;
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
    print("Writing index.html…")
    render_html(series)
    print(f"Done. Lake={series['latest_lake']:.3f} FF={series['latest_ff']:.1f} gap={series['gap_now']:+.1f}")
    print(f"Wrote {INDEX}, {CHART_PNG}, and {YTD_CHART_PNG}")


if __name__ == "__main__":
    main()
