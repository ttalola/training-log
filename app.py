import io
import json
import math
import os
import sqlite3
import statistics as _stats
import zipfile
from datetime import datetime, timezone

from flask import Flask, abort, jsonify, render_template, request
from werkzeug.utils import secure_filename

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
except ImportError:
    pass

app = Flask(__name__)

ACTIVITIES_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'activities')
DB_PATH         = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'activities.db')
GARMIN_EMAIL    = os.environ.get('GARMIN_EMAIL')
GARMIN_PASSWORD = os.environ.get('GARMIN_PASSWORD')
GARMIN_TOKENS   = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'garmin_tokens')

# Set your FTP/threshold power in watts to enable TSS/IF/VI calculation (e.g. 275)
ATHLETE_FTP = None

# Set your weight in kg to enable w/kg metrics (e.g. 75)
ATHLETE_WEIGHT_KG = None

NS = {
    'ns':  'http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2',
    'ns3': 'http://www.garmin.com/xmlschemas/ActivityExtension/v2',
}

SPORT_LABELS = {
    'Biking': 'Ride', 'Cycling': 'Ride', 'cycling': 'Ride',
    'VirtualBiking': 'Virtual Ride',
    'IndoorBiking': 'Indoor Ride', 'IndoorCycling': 'Indoor Ride',
    'Running': 'Run',  'running': 'Run',
    'Swimming': 'Swim', 'swimming': 'Swim',
    'Walking': 'Walk',  'walking': 'Walk',
    'Other': 'Other',   'generic': 'Other',
}

MAX_CHART_POINTS = 3000   # Downsample trackpoints to this many for charting


# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with get_db() as db:
        db.execute('''
            CREATE TABLE IF NOT EXISTS activities (
                filename         TEXT PRIMARY KEY,
                mtime            REAL NOT NULL,
                data_json        TEXT NOT NULL,
                coords_json      TEXT,
                trackpoints_json TEXT
            )
        ''')
        # Non-destructive migration for older DB schemas
        for col in ('coords_json', 'trackpoints_json'):
            try:
                db.execute(f'ALTER TABLE activities ADD COLUMN {col} TEXT')
            except Exception:
                pass


def db_get_summary(filename, mtime):
    with get_db() as db:
        row = db.execute(
            'SELECT data_json FROM activities WHERE filename=? AND mtime=?',
            (filename, mtime)
        ).fetchone()
    return json.loads(row['data_json']) if row else None


def db_get_detail(filename, mtime):
    with get_db() as db:
        row = db.execute(
            'SELECT data_json, coords_json, trackpoints_json FROM activities WHERE filename=? AND mtime=?',
            (filename, mtime)
        ).fetchone()
    if not row:
        return None
    # Treat NULL trackpoints_json as a cache miss (upgrading from old schema)
    if row['trackpoints_json'] is None:
        return None
    data = json.loads(row['data_json'])
    if 'laps' not in data or 'splits' not in data:
        return None
    data['coords']      = json.loads(row['coords_json'])      if row['coords_json']      else []
    data['trackpoints'] = json.loads(row['trackpoints_json']) if row['trackpoints_json'] else {}
    return data


def db_save(filename, mtime, data):
    coords      = data.pop('coords',      [])
    trackpoints = data.pop('trackpoints', {})
    with get_db() as db:
        db.execute(
            'INSERT OR REPLACE INTO activities '
            '(filename, mtime, data_json, coords_json, trackpoints_json) VALUES (?,?,?,?,?)',
            (filename, mtime, json.dumps(data), json.dumps(coords), json.dumps(trackpoints))
        )
    data['coords']      = coords
    data['trackpoints'] = trackpoints


def db_save_failed(filename, mtime):
    with get_db() as db:
        db.execute(
            'INSERT OR REPLACE INTO activities '
            '(filename, mtime, data_json, coords_json, trackpoints_json) VALUES (?,?,?,?,?)',
            (filename, mtime, '{"_failed":true}', None, None)
        )


def db_activity_exists(date_sort, activity_type, exclude_filename=None):
    """Return True if a real (non-failed) activity with the same start minute and type is cached."""
    minute = date_sort[:16]
    with get_db() as db:
        if exclude_filename:
            row = db.execute(
                "SELECT 1 FROM activities "
                "WHERE filename != ? "
                "AND json_extract(data_json, '$._failed') IS NULL "
                "AND json_extract(data_json, '$.date_sort') LIKE ? "
                "AND json_extract(data_json, '$.type') = ?",
                (exclude_filename, minute + '%', activity_type)
            ).fetchone()
        else:
            row = db.execute(
                "SELECT 1 FROM activities "
                "WHERE json_extract(data_json, '$._failed') IS NULL "
                "AND json_extract(data_json, '$.date_sort') LIKE ? "
                "AND json_extract(data_json, '$.type') = ?",
                (minute + '%', activity_type)
            ).fetchone()
    return row is not None


def deduplicate_activities():
    """Find groups of same start-minute + type, keep the richest file, delete the rest.
    Returns the number of duplicates removed."""
    with get_db() as db:
        rows = db.execute(
            "SELECT filename, trackpoints_json, data_json FROM activities "
            "WHERE json_extract(data_json, '$._failed') IS NULL"
        ).fetchall()

    from collections import defaultdict
    groups = defaultdict(list)
    for row in rows:
        d   = json.loads(row['data_json'])
        key = (d.get('date_sort', '')[:16], d.get('type', ''))
        groups[key].append((row['filename'], len(row['trackpoints_json'] or '')))

    removed = 0
    for entries in groups.values():
        if len(entries) <= 1:
            continue
        entries.sort(key=lambda x: x[1], reverse=True)   # best (most data) first
        for fname, _ in entries[1:]:
            with get_db() as db:
                db.execute('DELETE FROM activities WHERE filename=?', (fname,))
            path = os.path.join(ACTIVITIES_DIR, fname)
            if os.path.isfile(path):
                os.remove(path)
            removed += 1
    return removed


def db_remove_stale(active_filenames):
    with get_db() as db:
        cached = {r[0] for r in db.execute('SELECT filename FROM activities').fetchall()}
        stale  = cached - set(active_filenames)
        if stale:
            db.executemany('DELETE FROM activities WHERE filename=?', [(f,) for f in stale])


# ── Parsing helpers ───────────────────────────────────────────────────────────

def time_of_day_prefix(hour):
    if 5 <= hour < 12:  return 'Morning'
    if 12 <= hour < 17: return 'Afternoon'
    if 17 <= hour < 21: return 'Evening'
    return 'Night'


def fmt_time(seconds):
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s   = divmod(rem, 60)
    return f'{h:02d}:{m:02d}:{s:02d}' if h else f'{m:02d}:{s:02d}'


def calc_normalized_power(tp_watts):
    if len(tp_watts) < 30:
        return None
    tp_watts = sorted(tp_watts, key=lambda x: x[0])
    t0 = tp_watts[0][0].timestamp()
    n  = int(tp_watts[-1][0].timestamp() - t0) + 1
    j  = 0
    power_1s = []
    for i in range(n):
        t = t0 + i
        while j < len(tp_watts) - 1 and tp_watts[j + 1][0].timestamp() <= t:
            j += 1
        power_1s.append(tp_watts[j][1])
    rolling = [sum(power_1s[i:i + 30]) / 30 for i in range(n - 29)]
    return round((sum(x ** 4 for x in rolling) / len(rolling)) ** 0.25) if rolling else None


def calc_ascent(altitudes):
    if len(altitudes) < 2:
        return 0
    w = 5
    smoothed = [
        sum(altitudes[max(0, i - w // 2):min(len(altitudes), i + w // 2 + 1)]) /
        (min(len(altitudes), i + w // 2 + 1) - max(0, i - w // 2))
        for i in range(len(altitudes))
    ]
    return round(sum(max(0, smoothed[i + 1] - smoothed[i]) for i in range(len(smoothed) - 1)))


def calc_tss(duration_s, np_watts, ftp):
    if not (ftp and np_watts and duration_s):
        return None, None
    intensity    = np_watts / ftp
    tss          = round((duration_s * np_watts * intensity) / (ftp * 3600) * 100)
    tss_per_hour = round(tss / (duration_s / 3600)) if duration_s else None
    return tss, tss_per_hour


def compute_detailed_stats(chart_raw, hr_vals, summary):
    """
    chart_raw: list of (dist_km, alt_m, spd_kph, pwr_w, cad)
    hr_vals:   list of raw HR bpm values from trackpoints
    summary:   pre-computed summary dict
    Returns a dict of named sections, each a list of (value, unit, label) tuples.
    """
    speeds = sorted([r[2] for r in chart_raw if r[2] is not None and r[2] > 0.5])
    powers = sorted([r[3] for r in chart_raw if r[3] is not None and r[3] > 0])
    cads   = sorted([r[4] for r in chart_raw if r[4] is not None and r[4] > 0])
    hrs    = sorted([h for h in hr_vals if h and h > 0])

    elapsed_s = summary.get('total_time_s') or 0
    moving_s  = summary.get('moving_time_s') or elapsed_s
    calories  = summary.get('calories') or 0
    distance  = summary.get('distance_km') or 0
    sport     = summary.get('type', '')

    def pct(lst, p):
        if not lst: return None
        return lst[max(0, min(len(lst) - 1, int(len(lst) * p / 100)))]

    def sd(lst):
        return round(_stats.stdev(lst), 1) if len(lst) > 1 else None

    def pace_str(dist_km, time_s):
        """Returns pace as MM:SS /km (or /100m for swim)."""
        if not dist_km or not time_s:
            return None
        if 'Swim' in sport:
            min_per_100m = (time_s / 60) / (dist_km * 10)
            return f'{int(min_per_100m)}:{int((min_per_100m % 1) * 60):02d} /100m'
        min_per_km = (moving_s / 60) / dist_km
        return f'{int(min_per_km)}:{int((min_per_km % 1) * 60):02d} /km'

    result = {}

    # ── Essential ────────────────────────────────────────────────────────────
    move_ratio = round(moving_s / elapsed_s, 2) if elapsed_s else None
    cal_hr     = round(calories / (elapsed_s / 3600)) if (calories and elapsed_s) else None
    result['essential'] = [
        (summary.get('distance_km'),  'km',   'Distance'),
        (summary.get('ascent_m'),     'm',    'Ascent Gain'),
        (summary.get('moving_time'),  None,   'Moving Time'),
        (summary.get('total_time'),   None,   'Elapsed Time'),
        (move_ratio,                  None,   'Move Ratio'),
        (calories or None,            None,   'Calories'),
        (cal_hr,                      None,   'Calories / Hour'),
    ]

    # ── Speed ─────────────────────────────────────────────────────────────────
    has_swim_spd = 'Swim' in sport and (summary.get('avg_speed_kph') or summary.get('max_speed_kph'))
    has_run_spd  = 'Run'  in sport and (summary.get('avg_speed_kph') or summary.get('max_speed_kph'))
    if speeds or has_swim_spd or has_run_spd:
        if 'Swim' in sport:
            def kph_to_pace(kph):
                if not kph or kph <= 0: return None
                m, s = divmod(round(360 / kph), 60)
                return f'{m}:{s:02d}'
            result['speed'] = [
                (kph_to_pace(summary.get('avg_speed_kph')), '/100m', 'Average Pace'),
                (kph_to_pace(summary.get('max_speed_kph')), '/100m', 'Best Pace'),
            ]
            if speeds:
                result['speed'] += [
                    (kph_to_pace(pct(speeds, 25)), '/100m', '25% Quartile'),
                    (kph_to_pace(pct(speeds, 50)), '/100m', '50% Quartile'),
                    (kph_to_pace(pct(speeds, 75)), '/100m', '75% Quartile'),
                ]
        elif 'Run' in sport:
            def kph_to_run_pace(kph):
                if not kph or kph <= 0: return None
                m, s = divmod(round(3600 / kph), 60)
                return f'{m}:{s:02d}'
            result['speed'] = [
                (kph_to_run_pace(summary.get('avg_speed_kph')), '/km', 'Average Pace'),
                (kph_to_run_pace(summary.get('max_speed_kph')), '/km', 'Best Pace'),
            ]
            if speeds:
                result['speed'] += [
                    (kph_to_run_pace(pct(speeds, 25)), '/km', '25% Quartile'),
                    (kph_to_run_pace(pct(speeds, 50)), '/km', '50% Quartile'),
                    (kph_to_run_pace(pct(speeds, 75)), '/km', '75% Quartile'),
                ]
        else:
            result['speed'] = [
                (summary.get('avg_speed_kph'), 'kph',  'Average'),
                (summary.get('max_speed_kph'), 'kph',  'Max'),
                (pace_str(distance, moving_s), None,   'Average Pace'),
                (pct(speeds, 25),              'kph',  '25% Quartile'),
                (pct(speeds, 50),              'kph',  '50% Quartile'),
                (pct(speeds, 75),              'kph',  '75% Quartile'),
                (sd(speeds),                   'kph',  'Std Deviation σ'),
            ]

    # ── Power ────────────────────────────────────────────────────────────────
    if powers and 'Swim' not in sport and 'Run' not in sport:
        np_val  = summary.get('normalized_power')
        avg_w   = summary.get('avg_watts')
        ftp     = ATHLETE_FTP
        wkg     = ATHLETE_WEIGHT_KG
        work_kj = round(avg_w * elapsed_s / 1000) if avg_w and elapsed_s else None
        if_val  = round(np_val / ftp, 2) if (np_val and ftp) else None
        vi_val  = round(np_val / avg_w, 2) if (np_val and avg_w and avg_w > 0) else None
        result['power'] = [
            (avg_w,                             'w',    'Average'),
            (round(avg_w / wkg, 2) if (avg_w and wkg) else None, 'w/kg', 'Average /kg'),
            (np_val,                            'w',    'Avg NP®'),
            (round(np_val / wkg, 2) if (np_val and wkg) else None, 'w/kg', 'Avg NP® /kg'),
            (max(powers),                       'w',    'Max'),
            (work_kj,                           'kJ',   'Work'),
            (ftp,                               'w',    'Threshold'),
            (summary.get('tss'),                None,   'PSS'),
            (summary.get('tss_per_hour'),       None,   'PSS / Hour'),
            (if_val,                            None,   'IF®'),
            (vi_val,                            None,   'VI'),
            (pct(powers, 25),                   'w',    '25% Quartile'),
            (pct(powers, 50),                   'w',    '50% Quartile'),
            (pct(powers, 75),                   'w',    '75% Quartile'),
            (sd(powers),                        'w',    'Std Deviation σ'),
        ]

    # ── Cadence ───────────────────────────────────────────────────────────────
    if cads and 'Swim' not in sport and 'Run' not in sport:
        cad_unit = 'spm' if ('Swim' in sport or sport in ('Run', 'Walk')) else 'rpm'
        n_total  = sum(1 for r in chart_raw if r[4] is not None)
        ped_ratio = round(len(cads) / n_total, 2) if n_total else None
        ped_s     = moving_s * ped_ratio if ped_ratio else None
        result['cadence'] = [
            (summary.get('avg_active_cadence'), cad_unit, 'Active Average'),
            (summary.get('avg_cadence'),        cad_unit, 'Average'),
            (max(cads),                         cad_unit, 'Max'),
            (fmt_time(ped_s) if ped_s else None, None,    'Pedaling Time'),
            (ped_ratio,                          None,    'Pedaling Ratio'),
            (pct(cads, 25),                     cad_unit, '25% Quartile'),
            (pct(cads, 50),                     cad_unit, '50% Quartile'),
            (pct(cads, 75),                     cad_unit, '75% Quartile'),
            (sd(cads),                          cad_unit, 'Std Deviation σ'),
        ]

    # ── Heart Rate ────────────────────────────────────────────────────────────
    if hrs:
        result['heart_rate'] = [
            (summary.get('avg_hr'),  'bpm', 'Average'),
            (summary.get('max_hr'),  'bpm', 'Max'),
            (pct(hrs, 25),           'bpm', '25% Quartile'),
            (pct(hrs, 50),           'bpm', '50% Quartile'),
            (pct(hrs, 75),           'bpm', '75% Quartile'),
            (sd(hrs),                'bpm', 'Std Deviation σ'),
        ]

    return result


def compute_best_splits(raw, sport):
    """
    raw: list of (time_s, dist_m, spd_kph, pwr_w, cad, hr)
    Returns list of split dicts for best-effort distance and time windows.
    """
    if not raw or len(raw) < 10:
        return []

    is_swim = 'Swim' in sport
    is_run  = 'Run'  in sport

    if is_swim:
        dist_targets = [(50, '50 m'), (100, '100 m'), (200, '200 m'), (400, '400 m'), (1500, '1500 m')]
        time_targets = [(60, '01:00'), (300, '05:00'), (600, '10:00'), (1200, '20:00')]
    elif is_run:
        dist_targets = [(400, '400 m'), (1000, '1 km'), (5000, '5 km'), (10000, '10 km'), (21097, '21.1 km'), (42195, '42.2 km')]
        time_targets = [(60, '01:00'), (300, '05:00'), (600, '10:00'), (1200, '20:00'), (1800, '30:00'), (3600, '60:00')]
    else:
        dist_targets = [(1000, '1 km'), (5000, '5 km'), (10000, '10 km'), (20000, '20 km'), (50000, '50 km')]
        time_targets = [(60, '01:00'), (300, '05:00'), (600, '10:00'), (1200, '20:00'), (1800, '30:00'), (3600, '60:00')]

    pts_d = sorted(raw, key=lambda x: x[1] if x[1] is not None else 0)
    pts_t = sorted(raw, key=lambda x: x[0] if x[0] is not None else 0)

    def make_prefix(pts, idx):
        n = len(pts)
        ps = [0.0] * (n + 1)
        pc = [0]   * (n + 1)
        for k in range(n):
            v = pts[k][idx]
            ps[k+1] = ps[k] + (v if v is not None else 0.0)
            pc[k+1] = pc[k] + (1 if v is not None else 0)
        return ps, pc

    d_pre = [make_prefix(pts_d, i) for i in (2, 3, 4, 5)]
    t_pre = [make_prefix(pts_t, i) for i in (2, 3, 4, 5)]

    def _best(pts, dim_idx, target, prefixes):
        n = len(pts)
        dims = [p[dim_idx] for p in pts]
        if not dims or dims[-1] - dims[0] < target * 0.9:
            return None
        bests = [None] * len(prefixes)
        left = 0
        for right in range(n):
            while left + 1 < right and dims[right] - dims[left + 1] >= target:
                left += 1
            if dims[right] - dims[left] < target:
                continue
            for k, (ps, pc) in enumerate(prefixes):
                cnt = pc[right + 1] - pc[left]
                if cnt > 0:
                    avg = (ps[right + 1] - ps[left]) / cnt
                    if bests[k] is None or avg > bests[k]:
                        bests[k] = avg
        return bests if any(b is not None for b in bests) else None

    def _row(label, result):
        spd, pwr, cad, hr = result
        return {
            'label':        label,
            'best_speed':   round(spd, 1) if spd   is not None else None,
            'best_power':   round(pwr)    if pwr   is not None else None,
            'best_cadence': round(cad)    if cad   is not None else None,
            'best_hr':      round(hr)     if hr    is not None else None,
        }

    rows = []
    for target_m, label in dist_targets:
        r = _best(pts_d, 1, target_m, d_pre)
        if r and r[0] is not None:
            rows.append(_row(label, r))
    for target_s, label in time_targets:
        r = _best(pts_t, 0, target_s, t_pre)
        if r and r[0] is not None:
            rows.append(_row(label, r))
    return rows


def downsample(points, max_pts=MAX_CHART_POINTS):
    """Even-step downsampling to at most max_pts entries."""
    if len(points) <= max_pts:
        return points
    step = len(points) // max_pts
    return points[::step]


def build_trackpoints(raw_points):
    """
    raw_points: list of (dist_km, alt_m, spd_kph, pwr_w, cad) — all may be None.
    Returns column-oriented dict suitable for Plotly.
    """
    pts = [(d, a, s, p, c) for d, a, s, p, c in raw_points if d is not None]
    if not pts:
        return {}
    pts = downsample(pts)
    return {
        'dist_km': [p[0] for p in pts],
        'alt_m':   [p[1] for p in pts],
        'spd_kph': [p[2] for p in pts],
        'pwr_w':   [p[3] for p in pts],
        'cad':     [p[4] for p in pts],
    }


# ── TCX parser ────────────────────────────────────────────────────────────────

def parse_tcx(filepath):
    import xml.etree.ElementTree as ET

    tree     = ET.parse(filepath)
    root     = tree.getroot()
    activity = root.find('.//ns:Activity', NS)
    if activity is None:
        return None

    sport       = activity.get('Sport', 'Other')
    sport_label = SPORT_LABELS.get(sport, sport)
    id_text     = activity.findtext('ns:Id', namespaces=NS)
    start_utc   = datetime.fromisoformat(id_text.replace('Z', '+00:00'))
    start_local = start_utc.astimezone()

    total_time = total_dist = total_cal = 0
    hr_w = cad_w = watts_w = hr_t = cad_t = watts_t = 0
    max_hr = max_speed_ms = 0
    tp_watts_for_np = []
    coords          = []
    moving_s        = 0
    active_cadences = []
    chart_raw       = []   # (dist_km, alt_m, spd_kph, pwr_w, cad)
    hr_vals_all     = []   # per-trackpoint HR for stats

    laps_data  = []
    splits_raw = []
    lap_id     = 0

    for lap in activity.findall('ns:Lap', NS):
        lap_id += 1
        lt = float(lap.findtext('ns:TotalTimeSeconds', '0', NS))
        total_time += lt

        dist = lap.findtext('ns:DistanceMeters', namespaces=NS)
        if dist: total_dist += float(dist)

        cal = lap.findtext('ns:Calories', namespaces=NS)
        if cal: total_cal += int(cal)

        hr = lap.findtext('ns:AverageHeartRateBpm/ns:Value', namespaces=NS)
        if hr: hr_w += float(hr) * lt; hr_t += lt

        mhr = lap.findtext('ns:MaximumHeartRateBpm/ns:Value', namespaces=NS)
        if mhr: max_hr = max(max_hr, int(mhr))

        cad = lap.findtext('ns:Cadence', namespaces=NS)
        if cad: cad_w += float(cad) * lt; cad_t += lt

        ms = lap.findtext('ns:MaximumSpeed', namespaces=NS)
        if ms: max_speed_ms = max(max_speed_ms, float(ms))

        lx = lap.find('.//ns3:LX', NS)
        if lx is not None:
            aw = lx.findtext('ns3:AvgWatts', namespaces=NS)
            if aw: watts_w += float(aw) * lt; watts_t += lt

        prev_time    = None
        lap_moving_s = 0
        for tp in lap.findall('.//ns:Trackpoint', NS):
            t_el   = tp.findtext('ns:Time',            namespaces=NS)
            w_el   = tp.findtext('.//ns3:Watts',        namespaces=NS)
            lat_el = tp.findtext('ns:Position/ns:LatitudeDegrees',  namespaces=NS)
            lon_el = tp.findtext('ns:Position/ns:LongitudeDegrees', namespaces=NS)
            alt_el = tp.findtext('ns:AltitudeMeters',  namespaces=NS)
            spd_el = tp.findtext('.//ns3:Speed',        namespaces=NS)
            dst_el = tp.findtext('ns:DistanceMeters',  namespaces=NS)
            cad_el = tp.findtext('ns:Cadence',         namespaces=NS)
            hr_el  = tp.findtext('ns:HeartRateBpm/ns:Value', namespaces=NS)

            tp_time = datetime.fromisoformat(t_el.replace('Z', '+00:00')) if t_el else None

            if tp_time and w_el:
                tp_watts_for_np.append((tp_time, float(w_el)))

            if lat_el and lon_el:
                coords.append([round(float(lat_el), 6), round(float(lon_el), 6)])

            if spd_el and tp_time and prev_time:
                dt = (tp_time - prev_time).total_seconds()
                if float(spd_el) > 0.5 and 0 < dt <= 5:
                    moving_s     += dt
                    lap_moving_s += dt
            if tp_time:
                prev_time = tp_time

            cad_int = int(cad_el) if cad_el else None
            if cad_int and cad_int > 0:
                active_cadences.append(cad_int)

            if hr_el:
                hr_vals_all.append(int(float(hr_el)))

            chart_raw.append((
                round(float(dst_el) / 1000, 3) if dst_el else None,
                round(float(alt_el), 1)         if alt_el else None,
                round(float(spd_el) * 3.6, 1)   if spd_el else None,
                int(float(w_el))                 if w_el   else None,
                cad_int,
            ))

            if tp_time and dst_el:
                splits_raw.append((
                    (tp_time - start_utc).total_seconds(),
                    float(dst_el),
                    round(float(spd_el) * 3.6, 1) if spd_el else None,
                    int(float(w_el))               if w_el   else None,
                    cad_int,
                    int(float(hr_el))              if hr_el  else None,
                ))

        _lap_mov = lap_moving_s if lap_moving_s > 0 else lt
        _lap_dst = float(dist) if dist else 0
        _lap_spd = (_lap_dst / lt * 3.6) if (lt and _lap_dst) else None
        _aw      = round(float(lx.findtext('ns3:AvgWatts', '0', NS) or 0)) if lx else 0
        laps_data.append({
            'lap_id':         lap_id,
            'distance_km':    round(_lap_dst / 1000, 2) if _lap_dst else None,
            'elapsed_time':   fmt_time(lt),
            'elapsed_time_s': lt,
            'moving_time':    fmt_time(_lap_mov),
            'moving_time_s':  _lap_mov,
            'avg_speed_kph':  round(_lap_spd, 1) if _lap_spd else None,
            'max_speed_kph':  round(float(ms) * 3.6, 1) if ms else None,
            'avg_hr':         round(float(hr)) if hr else None,
            'max_hr':         int(mhr) if mhr else None,
            'avg_watts':      _aw if _aw > 0 else None,
            'calories':       int(cal) if cal else None,
            'active':         (lap.findtext('ns:Intensity', 'Active', NS) or 'Active').lower() == 'active',
        })

    altitudes_only = [p[1] for p in chart_raw if p[1] is not None]
    if sport_label == 'Ride' and not coords:
        sport_label = 'Indoor Ride'
    elif sport_label == 'Other' and not coords and total_dist > 100 and total_time > 0:
        if total_dist / total_time < 2.5:   # avg m/s: swimming < 2.5, cycling/running ≥ 2.5
            sport_label = 'Pool Swim'
    avg_speed = (total_dist / total_time * 3.6) if total_time else None
    np_val    = calc_normalized_power(tp_watts_for_np)
    mov_s     = moving_s if moving_s > 0 else total_time
    tss, tss_per_hour = calc_tss(total_time, np_val, ATHLETE_FTP)

    summary = {
        'filename':           os.path.basename(filepath),
        'date_sort':          start_utc.isoformat(),
        'date':               start_local.strftime('%a, %b %-d, %Y'),
        'datetime_str':       start_local.strftime('%A, %B %-d, %Y %-I:%M %p'),
        'name':               f'{time_of_day_prefix(start_local.hour)} {sport_label}',
        'type':               sport_label,
        'total_time':         fmt_time(total_time),
        'total_time_s':       total_time,
        'distance_km':        round(total_dist / 1000, 1),
        'avg_speed_kph':      round(avg_speed, 1) if avg_speed else None,
        'max_speed_kph':      round(max_speed_ms * 3.6, 1) if max_speed_ms else None,
        'avg_hr':             round(hr_w / hr_t) if hr_t else None,
        'max_hr':             max_hr or None,
        'avg_cadence':        round(cad_w / cad_t) if cad_t else None,
        'avg_watts':          round(watts_w / watts_t) if watts_t else None,
        'normalized_power':   np_val,
        'calories':           total_cal or None,
        'moving_time':        fmt_time(mov_s),
        'moving_time_s':      mov_s,
        'ascent_m':           calc_ascent(altitudes_only),
        'avg_active_cadence': round(sum(active_cadences) / len(active_cadences)) if active_cadences else (round(cad_w / cad_t) if cad_t else None),
        'tss':                tss,
        'tss_per_hour':       tss_per_hour,
        'ftp':                ATHLETE_FTP,
        'laps':               laps_data,
        'splits':             compute_best_splits(splits_raw, sport_label),
        'coords':             coords,
        'trackpoints':        build_trackpoints(chart_raw),
        'stats':              compute_detailed_stats(chart_raw, hr_vals_all, {
            'total_time_s': total_time, 'moving_time_s': mov_s,
            'total_time': fmt_time(total_time), 'moving_time': fmt_time(mov_s),
            'distance_km': round(total_dist / 1000, 1),
            'ascent_m': calc_ascent(altitudes_only),
            'calories': total_cal or None,
            'avg_speed_kph': round(avg_speed, 1) if avg_speed else None,
            'max_speed_kph': round(max_speed_ms * 3.6, 1) if max_speed_ms else None,
            'avg_hr': round(hr_w / hr_t) if hr_t else None,
            'max_hr': max_hr or None,
            'avg_cadence': round(cad_w / cad_t) if cad_t else None,
            'avg_active_cadence': round(sum(active_cadences) / len(active_cadences)) if active_cadences else None,
            'avg_watts': round(watts_w / watts_t) if watts_t else None,
            'normalized_power': np_val,
            'tss': tss, 'tss_per_hour': tss_per_hour, 'type': sport_label,
        }),
    }
    return summary


# ── FIT parser ────────────────────────────────────────────────────────────────

def parse_fit(filepath):
    from fitparse import FitFile
    ff      = FitFile(filepath)
    session = None
    for msg in ff.get_messages('session'):
        session = {f.name: f.value for f in msg.fields}
        break
    if not session:
        return None

    sport_raw   = str(session.get('sport', 'Other'))
    sport_label = SPORT_LABELS.get(sport_raw, sport_raw.capitalize())
    sub_sport   = str(session.get('sub_sport', '')).lower()
    if sub_sport == 'virtual_activity':
        sport_label = f'Virtual {sport_label}'
    elif sub_sport in ('indoor_cycling', 'indoor_rowing'):
        sport_label = f'Indoor {sport_label}'
    elif sub_sport == 'lap_swimming':
        sport_label = 'Pool Swim'
    elif sub_sport == 'open_water':
        sport_label = 'Open Water Swim'
    elif sub_sport == 'treadmill':
        sport_label = f'Treadmill {sport_label}'
    start_time  = session.get('start_time')
    if not start_time:
        return None

    start_utc   = start_time.replace(tzinfo=timezone.utc)
    start_local = start_utc.astimezone()

    total_time = session.get('total_elapsed_time') or session.get('total_timer_time')
    total_dist = session.get('total_distance')
    avg_speed  = session.get('enhanced_avg_speed') or session.get('avg_speed')
    max_speed  = session.get('enhanced_max_speed') or session.get('max_speed')
    np_val     = session.get('normalized_power') or None

    def nz(v): return v if v else None

    SEMI = 180 / 2 ** 31
    coords          = []
    altitudes_only  = []
    moving_s        = 0
    active_cadences = []
    chart_raw       = []
    hr_vals_all     = []
    prev_ts         = None
    cum_dist        = 0.0

    laps_data  = []
    splits_raw = []
    for i, msg in enumerate(ff.get_messages('lap'), 1):
        lf   = {f.name: f.value for f in msg.fields}
        lt_e = lf.get('total_elapsed_time') or 0
        lt_m = lf.get('total_timer_time') or lt_e
        ld   = lf.get('total_distance') or 0
        la_s = lf.get('enhanced_avg_speed') or lf.get('avg_speed')
        lm_s = lf.get('enhanced_max_speed') or lf.get('max_speed')
        laps_data.append({
            'lap_id':         i,
            'distance_km':    round(float(ld) / 1000, 2) if ld else None,
            'elapsed_time':   fmt_time(lt_e) if lt_e else None,
            'elapsed_time_s': lt_e,
            'moving_time':    fmt_time(lt_m) if lt_m else None,
            'moving_time_s':  lt_m,
            'avg_speed_kph':  round(float(la_s) * 3.6, 1) if la_s else None,
            'max_speed_kph':  round(float(lm_s) * 3.6, 1) if lm_s else None,
            'avg_hr':         int(lf['avg_heart_rate'])  if lf.get('avg_heart_rate')  else None,
            'max_hr':         int(lf['max_heart_rate'])  if lf.get('max_heart_rate')  else None,
            'avg_watts':      int(lf['avg_power'])       if lf.get('avg_power')       else None,
            'calories':       int(lf['total_calories'])  if lf.get('total_calories')  else None,
            'active':         str(lf.get('intensity', 'active')).lower() == 'active',
        })

    for rec in ff.get_messages('record'):
        fields = {f.name: f.value for f in rec.fields}

        lat = fields.get('position_lat')
        lon = fields.get('position_long')
        if lat and lon:
            coords.append([round(lat * SEMI, 6), round(lon * SEMI, 6)])

        alt = fields.get('enhanced_altitude') or fields.get('altitude')
        if alt:
            altitudes_only.append(float(alt))

        spd = fields.get('enhanced_speed') or fields.get('speed')
        ts  = fields.get('timestamp')
        dt  = (ts - prev_ts).total_seconds() if (spd and ts and prev_ts) else 0
        if spd and dt > 0 and float(spd) > 0.5 and dt <= 5:
            moving_s += dt
            cum_dist += float(spd) * dt
        prev_ts = ts

        c = fields.get('cadence')
        if c and int(c) > 0:
            active_cadences.append(int(c))

        hr = fields.get('heart_rate')
        if hr and int(hr) > 0:
            hr_vals_all.append(int(hr))

        dist_field = fields.get('distance')
        chart_raw.append((
            round(float(dist_field) / 1000, 3) if dist_field else (round(cum_dist / 1000, 3) if cum_dist else None),
            round(float(alt), 1)                if alt       else None,
            round(float(spd) * 3.6, 1)          if spd       else None,
            int(fields['power'])                 if fields.get('power') else None,
            int(c)                               if (c and int(c) > 0)  else None,
        ))

        splits_dist = float(dist_field) if dist_field else (cum_dist if cum_dist > 0 else None)
        if ts and splits_dist is not None:
            splits_raw.append((
                (ts - start_time).total_seconds(),
                splits_dist,
                round(float(spd) * 3.6, 1) if spd else None,
                int(fields['power'])        if fields.get('power') else None,
                int(c)                      if (c and int(c) > 0)  else None,
                int(hr)                     if (hr and int(hr) > 0) else None,
            ))

    if sport_label == 'Ride' and not coords:
        sport_label = 'Indoor Ride'
    mov_s = moving_s if moving_s > 0 else total_time
    tss, tss_per_hour = calc_tss(total_time, nz(np_val), ATHLETE_FTP)
    avg_cad_val = nz(session.get('avg_cadence'))
    avg_w_val   = nz(session.get('avg_power'))
    avg_hr_val  = nz(session.get('avg_heart_rate'))
    max_hr_val  = nz(session.get('max_heart_rate'))
    avg_active  = round(sum(active_cadences) / len(active_cadences)) if active_cadences else avg_cad_val

    fit_summary = {
        'filename':           os.path.basename(filepath),
        'date_sort':          start_utc.isoformat(),
        'date':               start_local.strftime('%a, %b %-d, %Y'),
        'datetime_str':       start_local.strftime('%A, %B %-d, %Y %-I:%M %p'),
        'name':               f'{time_of_day_prefix(start_local.hour)} {sport_label}',
        'type':               sport_label,
        'total_time':         fmt_time(total_time) if total_time else None,
        'total_time_s':       total_time,
        'distance_km':        round(total_dist / 1000, 1) if total_dist else None,
        'avg_speed_kph':      round(avg_speed * 3.6, 1) if avg_speed else None,
        'max_speed_kph':      round(max_speed * 3.6, 1) if max_speed else None,
        'avg_hr':             avg_hr_val,
        'max_hr':             max_hr_val,
        'avg_cadence':        avg_cad_val,
        'avg_watts':          avg_w_val,
        'normalized_power':   nz(np_val),
        'calories':           nz(session.get('total_calories')),
        'moving_time':        fmt_time(mov_s) if mov_s else None,
        'moving_time_s':      mov_s,
        'ascent_m':           calc_ascent(altitudes_only),
        'avg_active_cadence': avg_active,
        'tss':                tss,
        'tss_per_hour':       tss_per_hour,
        'ftp':                ATHLETE_FTP,
        'laps':               laps_data,
        'splits':             compute_best_splits(splits_raw, sport_label),
        'coords':             coords,
        'trackpoints':        build_trackpoints(chart_raw),
        'stats':              compute_detailed_stats(chart_raw, hr_vals_all, {
            'total_time_s': total_time, 'moving_time_s': mov_s,
            'total_time': fmt_time(total_time) if total_time else None,
            'moving_time': fmt_time(mov_s) if mov_s else None,
            'distance_km': round(total_dist / 1000, 1) if total_dist else None,
            'ascent_m': calc_ascent(altitudes_only),
            'calories': nz(session.get('total_calories')),
            'avg_speed_kph': round(avg_speed * 3.6, 1) if avg_speed else None,
            'max_speed_kph': round(max_speed * 3.6, 1) if max_speed else None,
            'avg_hr': avg_hr_val, 'max_hr': max_hr_val,
            'avg_cadence': avg_cad_val, 'avg_active_cadence': avg_active,
            'avg_watts': avg_w_val, 'normalized_power': nz(np_val),
            'tss': tss, 'tss_per_hour': tss_per_hour, 'type': sport_label,
        }),
    }
    return fit_summary


# ── Cache-aware loaders ───────────────────────────────────────────────────────

def _parse(filepath):
    ext = filepath.rsplit('.', 1)[-1].lower()
    try:
        if ext == 'tcx': return parse_tcx(filepath)
        if ext == 'fit': return parse_fit(filepath)
    except Exception as e:
        print(f'Parse error {filepath}: {e}')
        import traceback; traceback.print_exc()
    return None


def _get_or_parse(filepath):
    filename = os.path.basename(filepath)
    mtime    = os.path.getmtime(filepath)
    cached   = db_get_detail(filename, mtime)
    if cached:
        return cached
    print(f'Parsing {filename}…')
    data = _parse(filepath)
    if data:
        db_save(filename, mtime, data)
    return data


def load_activities():
    if not os.path.isdir(ACTIVITIES_DIR):
        return []

    # Load entire cache in one query instead of N individual lookups
    with get_db() as db:
        rows = db.execute('SELECT filename, mtime, data_json FROM activities').fetchall()
    cache = {r['filename']: (r['mtime'], r['data_json']) for r in rows}

    filenames  = []
    activities = []
    for fname in sorted(os.listdir(ACTIVITIES_DIR)):
        ext = fname.lower().rsplit('.', 1)[-1] if '.' in fname else ''
        if ext not in ('tcx', 'fit'):
            continue
        filenames.append(fname)
        path  = os.path.join(ACTIVITIES_DIR, fname)
        mtime = os.path.getmtime(path)

        cached_mtime, cached_json = cache.get(fname, (None, None))
        if cached_mtime == mtime and cached_json is not None:
            data = json.loads(cached_json)
            if data.get('_failed'):
                continue
        else:
            data = _parse(path)
            if data:
                if db_activity_exists(data['date_sort'], data['type'], exclude_filename=fname):
                    db_save_failed(fname, mtime)
                    continue
                db_save(fname, mtime, data)
            else:
                db_save_failed(fname, mtime)
                continue

        activities.append({k: v for k, v in data.items() if k not in ('coords', 'trackpoints', 'laps', 'stats', 'splits')})
    db_remove_stale(filenames)
    activities.sort(key=lambda x: x['date_sort'], reverse=True)
    return activities


# ── Garmin Connect sync ───────────────────────────────────────────────────────

def sync_garmin():
    try:
        from garminconnect import Garmin
    except ImportError:
        return 0, 0, 'garminconnect not installed — run: pip install garminconnect'

    if not GARMIN_EMAIL or not GARMIN_PASSWORD:
        return 0, 0, 'GARMIN_EMAIL and GARMIN_PASSWORD not set in .env'

    try:
        api = Garmin(email=GARMIN_EMAIL, password=GARMIN_PASSWORD)
        api.login(tokenstore=GARMIN_TOKENS)
    except Exception as e:
        return 0, 0, f'Login failed: {e}. Run garmin_setup.py to re-authenticate.'

    # Collect activity IDs already on disk to skip re-downloads
    existing_ids = set()
    if os.path.isdir(ACTIVITIES_DIR):
        for fname in os.listdir(ACTIVITIES_DIR):
            name = fname.lower().rsplit('.', 1)[0]
            part = name.rsplit('_', 1)[-1]
            if part.isdigit() and len(part) >= 8:
                existing_ids.add(part)

    garmin_activities = api.get_activities(0, 100)
    imported = skipped = 0
    os.makedirs(ACTIVITIES_DIR, exist_ok=True)

    for act in garmin_activities:
        activity_id = str(act['activityId'])
        if activity_id in existing_ids:
            skipped += 1
            continue
        try:
            data = api.download_activity(
                int(activity_id),
                dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL,
            )
            # ORIGINAL comes as a zip; extract the FIT inside
            if data[:2] == b'PK':
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    fits = [n for n in zf.namelist() if n.lower().endswith('.fit')]
                    if not fits:
                        skipped += 1
                        continue
                    data = zf.read(fits[0])
            path = os.path.join(ACTIVITIES_DIR, f'garmin_{activity_id}.fit')
            with open(path, 'wb') as f:
                f.write(data)
            imported += 1
        except Exception as e:
            print(f'Failed to download activity {activity_id}: {e}')
            skipped += 1

    return imported, skipped, None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/activity/<path:filename>')
def activity_detail(filename):
    return render_template('detail.html', filename=filename)


@app.route('/api/import', methods=['POST'])
def api_import():
    files = request.files.getlist('files')
    if not files or all(f.filename == '' for f in files):
        return jsonify({'error': 'No files provided'}), 400

    os.makedirs(ACTIVITIES_DIR, exist_ok=True)
    results = []
    for f in files:
        if not f.filename:
            continue
        fname = secure_filename(f.filename)
        ext = fname.rsplit('.', 1)[-1].lower() if '.' in fname else ''
        if ext not in ('tcx', 'fit'):
            results.append({'filename': f.filename, 'status': 'skipped'})
            continue
        f.save(os.path.join(ACTIVITIES_DIR, fname))
        results.append({'filename': fname, 'status': 'ok'})

    return jsonify({'results': results})


@app.route('/api/deduplicate', methods=['POST'])
def api_deduplicate():
    removed = deduplicate_activities()
    return jsonify({'removed': removed, 'message': f'{removed} duplicate{"s" if removed != 1 else ""} removed'})


@app.route('/api/sync', methods=['POST'])
def api_sync():
    imported, skipped, error = sync_garmin()
    if error:
        return jsonify({'error': error}), 400
    noun = 'activity' if imported == 1 else 'activities'
    msg  = f'{imported} new {noun} imported'
    if skipped:
        msg += f', {skipped} already up to date'
    return jsonify({'imported': imported, 'skipped': skipped, 'message': msg})


@app.route('/api/activities')
def api_activities():
    return jsonify(load_activities())


@app.route('/api/activity/<path:filename>')
def api_activity(filename):
    path = os.path.join(ACTIVITIES_DIR, filename)
    if not os.path.isfile(path):
        abort(404)
    data = _get_or_parse(path)
    if not data:
        abort(500)
    return jsonify(data)


# ── Startup ───────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    os.makedirs(ACTIVITIES_DIR, exist_ok=True)
    init_db()
    removed = deduplicate_activities()
    if removed:
        print(f'Removed {removed} duplicate activities')
    print(f'\nActivities directory: {ACTIVITIES_DIR}')
    print('Open http://localhost:8080\n')
    app.run(debug=True, port=8080)
