#!/usr/bin/env python3
"""Retry activities the cache gave up on.

When a file cannot be parsed, its failure is cached so we don't re-read 4,500
files on every page load. The cost is that a parser fix never reaches the files
it was written for — they stay marked failed forever. Failures written from now
on carry a parser version (see PARSE_FAIL_V) and are retried automatically, but
markers written before that carry no version and have to be cleared by hand.

This drops the cache rows for files that are marked failed but that the current
parser can read. They are re-parsed on the next page load, which is also where
duplicate detection runs — so a file that turns out to duplicate an existing
activity is simply marked failed again.

Only cache rows are touched; the activity files themselves are never modified,
and anything dropped in error is rebuilt by re-parsing.

Usage:
    python3 retry_failed.py            # report what would change
    python3 retry_failed.py --apply    # drop those cache rows
"""
import json
import os
import sqlite3
import sys

import app

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    apply_changes = '--apply' in sys.argv

    db = sqlite3.connect(os.path.join(HERE, 'activities.db'))
    rows = db.execute('SELECT filename, data_json FROM activities').fetchall()

    retryable = []
    for filename, data_json in rows:
        data = json.loads(data_json)
        if not data.get('_failed'):
            continue
        path = os.path.join(app.ACTIVITIES_DIR, filename)
        ext = filename.lower().rsplit('.', 1)[-1] if '.' in filename else ''
        if ext not in ('tcx', 'fit') or not os.path.exists(path):
            continue
        try:
            parsed = app.parse_fit(path) if ext == 'fit' else app.parse_tcx(path)
        except Exception:
            continue
        if parsed:
            retryable.append((filename, parsed.get('date_sort'), parsed.get('type')))

    print('%d cached failures, %d of which the current parser can read'
          % (sum(1 for _, j in rows if json.loads(j).get('_failed')), len(retryable)))
    for filename, date_sort, kind in sorted(retryable, key=lambda r: r[1] or '')[-15:]:
        print('    %s  %s  %s' % (date_sort, (kind or '')[:16].ljust(16), filename))

    if not retryable:
        return
    if apply_changes:
        db.executemany('DELETE FROM activities WHERE filename = ?',
                       [(f,) for f, _, _ in retryable])
        db.commit()
        print('\ndropped %d cache rows — they are re-parsed on the next page load'
              % len(retryable))
    else:
        print('\ndry run; pass --apply to drop these cache rows')


if __name__ == '__main__':
    main()
