#!/usr/bin/env python3
"""Append-only event libraries for unlock / delist / perp / burn trackers."""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import re
import sys

TZ8 = dt.timezone(dt.timedelta(hours=8))
NA = "N/A"
DAY = 86400
FREEZE_AFTER = 30 * DAY
DAILY_NUM = {f"D-{d}_pct" for d in range(7, 0, -1)} | {"total_7d_pct"}

TYPE_CONFIG = {
    "unlock": {
        "file": "unlock.json",
        "main_csv": "unlocks.csv",
        "time_field": "unlock_time_utc8",
        "ticker_field": "ticker",
        "source_field": "unlock_source",
        "fallback_source": "sources",
        "pre7d_link": ("ticker", "unlock_time_utc8"),
        "rel_btc_field": "relative_vs_btc_post_pp",
        "post_field": "post_unlock_change_pct",
        "pre7_field": "pre7d_change_pct",
        "future_hint_fields": [],
    },
    "delist": {
        "file": "delist.json",
        "main_csv": "delistings.csv",
        "time_field": "delist_utc8",
        "ticker_field": "ticker",
        "source_field": "exchange",
        "fallback_source": "exchange",
        "pre7d_link": ("ticker", "delist_utc8"),
        "rel_btc_field": "relative_vs_btc_post_pp",
        "post_field": "post_delist_change_pct",
        "pre7_field": "pre7d_change_pct",
    },
    "perp_listing": {
        "file": "perp_listing.json",
        "main_csv": "listings.csv",
        "time_field": "launch_utc8",
        "ticker_field": "token",
        "source_field": "exchange",
        "fallback_source": "exchange",
        "pre7d_link": ("token", "launch_utc8"),
        "rel_btc_field": None,
        "post_field": "post_pct",
        "pre7_field": "pre7d_pct",
    },
    "burn": {
        "file": "burn.json",
        "main_csv": "burns.csv",
        "time_field": "burn_utc8",
        "ticker_field": "token",
        "source_field": "type",
        "fallback_source": "type",
        "pre7d_link": ("token", "burn_utc8"),
        "rel_btc_field": None,
        "post_field": "post_pct",
        "pre7_field": "pre7d_pct",
    },
}

MUTABLE_KEYS = {
    "price_now", "price_now_time", "price_now_time_utc8", "price_24h_after", "change_24h_after_pct",
    "post_unlock_change_pct", "post_delist_change_pct", "post_pct", "post_low_vs_unlock_pct",
    "post_high_vs_unlock_pct", "post_low_pct", "post_high_pct", "relative_vs_btc_post_pp",
    "btc_post_change_pct", "btc_post_pct", "post_window_hours", "post_hours", "ann_to_delist_change_pct",
    "ann_24h_change_pct", "ann_to_launch_pct", "mcap_usd_at_delist_cg", "vol24h_usd_at_delist_cg",
    "mcap_usd_at_launch_cg", "vol24h_usd_at_launch_cg", "flags", "note",
}


def ts_from_utc8(s):
    if not s or s == NA:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(dt.datetime.strptime(s[:19], fmt).replace(tzinfo=TZ8).timestamp())
        except ValueError:
            continue
    return None


def hour_bucket(ts):
    return (ts // 3600) * 3600 if ts else None


def parse_cell(key, raw):
    if raw is None or raw == "" or raw == NA:
        return None
    if key in DAILY_NUM or key.endswith("_pct") or key.endswith("_pp") or key.endswith("_hours"):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    if key in ("amount", "unlock_amount", "gap_days", "gap_h", "max_leverage", "n_sources", "mcap_usd_at_delist_cg",
               "vol24h_usd_at_delist_cg", "mcap_usd_at_launch_cg", "vol24h_usd_at_launch_cg", "usd_value",
               "pct_circ", "pct_circulating", "pct_total_preburn", "source_value_usd", "computed_value_usd",
               "value_usd_source", "value_usd_at_unlock", "price_7d_before", "price_at_unlock", "price_at_delist",
               "price_at_launch", "price_at_burn", "price_24h_after"):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    if key == "single_source":
        return raw == "yes" or raw is True
    if key in ("spot_on_exchange", "ann_in_pre7d", "in_window"):
        return raw
    return raw


def read_csv_rows(path):
    if not os.path.isfile(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{k: parse_cell(k, v) for k, v in row.items()} for row in csv.DictReader(f)]


def norm_source(s):
    if not s:
        return "unknown"
    s = re.sub(r"\s+", " ", str(s).strip().lower())
    return s[:80]


def event_id(etype, fields, cfg):
    sym = (fields.get(cfg["ticker_field"]) or "").upper()
    ts = ts_from_utc8(fields.get(cfg["time_field"]))
    hb = hour_bucket(ts) or 0
    src = norm_source(fields.get(cfg["source_field"]) or fields.get(cfg.get("fallback_source")))
    if etype == "delist":
        raw = f"{etype}|{sym}|{src}|{hb}"
    elif etype == "perp_listing":
        role = fields.get("role") or ""
        raw = f"{etype}|{sym}|{src}|{role}|{hb}"
    elif etype == "burn":
        tx = (fields.get("tx") or "")[:16]
        raw = f"{etype}|{sym}|{src}|{hb}|{tx}"
    else:
        raw = f"{etype}|{sym}|{src}|{hb}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def empty_lib(etype):
    return {"type": etype, "updated": 0, "events": []}


def load_lib(path, etype):
    if not os.path.isfile(path):
        return empty_lib(etype)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("type", etype)
    data.setdefault("events", [])
    return data


def save_lib(path, lib):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, indent=2)


def build_pre7d_map(rows, link_fields):
    m = {}
    for r in rows:
        key = tuple(r.get(k) for k in link_fields)
        if key[0] is None:
            continue
        m.setdefault(key, []).append(r)
    return m


def row_to_event(etype, fields, pre7d_rows, now_ts, cfg):
    ts = ts_from_utc8(fields.get(cfg["time_field"]))
    if ts is None:
        return None
    eid = event_id(etype, fields, cfg)
    return {
        "id": eid,
        "event_ts": ts,
        "event_time_utc8": fields.get(cfg["time_field"]),
        "is_future": ts > now_ts,
        "frozen": (now_ts - ts) > FREEZE_AFTER if ts <= now_ts else False,
        "last_updated_ts": now_ts,
        "fields": fields,
        "pre7d_daily": pre7d_rows or [],
    }


def merge_event(old, new, now_ts):
    ts = old.get("event_ts") or new.get("event_ts")
    frozen = old.get("frozen") or ((now_ts - ts) > FREEZE_AFTER if ts and ts <= now_ts else False)
    if frozen:
        old["frozen"] = True
        return old
    out = {**old, "last_updated_ts": now_ts, "frozen": False}
    out["is_future"] = ts > now_ts if ts else new.get("is_future")
    nf, of = new.get("fields") or {}, old.get("fields") or {}
    merged = dict(of)
    for k, v in nf.items():
        if k in MUTABLE_KEYS:
            if v is not None:
                merged[k] = v
        elif v is not None and (k not in merged or merged[k] is None):
            merged[k] = v
    out["fields"] = merged
    if new.get("pre7d_daily"):
        out["pre7d_daily"] = new["pre7d_daily"]
    return out


def merge_libraries(existing, incoming, now_ts):
    by_id = {e["id"]: e for e in existing.get("events", [])}
    before = len(by_id)
    for inc in incoming:
        if inc["id"] in by_id:
            by_id[inc["id"]] = merge_event(by_id[inc["id"]], inc, now_ts)
        else:
            by_id[inc["id"]] = inc
    existing["events"] = sorted(by_id.values(), key=lambda e: e.get("event_ts") or 0)
    existing["updated"] = now_ts
    return {"before": before, "after": len(by_id), "added": len(by_id) - before}


def normalize_fields(etype, row):
    row = dict(row)
    if etype == "delist":
        row.setdefault("delist_utc8", row.get("delist_time_utc8"))
        row.setdefault("announce_utc8", row.get("announce_time_utc8"))
    elif etype == "perp_listing":
        row.setdefault("launch_utc8", row.get("launch_time_utc8"))
        row.setdefault("announce_utc8", row.get("announce_time_utc8"))
        row.setdefault("token", row.get("symbol"))
    elif etype == "unlock":
        row.setdefault("amount", row.get("unlock_amount"))
        row.setdefault("value_usd_at_unlock", row.get("computed_value_usd"))
        row.setdefault("sources", row.get("unlock_source"))
    return row


def ingest_tracker_dir(etype, out_dir, now_ts):
    cfg = TYPE_CONFIG[etype]
    main_path = os.path.join(out_dir, cfg["main_csv"])
    pre7_path = os.path.join(out_dir, "pre7d_daily.csv")
    rows = [normalize_fields(etype, r) for r in read_csv_rows(main_path)]
    pre7_all = read_csv_rows(pre7_path)
    for r in pre7_all:
        if etype == "perp_listing" and r.get("launch_time_utc8") and not r.get("launch_utc8"):
            r["launch_utc8"] = r["launch_time_utc8"]
        if etype == "delist" and r.get("delist_time_utc8") and not r.get("delist_utc8"):
            r["delist_utc8"] = r["delist_time_utc8"]
    pre7_map = build_pre7d_map(pre7_all, cfg["pre7d_link"])
    events = []
    for row in rows:
        key = tuple(row.get(k) for k in cfg["pre7d_link"])
        pre7 = pre7_map.get(key, [])
        ev = row_to_event(etype, row, pre7, now_ts, cfg)
        if ev:
            events.append(ev)
    return events


def ingest_main_csv(etype, csv_path, now_ts):
    cfg = TYPE_CONFIG[etype]
    rows = [normalize_fields(etype, r) for r in read_csv_rows(csv_path)]
    events = []
    for row in rows:
        ev = row_to_event(etype, row, [], now_ts, cfg)
        if ev:
            events.append(ev)
    return events


def migrate_unlock_snapshot(snapshot_path, now_ts):
    with open(snapshot_path, encoding="utf-8") as f:
        snap = json.load(f)
    cfg = TYPE_CONFIG["unlock"]
    pre7_map = build_pre7d_map(snap.get("pre7d_daily") or [], cfg["pre7d_link"])
    events = []
    for row in snap.get("events") or []:
        key = (row.get("ticker"), row.get("unlock_time_utc8"))
        pre7 = pre7_map.get(key, [])
        ev = row_to_event("unlock", row, pre7, now_ts, cfg)
        if ev:
            events.append(ev)
    return events


def rebuild_index(events_dir):
    idx = {"updated": 0, "types": {}}
    for etype, cfg in TYPE_CONFIG.items():
        path = os.path.join(events_dir, cfg["file"])
        lib = load_lib(path, etype)
        idx["types"][etype] = {
            "file": cfg["file"],
            "updated": lib.get("updated", 0),
            "total": len(lib.get("events", [])),
        }
        idx["updated"] = max(idx["updated"], lib.get("updated", 0))
    with open(os.path.join(events_dir, "index.json"), "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)
    return idx


def window_count(lib, lo_ts, hi_ts, include_future=False, now_ts=None):
    now_ts = now_ts or int(dt.datetime.now(TZ8).timestamp())
    n = 0
    for e in lib.get("events", []):
        ts = e.get("event_ts")
        if ts is None:
            continue
        if ts > now_ts and not include_future:
            continue
        if lo_ts <= ts <= hi_ts:
            n += 1
        elif include_future and ts > hi_ts:
            n += 1
    return n
