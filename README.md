# Training Log

A locally-run web app for viewing Garmin activity files. Import TCX and FIT files exported from Garmin Connect and browse them in your browser — no cloud account required.

![List view](https://github.com/user-attachments/assets/placeholder)

## Features

- **Activity list** — sortable table with date, type, distance, time, speed, HR, cadence, power, and calories
- **Detail view** — GPS map, key stats, elevation/speed/power/cadence chart, detailed stats breakdown, and per-lap intervals table
- **Import** — upload multiple files via button or drag-and-drop anywhere on the page
- **SQLite cache** — each file is parsed once and cached; subsequent loads are instant
- **Activity type detection** — distinguishes outdoor rides, virtual rides (Zwift etc.), indoor rides, treadmill runs, pool swims, and open water swims from the file metadata

## Requirements

- Python 3.8+
- [fitparse](https://github.com/dtcooper/python-fitparse) for FIT file support

Install dependencies:

```bash
pip install flask fitparse
```

## Getting started

```bash
git clone https://github.com/ttalola/training-log.git
cd training-log
pip install flask fitparse
python app.py
```

Then open [http://localhost:8080](http://localhost:8080) in your browser.

## Adding activities

**Option A — Import in the browser:** click **+ Import** in the top-right corner and select one or more `.tcx` or `.fit` files. You can also drag and drop files anywhere on the page.

**Option B — Copy files manually:** drop `.tcx` or `.fit` files into the `activities/` folder (created automatically on first run) and refresh the page.

## Optional configuration

Open `app.py` and set these two constants near the top to unlock power-based metrics (TSS, IF, VI, w/kg):

```python
ATHLETE_FTP = 275        # your FTP in watts
ATHLETE_WEIGHT_KG = 70   # your weight in kg
```

## Exporting files from Garmin Connect

1. Open an activity on [Garmin Connect](https://connect.garmin.com)
2. Click the **⋯** menu → **Export to TCX** (or **Export Original** for FIT)
3. Import the downloaded file into Training Log
