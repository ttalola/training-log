#!/usr/bin/env python3
"""Batch-generate long-form AI analyses for activities and cache them.

Slow (~45-60s each on a 26B local model) — designed to run in the background
for hours. Resumable: skips activities that already have an analysis (unless
--force) and degenerate/near-empty ones. Re-run any time to continue.

Usage:
    python generate_analyses.py                # all eligible, resumable
    python generate_analyses.py --limit 5      # first 5 (test)
    python generate_analyses.py --force        # re-analyze everything
    python generate_analyses.py --model "<id>"
"""

import os
import sys
import time

import app
import describe
from generate_descriptions import load_worklist, start_coord


def analyzed_keys():
    db = app.get_db()
    try:
        return {r['activity_key'] for r in
                db.execute("SELECT activity_key FROM activity_meta WHERE analysis IS NOT NULL").fetchall()}
    finally:
        db.close()


def main():
    argv  = sys.argv[1:]
    force = '--force' in argv
    limit = int(argv[argv.index('--limit') + 1]) if '--limit' in argv else None
    model = argv[argv.index('--model') + 1] if '--model' in argv else describe.LMSTUDIO_MODEL

    app.init_db()
    done = set() if force else analyzed_keys()
    worklist = load_worklist()
    print(f'model={model} already_analyzed={len(done)} activities={len(worklist)} force={force}', flush=True)

    n = skipped = degenerate = errors = 0
    t0 = time.time()
    for fname, detail in worklist:
        ds  = detail.get('date_sort')
        key = (ds or '')[:16]
        if not ds:
            continue
        if key in done:
            skipped += 1
            continue
        if describe.is_degenerate(detail):
            degenerate += 1
            continue
        if limit is not None and n >= limit:
            break
        sc       = start_coord(fname)
        coords   = [sc] if sc else []
        fit_path = os.path.join(app.ACTIVITIES_DIR, fname)
        try:
            text = describe.analyze(detail, coords=coords,
                                    fit_path=fit_path if os.path.isfile(fit_path) else None,
                                    model=model)
            app.set_analysis(ds, text)
            done.add(key)
            n += 1
            rate = n / (time.time() - t0)
            print(f'  [{n}] {detail.get("type",""):<12} {detail.get("name","")[:40]:<40} '
                  f'{len(text)} chars  ({rate*60:.1f}/min)', flush=True)
        except Exception as e:
            errors += 1
            print(f'  ! {fname}: {e}', flush=True)

    dt = time.time() - t0
    print(f'\nDone in {dt/60:.1f} min — analyzed={n} skipped={skipped} '
          f'degenerate={degenerate} errors={errors}', flush=True)


if __name__ == '__main__':
    main()
