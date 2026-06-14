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

## Requirements

- Python 3.8+
- [fitparse](https://github.com/dtcooper/python-fitparse) for FIT file support
- [garminconnect](https://github.com/cyberjunky/python-garminconnect) for Garmin Connect sync
- [python-dotenv](https://github.com/theskumar/python-dotenv) for loading credentials from `.env`
- [reverse_geocode](https://github.com/richardpenman/reverse_geocode) for offline city lookup in auto-generated descriptions

Install dependencies:

```bash
pip install flask fitparse garminconnect python-dotenv reverse_geocode
```

## Getting started

```bash
git clone https://github.com/ttalola/training-log.git
cd training-log
pip install flask fitparse garminconnect python-dotenv reverse_geocode
python app.py
```

Then open [http://localhost:8080](http://localhost:8080) in your browser.

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
python garmin_setup.py
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
