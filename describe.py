"""Activity description generation — rule-based or via a local LLM (LM Studio).

Signals are extracted once (type, place, distance, duration, effort from HR zones,
ascent, temperature, time of day) and rendered either by a deterministic template
or by a local model. Location uses an offline reverse geocoder (no network).
"""

import json
import os
import urllib.request

# ── Config ────────────────────────────────────────────────────────────────────
ATHLETE_MAX_HR = 170
LMSTUDIO_URL   = 'http://127.0.0.1:1234/v1/chat/completions'
LMSTUDIO_MODEL = 'google_gemma-4-26b-a4b-it'

_VERB = {
    'Ride': 'bike ride', 'Virtual Ride': 'virtual ride', 'Indoor Ride': 'indoor ride',
    'Run': 'run', 'Treadmill Run': 'treadmill run', 'Walk': 'walk', 'Hike': 'hike',
    'Pool Swim': 'pool swim', 'Open Water Swim': 'open-water swim',
    'Triathlon': 'triathlon', 'Duathlon': 'duathlon',
}


def verb(t):
    return _VERB.get(t, (t or 'activity').lower())


# Skip only activities with essentially nothing to describe (GPS-glitch blips,
# empty recordings). Short-but-real efforts — e.g. separately-recorded interval
# reps — are kept: they still carry distance and/or heart rate.
MIN_DURATION_S = 30


def is_degenerate(summary):
    """True only for near-empty activities: under 30 s, or no distance AND no HR/calories."""
    if (summary.get('total_time_s') or 0) < MIN_DURATION_S:
        return True
    has_distance = bool(summary.get('distance_km'))                       # > 0 km
    has_signal   = bool(summary.get('avg_hr') or summary.get('calories'))
    return not (has_distance or has_signal)


def effort_word(avg_hr, max_hr=ATHLETE_MAX_HR):
    """Effort label from %HRmax zones."""
    if not avg_hr or not max_hr:
        return None
    p = avg_hr / max_hr
    return 'easy' if p < 0.61 else 'moderate' if p < 0.73 else 'hard' if p < 0.87 else 'very hard'


# ── Offline city lookup (cached) ──────────────────────────────────────────────
_geocache = {}


def city_for(lat, lon):
    if lat is None or lon is None:
        return None
    key = (round(lat, 2), round(lon, 2))
    if key in _geocache:
        return _geocache[key]
    city = None
    try:
        import reverse_geocode
        r = reverse_geocode.get((lat, lon))
        city = r.get('county') or r.get('city')   # county is usually the municipality (e.g. "Turku")
    except Exception:
        city = None
    _geocache[key] = city
    return city


def fit_avg_temp(path):
    """Average recorded temperature (°C) from a FIT file, or None."""
    if not path or not os.path.isfile(path) or not path.lower().endswith('.fit'):
        return None
    try:
        from fitparse import FitFile
        vals = [f.value for rec in FitFile(path).get_messages('record')
                for f in rec.fields if f.name == 'temperature' and f.value is not None]
        return round(sum(vals) / len(vals)) if vals else None
    except Exception:
        return None


def _time_of_day(name):
    w = (name or '').split(' ')[0]
    return w if w in ('Morning', 'Midday', 'Afternoon', 'Evening', 'Night') else None


def _plausibility(typ, spd):
    if typ and 'Run' in typ and spd and spd > 18:
        return 'pace looks too fast for a run — possibly mislabeled'
    return None


def build_signals(summary, coords=None, fit_path=None):
    start = coords[0] if coords else None
    typ   = summary.get('type')
    return {
        'type':          typ,
        'verb':          verb(typ),
        'time_of_day':   _time_of_day(summary.get('name')),
        'place':         city_for(start[0], start[1]) if start else None,
        'distance_km':   summary.get('distance_km'),
        'duration':      summary.get('moving_time') or summary.get('total_time'),
        'avg_speed_kph': summary.get('avg_speed_kph'),
        'avg_hr':        summary.get('avg_hr'),
        'effort':        effort_word(summary.get('avg_hr')),
        'ascent_m':      summary.get('ascent_m'),
        'temp_c':        fit_avg_temp(fit_path),
        'cadence':       summary.get('avg_cadence'),
        'note':          _plausibility(typ, summary.get('avg_speed_kph')),
    }


# ── Rule-based renderer ───────────────────────────────────────────────────────

def render_rule(s):
    parts = []
    head = ((s['time_of_day'] + ' ') if s['time_of_day'] else '') + s['verb']
    if s['place']:
        head += f" around {s['place']}"
    parts.append(head[:1].upper() + head[1:])

    seg = f"{s['distance_km']} km" if s['distance_km'] else None
    if seg and s['duration']:
        seg += f" in {s['duration']}"
    if seg and s['avg_speed_kph'] and s['type'] in ('Ride', 'Indoor Ride', 'Virtual Ride'):
        seg += f" at {s['avg_speed_kph']} km/h"
    if seg:
        parts.append(seg)

    if s['effort']:
        parts.append(f"{s['effort']} effort" + (f" (avg HR {s['avg_hr']})" if s['avg_hr'] else ''))
    if s['ascent_m'] and s['ascent_m'] >= 50:
        parts.append(f"{s['ascent_m']} m climbing")
    if s['temp_c'] is not None:
        parts.append(f"+{s['temp_c']}°C" if s['temp_c'] >= 0 else f"{s['temp_c']}°C")

    out = ', '.join(p for p in parts if p) + '.'
    if s['note']:
        out += f" (Note: {s['note']}.)"
    return out


# ── Local LLM renderer ────────────────────────────────────────────────────────

_SYS = (
    "You write ONE short, factual sentence (max ~22 words) summarizing a workout for a personal "
    "training log, in this style: 'Evening bike ride around Turku, 23 km, moderate effort, +18°C.' "
    "Use ONLY the provided fields; omit any that are missing and never invent data. "
    "If a 'note' field is present, append it briefly. Output only the sentence, no preamble."
)


def llm_describe(s, model=LMSTUDIO_MODEL, url=LMSTUDIO_URL, timeout=120):
    fields = {k: v for k, v in s.items() if v not in (None, '') and k != 'verb'}
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": _SYS},
            {"role": "user", "content": json.dumps(fields, ensure_ascii=False)},
        ],
        "temperature": 0.4,
        "max_tokens": 90,
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.load(r)
    text = (out['choices'][0]['message']['content'] or '').strip()
    if not text:
        raise RuntimeError('empty LLM response')
    return text


def generate(summary, coords=None, fit_path=None, mode='rule', **kw):
    s = build_signals(summary, coords, fit_path)
    return render_rule(s) if mode == 'rule' else llm_describe(s, **kw)
