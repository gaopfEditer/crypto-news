#!/usr/bin/env python3
"""Merge tracker runs / fixtures / legacy snapshots into gh-pages event libraries."""
import argparse
import datetime as dt
import glob
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from store import (  # noqa: E402
    TYPE_CONFIG,
    ingest_main_csv,
    ingest_tracker_dir,
    load_lib,
    merge_libraries,
    migrate_unlock_snapshot,
    rebuild_index,
    save_lib,
    ts_from_utc8,
    window_count,
)

TZ8 = dt.timezone(dt.timedelta(hours=8))


FIXTURE_GLOBS = {
    "unlock": ["unlocks_*.csv", "unlocks.csv"],
    "delist": ["delistings_*.csv"],
    "perp_listing": ["perp_listings_*.csv", "listings_*.csv"],
    "burn": ["burns_*.csv"],
}


def bootstrap(events_dir, fixtures_dir, legacy_unlocks_dir=None):
    now_ts = int(time.time())
    for etype, cfg in TYPE_CONFIG.items():
        path = os.path.join(events_dir, cfg["file"])
        lib = load_lib(path, etype)
        files = []
        for pat in FIXTURE_GLOBS.get(etype, [cfg["main_csv"]]):
            files.extend(glob.glob(os.path.join(fixtures_dir, pat)))
        files = sorted(set(files))
        for fp in files:
            inc = ingest_main_csv(etype, fp, now_ts)
            merge_libraries(lib, inc, now_ts)
        save_lib(path, lib)
        print(f"bootstrap {etype}: {len(lib['events'])} events", file=sys.stderr)
    if legacy_unlocks_dir and os.path.isdir(legacy_unlocks_dir):
        path = os.path.join(events_dir, TYPE_CONFIG["unlock"]["file"])
        lib = load_lib(path, "unlock")
        for fp in glob.glob(os.path.join(legacy_unlocks_dir, "20*.json")):
            if os.path.basename(fp) == "index.json":
                continue
            inc = migrate_unlock_snapshot(fp, now_ts)
            merge_libraries(lib, inc, now_ts)
        save_lib(path, lib)
        print(f"migrated unlock snapshots: {len(lib['events'])} total", file=sys.stderr)
    rebuild_index(events_dir)


def merge_run(etype, out_dir, events_dir, now_ts=None):
    now_ts = now_ts or int(time.time())
    cfg = TYPE_CONFIG[etype]
    path = os.path.join(events_dir, cfg["file"])
    lib = load_lib(path, etype)
    before_total = len(lib["events"])
    incoming = ingest_tracker_dir(etype, out_dir, now_ts)
    stats = merge_libraries(lib, incoming, now_ts)
    save_lib(path, lib)
    lo = now_ts - 7 * 86400
    recent_before = window_count(lib, lo, now_ts, now_ts=now_ts)
    print(json.dumps({
        "type": etype,
        "incoming": len(incoming),
        "library_total": len(lib["events"]),
        "added": stats["added"],
        "recent_7d": recent_before,
    }))
    if incoming and len(incoming) < before_total * 0.2 and before_total > 5:
        print(
            f"::warning title={etype} 运行结果偏少::"
            f"本次解析 {len(incoming)} 条，库中已有 {before_total} 条；已合并但未删除历史",
            file=sys.stderr,
        )
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bootstrap")
    b.add_argument("events_dir")
    b.add_argument("--fixtures", default=os.path.join(HERE, "fixtures"))
    b.add_argument("--legacy-unlocks", default=None)
    m = sub.add_parser("merge")
    m.add_argument("etype", choices=list(TYPE_CONFIG.keys()))
    m.add_argument("out_dir")
    m.add_argument("events_dir")
    m.add_argument("--now", help="YYYY-MM-DD HH:MM UTC+8")
    args = ap.parse_args()
    if args.cmd == "bootstrap":
        bootstrap(args.events_dir, args.fixtures, args.legacy_unlocks)
        return 0
    now_ts = int(time.time())
    if args.now:
        now_ts = ts_from_utc8(args.now) or now_ts
    return merge_run(args.etype, args.out_dir, args.events_dir, now_ts)


if __name__ == "__main__":
    sys.exit(main())
