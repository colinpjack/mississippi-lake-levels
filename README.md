# Mississippi Lake — People of the Lake

Live cottager briefing for **Mississippi Lake, Ontario**: lake level, Ferguson’s Falls inflow, Appleton outflow, and 7-day and 60-day outlook charts.

**Site:** https://colinpjack.github.io/mississippi-lake-levels/  
GitHub Actions refreshes gauges about hourly, commits the generated page, and deploys GitHub Pages.

## What’s included

| File | Purpose |
|------|---------|
| `index.html` | Public briefing page |
| `chart.png` | Inflow / outflow / lake level + 7-day outlook |
| `chart_60.png` | Same water-balance chart for the last ~60 days + 60-day outlook |
| `update_site.py` | Fetches gauges + weather, rebuilds page |
| `data/chart_series.json` | Latest numbers used for the chart |
| `.github/workflows/update.yml` | Refreshes gauges, commits files, and deploys Pages |

## Local refresh

```bash
cd ~/Projects/mississippi-lake-levels
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python update_site.py
open index.html
```

## Notes

- Data: Water Survey of Canada (flows) + MVCA/KiWIS (lake level) + Open-Meteo (rain for projection bumps).
- Projection is a simple storage-routing model — **not** an official MVCA forecast.
- Schedule cron is **twice an hour** at :23 and :53 UTC (not :00 — GitHub often delays or drops jobs at the start of the hour). Cron is still best-effort; GitHub may skip slots under load.

## Disclaimer

Provisional public data for cottager awareness only. Follow [MVCA flood status](https://mvc.on.ca/flood-status/) for official messages.
