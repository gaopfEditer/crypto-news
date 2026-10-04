#!/usr/bin/env python3
"""Sync and trim CoinMarketCap snapshot cache for gh-pages (unlock_cache/cmc_snapshots/)."""
import argparse
import os
import re
import shutil
import sys
import time

SNAP_RE = re.compile(r"^(\d{8})T\d{4}\.json$")


def _snap_epoch(name):
    m = SNAP_RE.match(name)
    if not m:
        return None
    y, mo, d = int(m.group(1)[:4]), int(m.group(1)[4:6]), int(m.group(1)[6:8])
    try:
        return time.mktime((y, mo, d, 12, 0, 0, 0, 0, -1))
    except ValueError:
        return None


def trim_cmc_snapshots(sdir, keep_days=30):
    if not os.path.isdir(sdir):
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for fn in list(os.listdir(sdir)):
        if not fn.endswith(".json"):
            continue
        ep = _snap_epoch(fn)
        if ep is None:
            continue
        path = os.path.join(sdir, fn)
        if ep < cutoff and os.path.isfile(path):
            os.remove(path)
            removed += 1
    return removed


def restore_to_local(publish_dir, local_cache_dir):
    """Pull CMC snapshots from gh-pages layout into tracker cache dir."""
    os.makedirs(local_cache_dir, exist_ok=True)
    candidates = [
        os.path.join(publish_dir, "unlock_cache", "cmc_snapshots"),
        os.path.join(publish_dir, "unlocks", "cache", "cmc_snapshots"),
    ]
    for src in candidates:
        if os.path.isdir(src) and os.listdir(src):
            for fn in os.listdir(src):
                if fn.endswith(".json"):
                    shutil.copy2(os.path.join(src, fn), os.path.join(local_cache_dir, fn))
            return src
    return None


def publish_from_local(local_cache_dir, publish_dir, keep_days=30):
    dest = os.path.join(publish_dir, "unlock_cache", "cmc_snapshots")
    os.makedirs(dest, exist_ok=True)
    if os.path.isdir(local_cache_dir):
        for fn in os.listdir(local_cache_dir):
            if fn.endswith(".json"):
                shutil.copy2(os.path.join(local_cache_dir, fn), os.path.join(dest, fn))
    removed = trim_cmc_snapshots(dest, keep_days=keep_days)
    legacy = os.path.join(publish_dir, "unlocks", "cache")
    if os.path.isdir(legacy):
        shutil.rmtree(legacy, ignore_errors=True)
    return dest, removed


def main(argv=None):
    ap = argparse.ArgumentParser(description="Restore/publish CMC snapshot cache for unlock workflow")
    ap.add_argument("action", choices=("restore", "publish"))
    ap.add_argument("publish_dir", help="gh-pages publish root")
    ap.add_argument("local_cache_dir", help="unlock_tracker/cache/cmc_snapshots")
    ap.add_argument("--keep-days", type=int, default=30)
    args = ap.parse_args(argv)
    if args.action == "restore":
        src = restore_to_local(args.publish_dir, args.local_cache_dir)
        n = len([f for f in os.listdir(args.local_cache_dir) if f.endswith(".json")]) if os.path.isdir(args.local_cache_dir) else 0
        print(f"restored_from={src or 'none'} files={n}")
        return 0
    dest, removed = publish_from_local(args.local_cache_dir, args.publish_dir, args.keep_days)
    n = len([f for f in os.listdir(dest) if f.endswith(".json")])
    print(f"published={dest} files={n} trimmed={removed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
