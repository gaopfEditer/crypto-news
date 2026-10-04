#!/usr/bin/env python3
"""Per-source run health from tracker run_meta.json (for conservative future reconcile)."""
import json
import os
import re

# Normalized source keys used in lib["source_health"]
UNLOCK_SOURCE_MAP = (
    (re.compile(r"defillama", re.I), "defillama"),
    (re.compile(r"coinmarketcap|cmc", re.I), "cmc"),
    (re.compile(r"news", re.I), "news"),
    (re.compile(r"extra", re.I), "extra"),
)

EXCHANGE_KEYS = (
    "binance", "okx", "bybit", "bitget", "upbit", "coinbase", "gate",
)


def norm_source_key(raw):
    if not raw:
        return "unknown"
    s = str(raw).strip().lower()
    for rx, key in UNLOCK_SOURCE_MAP:
        if rx.search(s):
            return key
    for ex in EXCHANGE_KEYS:
        if ex in s:
            return ex
    return re.sub(r"[^a-z0-9_]", "_", s)[:24] or "unknown"


def parse_run_meta(etype, out_dir):
    path = os.path.join(out_dir, "run_meta.json")
    if not os.path.isfile(path):
        return {"failed": set(), "errors": []}
    with open(path, encoding="utf-8") as f:
        meta = json.load(f)
    errors = meta.get("errors") or []
    failed = set()
    for err in errors:
        err_l = str(err).lower()
        if etype == "unlock":
            if err_l.startswith("defillama") or "defillama" in err_l[:30]:
                failed.add("defillama")
            if "coinmarkcap" in err_l or "cmc" in err_l[:20]:
                failed.add("cmc")
        elif etype == "delist":
            for ex in EXCHANGE_KEYS:
                if err_l.startswith(ex + ":") or err_l.startswith(ex + " "):
                    failed.add(ex)
            if "gate" in err_l and "403" in err_l:
                failed.add("gate")
        elif etype == "perp_listing":
            for ex in ("binance", "okx", "bybit", "bitget"):
                if ex in err_l[:40]:
                    failed.add(ex)
        elif etype == "burn":
            for s in ("panews", "odaily", "hyperliquid", "pump"):
                if s in err_l:
                    failed.add(s)
    return {"failed": failed, "errors": errors, "meta": meta}


def count_future_by_source(future_events):
    counts = {}
    for e in future_events:
        src = e.get("data_source") or "unknown"
        counts[src] = counts.get(src, 0) + 1
    return counts


def update_source_health(lib, etype, future_events, run_info, now_ts, min_ratio=0.7):
    """Update lib['source_health'] after a merge run. Returns dict of sources ok this run."""
    lib.setdefault("source_health", {})
    health = lib["source_health"]
    counts = count_future_by_source(future_events)
    failed = run_info.get("failed") or set()
    ok_this_run = {}

    # also consider unlock past+future ingest success: if meta has source with 0 raw and error, failed
    meta = run_info.get("meta") or {}
    if etype == "unlock":
        srcs = meta.get("sources") or {}
        for label, key in (("DefiLlama", "defillama"), ("CoinMarketCap", "cmc")):
            if any(str(e).lower().startswith(key) or key in str(e).lower() for e in run_info.get("errors") or []):
                failed.add(key)
            if label not in srcs and key not in failed:
                # source not attempted this run — treat as failed for reconcile volume
                failed.add(key)
        for label, key in (("DefiLlama", "defillama"), ("CoinMarketCap", "cmc")):
            info = srcs.get(label)
            if info is not None and int(info.get("events_raw") or 0) == 0:
                failed.add(key)

    all_keys = set(counts.keys()) | set(health.keys()) | set(failed)
    for src in all_keys:
        h = health.setdefault(src, {"last_count": 0, "consecutive_ok": 0, "last_ok_ts": None, "last_run_ok": False})
        cnt = counts.get(src, 0)
        prev = h.get("last_count") or 0
        src_failed = src in failed
        volume_ok = prev == 0 or cnt >= prev * min_ratio
        run_ok = not src_failed and volume_ok
        h["last_run_count"] = cnt
        h["last_run_ok"] = run_ok
        if run_ok:
            h["consecutive_ok"] = int(h.get("consecutive_ok") or 0) + 1
            h["last_count"] = cnt if cnt > 0 else prev
            h["last_ok_ts"] = now_ts
        else:
            h["consecutive_ok"] = 0
        ok_this_run[src] = run_ok
    return ok_this_run
