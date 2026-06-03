#!/usr/bin/env python3
"""One-time importer for a Strava bulk export into Training Log.

Strava → Settings → My Account → "Download or Delete Your Account" →
"Request your archive". The emailed zip contains `activities.csv` (names,
descriptions, private notes, dates) and an `activities/` folder with the
original recordings (often gzipped: .fit.gz / .tcx.gz / .gpx).

What this does, per Strava activity:
  • Matches it against activities you already have, by start time. Strava keeps
    the same original recording you uploaded from Garmin, so the start time
    matches to the second — that's a reliable duplicate signal (type is not,
    because re-encoded files can change the sport label).
  • Duplicate  → keep your existing activity; just attach the Strava title /
                 private note / description to it.
  • New + .fit/.tcx → copy the original into activities/ so the app parses it,
                 and attach its metadata.
  • New + .gpx / manual entry (no usable file) → store a limited, file-less
                 activity built from the CSV row, plus its metadata.

Idempotent: safe to re-run. Default is a DRY RUN that only reports what it
would do. Pass --apply to make changes; --limit N to process only N rows
(handy for a first test).

Usage:
    python import_strava.py <export_dir>                # survey only
    python import_strava.py <export_dir> --apply        # do the import
    python import_strava.py <export_dir> --apply --limit 5
"""

import csv
import gzip
import os
import sys
from datetime import datetime, timezone

import app  # Training Log: DB helpers, parsers, constants


# Two activities are the "same" if their start times are within this window.
# Same recording => identical start time; the slack only guards rounding.
MATCH_TOLERANCE_S = 60

# Strava "Activity Type" -> Training Log type label
STRAVA_TYPE_MAP = {
    'Ride': 'Ride', 'VirtualRide': 'Virtual Ride', 'EBikeRide': 'Ride',
    'MountainBikeRide': 'Ride', 'GravelRide': 'Ride',
    'Run': 'Run', 'TrailRun': 'Run', 'VirtualRun': 'Run',
    'Swim': 'Swim', 'Walk': 'Walk', 'Hike': 'Hike',
    'Workout': 'Workout', 'WeightTraining': 'Strength Training', 'Yoga': 'Yoga',
    'Rowing': 'Rowing', 'Elliptical': 'Elliptical',
}

# Candidate header names for each field (first match wins). Strava varies the
# CSV between versions/locales, and reuses some names (e.g. two "Distance"
# columns) — we take the FIRST occurrence, which is the human-facing one.
COLUMNS = {
    'id':          ['Activity ID'],
    'date':        ['Activity Date'],
    'name':        ['Activity Name'],
    'type':        ['Activity Type'],
    'description': ['Activity Description'],
    'note':        ['Activity Private Note'],
    'filename':    ['Filename'],
    'elapsed':     ['Elapsed Time'],
    'moving':      ['Moving Time'],
    'distance':    ['Distance'],
    'avg_hr':      ['Average Heart Rate'],
    'max_hr':      ['Max Heart Rate'],
    'calories':    ['Calories'],
}

DATE_FORMATS = [
    '%b %d, %Y, %I:%M:%S %p',   # Jul 19, 2025, 11:00:03 AM
    '%b %d, %Y, %H:%M:%S',      # Jul 19, 2025, 13:00:03
    '%Y-%m-%d %H:%M:%S',
    '%d %b %Y, %H:%M:%S',
    '%Y-%m-%dT%H:%M:%SZ',
    '%Y-%m-%dT%H:%M:%S%z',
]


# ── small parsing helpers ─────────────────────────────────────────────────────

def parse_date_utc(s):
    s = (s or '').strip()
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def num(v):
    try:
        return float(str(v).replace(',', '.').strip())
    except (ValueError, AttributeError, TypeError):
        return None


def to_int(v):
    n = num(v)
    return int(round(n)) if n is not None else None


def map_type(t):
    t = (t or '').strip()
    return STRAVA_TYPE_MAP.get(t) or app.SPORT_LABELS.get(t) or (t or 'Other')


def find_csv(export_dir):
    for name in os.listdir(export_dir):
        if name.lower() == 'activities.csv':
            return os.path.join(export_dir, name)
    return None


def build_index(header):
    lower = [h.strip().lower() for h in header]
    idx = {}
    for key, names in COLUMNS.items():
        for n in names:
            if n.lower() in lower:
                idx[key] = lower.index(n.lower())
                break
    return idx


def cell(row, idx, key):
    i = idx.get(key)
    if i is None or i >= len(row):
        return ''
    return (row[i] or '').strip()


def resolve_file(export_dir, rel):
    """Return (path, inner_ext) for a Strava filename, or (None, None)."""
    rel = (rel or '').strip()
    if not rel:
        return None, None
    candidates = [
        os.path.join(export_dir, rel),
        os.path.join(export_dir, 'activities', os.path.basename(rel)),
    ]
    path = next((p for p in candidates if os.path.isfile(p)), None)
    if not path:
        return None, None
    base = os.path.basename(path)
    inner = base[:-3] if base.lower().endswith('.gz') else base
    ext = inner.rsplit('.', 1)[-1].lower() if '.' in inner else ''
    return path, ext


def read_bytes(path):
    if path.lower().endswith('.gz'):
        with gzip.open(path, 'rb') as f:
            return f.read()
    with open(path, 'rb') as f:
        return f.read()


def load_known():
    """List of (start_datetime_utc, full_date_sort) for every existing activity."""
    known = []
    for a in app.load_activities():
        ds = a.get('date_sort')
        if not ds:
            continue
        try:
            known.append((datetime.fromisoformat(ds), ds))
        except ValueError:
            pass
    return known


def find_match(dt_utc, known):
    for kdt, ds in known:
        if abs((dt_utc - kdt).total_seconds()) <= MATCH_TOLERANCE_S:
            return ds
    return None


def build_external(aid, dt_utc, name, type_label, elapsed, moving, dist_km, avg_hr, max_hr, calories):
    local = dt_utc.astimezone()
    ds = dt_utc.isoformat()
    nm = name or f'{app.time_of_day_prefix(local.hour)} {type_label}'
    return {
        'filename':      f'strava-{aid}',
        'date_sort':     ds,
        'date':          local.strftime('%a, %b %-d, %Y'),
        'datetime_str':  local.strftime('%A, %B %-d, %Y %-I:%M %p'),
        'name':          nm,
        'type':          type_label,
        'total_time':    app.fmt_time(elapsed) if elapsed else None,
        'total_time_s':  elapsed,
        'moving_time':   app.fmt_time(moving) if moving else None,
        'moving_time_s': moving,
        'distance_km':   round(dist_km, 2) if dist_km is not None else None,
        'avg_speed_kph': round(dist_km / (elapsed / 3600), 1) if (dist_km and elapsed) else None,
        'avg_hr':        avg_hr,
        'max_hr':        max_hr,
        'calories':      calories,
        'source':        'strava',
        'external':      True,
    }


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    argv  = sys.argv[1:]
    apply = '--apply' in argv
    limit = None
    if '--limit' in argv:
        try:
            limit = int(argv[argv.index('--limit') + 1])
        except (IndexError, ValueError):
            print('--limit needs a number'); return
    positional = [a for a in argv if not a.startswith('--') and not a.isdigit()]
    if not positional:
        print(__doc__)
        return
    export_dir = positional[0]
    if not os.path.isdir(export_dir):
        print(f'Not a directory: {export_dir}'); return
    csv_path = find_csv(export_dir)
    if not csv_path:
        print(f'No activities.csv found in {export_dir}'); return

    app.init_db()
    os.makedirs(app.ACTIVITIES_DIR, exist_ok=True)
    known = load_known()
    print(f'{"APPLY" if apply else "DRY RUN"} — {len(known)} existing activities; reading {csv_path}\n')

    counts = {'rows': 0, 'duplicate': 0, 'new_file': 0, 'new_external': 0,
              'unparseable': 0, 'no_date': 0}
    samples = []

    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            print('Empty CSV'); return
        idx = build_index(header)
        missing = [k for k in ('date', 'type') if k not in idx]
        if missing:
            print(f'CSV is missing expected columns {missing}; headers were:\n  {header}')
            return

        for row in reader:
            if limit is not None and counts['rows'] >= limit:
                break
            counts['rows'] += 1

            aid   = cell(row, idx, 'id') or str(counts['rows'])
            dt    = parse_date_utc(cell(row, idx, 'date'))
            name  = cell(row, idx, 'name')
            stype = cell(row, idx, 'type')
            desc  = cell(row, idx, 'description')
            note  = cell(row, idx, 'note')
            rel   = cell(row, idx, 'filename')
            tlabel = map_type(stype)

            if dt is None:
                counts['no_date'] += 1
                continue

            match_ds = find_match(dt, known)
            path, ext = resolve_file(export_dir, rel)

            if match_ds:
                action = 'duplicate'
                counts['duplicate'] += 1
                if apply:
                    app.set_activity_meta(match_ds, name=name, note=note, description=desc)
            elif ext in ('fit', 'tcx'):
                action = f'new ({ext})'
                out = os.path.join(app.ACTIVITIES_DIR, f'strava_{aid}.{ext}')
                if apply:
                    with open(out, 'wb') as wf:
                        wf.write(read_bytes(path))
                    parsed = app._parse(out)
                    if not parsed:
                        os.remove(out)
                        counts['unparseable'] += 1
                        action = 'unparseable -> skipped'
                    else:
                        ds = parsed['date_sort']
                        app.set_activity_meta(ds, name=name, note=note, description=desc)
                        known.append((datetime.fromisoformat(ds), ds))
                        counts['new_file'] += 1
                else:
                    counts['new_file'] += 1
            else:
                action = 'new (file-less)' + (f' [{ext}]' if ext else '')
                elapsed  = to_int(cell(row, idx, 'elapsed'))
                moving   = to_int(cell(row, idx, 'moving'))
                dist_km  = num(cell(row, idx, 'distance'))
                summary  = build_external(
                    aid, dt, name, tlabel, elapsed, moving, dist_km,
                    to_int(cell(row, idx, 'avg_hr')), to_int(cell(row, idx, 'max_hr')),
                    to_int(cell(row, idx, 'calories')),
                )
                if apply:
                    app.add_external_activity(f'strava-{aid}', summary)
                    app.set_activity_meta(summary['date_sort'], name=name, note=note, description=desc)
                known.append((dt, summary['date_sort']))
                counts['new_external'] += 1

            if len(samples) < 12:
                flags = []
                if note: flags.append('note')
                if desc: flags.append('desc')
                samples.append(f'  {dt.date()}  {tlabel:<14} {action:<22} '
                               f'{(name or "(unnamed)")[:32]:<32} {",".join(flags)}')

    print('Sample (first rows):')
    print('\n'.join(samples) or '  (none)')
    print('\nSummary:')
    for k in ('rows', 'duplicate', 'new_file', 'new_external', 'unparseable', 'no_date'):
        print(f'  {k:<13} {counts[k]}')
    if not apply:
        print('\nDry run — nothing written. Re-run with --apply to import.')
    else:
        print('\nDone. Reload the app (or restart) to see the imported activities and notes.')


if __name__ == '__main__':
    main()
