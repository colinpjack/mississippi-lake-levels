# Mississippi Lake — People of the Lake

Live cottager briefing for **Mississippi Lake, Ontario**: lake level, Ferguson’s Falls inflow, Appleton outflow, 7-day outlook chart, dock/boat guidance, and (optionally) **cottage weather from an Ambient Weather WS-4000**.

**Site (after you enable Pages):**  
`https://<your-github-username>.github.io/mississippi-lake-levels/`

## What’s included

| File | Purpose |
|------|---------|
| `index.html` | Public briefing page |
| `chart.png` | Inflow / outflow / lake level + projection |
| `weather_chart.png` | Cottage weather (~24h) when Ambient keys are set |
| `update_site.py` | Fetches gauges + weather, rebuilds page |
| `ambient_weather.py` | Free AmbientWeather.net REST client (metric) |
| `pi_weather_server.py` | Raspberry Pi Custom Server receiver (LAN, no cloud keys) |
| `pi/publish.sh` | On the Pi: rebuild site + `git push` for Pages |
| `pi/mississippi-weather.service` | systemd unit for the receiver |
| `data/chart_series.json` | Latest lake numbers used for the chart |
| `data/weather.json` | Latest cottage weather (Ambient API **or** Pi receiver) |
| `.github/workflows/update.yml` | Runs **hourly** and deploys Pages |

## One-time GitHub setup

1. Create the GitHub repo (if the script hasn’t already):
   ```bash
   gh repo create mississippi-lake-levels --public --source=. --remote=origin --push
   ```
2. In the repo on GitHub:
   - **Settings → Pages → Build and deployment → Source:** **GitHub Actions**
3. Allow the workflow to write:
   - **Settings → Actions → General → Workflow permissions → Read and write**
4. Trigger once: **Actions → Update lake briefing → Run workflow**

## Ambient Weather WS-4000 (free API — no AWN+ required)

The public site cannot read the console on your home Wi‑Fi. Instead the station uploads to **AmbientWeather.net** (free with the device), and GitHub Actions pulls JSON with your free API keys.

### After you buy / install the station

1. Connect the WS-4000 to **2.4 GHz Wi‑Fi** and register it on [AmbientWeather.net](https://ambientweather.net) (email walkthrough from the console is fine).
2. Wait until the station appears on your Ambient dashboard (can take a short while the first time).
3. Create free keys at [ambientweather.net/account](https://ambientweather.net/account):
   - **API Key** (user / device access)
   - **Application Key** (identifies this site)
4. In this GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**
   - `AMBIENT_API_KEY` = your API key
   - `AMBIENT_APPLICATION_KEY` = your application key
   - Optional: `AMBIENT_MAC` = station MAC if you own more than one Ambient device
5. Run **Actions → Update lake briefing → Run workflow**.  
   The job writes `data/weather.json`, `weather_chart.png`, and a **Cottage weather** section on `index.html`.

Local test (keys in the shell, never commit them):

```bash
export AMBIENT_API_KEY=...
export AMBIENT_APPLICATION_KEY=...
# optional: export AMBIENT_MAC=AA:BB:CC:DD:EE:FF
python ambient_weather.py   # or: python update_site.py
```

**Notes**

- Official REST docs: [ambientweather.docs.apiary.io](https://ambientweather.docs.apiary.io/)
- Field list: [Device Data Specs](https://github.com/ambient-weather/api-docs/wiki/Device-Data-Specs)
- Paid **AWN+** is only for Ambient’s own app extras (longer in-app history, map layers). It is **not** needed for this site’s API access.
- Do not put API keys in client-side JavaScript or in the repo.

Until secrets are configured, the lake briefing still updates; the cottage weather block appears once live data (or a prior `data/weather.json`) is available.

## Raspberry Pi 5 (recommended local path)

If the WS-4000 and a Pi share your LAN, the console can push directly to the Pi via **Custom Server**. That needs **no Ambient API keys** for capture (you can still register on AmbientWeather.net for their app if you want).

```text
WS-4000  --Custom Server HTTP-->  Pi (pi_weather_server.py)
                                      | writes data/weather.json
                                      v
                                 pi/publish.sh  -->  git push  -->  GitHub Pages
```

### One-time Pi setup

```bash
# On the Pi
cd ~
git clone <your-repo-url> mississippi-lake-levels
cd mississippi-lake-levels
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
chmod +x pi/publish.sh

# Install and start the receiver (edit paths/user in the unit if not user `pi`)
sudo cp pi/mississippi-weather.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mississippi-weather.service
curl -s http://127.0.0.1:8080/health
```

Give the Pi a **DHCP reservation** (stable LAN IP). Open port **8080** on the Pi only to the LAN (not the public internet).

### WS-4000 console → Custom Server

On the display: **Setup → Weather Server → Customized** (wording varies slightly by firmware):

| Field | Value |
|-------|--------|
| Enable | On |
| Protocol | Same as **AMBWeather** (preferred) |
| Server IP | Pi LAN IP (e.g. `192.168.1.50`) |
| Port | `8080` |
| Path | `/data/?` (trailing `?` matters on many consoles) |
| Interval | 16–60 s |

Optional: set the same `PASSKEY` on the console and in the systemd unit (`WEATHER_PASSKEY=...`).

### Publish from the Pi

```bash
# Manual
./pi/publish.sh

# Or hourly cron (keeps Pages fresh; GitHub Actions also runs hourly)
crontab -e
# 5 * * * * /home/pi/mississippi-lake-levels/pi/publish.sh >>/home/pi/lake-publish.log 2>&1
```

`publish.sh` runs `update_site.py` using the Pi’s `data/weather.json` (Ambient cloud keys are intentionally unset), then commits and pushes. Configure git on the Pi with a deploy key or `gh auth` that can push to the repo.

**Cloud vs Pi:** Prefer **one** weather source. If the Pi is publishing `weather.json`, you can leave Ambient Actions secrets unset so GitHub Actions does not overwrite Pi data on its scheduled runs—or disable weather in Actions and let the Pi own publishes.

## Local refresh

```bash
cd ~/Projects/mississippi-lake-levels
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python update_site.py
open index.html
```

## Notes

- Lake data: Water Survey of Canada (flows) + MVCA/KiWIS (lake level) + Open-Meteo (rain for projection bumps).
- Weather data: AmbientWeather.net REST **or** Raspberry Pi Custom Server receiver (`pi_weather_server.py`); imperial → metric in `ambient_weather.py`.
- Projection is a simple storage-routing model — **not** an official MVCA forecast.
- GitHub Actions schedule runs **hourly** (`0 * * * *` UTC). Free-plan cron can still be delayed by several minutes.

## Disclaimer

Provisional public data for cottager awareness only. Follow [MVCA flood status](https://mvc.on.ca/flood-status/) for official messages.
