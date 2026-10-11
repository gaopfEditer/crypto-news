#!/usr/bin/env python3
"""OKX spot xStocks (instCategory=3) filter for spot_listing merge + tracker."""
import json
import os
import re

OKX_INST_XSTOCK = "3"
OKX_X_PREFIX_CRYPTO_ALLOW = frozenset({"XRP", "XLM", "XTZ", "XCH", "XPL", "XDP"})


def okx_is_xstock_instrument(inst):
    return str(inst.get("instCategory") or "") == OKX_INST_XSTOCK


def okx_xstock_symbols_from_payload(d):
    syms = set()
    for s in d.get("data") or []:
        if okx_is_xstock_instrument(s):
            base = (s.get("baseCcy") or "").upper()
            if base:
                syms.add(base)
    return syms


def okx_heuristic_xstock_ticker(ticker):
    t = (ticker or "").upper()
    if not t.startswith("X") or len(t) < 4 or t in OKX_X_PREFIX_CRYPTO_ALLOW:
        return False
    return bool(re.fullmatch(r"X[A-Z0-9]{3,11}", t))


def load_okx_xstock_symbols(cache_json_path):
    if not cache_json_path or not os.path.isfile(cache_json_path):
        return set()
    with open(cache_json_path, encoding="utf-8") as f:
        return okx_xstock_symbols_from_payload(json.load(f))


def is_okx_xstock_field(exchange, ticker, xstock_syms):
    if (exchange or "").upper() != "OKX":
        return False
    sym = (ticker or "").upper()
    if xstock_syms:
        return sym in xstock_syms
    return okx_heuristic_xstock_ticker(sym)


def prune_spot_listing_lib(lib, xstock_syms):
    kept = []
    removed = 0
    for e in lib.get("events", []):
        f = e.get("fields") or {}
        if is_okx_xstock_field(f.get("exchange"), f.get("ticker"), xstock_syms):
            removed += 1
            continue
        kept.append(e)
    lib["events"] = kept
    return removed
