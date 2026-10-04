#!/usr/bin/env python3
"""Build future event rows from tracker auxiliary CSV outputs."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from store import (  # noqa: E402
    TYPE_CONFIG,
    infer_data_source,
    normalize_fields,
    read_csv_rows,
    row_to_event,
    ts_from_utc8,
)

DAY = 86400


def _days_until(ts, now_ts):
    if not ts:
        return None
    return round((ts - now_ts) / DAY, 1)


def _flag_has(flags, *needles):
    fl = (flags or "").lower()
    return any(n in fl for n in needles)


def ingest_explicit_side(etype, out_dir, now_ts, horizon=14 * DAY):
    """Rows that explicitly cancel or reschedule future events (from other_events / future CSV)."""
    hi = now_ts + horizon
    path = os.path.join(out_dir, "other_events.csv")
    if not os.path.isfile(path):
        return []
    cfg = TYPE_CONFIG[etype]
    out = []
    for row in read_csv_rows(path):
        st_raw = (row.get("status") or "").lower()
        flags = row.get("flags") or ""
        if etype == "delist":
            ts = ts_from_utc8(row.get("delist_time_utc8"))
            if not ts:
                continue
            cancelled = st_raw in ("cancelled", "canceled", "已取消") or _flag_has(flags, "cancelled", "canceled", "取消")
            postponed = st_raw == "postponed" or _flag_has(flags, "postponed", "推迟", "延期")
            if not cancelled and not postponed:
                continue
            fields = normalize_fields("delist", {
                "ticker": row.get("ticker"),
                "exchange": row.get("exchange"),
                "delist_utc8": row.get("delist_time_utc8"),
                "delist_time_utc8": row.get("delist_time_utc8"),
                "announce_utc8": row.get("announce_time_utc8"),
                "announce_time_utc8": row.get("announce_time_utc8"),
                "kind": row.get("kind"),
                "pairs": row.get("pairs"),
                "flags": flags,
                "source_url": row.get("source_url"),
                "status": "cancelled" if cancelled else "scheduled",
            })
            ev = row_to_event("delist", fields, [], now_ts, cfg)
            if not ev:
                continue
            ev["status"] = "cancelled" if cancelled else "scheduled"
            ev["data_source"] = infer_data_source("delist", fields, cfg)
            if cancelled:
                ev["is_future"] = False
            elif ts > now_ts and ts <= hi:
                ev["is_future"] = True
            out.append(ev)
        elif etype == "perp_listing":
            if row.get("type") != "upcoming" and not _flag_has(flags, "cancelled", "postponed"):
                continue
            ts = ts_from_utc8(row.get("launch_time_utc8"))
            cancelled = _flag_has(flags, "cancelled", "canceled", "取消")
            if st_raw in ("cancelled", "canceled"):
                cancelled = True
            if not cancelled and st_raw != "postponed" and not _flag_has(flags, "postponed"):
                continue
            fields = normalize_fields("perp_listing", {
                "token": row.get("token"),
                "exchange": row.get("exchange"),
                "role": "upcoming",
                "launch_utc8": row.get("launch_time_utc8"),
                "launch_time_utc8": row.get("launch_time_utc8"),
                "flags": flags,
                "source_url": row.get("source_url"),
                "status": "cancelled" if cancelled else "scheduled",
            })
            ev = row_to_event("perp_listing", fields, [], now_ts, cfg)
            if ev:
                ev["status"] = fields["status"]
                ev["data_source"] = infer_data_source("perp_listing", fields, cfg)
                out.append(ev)
        elif etype == "burn":
            t = row.get("type") or ""
            if not t.startswith("upcoming"):
                continue
            bflags = row.get("flags") or ""
            cancelled = _flag_has(bflags, "cancelled", "canceled", "取消")
            if not cancelled:
                continue
            fields = normalize_fields("burn", {
                "token": row.get("token"),
                "type": row.get("detail") or "计划销毁",
                "burn_utc8": row.get("time_utc8"),
                "flags": bflags,
                "source_url": row.get("source_url"),
                "status": "cancelled",
            })
            ev = row_to_event("burn", fields, [], now_ts, cfg)
            if ev:
                ev["status"] = "cancelled"
                ev["data_source"] = infer_data_source("burn", fields, cfg)
                out.append(ev)
    return out


def ingest_future_unlock(out_dir, now_ts):
    rows = read_csv_rows(os.path.join(out_dir, "future_unlocks.csv"))
    pre7 = read_csv_rows(os.path.join(out_dir, "future_pre7d_daily.csv"))
    if not pre7:
        pre7 = read_csv_rows(os.path.join(out_dir, "pre7d_daily.csv"))
    link = TYPE_CONFIG["unlock"]["pre7d_link"]
    pre7_map = {}
    for r in pre7:
        pre7_map.setdefault((r.get(link[0]), r.get(link[1])), []).append(r)
    events = []
    for row in rows:
        row = normalize_fields("unlock", row)
        row.setdefault("status", "scheduled")
        key = (row.get(link[0]), row.get(link[1]))
        ev = row_to_event("unlock", row, pre7_map.get(key, []), now_ts, TYPE_CONFIG["unlock"])
        if ev:
            ev["status"] = row.get("status") or "scheduled"
            ev["data_source"] = infer_data_source("unlock", row, TYPE_CONFIG["unlock"])
            if (row.get("status") or "").lower() == "cancelled":
                ev["is_future"] = False
            events.append(ev)
    return events


def ingest_future_delist(out_dir, now_ts, horizon=14 * DAY):
    hi = now_ts + horizon
    events = []
    for row in read_csv_rows(os.path.join(out_dir, "other_events.csv")):
        if (row.get("status") or "").lower() not in ("scheduled", "待执行"):
            continue
        ts = ts_from_utc8(row.get("delist_time_utc8"))
        if not ts or ts <= now_ts or ts > hi:
            continue
        fields = normalize_fields("delist", {
            "ticker": row.get("ticker"),
            "exchange": row.get("exchange"),
            "delist_utc8": row.get("delist_time_utc8"),
            "delist_time_utc8": row.get("delist_time_utc8"),
            "announce_utc8": row.get("announce_time_utc8"),
            "announce_time_utc8": row.get("announce_time_utc8"),
            "kind": row.get("kind"),
            "pairs": row.get("pairs"),
            "flags": row.get("flags"),
            "source_url": row.get("source_url"),
            "status": "scheduled",
            "days_until": _days_until(ts, now_ts),
        })
        ev = row_to_event("delist", fields, [], now_ts, TYPE_CONFIG["delist"])
        if ev:
            ev["status"] = "scheduled"
            ev["data_source"] = infer_data_source("delist", fields, TYPE_CONFIG["delist"])
            events.append(ev)
    return events


def ingest_future_perp(out_dir, now_ts, horizon=14 * DAY):
    hi = now_ts + horizon
    events = []
    for row in read_csv_rows(os.path.join(out_dir, "other_events.csv")):
        if row.get("type") != "upcoming":
            continue
        ts = ts_from_utc8(row.get("launch_time_utc8"))
        if not ts or ts <= now_ts or ts > hi:
            continue
        fields = normalize_fields("perp_listing", {
            "token": row.get("token"),
            "exchange": row.get("exchange"),
            "role": "upcoming",
            "launch_utc8": row.get("launch_time_utc8"),
            "launch_time_utc8": row.get("launch_time_utc8"),
            "announce_utc8": row.get("announce_time_utc8"),
            "announce_time_utc8": row.get("announce_time_utc8"),
            "max_leverage": row.get("max_leverage"),
            "flags": row.get("flags"),
            "source_url": row.get("source_url"),
            "status": "scheduled",
            "days_until": _days_until(ts, now_ts),
        })
        ev = row_to_event("perp_listing", fields, [], now_ts, TYPE_CONFIG["perp_listing"])
        if ev:
            ev["status"] = "scheduled"
            ev["data_source"] = infer_data_source("perp_listing", fields, TYPE_CONFIG["perp_listing"])
            events.append(ev)
    return events


def ingest_future_burn(out_dir, now_ts, horizon=14 * DAY):
    hi = now_ts + horizon
    events = []
    for row in read_csv_rows(os.path.join(out_dir, "other_events.csv")):
        t = row.get("type") or ""
        if not t.startswith("upcoming"):
            continue
        ts = ts_from_utc8(row.get("time_utc8"))
        if not ts or ts <= now_ts or ts > hi:
            continue
        fields = normalize_fields("burn", {
            "token": row.get("token"),
            "type": row.get("detail") or "计划销毁",
            "burn_utc8": row.get("time_utc8"),
            "amount": row.get("amount"),
            "usd_value": row.get("usd_value"),
            "source_url": row.get("source_url"),
            "status": "scheduled",
            "days_until": _days_until(ts, now_ts),
            "flags": "scheduled",
        })
        ev = row_to_event("burn", fields, [], now_ts, TYPE_CONFIG["burn"])
        if ev:
            ev["status"] = "scheduled"
            ev["data_source"] = infer_data_source("burn", fields, TYPE_CONFIG["burn"])
            events.append(ev)
    return events


def collect_future(etype, out_dir, now_ts, horizon_days=14):
    horizon = horizon_days * DAY
    if etype == "unlock":
        return ingest_future_unlock(out_dir, now_ts)
    if etype == "delist":
        return ingest_future_delist(out_dir, now_ts, horizon)
    if etype == "perp_listing":
        return ingest_future_perp(out_dir, now_ts, horizon)
    if etype == "burn":
        return ingest_future_burn(out_dir, now_ts, horizon)
    return []


def collect_side_effects(etype, out_dir, now_ts, horizon_days=14):
    return ingest_explicit_side(etype, out_dir, now_ts, horizon_days * DAY)
