# CityTwin — AI-Powered Urban Digital Twin (working prototype)

A FastAPI backend + vanilla JS/Leaflet dashboard that models a city's traffic,
weather, air quality, and flood risk, with a "What If?" scenario simulator.

## Run it locally

```bash
python -m venv venv && source venv/bin/activate   # or venv\Scripts\activate on Windows
pip install -r requirements.txt
python train_model.py                             # builds model/traffic_model.joblib (~1-3 min)
uvicorn main:app --reload --port 8000
```
The `python train_model.py` step is required on a fresh clone: the trained model
binary is intentionally not committed (see below), so it has to be built once
from the public CSVs in `data/` before the traffic forecast endpoint works.

Then open **http://localhost:8000** in your browser. No API key, no signup,
no billing account needed — the map runs on free OpenStreetMap tiles.

### Local login — demo credentials

> **Username: `operator`**
> **Password: `citytwin-demo`**

The sign-in form is **pre-filled with these demo credentials**, so anyone who
opens the dashboard just clicks "Enter operations center" to get in — no need to
know the credentials for this prototype.

The dashboard uses SQLite-backed username/password authentication. On first
startup it creates the operator account from environment variables, if you want
to override the demo defaults:

```powershell
$env:CITYTWIN_DEFAULT_USERNAME="operator"
$env:CITYTWIN_DEFAULT_PASSWORD="your-strong-password"
```

If they are not set, the development defaults are `operator` / `citytwin-demo`.
Passwords are stored as salted PBKDF2 hashes and sessions use HttpOnly cookies.
If you change these environment variables, update `DEMO_CREDENTIALS` in
`static/app.js` (and the `value` attributes in `static/index.html`) to match, so
the pre-filled form keeps working.

### Docker

```bash
docker compose up --build
```

The SQLite database is stored in the named `citytwin_data` volume so assets,
alerts, and simulation history survive container restarts.

### Tests

```bash
pytest -q
```

The API emits one-line JSON logs to stdout. Prediction logs include model
inputs, peak output, and latency; simulation logs include inputs and results;
unhandled request errors include exception stack traces. Set
`CITYTWIN_DB_PATH` to point SQLite at another location.

The trained model binary (`model/traffic_model.joblib`) is **deliberately not
committed**: it is a ~212 MB file, which is above GitHub's 100 MB per-file
limit, and it is fully reproducible from the small public CSVs in `data/`.
Run `python train_model.py` once after installing dependencies to build it; the
same command regenerates it (and rewrites `model/metrics.json`) any time you
change the data or features. The Docker build runs this step automatically, so
`docker compose up --build` still works with no extra setup.

## Deploy it live (public URL)

**GitHub Pages will not work for this project.** Pages only serves static files
(HTML/CSS/JS), while CityTwin has a Python/FastAPI backend, a WebSocket feed,
and a trained model — it needs a host that runs a process. The repository is
already Dockerised, which is what makes deployment a few clicks.

### Memory is the constraint

With the traffic model loaded the process needs roughly **1–2 GB of RAM**
(measured: ~990 MB resident, ~1.9 GB committed). That rules out most free
tiers, which only give 256–512 MB:

| Host | Free RAM | Runs this app? |
|---|---|---|
| Hugging Face Spaces | 16 GB (CPU basic) | Yes |
| Google Cloud Run | up to 8 GB within the free allowance | Yes |
| Render (free / starter) | 512 MB | No — runs out of memory |
| Koyeb / Fly.io (free) | 256–512 MB | No — runs out of memory |

### Option A — Hugging Face Spaces (free, recommended)

1. Create a Space at <https://huggingface.co/new-space> → **SDK: Docker** →
   *Blank* → hardware **CPU basic (free)**.
2. Hugging Face reads the Space config from `README.md`, so add this front
   matter to the very top of the file:

   ```yaml
   ---
   title: CityTwin
   emoji: 🏙️
   colorFrom: blue
   colorTo: green
   sdk: docker
   app_port: 8000
   pinned: false
   ---
   ```
3. Push this repository to the Space (it is its own git remote):

   ```bash
   git remote add space https://huggingface.co/spaces/<your-user>/<space-name>
   git push space main
   ```
4. Hugging Face builds the `Dockerfile` (the build runs `train_model.py` from
   the committed CSV) and serves the app at
   `https://<your-user>-<space-name>.hf.space`.

Free Spaces sleep after a period without traffic and wake on the next request —
fine for a demo.

### Option B — Render (paid; needs a 2 GB instance)

This repository ships a `render.yaml` blueprint: in the Render dashboard go to
**New +** → **Blueprint** → pick this repository. The blueprint is pre-set to the
2 GB *standard* instance because the 512 MB free/starter instances run out of
memory as soon as a forecast is requested. Render shows the price before you
confirm.

Either option builds the model during the image build from the committed
`data/` CSVs, so no extra setup is needed.

## What's genuinely real here, and what's illustrative

Being upfront about this matters if you're using this project in an
interview — here's the honest breakdown:

| Piece | Status |
|---|---|
| **Traffic forecast** (`/api/predictions/traffic`, the "Traffic Congestion Forecast" chart) | **Real, trained ML.** A weighted RandomForest + ExtraTrees + HistGradientBoosting ensemble trained on the public *Metro Interstate Traffic Volume* dataset (Hogue, 2019, UCI ML Repository, doi:10.24432/C5X60B) — 40,565 real hourly traffic + weather records from I-94 near Minneapolis-St Paul, 2012–2018. Evaluated on a **chronological** hold-out (not shuffled) split: **R² = 0.953, MAE ≈ 241 vehicles/hour**. See `train_model.py` and `model/metrics.json`. |
| **Supplementary road demand model** | **Real but auxiliary.** A separate RandomForest is trained on 98 open Government of Canada provincial-road ADT observations. Its target is annual average daily traffic, not hourly congestion; its current chronological holdout R² is **-0.112**, so it is packaged for research/provenance but deliberately not blended into the CityTwin hourly forecast. |
| **Live weather / humidity / wind / rain** (top-left card) | **Real, live.** Fetched client-side from [Open-Meteo](https://open-meteo.com)'s free, keyless forecast API for whatever city you search. |
| **Live AQI** (Air Quality card) | **Real, live.** From Open-Meteo's air-quality API (US AQI, PM2.5, PM10). |
| **Map** | **Real.** Leaflet.js + OpenStreetMap tiles, no key required. Markers are custom demo assets (see below). |
| **City search / geocoding** | **Real.** Open-Meteo's geocoding API — works for any city worldwide. |
| **Telemetry stream** (`/ws/telemetry`, sparklines under the metric cards) | **Speed demo stream.** WebSocket speed samples remain simulated. Traffic forecast is model-driven; power demand and transit reliability are explicitly model-estimated from live weather + traffic until utility/transit provider feeds are connected. |
| **Demo map assets** (Connaught Place, India Gate, Pragati Maidan, AIIMS accident, Yamuna flood zone) | **SQLite-persisted seed data**, scoped to New Delhi. Switching cities in search re-centers the map and replaces these with a single live marker — the demo assets don't relocate. |
| **Flood-risk heat overlay** on the map | **Illustrative geospatial visualization**, not a calibrated hydrology model. It weights points near flood-tagged assets. A real version would need elevation, drainage network, and historical flood data (see "Next steps"). |
| **Flood/traffic risk scores in "What If? Simulation"** | **Rule-based heuristic** (rainfall × drainage-style multiplier, traffic × network-load multiplier). Explainable by design, not ML — see `run_simulation()` in `main.py`. |
| **City Health Index breakdown** (Environment/Mobility/Safety/Infrastructure) | **Demo values**, except Environment, which shifts with live AQI. |

## Project structure

```
main.py              FastAPI app: overview, model info, forecasts, simulations, telemetry WS
database.py          SQLite schema, seed data, and persistence helpers
logging_config.py    JSON formatter and application logging setup
traffic_model.py      Loads the trained model, turns predicted volume into a 0-100 congestion score
train_model.py         Trains the RandomForest on the real dataset (rerun anytime)
data/                  Metro_Interstate_Traffic_Volume.csv (real, public dataset)
					   Traffic_Volumes_Provincial_Highway_System.csv (Government of Canada road counts)
model/                 metrics.json (accuracy report) + traffic_model.joblib (built by train_model.py, not committed)
static/                index.html, app.js, styles.css — the dashboard
tests/                 pytest coverage for API persistence and prediction shape
Dockerfile             Reproducible production container
docker-compose.yml     One-command local deployment with persistent SQLite
```

## API surface

- `GET /api/overview` — city, health index, metrics, alerts, map assets
- `GET /api/model/info` — real training metrics (dataset size, date range, R², MAE)
- `GET /api/predictions/traffic?location=&rain_mm=&temp_c=&clouds=&weather=` — ML-driven forecast, next 3 hours
- `POST /api/simulations` — what-if scenario (rainfall, blocked road, traffic increase)
- `GET /api/simulations` — persisted simulation history and count
- `POST /api/routes/emergency` — routing decision metadata (stubbed)
- `WS /ws/telemetry` — simulated live telemetry (traffic speed, AQI, power draw)

## Next steps if you keep building this

- **Flood model**: swap the heuristic for a model trained on real rainfall + elevation + drainage + historical-flood data (e.g., a DEM for your city + IMD rainfall records).
- **Emergency routing**: replace the stub with a real shortest-path/ETA engine over OSM road-network data (e.g., OSRM or a NetworkX graph you build from `osmnx`).
- **PostGIS**: move the hard-coded `ASSETS` list into a real PostGIS table so assets are queryable spatially.
- **Multiple traffic sensors**: the current model comes from a single physical sensor; a per-city deployment would need local traffic-count data to retrain against.
