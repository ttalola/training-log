#!/usr/bin/env python3
"""Batch-generate activity descriptions and save them to the metadata store.

Resumable: skips activities that already have a description (unless --force).
Default mode is the local LLM (LM Studio / Gemma); --rule uses the offline
template generator. LLM failures are logged and skipped, never fatal.

Usage:
    python generate_descriptions.py                 # LLM, all activities w/o a description
    python generate_descriptions.py --rule          # rule-based instead
    python generate_descriptions.py --limit 5       # first 5 only (test)
    python generate_descriptions.py --force         # overwrite existing descriptions
    python generate_descriptions.py --model "<id>"  # override LLM model
"""

import json
import os
import sys
import time

import app
import describe


def load_worklist():
    """Materialize [(filename, summary)] for every real activity (file-based + external).
    Fully read and close before any writes, so the writer never hits a read lock."""
    db = app.get_db()
    try:
        items = [(r['filename'], json.loads(r['data_json']))
                 for r in db.execute("SELECT filename, data_json FROM activities "
                                     "WHERE json_extract(data_json,'$._failed') IS NULL").fetchall()]
        items += [(r['id'], json.loads(r['data_json']))
                  for r in db.execute("SELECT id, data_json FROM external_activities").fetchall()]
    finally:
        db.close()
    return items


def start_coord(filename):
    """Start [lat, lon] for a file-based activity, via a short-lived read connection."""
    db = app.get_db()
    try:
        row = db.execute("SELECT coords_json FROM activities WHERE filename=?", (filename,)).fetchone()
    finally:
        db.close()
    if row and row['coords_json']:
        c = json.loads(row['coords_json'])
        return c[0] if c else None
    return None


def main():
    argv  = sys.argv[1:]
    mode  = 'rule' if '--rule' in argv else 'llm'
    force = '--force' in argv
    limit = int(argv[argv.index('--limit') + 1]) if '--limit' in argv else None
    model = argv[argv.index('--model') + 1] if '--model' in argv else describe.LMSTUDIO_MODEL

    app.init_db()
    have_desc = {k for k, m in app.load_meta_map().items() if m.get('description')}
    print(f'mode={mode} model={model if mode=="llm" else "-"} '
          f'already_described={len(have_desc)} force={force}', flush=True)

    worklist = load_worklist()
    print(f'activities to consider: {len(worklist)}', flush=True)

    done = skipped = errors = 0
    t0 = time.time()
    for fname, summary in worklist:
        ds  = summary.get('date_sort')
        key = (ds or '')[:16]
        if not ds:
            continue
        if not force and key in have_desc:
            skipped += 1
            continue
        if limit is not None and done >= limit:
            break
        sc       = start_coord(fname)
        coords   = [sc] if sc else []
        fit_path = os.path.join(app.ACTIVITIES_DIR, fname)
        try:
            text = describe.generate(
                summary, coords=coords,
                fit_path=fit_path if os.path.isfile(fit_path) else None,
                mode=mode, model=model,
            )
            app.set_description(ds, text)
            have_desc.add(key)
            done += 1
            if done <= 3 or done % 25 == 0:
                rate = done / (time.time() - t0)
                print(f'  [{done}] {summary.get("type",""):<12} {text[:70]}  ({rate:.1f}/s)', flush=True)
        except Exception as e:
            errors += 1
            print(f'  ! {fname}: {e}', flush=True)

    dt = time.time() - t0
    print(f'\nDone in {dt/60:.1f} min — generated={done} skipped={skipped} errors={errors}', flush=True)


if __name__ == '__main__':
    main()
