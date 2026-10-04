#!/usr/bin/env python3
"""Convert unlock_tracker CSV outputs + run_meta.json to gh-pages JSON snapshot."""
import csv
import datetime as dt
import json
import os
import sys

TZ8 = dt.timezone(dt.timedelta(hours=8))
NA = "N/A"
NUM_FIELDS = {
    "amount", "value_usd_source", "value_usd_at_unlock", "pct_circ", "n_sources",
    "price_7d_before", "price_at_unlock", "price_24h_after", "price_now",
    "pre7d_change_pct", "post_unlock_change_pct", "change_24h_after_pct",
    "post_low_vs_unlock_pct", "post_high_vs_unlock_pct",
    "btc_pre7d_change_pct", "btc_post_change_pct", "relative_vs_btc_post_pp",
    "post_window_hours",
}
DAILY_NUM = {f"D-{d}_pct" for d in range(7, 0, -1)} | {"total_7d_pct"}


def parse_val(key, raw):
    if raw is None or raw == "" or raw == NA:
        return None
    if key in NUM_FIELDS or key in DAILY_NUM:
        try:
            return float(raw)
        except ValueError:
            return None
    if key == "single_source":
        return raw == "yes"
    return raw


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return [{k: parse_val(k, v) for k, v in row.items()} for row in csv.DictReader(f)]


def ts_from_utc8(s):
    return int(dt.datetime.strptime(s.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=TZ8).timestamp())


def build_observations(events, daily_token):
    weak = sorted(
        [e for e in events
         if e.get("relative_vs_btc_post_pp") is not None
         and e["relative_vs_btc_post_pp"] <= -5
         and (e.get("post_window_hours") or 0) >= 24],
        key=lambda e: e["relative_vs_btc_post_pp"],
    )
    strong = sorted(
        [e for e in events
         if e.get("relative_vs_btc_post_pp") is not None
         and e["relative_vs_btc_post_pp"] >= 5
         and (e.get("post_window_hours") or 0) >= 24],
        key=lambda e: -e["relative_vs_btc_post_pp"],
    )
    big_d1 = []
    for e in events:
        d = daily_token.get((e["ticker"], e["unlock_time_utc8"]))
        if d and d.get("D-1_pct") is not None and abs(d["D-1_pct"]) >= 8:
            big_d1.append(e["ticker"])
    return {
        "weak_vs_btc": [{"ticker": e["ticker"], "pp": e["relative_vs_btc_post_pp"]} for e in weak[:10]],
        "strong_vs_btc": [{"ticker": e["ticker"], "pp": e["relative_vs_btc_post_pp"]} for e in strong[:10]],
        "big_d1_moves": big_d1,
        "summary_lines": _summary_lines(weak, strong),
    }


def _summary_lines(weak, strong):
    lines = []
    if weak:
        top = weak[:3]
        lines.append(
            "解锁后相对 BTC 最弱："
            + "、".join(f"{e['ticker']}({e['relative_vs_btc_post_pp']:+.1f}pp)" for e in top)
        )
    if strong:
        top = strong[:3]
        lines.append(
            "解锁后相对 BTC 最强："
            + "、".join(f"{e['ticker']}({e['relative_vs_btc_post_pp']:+.1f}pp)" for e in top)
        )
    return lines


def snapshot_from_dir(out_dir):
    meta_path = os.path.join(out_dir, "run_meta.json")
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    events = read_csv(os.path.join(out_dir, "unlocks.csv"))
    daily = read_csv(os.path.join(out_dir, "pre7d_daily.csv"))
    daily_token = {(d["ticker"], d["unlock_time_utc8"]): d for d in daily if d.get("series") != "BTC"}
    generated = meta["generated_utc8"]
    snapshot_date = generated[:10]
    args = meta.get("args") or {}
    return {
        "updated": ts_from_utc8(generated),
        "snapshot_date": snapshot_date,
        "window_utc8": meta.get("window_utc8"),
        "now_utc8": meta.get("now_utc8"),
        "generated_utc8": generated,
        "thresholds": {
            "min_usd": args.get("min_usd"),
            "min_pct": args.get("min_pct"),
            "min_usd_floor": args.get("min_usd_floor"),
            "max_tokens": args.get("max_tokens"),
        },
        "events": events,
        "pre7d_daily": daily,
        "observations": build_observations(events, daily_token),
        "meta": {
            "sources": meta.get("sources"),
            "counts": meta.get("counts"),
            "errors": meta.get("errors"),
            "unmapped": meta.get("unmapped"),
            "price_notes": meta.get("price_notes"),
        },
    }


def write_snapshot(out_dir, deploy_unlocks_dir, min_keep_ratio=0.8):
    snap = snapshot_from_dir(out_dir)
    os.makedirs(deploy_unlocks_dir, exist_ok=True)
    path = os.path.join(deploy_unlocks_dir, snap["snapshot_date"] + ".json")
    new_n = len(snap.get("events") or [])
    skipped = False
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            old = json.load(f)
        old_n = len(old.get("events") or [])
        if old_n > 0 and new_n < old_n * min_keep_ratio:
            print(
                f"::warning title=解锁快照未覆盖::"
                f"{snap['snapshot_date']} 新结果 {new_n} 条 < 已有 {old_n} 条的 {int(min_keep_ratio * 100)}%，保留旧 JSON",
                file=sys.stderr,
            )
            skipped = True
            snap = old
    if not skipped:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
    return snap, skipped


def rebuild_index(deploy_unlocks_dir, keep_days=60):
    dates = []
    for fn in os.listdir(deploy_unlocks_dir):
        if fn.endswith(".json") and fn != "index.json" and len(fn) == 15:
            dates.append(fn[:-5])
    dates.sort(reverse=True)
    dates = dates[:keep_days]
    snapshots = []
    latest_updated = 0
    for d in dates:
        with open(os.path.join(deploy_unlocks_dir, d + ".json"), encoding="utf-8") as f:
            doc = json.load(f)
        snapshots.append({
            "date": d,
            "updated": doc.get("updated"),
            "generated_utc8": doc.get("generated_utc8"),
            "event_count": len(doc.get("events") or []),
            "window_utc8": doc.get("window_utc8"),
        })
        latest_updated = max(latest_updated, doc.get("updated") or 0)
    index = {"updated": latest_updated, "snapshots": sorted(snapshots, key=lambda x: x["date"], reverse=True)}
    with open(os.path.join(deploy_unlocks_dir, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    return index


def seed_fixtures(repo_root, deploy_unlocks_dir):
    fixtures = os.path.join(repo_root, "unlock_tracker", "fixtures")
    if not os.path.isdir(fixtures):
        return
    os.makedirs(deploy_unlocks_dir, exist_ok=True)
    for name in sorted(os.listdir(fixtures)):
        sub = os.path.join(fixtures, name)
        if os.path.isdir(sub) and os.path.isfile(os.path.join(sub, "run_meta.json")):
            target = os.path.join(deploy_unlocks_dir, name + ".json")
            if not os.path.isfile(target):
                write_snapshot(sub, deploy_unlocks_dir)[0]


def main(argv=None):
    argv = argv or sys.argv[1:]
    if len(argv) < 2:
        print("usage: export_json.py <tracker_out_dir> <deploy_unlocks_dir>", file=sys.stderr)
        sys.exit(2)
    out_dir, deploy_dir = argv[0], argv[1]
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    seed_fixtures(repo_root, deploy_dir)
    snap, skipped = write_snapshot(out_dir, deploy_dir)
    index = rebuild_index(deploy_dir)
    print(json.dumps({
        "snapshot": snap["snapshot_date"],
        "events": len(snap["events"]),
        "skipped_overwrite": skipped,
        "index_dates": [s["date"] for s in index["snapshots"]],
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
