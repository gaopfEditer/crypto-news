#!/usr/bin/env python3
"""Restore / publish tracker cache dirs on gh-pages under event_cache/<type>/."""
import argparse
import os
import shutil
import time

DEFAULT_KEEP_DAYS = 30

# Upbit trade list/detail JSON is identical for delist vs spot_listing trackers.
_UPBIT_RAW_PREFIX = "upbit_"


def _raw_dir(cache_root):
    return os.path.join(cache_root, "raw")


def mirror_upbit_cache(cache_roots):
    """Merge upbit_*.json across tracker cache dirs (newer mtime wins)."""
    roots = [r for r in cache_roots if r]
    if len(roots) < 2:
        return
    by_name = {}
    for root in roots:
        raw = _raw_dir(root)
        if not os.path.isdir(raw):
            continue
        for fn in os.listdir(raw):
            if not fn.startswith(_UPBIT_RAW_PREFIX) or not fn.endswith(".json"):
                continue
            path = os.path.join(raw, fn)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            prev = by_name.get(fn)
            if prev is None or mtime > prev[0]:
                by_name[fn] = (mtime, path)
    for root in roots:
        raw = _raw_dir(root)
        os.makedirs(raw, exist_ok=True)
        for fn, (_, src) in by_name.items():
            dest = os.path.join(raw, fn)
            try:
                if not os.path.isfile(dest) or os.path.getmtime(src) > os.path.getmtime(dest):
                    shutil.copy2(src, dest)
            except OSError:
                pass


def restore(publish_dir, local_pairs):
    """local_pairs: list of (type_name, local_cache_root)."""
    for name, local in local_pairs:
        os.makedirs(local, exist_ok=True)
        src = os.path.join(publish_dir, "event_cache", name)
        if os.path.isdir(src):
            for root, _, files in os.walk(src):
                rel = os.path.relpath(root, src)
                dest_root = os.path.join(local, rel) if rel != "." else local
                os.makedirs(dest_root, exist_ok=True)
                for fn in files:
                    shutil.copy2(os.path.join(root, fn), os.path.join(dest_root, fn))
    legacy = os.path.join(publish_dir, "unlock_cache", "cmc_snapshots")
    if os.path.isdir(legacy):
        dest = local_pairs[0][1] if local_pairs else None
        if dest:
            cmc = os.path.join(dest, "cmc_snapshots")
            os.makedirs(cmc, exist_ok=True)
            for fn in os.listdir(legacy):
                if fn.endswith(".json"):
                    shutil.copy2(os.path.join(legacy, fn), os.path.join(cmc, fn))
    mirror_upbit_cache([local for _, local in local_pairs])


def publish(local_pairs, publish_dir, keep_days=DEFAULT_KEEP_DAYS):
    mirror_upbit_cache([local for _, local in local_pairs])
    for name, local in local_pairs:
        if not os.path.isdir(local):
            continue
        dest = os.path.join(publish_dir, "event_cache", name)
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        shutil.copytree(local, dest)
        _trim_old(dest, keep_days)
    # drop legacy path after migration
    leg = os.path.join(publish_dir, "unlock_cache")
    if os.path.isdir(leg):
        shutil.rmtree(leg, ignore_errors=True)


def _trim_old(root, keep_days):
    cutoff = time.time() - keep_days * 86400
    cmc = os.path.join(root, "cmc_snapshots")
    if not os.path.isdir(cmc):
        return
    for fn in os.listdir(cmc):
        if not fn.endswith(".json") or len(fn) < 8:
            continue
        try:
            y, mo, d = int(fn[:4]), int(fn[4:6]), int(fn[6:8])
            ep = time.mktime((y, mo, d, 12, 0, 0, 0, 0, -1))
        except ValueError:
            continue
        if ep < cutoff:
            os.remove(os.path.join(cmc, fn))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=("restore", "publish"))
    ap.add_argument("publish_dir")
    ap.add_argument("--keep-days", type=int, default=DEFAULT_KEEP_DAYS)
    ap.add_argument("--unlock-cache", default="unlock_tracker/cache")
    ap.add_argument("--delist-cache", default="delist_tracker/cache")
    ap.add_argument("--perp-cache", default="futures_listing_tracker/cache")
    ap.add_argument("--burn-cache", default="burn_tracker/cache")
    ap.add_argument("--spot-cache", default="spot_listing_tracker/cache")
    args = ap.parse_args()
    pairs = [
        ("unlock", args.unlock_cache),
        ("delist", args.delist_cache),
        ("perp_listing", args.perp_cache),
        ("burn", args.burn_cache),
        ("spot_listing", args.spot_cache),
    ]
    if args.action == "restore":
        restore(args.publish_dir, pairs)
    else:
        publish(pairs, args.publish_dir, args.keep_days)
    print("ok")


if __name__ == "__main__":
    main()
