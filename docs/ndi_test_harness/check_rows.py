"""Segment files on disk against rows in metadata.db, per feed.

    python check_rows.py <base_data_dir>

A healthy feed runs ~1-2 files ahead of its rows (the segment being written, and
one whose row lands at the next rotation). A feed whose footage never reached the
database (NDI_OPEN_BUGS.md bug 1) shows many files and ZERO rows. File sizes prove
nothing; only rows do. Prints one line per feed:

    ndi_1  files=7  rows=6  OK
"""
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

data_dir = Path(sys.argv[1])
files = Counter()
for path in data_dir.glob("sessions/*/recording/*/*/segment_*.mkv"):
    files[path.parent.name] += 1

rows = Counter()
db = data_dir / "metadata.db"
if db.exists():
    connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    for feed_id, count in connection.execute(
        "SELECT feed_id, COUNT(*) FROM segments WHERE state IN ('complete', 'dirty') GROUP BY feed_id"
    ):
        rows[feed_id] += count
    connection.close()

if not files:
    print("no segment files found")
for feed_id in sorted(set(files) | set(rows)):
    f, r = files[feed_id], rows[feed_id]
    verdict = "OK" if r >= max(1, f - 2) else ("NO ROWS" if r == 0 else "ROWS BEHIND")
    print(f"{feed_id}  files={f}  rows={r}  {verdict}")
