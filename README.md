# Training Log

A locally-run web app for viewing Garmin activity files. Import TCX and FIT files, sync automatically from Garmin Connect, and browse your training history in the browser — no cloud account required.

## Features

- **Activity list** — sortable table with date, type, distance, time, speed, HR, cadence, power, and calories
- **Detail view** — GPS map, key stats, elevation/speed/power/cadence chart, detailed stats breakdown, per-lap intervals, and best splits
- **Best Splits** — sliding-window analysis of best average speed, power, HR, and cadence over standard distances and durations
- **Garmin Connect sync** — one-click sync of recent activities via the Garmin Connect API
- **Import** — upload multiple files via button or drag-and-drop anywhere on the page
- **Sport-aware display** — pace in min:sec/km for runs, min:sec/100m for swims; power and cadence hidden where not applicable
- **Activity type detection** — distinguishes outdoor rides, virtual rides (Zwift etc.), indoor rides, treadmill runs, pool swims, and open water swims
- **SQLite cache** — each file is parsed once and cached; subsequent loads are instant
- **Duplicate prevention** — activities with the same start time and type are automatically deduplicated

## Starting the app

```bash
cd ~/apps/training-log
./start.sh
```

Then open [http://localhost:8080](http://localhost:8080). Stop it with **Ctrl+C**.

The first run creates a project virtualenv in `.venv/` (Python 3.14, via `uv` if installed, otherwise `python3 -m venv`) and installs `requirements.txt`. Later runs start straight away.

> **Why not `python3 app.py`?** Homebrew upgrades `python3` to new versions (it is 3.15 at the time of writing), and packages installed into the old version disappear from the new one — the app then dies with `No module named 'flask'`. The `.venv` pins the app to its own interpreter, so Homebrew upgrades no longer break it.

If the venv itself breaks (e.g. Homebrew removed Python 3.14), rebuild it:

```bash
./start.sh --reinstall
```

On startup the app creates any missing DB tables, removes duplicate activities, and then parses new files in `activities/` on the first page load (cached in `activities.db` afterwards). Nothing runs in the background — the app is only up while `start.sh` is running in a terminal.

### Requirements

- Python 3.14 (Homebrew: `brew install python@3.14`), or `uv` which fetches it automatically
- Packages in `requirements.txt`: Flask, [fitparse](https://github.com/dtcooper/python-fitparse) (FIT files), [garminconnect](https://github.com/cyberjunky/python-garminconnect) (sync), [python-dotenv](https://github.com/theskumar/python-dotenv) (`.env` credentials), [reverse_geocode](https://github.com/richardpenman/reverse_geocode) (offline place names in descriptions)
- Optional: [LM Studio](https://lmstudio.ai) serving `google_gemma-4-26b-a4b-it` on `http://127.0.0.1:1234` for AI descriptions/analyses (see `describe.py`)

## Helper scripts

Run these with the venv's Python from the project directory, so they see the same packages and database:

| Command | What it does |
|---|---|
| `.venv/bin/python garmin_setup.py` | One-time Garmin Connect login (handles MFA), saves tokens to `garmin_tokens/` |
| `.venv/bin/python import_strava.py <export_dir> [--apply] [--limit N]` | Import a Strava bulk export; dry run unless `--apply` |
| `.venv/bin/python generate_descriptions.py [--rule] [--limit N] [--force]` | Batch-generate short activity descriptions (LM Studio, or `--rule` offline) |
| `.venv/bin/python generate_analyses.py [--limit N] [--force]` | Batch-generate long AI analyses (slow, ~1 min each; resumable) |
| `.venv/bin/python retry_failed.py [--apply]` | Re-queue files the parse cache had marked as failed |

## Adding activities

**Option A — Sync from Garmin Connect:** set up credentials (see below) and click **↻ Sync Garmin** in the top-right corner to import your latest activities automatically.

**Option B — Import in the browser:** click **+ Import** and select one or more `.tcx` or `.fit` files. You can also drag and drop files anywhere on the page.

**Option C — Copy files manually:** drop `.tcx` or `.fit` files into the `activities/` folder (created automatically on first run) and refresh the page.

## Garmin Connect sync setup

Create a `.env` file in the project root:

```
GARMIN_EMAIL=your@email.com
GARMIN_PASSWORD=yourpassword
```

Then run the setup script once from the terminal to authenticate and save tokens:

```bash
.venv/bin/python garmin_setup.py
```

This handles multi-factor authentication interactively if enabled on your account. After the initial setup, the **↻ Sync Garmin** button in the app works without re-entering credentials. Tokens are stored in `garmin_tokens/` (gitignored).

## Importing your full Garmin history

You can download your complete activity history from Garmin Connect:

1. Go to [Garmin Connect](https://connect.garmin.com) → Account → Data Management → Export Data
2. Wait for the export email (can take up to 24 hours)
3. Extract `DI_CONNECT/DI-Connect-Uploaded-Files/UploadedFiles_0-_Part1.zip` into the `activities/` folder

## Optional configuration

Open `app.py` and set these two constants near the top to unlock power-based metrics (TSS, IF, VI, w/kg):

```python
ATHLETE_FTP = 275        # your FTP in watts
ATHLETE_WEIGHT_KG = 70   # your weight in kg
```

## Exporting individual files from Garmin Connect

1. Open an activity on [Garmin Connect](https://connect.garmin.com)
2. Click the **⋯** menu → **Export to TCX** (or **Export Original** for FIT)
3. Import the downloaded file into Training Log
