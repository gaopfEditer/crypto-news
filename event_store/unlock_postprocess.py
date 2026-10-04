#!/usr/bin/env python3
"""Post-merge enrichment for unlock event library: manual rows, dedup, conflicts, impact_score."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics

HERE = os.path.dirname(os.path.abspath(__file__))
MANUAL_PATH = os.path.join(HERE, "unlock_manual.json")
DEDUP_TOL = 3600
AMOUNT_CONFLICT_RATIO = 1.05
SRC_PREF = {"manual": 0, "news": 1, "extra": 1, "cmc": 2, "defillama": 3, "CoinMarketCap": 2, "DefiLlama": 3}


def _hour_bucket(ts):
    return (ts // 3600) * 3600 if ts else 0


def canonical_cluster_id(ticker, anchor_ts):
    hb = _hour_bucket(anchor_ts)
    raw = f"unlock|{ticker.upper()}|cluster|{hb}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _parse_flags(s):
    if not s:
        return []
    return [x.strip() for x in str(s).split(";") if x.strip()]


def _join_flags(parts):
    seen = []
    for p in parts:
        for x in _parse_flags(p):
            if x not in seen:
                seen.append(x)
    return "; ".join(seen) if seen else None


def normalize_unlock_fields(e):
    """Map legacy CSV / snapshot field names to the canonical unlock `fields` shape."""
    f = dict(e.get("fields") or {})
    if f.get("amount") is None and f.get("unlock_amount") is not None:
        f["amount"] = f["unlock_amount"]
    if f.get("value_usd_at_unlock") is None and f.get("computed_value_usd") is not None:
        f["value_usd_at_unlock"] = f["computed_value_usd"]
    if f.get("sources") is None and f.get("unlock_source") is not None:
        f["sources"] = f["unlock_source"]
    if f.get("value_usd_source") is None and f.get("source_value_usd") is not None:
        f["value_usd_source"] = f["source_value_usd"]
    for legacy in ("unlock_amount", "computed_value_usd", "unlock_source", "source_value_usd"):
        f.pop(legacy, None)
    if not e.get("status"):
        st = f.get("status")
        ts = e.get("event_ts")
        now = e.get("_now_ts")
        if not st and ts and now:
            st = "scheduled" if ts > now else "occurred"
        e["status"] = st or "occurred"
        f["status"] = e["status"]
    e["fields"] = f
    return e


def _source_kind(fields):
    src = (fields.get("sources") or fields.get("unlock_source") or "").lower()
    if "manual" in src:
        return "manual"
    if "panews" in src or "news" in src:
        return "news"
    if "coinmarketcap" in src or "cmc" in src:
        return "cmc"
    if "defillama" in src:
        return "defillama"
    return "unknown"


def _parse_source_amounts(text):
    out = {}
    if not text:
        return out
    for part in re.split(r"[;|]", str(text)):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"([^:：]+)[:：]\s*([\d.eE+-]+)", part)
        if m:
            try:
                out[m.group(1).strip()] = float(m.group(2))
            except ValueError:
                continue
    return out


def _pct_basis(fields):
    src = (fields.get("sources") or "").lower()
    if fields.get("pct_circ_basis"):
        return fields["pct_circ_basis"]
    if "coinmarketcap" in src or "cmc" in src:
        return "cmc_circulating"
    if "defillama" in src:
        return "defillama_circulating"
    if "coingecko" in (fields.get("price_source") or "").lower():
        return "coingecko_circulating"
    if "tokenomist" in src or "manual" in src:
        return "tokenomist"
    return "unknown"


def _sanitize_pct(fields):
    pct = fields.get("pct_circ")
    basis = _pct_basis(fields)
    fields["pct_circ_basis"] = basis
    if pct is None:
        fields["pct_circ_display"] = None
        return
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        fields["pct_circ_display"] = None
        return
    if pct > 100:
        fields["pct_circ_anomaly"] = True
        fields["pct_circ_display"] = "异常"
        fl = _join_flags([fields.get("flags"), "pct_circ_anomaly"])
        fields["flags"] = fl
    else:
        fields["pct_circ_display"] = round(pct, 4)


def _detect_conflicts(fields):
    flags = _parse_flags(fields.get("flags"))
    amap = _parse_source_amounts(fields.get("source_amounts"))
    amounts = list(amap.values())
    if fields.get("amount") is not None:
        try:
            amounts.append(float(fields["amount"]))
        except (TypeError, ValueError):
            pass
    if len(amounts) >= 2:
        lo, hi = min(amounts), max(amounts)
        if lo > 0 and hi / lo > AMOUNT_CONFLICT_RATIO and "amount_conflict" not in flags:
            flags.append("amount_conflict")
    pcts = []
    for k, v in amap.items():
        if "pct" in k.lower():
            try:
                pcts.append(float(str(v).replace("%", "")))
            except ValueError:
                pass
    if fields.get("pct_circ") is not None and not fields.get("pct_circ_anomaly"):
        try:
            pcts.append(float(fields["pct_circ"]))
        except (TypeError, ValueError):
            pass
    if len(pcts) >= 2:
        lo, hi = min(pcts), max(pcts)
        if lo > 0 and hi / lo > AMOUNT_CONFLICT_RATIO and "pct_conflict" not in flags:
            flags.append("pct_conflict")
    times = fields.get("source_times_utc8") or ""
    if "date_conflict" in str(fields.get("flags") or "") or re.search(r"date_conflict", times, re.I):
        if "date_conflict" not in flags:
            flags.append("date_conflict")
    elif re.search(r"date_conflict\(", fields.get("flags") or ""):
        if "date_conflict" not in flags:
            flags.append("date_conflict")
    nsrc = fields.get("n_sources")
    single = (fields.get("single_source") == "yes") or (nsrc == 1) or ("single_source" in flags)
    if len(amap) >= 2:
        single = False
    note_l = (fields.get("note") or "").lower()
    src_l = (fields.get("sources") or "").lower()
    if single and ("tokenomist" in note_l or "已全部解锁" in note_l or "fully unlocked" in note_l):
        if "source_conflict" not in flags:
            flags.append("source_conflict")
    if single and "coinmarketcap" in src_l and fields.get("amount") and float(fields.get("amount") or 0) > 1e8:
        if "source_conflict" not in flags:
            flags.append("source_conflict")
    fields["flags"] = _join_flags(["; ".join(flags)])


def recipient_weight(recipients):
    s = (recipients or "").lower()
    high = ("team", "investor", "invest", "private", "vc", "founder", "core", "insider", "creator")
    mid = ("ecosystem", "community", "mining", "airdrop", "contributor")
    low = ("treasury", "reserve", "foundation", "protocol")
    if any(k in s for k in high):
        return 1.0
    if any(k in s for k in mid):
        return 0.72
    if any(k in s for k in low):
        return 0.45
    return 0.62


def unlock_type_weight(unlock_type):
    u = (unlock_type or "").lower()
    if "cliff" in u:
        return 1.0
    if "linear" in u:
        return 0.68
    return 0.78


def compute_impact_score(fields):
    """Return 0–100 impact score (documented in event_store/README.md)."""
    pct = fields.get("pct_circ")
    if fields.get("pct_circ_anomaly"):
        pct = None
    else:
        try:
            pct = float(pct) if pct is not None else None
        except (TypeError, ValueError):
            pct = None
    val = fields.get("value_usd_at_unlock") or fields.get("computed_value_usd") or fields.get("value_usd_source")
    try:
        val = float(val) if val is not None else None
    except (TypeError, ValueError):
        val = None
    vol = fields.get("vol24h_usd")
    try:
        vol = float(vol) if vol is not None else None
    except (TypeError, ValueError):
        vol = None

    score = 0.0
    if pct is not None and 0 < pct <= 100:
        score += min(40.0, pct * 2.0)
    if val is not None and val > 0:
        score += min(30.0, math.log10(val + 1.0) * 4.5)
    if val and vol and vol > 0:
        ratio = val / vol
        score += min(15.0, ratio * 12.0)
    mult = recipient_weight(fields.get("recipients")) * unlock_type_weight(fields.get("unlock_type"))
    score *= mult
    return int(max(0, min(100, round(score))))


def _load_manual():
    if not os.path.isfile(MANUAL_PATH):
        return {"overrides": [], "events": []}
    with open(MANUAL_PATH, encoding="utf-8") as f:
        return json.load(f)


def _apply_overrides(events, overrides, now_ts):
    from store import TYPE_CONFIG, row_to_event, ts_from_utc8  # noqa: WPS433

    cfg = TYPE_CONFIG["unlock"]
    for ov in overrides or []:
        match = ov.get("match") or {}
        patch = ov.get("fields") or {}
        ticker = (match.get("ticker") or "").upper()
        prefix = match.get("date_prefix")
        for e in events:
            if e.get("merged_into"):
                continue
            f = e.get("fields") or {}
            if (f.get("ticker") or "").upper() != ticker:
                continue
            tstr = f.get("unlock_time_utc8") or e.get("event_time_utc8") or ""
            if prefix and not str(tstr).startswith(prefix):
                continue
            for k, v in patch.items():
                if k == "flags":
                    f["flags"] = _join_flags([f.get("flags"), v])
                else:
                    f[k] = v
            e["fields"] = f
            normalize_unlock_fields(e)
            from store import ts_from_utc8  # noqa: WPS433
            ts = ts_from_utc8(f.get("unlock_time_utc8"))
            if ts:
                e["event_ts"] = ts
                e["event_time_utc8"] = f.get("unlock_time_utc8")
            e["last_updated_ts"] = now_ts


def _manual_to_events(manual_rows, now_ts):
    from store import TYPE_CONFIG, row_to_event  # noqa: WPS433

    cfg = TYPE_CONFIG["unlock"]
    out = []
    for row in manual_rows or []:
        row = dict(row)
        row.setdefault("sources", row.get("sources") or "manual")
        row.setdefault("status", row.get("status") or "scheduled")
        row["unlock_source"] = row.get("sources")
        ev = row_to_event("unlock", row, [], now_ts, cfg)
        if not ev:
            continue
        ev["data_source"] = "manual"
        ev["status"] = row.get("status") or ev["status"]
        ev["is_future"] = ev["event_ts"] > now_ts and ev["status"] == "scheduled"
        out.append(ev)
    return out


def _merge_cluster_members(cluster, now_ts):
    cluster = sorted(
        cluster,
        key=lambda e: (SRC_PREF.get(_source_kind(e.get("fields") or {}), 9), e.get("event_ts") or 0),
    )
    anchor = int(statistics.median([e["event_ts"] for e in cluster]))
    ticker = (cluster[0].get("fields") or {}).get("ticker", "").upper()
    cid = canonical_cluster_id(ticker, anchor)
    bf = dict(cluster[0].get("fields") or {})
    alias_ids = []
    flags = _parse_flags(bf.get("flags"))
    amounts = []
    for e in cluster:
        alias_ids.append(e["id"])
        f = e.get("fields") or {}
        flags.extend(_parse_flags(f.get("flags")))
        if f.get("amount") is not None:
            amounts.append((f.get("sources") or e["id"], f.get("amount")))
        for k in (
            "note", "source_urls", "source_amounts", "source_times_utc8", "recipients",
            "pct_circ", "value_usd_at_unlock", "value_usd_source", "unlock_type", "name",
        ):
            if bf.get(k) is None and f.get(k) is not None:
                bf[k] = f[k]
    if len(amounts) > 1:
        bf["source_amounts"] = bf.get("source_amounts") or "; ".join(f"{s}:{a}" for s, a in amounts)
        if "amount_conflict" not in flags:
            flags.append("amount_conflict")
    ts_spread = max(e["event_ts"] for e in cluster) - min(e["event_ts"] for e in cluster)
    if ts_spread > 6 * 3600 and "date_conflict" not in flags:
        flags.append("date_conflict")
    bf["flags"] = _join_flags(["; ".join(flags)])
    st = next((e.get("status") for e in cluster if e.get("status")), "occurred")
    merged = {
        **cluster[0],
        "id": cid,
        "event_ts": anchor,
        "event_time_utc8": bf.get("unlock_time_utc8") or cluster[0].get("event_time_utc8"),
        "status": st,
        "fields": bf,
        "id_aliases": sorted(set(alias_ids)),
        "last_updated_ts": now_ts,
        "merged_from": sorted(set(alias_ids)),
    }
    return merged, alias_ids


def _same_date_utc8(ts):
    import datetime as dt
    TZ8 = dt.timezone(dt.timedelta(hours=8))
    return dt.datetime.fromtimestamp(ts, TZ8).strftime("%Y-%m-%d")


def dedupe_unlock_library(events, now_ts):
    active = [e for e in events if not e.get("merged_into")]
    by_ticker = {}
    for e in active:
        t = ((e.get("fields") or {}).get("ticker") or "").upper()
        if not t:
            continue
        by_ticker.setdefault(t, []).append(e)

    canonical = {}

    def _cluster_and_merge(evs, tol_seconds):
        evs = sorted(evs, key=lambda x: x.get("event_ts") or 0)
        clusters = []
        for e in evs:
            placed = False
            for c in clusters:
                if any(abs(e["event_ts"] - x["event_ts"]) <= tol_seconds for x in c):
                    c.append(e)
                    placed = True
                    break
            if not placed:
                clusters.append([e])
        for c in clusters:
            if len(c) < 2:
                continue
            merged, _alias = _merge_cluster_members(c, now_ts)
            canonical[merged["id"]] = merged
            for e in c:
                if e["id"] != merged["id"]:
                    e["merged_into"] = merged["id"]
                e["last_updated_ts"] = now_ts

    for _ticker, evs in by_ticker.items():
        _cluster_and_merge(evs, DEDUP_TOL)
        # Same calendar day (UTC+8) + amount within 5% → one unlock (multi-source time skew)
        by_day = {}
        for e in evs:
            if e.get("merged_into"):
                continue
            ts = e.get("event_ts")
            if not ts:
                continue
            by_day.setdefault(_same_date_utc8(ts), []).append(e)
        for day_evs in by_day.values():
            if len(day_evs) < 2:
                continue
            _cluster_and_merge(day_evs, 86400)

    if not canonical:
        return events
    out = []
    seen_canonical = set()
    for e in events:
        mid = e.get("merged_into")
        if mid:
            out.append(e)
            continue
        if e["id"] in canonical:
            if e["id"] not in seen_canonical:
                out.append(canonical[e["id"]])
                seen_canonical.add(e["id"])
            continue
        if e["id"] in seen_canonical:
            continue
        out.append(e)
    for cid, ev in canonical.items():
        if cid not in seen_canonical:
            out.append(ev)
            seen_canonical.add(cid)
    return out


def enrich_unlock_event(e, now_ts):
    normalize_unlock_fields(e)
    e["_now_ts"] = now_ts
    f = e["fields"]
    _sanitize_pct(f)
    _detect_conflicts(f)
    if f.get("pct_circ") is None and f.get("amount"):
        amap = _parse_source_amounts(f.get("source_amounts"))
        for _k, v in amap.items():
            if "pct" in _k.lower():
                f["pct_circ"] = v
                _sanitize_pct(f)
                break
    score = compute_impact_score(f)
    f["impact_score"] = score
    e["impact_score"] = score
    e.pop("_now_ts", None)
    return e


def postprocess_unlock_library(lib, now_ts):
    from store import merge_libraries  # noqa: WPS433

    manual = _load_manual()
    events = lib.get("events") or []
    for e in events:
        e.setdefault("fields", {})
        normalize_unlock_fields(e)

    manual_events = _manual_to_events(manual.get("events"), now_ts)
    if manual_events:
        merge_libraries(lib, manual_events, now_ts, "unlock")
        events = lib["events"]

    _apply_overrides(events, manual.get("overrides"), now_ts)

    lib["events"] = dedupe_unlock_library(events, now_ts)

    for e in lib["events"]:
        enrich_unlock_event(e, now_ts)

    lib["unlock_postprocess"] = {"version": 1, "ts": now_ts}
    return lib
