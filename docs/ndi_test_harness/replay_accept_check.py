"""Acceptance check for the replay + live-recording benchmark (read-only; reads app.log).

Criteria (2 feeds at 1080p with replay playing):
  - replay shows >= 20 frames/s on every feed (the "replay frames feed=... shown_fps=" lines the
    app logs every 5 s while replay runs); on logs from before the replay workers existed, the
    old rule is used instead: replay tick interval stays <= 50 ms
  - recording queue stays < 20 of 256 on every feed
  - source_fps >= 29.5 on every feed (median over the window; per-second dips reported)
  - no feed_lost health events in the window (a lost source rebuilds the feed and makes overload worse)
  Also reported (not pass/fail): how long the app said recording was behind live ("qrec_s=" in the
  telemetry lines) and whether it raised the recording_behind / recording_backpressure warnings.

Usage: python replay_accept_check.py [--log PATH] [--seconds N] [--end HH:MM:SS]
Default window: the last N=60 seconds of the log.
"""
import argparse, re, statistics, sys
from datetime import datetime, timedelta

TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})")
TEL = re.compile(r"telemetry feed_id=(\S+) source_fps=([\d.]+) preview_fps=([\d.]+) recording_fps=([\d.]+).*?qrec=(\d+)/(\d+)")
TICK = re.compile(r"replay tick interval (\d+) -> (\d+) ms \(tick cost ([\d.]+) ms\)")
CLOCK = re.compile(r"Replay clock (started|stopped)")
LOST = re.compile(r"health_event .*feed_id=(\S+) .*category=feed_lost")
BEHIND = re.compile(r"health_event .*feed_id=(\S+) .*category=(recording_behind|recording_backpressure)")
BACKLOG = re.compile(r"telemetry feed_id=(\S+) .*?qrec_s=([\d.]+)")
FRAMES = re.compile(r"replay frames feed=(\S+) shown_fps=([\d.]+) dropped_fps=([\d.]+) decode_ms=([\d.]+)")


def parse_ts(line):
    m = TS.match(line)
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=r"C:\SportsReplay\logs\app.log")
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--end", default=None, help="window end time HH:MM:SS (same day as last log line)")
    args = ap.parse_args()
    with open(args.log, encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()[-60000:]
    stamped = [(parse_ts(l), l) for l in lines]
    stamped = [(t, l) for t, l in stamped if t]
    last = stamped[-1][0]
    end = last if not args.end else datetime.combine(last.date(), datetime.strptime(args.end, "%H:%M:%S").time())
    start = end - timedelta(seconds=args.seconds)
    win = [(t, l) for t, l in stamped if start <= t <= end]

    # interval in force at window start = last tick-change line before it, else 33
    interval_at_start = 33
    for t, l in stamped:
        if t >= start:
            break
        m = TICK.search(l)
        if m:
            interval_at_start = int(m.group(2))
    intervals = [interval_at_start]
    costs = []
    replay_seen = any(CLOCK.search(l) and "started" in l for _, l in win) or any(
        TICK.search(l) for _, l in win
    )
    for _, l in win:
        m = TICK.search(l)
        if m:
            intervals.append(int(m.group(2)))
            costs.append(float(m.group(3)))
    shown = {}
    for _, l in win:
        m = FRAMES.search(l)
        if m:
            row = shown.setdefault(m.group(1), {"shown": [], "dropped": [], "decode": []})
            row["shown"].append(float(m.group(2)))
            row["dropped"].append(float(m.group(3)))
            row["decode"].append(float(m.group(4)))
    if shown:
        replay_seen = True
    feeds = {}
    for _, l in win:
        m = TEL.search(l)
        if m:
            f = feeds.setdefault(m.group(1), {"src": [], "prev": [], "rec": [], "q": []})
            f["src"].append(float(m.group(2)))
            f["prev"].append(float(m.group(3)))
            f["rec"].append(float(m.group(4)))
            f["q"].append(int(m.group(5)))
    print(f"window {start:%H:%M:%S} - {end:%H:%M:%S}  ({args.seconds} s)   feeds seen: {sorted(feeds)}")
    ok = True

    def check(name, passed, detail):
        nonlocal ok
        ok = ok and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}: {detail}")

    check("replay was playing in window", replay_seen, "replay clock/tick lines found" if replay_seen else "NO replay lines - not a valid replay sample")
    if shown:
        for fid, row in sorted(shown.items()):
            low = min(row["shown"])
            check(f"{fid} replay shows >= 20 frames/s", low >= 20.0,
                  f"min {low:.1f}, median {statistics.median(row['shown']):.1f} shown/s over {len(row['shown'])} stats lines; "
                  f"dropped median {statistics.median(row['dropped']):.1f}/s; decode+scale median {statistics.median(row['decode']):.1f} ms")
    else:
        check("replay tick interval <= 50 ms", max(intervals) <= 50,
              f"max {max(intervals)} ms (start {interval_at_start}, changes {intervals[1:]}), worst tick cost {max(costs) if costs else 'n/a'} ms (no 'replay frames' lines: log is from before the replay workers)")
    for fid, f in sorted(feeds.items()):
        check(f"{fid} recording queue < 20/256", max(f["q"]) < 20, f"max {max(f['q'])}, median {statistics.median(f['q'])}")
        med = statistics.median(f["src"])
        check(f"{fid} source_fps >= 29.5", med >= 29.5,
              f"median {med:.2f}, min {min(f['src']):.1f}, seconds below 29.5: {sum(1 for v in f['src'] if v < 29.5)}/{len(f['src'])}; "
              f"preview median {statistics.median(f['prev']):.1f}, recording median {statistics.median(f['rec']):.1f}")
    lost = [m.group(1) for _, l in win for m in [LOST.search(l)] if m]
    check("no feed_lost events", not lost, f"{len(lost)} event(s)" + (f" on {sorted(set(lost))}" if lost else ""))
    backlog = {}
    for _, l in win:
        m = BACKLOG.search(l)
        if m:
            backlog.setdefault(m.group(1), []).append(float(m.group(2)))
    for fid, values in sorted(backlog.items()):
        print(f"  [info] {fid} recording behind live: max {max(values):.1f} s, median {statistics.median(values):.1f} s")
    warned = sorted({(m.group(1), m.group(2)) for _, l in win for m in [BEHIND.search(l)] if m})
    print(f"  [info] operator warnings raised: {warned if warned else 'none'}")
    check("2 feeds present", len(feeds) >= 2, f"{len(feeds)} feed(s) in telemetry (acceptance target is 2)")
    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


main()
