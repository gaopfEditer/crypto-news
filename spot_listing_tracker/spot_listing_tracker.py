#!/usr/bin/env python3
"""spot_listing_tracker: 现货上交易所（含韩国 Upbit 等）追踪 + 上线前后价格表现 (stdlib only).

公告/数据源:
  upbit   : api-manager.upbit.com 交易公告（신규 거래지원 / 디지털 자산 추가）
  bitget  : api/v2/public/annoucements coin_listings (annSubType=spot)
  okx     : /api/v5/public/instruments?instType=SPOT (listTime)
  binance : CMS catalogId=48 标题含 Binance Will List（排除合约/杠杆/Alpha）
  bithumb : api.bithumb.com/v1/notices（最新 20 条，마켓 추가）+ --extra-events 补充更早公告
"""
import argparse
import csv
import datetime as dt
import html as htmlmod
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "unlock_tracker"))
sys.path.insert(0, os.path.join(HERE, "..", "delist_tracker"))
import unlock_tracker as ut  # noqa: E402
import delist_tracker as dlt  # noqa: E402
from unlock_tracker import TZ8, NA, H, DAY, fmt_ts, fp, fmt_usd, csvv, pc, at  # noqa: E402
from delist_tracker import html_text, parse_en_dt, map_gecko  # noqa: E402

KST = dt.timezone(dt.timedelta(hours=9))
LOOKBACK_ANN = 60 * DAY
ALL_EX = ["upbit", "bitget", "okx", "binance", "bithumb"]
UPBIT_LIST = re.compile(r"신규 거래지원|디지털 자산 추가|마켓 추가")
UPBIT_SKIP = re.compile(r"거래지원 종료|거래 유의|입출금.*중단|출금.*중단|제한 해제")
UPBIT_ROW = re.compile(
    r"<tr>\s*<td>([^<]{1,120}?)\(([A-Z0-9]{1,15})\)</td>\s*<td>([^<]+)</td>",
    re.I,
)
QUOTES = ("USDT", "USDC", "KRW", "BTC", "ETH", "BRL", "TRY")
# analyze 配额：韩国所公告优先，避免 OKX 全量挤掉 Upbit/Bithumb（合并库会保留旧条，易出现「有 POD 无 KAIA」）
ANALYZE_EX_PRIORITY = ("Upbit", "Bithumb", "Binance", "Bitget", "OKX")
BITHUMB_LIST = re.compile(r"마켓\s*추가|원화\s*마켓\s*추가")
BITHUMB_SKIP = re.compile(r"거래지원\s*종료|거래유의|입출금.*중지|입출금.*중단|출금.*중단|명칭\s*변경")


def upbit_iso_ts(s):
    """Upbit API ISO8601 (+09:00 / Z / fractional seconds) → unix ts."""
    if not s:
        raise ValueError("empty time")
    s = str(s).strip().replace("Z", "+00:00")
    m = re.match(r"(\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d)?)(?:\.(\d+))?(.*)$", s)
    if not m:
        raise ValueError(s)
    base, _frac, tz = m.groups()
    d = dt.datetime.fromisoformat(base + (tz or ""))
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp())


def log(*a):
    print("[spot_listing_tracker]", *a, file=sys.stderr, flush=True)


def mk(exchange, symbol, list_ts, ann_ts, region=None, **kw):
    e = dict(
        exchange=exchange,
        symbol=symbol.upper().strip(),
        name=None,
        list_ts=list_ts,
        ann_ts=ann_ts,
        region=region or ("KR" if exchange in ("Upbit", "Bithumb") else "global"),
        kind="token",
        pairs=None,
        url=None,
        title=None,
        status="scheduled",
        flags=[],
    )
    e.update(kw)
    return e


def parse_korean_list_time(text, ref_ts):
    """'10월 8일 14시 예정' → unix ts (KST)."""
    m = re.search(r"(\d{1,2})월\s*(\d{1,2})일\s*(\d{1,2})시", text or "")
    if not m:
        return None
    ref = dt.datetime.fromtimestamp(ref_ts, KST)
    y, mo, d, hh = ref.year, int(m.group(1)), int(m.group(2)), int(m.group(3))
    if mo < ref.month - 2:
        y += 1
    try:
        return int(dt.datetime(y, mo, d, hh, 0, tzinfo=KST).timestamp())
    except ValueError:
        return None


def sym_from_title(title):
    syms = syms_from_title(title)
    return syms[0] if syms else None


def syms_from_title(title):
    return [m.group(1).upper() for m in re.finditer(r"\(([A-Z0-9]{1,15})\)", title or "")]


def bithumb_pub_ts(s):
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return int(dt.datetime.strptime(s[:19], fmt).replace(tzinfo=KST).timestamp())
        except ValueError:
            continue
    raise ValueError(s)


def event_ded_key(e):
    return (e.get("exchange", "").lower(), e.get("symbol"), e["list_ts"] // H if e.get("list_ts") else None)


def pin_extra_for_analyze(analyze, events, lo, hi, now_ts):
    """kr_listings_seed / --extra-events 条目始终进入 spot_listings 输出，不受 --max-analyze 挤掉。"""
    chosen, seen = list(analyze), {event_ded_key(e) for e in analyze}
    for e in events:
        if "extra" not in (e.get("flags") or []):
            continue
        if not e.get("list_ts") or not (lo <= e["list_ts"] <= hi and e["list_ts"] <= now_ts):
            continue
        k = event_ded_key(e)
        if k in seen:
            continue
        seen.add(k)
        chosen.append(e)
    return chosen


def http_err_code(ex):
    if isinstance(ex, urllib.error.HTTPError):
        return ex.code
    m = re.search(r"\b403\b", str(ex))
    return 403 if m else None


def select_for_analyze(events, lo, hi, now_ts, cap):
    pool = [e for e in events if e.get("list_ts") and lo <= e["list_ts"] <= hi and e["list_ts"] <= now_ts]
    chosen, seen = [], set()

    def ded_key(e):
        return event_ded_key(e)

    for ex in ANALYZE_EX_PRIORITY:
        for e in sorted((x for x in pool if x.get("exchange") == ex), key=lambda x: x["list_ts"], reverse=True):
            k = ded_key(e)
            if k in seen:
                continue
            seen.add(k)
            chosen.append(e)
    for e in sorted(pool, key=lambda x: x["list_ts"], reverse=True):
        k = ded_key(e)
        if k in seen:
            continue
        if len(chosen) >= cap:
            break
        seen.add(k)
        chosen.append(e)
    return chosen[:cap]


def _upbit_list_time_from_body(body, sym, ref_ts):
    tcell = re.search(
        rf"{re.escape(sym)}.*?</td>\s*<td>([^<]+)</td>\s*<td>([^<]+)</td>\s*<td>([^<]+)</td>\s*<td>([^<]+)</td>",
        body,
        re.S | re.I,
    )
    list_txt = tcell.group(4) if tcell else body
    return parse_korean_list_time(list_txt, ref_ts) or ref_ts


def _upbit_emit_title_fallback(out, title, rel, ann_id, list_src):
    url = f"https://upbit.com/service_center/notice?id={ann_id}"
    for sym in syms_from_title(title):
        out.append(mk("Upbit", sym, rel, rel, url=url, title=title, list_src=list_src))


def _upbit_parse_body(out, body, rel, title, ann_id, url):
    found = False
    for nm, sym, markets in UPBIT_ROW.findall(body):
        sym = sym.upper()
        lt = _upbit_list_time_from_body(body, sym, rel)
        out.append(
            mk("Upbit", sym, lt, rel, name=nm.strip(), pairs=markets.strip(), url=url, title=title, list_src="announcement")
        )
        found = True
    if not found:
        for sym in syms_from_title(title):
            lt = _upbit_list_time_from_body(body, sym, rel)
            out.append(mk("Upbit", sym, lt, rel, url=url, title=title, list_src="title_fallback"))
        if not syms_from_title(title):
            log(f"upbit: cannot parse {title}")


def src_upbit(http, lo, hi, source_notes=None):
    out, items = [], []
    page_errors = []
    upbit_forbidden = False

    def note(msg):
        if source_notes is not None:
            source_notes.append(msg)

    for page in range(1, 10):
        if upbit_forbidden:
            break
        try:
            d = http.get(
                f"https://api-manager.upbit.com/api/v1/announcements?os=web&page={page}&per_page=20&category=trade",
                f"upbit_trade_p{page}.json",
                ttl=30 * 60,
            )
            lst = (d.get("data") or {}).get("notices") or []
        except Exception as ex:
            page_errors.append(f"page{page}: {ex}")
            log(f"upbit list page {page}: {ex}")
            if http_err_code(ex) == 403:
                upbit_forbidden = True
                note("upbit: HTTP 403 on announcements list (CI/datacenter IP); using title fallback / seed only")
            break
        if not lst:
            break
        items += lst
        try:
            oldest = upbit_iso_ts(lst[-1].get("listed_at"))
        except (ValueError, TypeError, KeyError) as ex:
            log(f"upbit: bad listed_at on page {page}: {ex}")
            break
        if oldest < lo - LOOKBACK_ANN:
            break
    pending = []
    for a in items:
        title = a.get("title") or ""
        try:
            sort_ts = upbit_iso_ts(a.get("listed_at") or a.get("first_listed_at"))
            rel = upbit_iso_ts(a.get("first_listed_at") or a.get("listed_at"))
        except (ValueError, TypeError, KeyError):
            log(f"upbit: skip bad time id={a.get('id')} title={title[:40]}")
            continue
        if sort_ts < lo - LOOKBACK_ANN or sort_ts > hi:
            continue
        if not UPBIT_LIST.search(title) or UPBIT_SKIP.search(title):
            continue
        ann_id = a.get("id")
        if not ann_id:
            continue
        pending.append((sort_ts, rel, title, ann_id))
    pending.sort(key=lambda x: x[0], reverse=True)
    in_window = [p for p in pending if p[0] >= lo]
    older = [p for p in pending if p[0] < lo]
    detail_budget = max(48, len(in_window) + 8)

    def handle(sort_ts, rel, title, ann_id, spend_budget):
        nonlocal detail_budget, upbit_forbidden
        url = f"https://upbit.com/service_center/notice?id={ann_id}"
        must_detail = sort_ts >= lo
        if upbit_forbidden:
            _upbit_emit_title_fallback(out, title, rel, ann_id, "title_fallback_api_forbidden")
            return
        if spend_budget and detail_budget <= 0 and not must_detail:
            _upbit_emit_title_fallback(out, title, rel, ann_id, "title_fallback_budget")
            return
        if spend_budget and detail_budget <= 0 and must_detail:
            _upbit_emit_title_fallback(out, title, rel, ann_id, "title_fallback_budget")
            return
        if spend_budget:
            detail_budget -= 1
        try:
            det = http.get(
                f"https://api-manager.upbit.com/api/v1/announcements/{ann_id}",
                f"upbit_ann_{ann_id}.json",
                ttl=7 * DAY,
            )
            body = (det.get("data") or {}).get("body") or ""
        except Exception as ex:
            page_errors.append(f"ann{ann_id}: {ex}")
            log(f"upbit detail {ann_id}: {ex}")
            if http_err_code(ex) == 403:
                upbit_forbidden = True
                note("upbit: HTTP 403 on announcement detail; skipping further Upbit API detail calls")
            _upbit_emit_title_fallback(out, title, rel, ann_id, "title_fallback_no_body")
            return
        _upbit_parse_body(out, body, rel, title, ann_id, url)

    for row in in_window:
        handle(*row, True)
    for row in older:
        handle(*row, True)
    if upbit_forbidden:
        note(f"upbit: api_forbidden=true; parsed_from_api={len(out)} list_errors={len(page_errors)}")
    if not out and page_errors and not items:
        note("upbit: no list data: " + "; ".join(page_errors[:5]))
        log("upbit: no announcements from API (" + "; ".join(page_errors[:3]) + ")")
    return out


def src_bithumb(http, lo, hi):
    """Bithumb Open API v1 /notices（published_at 降序，单次最多 20 条）。来源：apidocs.bithumb.com"""
    out = []
    rows = http.get("https://api.bithumb.com/v1/notices?count=20", "bithumb_notices_20.json", ttl=30 * 60)
    if not isinstance(rows, list):
        raise RuntimeError(f"bithumb notices unexpected type {type(rows)}")
    seen_notice = set()
    for row in rows:
        title = row.get("title") or ""
        cats = row.get("categories") or []
        if not any("마켓" in c and "추가" in c for c in cats):
            continue
        if BITHUMB_SKIP.search(title):
            continue
        try:
            rel = bithumb_pub_ts(row.get("published_at"))
        except ValueError:
            log(f"bithumb: bad published_at {row.get('published_at')}")
            continue
        if rel < lo - LOOKBACK_ANN or rel > hi:
            continue
        url = row.get("pc_url") or ""
        m = re.search(r"/notice/(\d+)", url)
        notice_key = m.group(1) if m else url
        if notice_key in seen_notice:
            continue
        seen_notice.add(notice_key)
        syms = syms_from_title(title)
        if not syms:
            log(f"bithumb: no ticker in {title[:60]}")
            continue
        pairs = "KRW" if ("원화" in title or "KRW" in title.upper()) else "KRW"
        for sym in syms:
            out.append(
                mk(
                    "Bithumb",
                    sym,
                    rel,
                    rel,
                    pairs=pairs,
                    url=url,
                    title=title,
                    list_src="bithumb_v1_notices",
                )
            )
    return out


def src_bitget(http, lo, hi):
    out, cursor = [], None
    for _ in range(8):
        url = "https://api.bitget.com/api/v2/public/annoucements?language=en_US&annType=coin_listings" + (
            f"&cursor={cursor}" if cursor else ""
        )
        d = http.get(url, f"bitget_spot_list_{cursor or 'first'}.json", ttl=30 * 60)
        for a in d.get("data") or []:
            if (a.get("annSubType") or "").lower() != "spot":
                continue
            title = a.get("annTitle") or ""
            rel = int(a.get("cTime", 0)) // 1000
            if rel < lo - LOOKBACK_ANN or rel > hi:
                continue
            sym = None
            m = re.search(r"list\s+([A-Za-z][A-Za-z0-9]{1,14})\s*\(", title, re.I) or re.search(
                r"list\s+([A-Z0-9]{2,12})\b", title, re.I
            )
            if m:
                sym = m.group(1).upper()
            if not sym:
                m2 = re.search(r"\(([A-Z0-9]{2,12})\)", title)
                sym = m2.group(1).upper() if m2 else None
            if not sym:
                continue
            link = a.get("annUrl") or a.get("url")
            out.append(mk("Bitget", sym, rel, rel, url=link, title=title, list_src="announcement_time"))
        cursor = (d.get("data") or [{}])[-1].get("cursor") if d.get("data") else None
        if not cursor:
            break
    return out


def src_okx(http, lo, hi):
    out = []
    d = http.get("https://www.okx.com/api/v5/public/instruments?instType=SPOT", "okx_spot_instruments.json", ttl=30 * 60)
    for s in d.get("data") or []:
        if not s.get("listTime"):
            continue
        lt = int(s["listTime"]) // 1000
        if lt < max(lo, hi - 35 * DAY) or lt > hi + DAY:
            continue
        base = (s.get("baseCcy") or "").upper()
        if not base or not re.fullmatch(r"[A-Z0-9]{2,12}", base):
            continue
        quote = (s.get("quoteCcy") or "").upper()
        if quote not in QUOTES:
            continue
        out.append(
            mk(
                "OKX",
                base,
                lt,
                None,
                pairs=f"{base}-{quote}",
                url=f"https://www.okx.com/trade-spot/{base.lower()}-{quote.lower()}",
                title=f"OKX spot {s.get('instId')}",
                list_src="instruments listTime",
                status="launched" if s.get("state") == "live" else s.get("state"),
            )
        )
    return out


def binance_body_text(body):
    return html_text(body)


def src_binance(http, lo, hi):
    out, arts = [], []
    for page in range(1, 6):
        d = http.get(
            f"https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1&catalogId=48&pageNo={page}&pageSize=50",
            f"binance_spot_list_p{page}.json",
            ttl=30 * 60,
        )
        a = d["data"]["catalogs"][0]["articles"]
        arts += a
        if not a or a[-1]["releaseDate"] / 1000 < lo - LOOKBACK_ANN:
            break
    for a in arts:
        rel = a["releaseDate"] // 1000
        title = a.get("title") or ""
        if rel < lo - LOOKBACK_ANN or rel > hi:
            continue
        if not re.search(r"Binance Will List|Will Open Trading for", title, re.I):
            continue
        if re.search(r"Futures|Perpetual|Margin|Alpha|Delist|Monitoring Tag|bStock|Earn", title, re.I):
            continue
        d = http.get(
            f"https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query?articleCode={a['code']}",
            f"binance_spot_art_{a['code']}.json",
            ttl=7 * DAY,
        )
        text = binance_body_text(d["data"]["body"])
        url = f"https://www.binance.com/en/support/announcement/detail/{a['code']}"
        found = False
        for ds, tm, sym in re.findall(
            r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s*\(UTC\)\s*:?\s*([A-Z0-9]{2,12})(?:\s*/\s*[A-Z0-9]+)?",
            text,
        ):
            base = sym
            for q in QUOTES:
                if base.endswith(q) and len(base) > len(q):
                    base = base[: -len(q)]
                    break
            lt = parse_en_dt(ds, tm)
            out.append(mk("Binance", base, lt, rel, url=url, title=title, list_src="announcement"))
            found = True
        if not found:
            for sym in re.findall(r"\(([A-Z0-9]{2,12})\)", title):
                out.append(mk("Binance", sym, rel, rel, url=url, title=title, list_src="announcement_fallback"))
    return out


def src_extra(path):
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            ts = row.get("list_time_utc8") or row.get("list_utc8")
            if not ts:
                continue
            lt = int(dt.datetime.strptime(ts[:16], "%Y-%m-%d %H:%M").replace(tzinfo=TZ8).timestamp())
            ann = row.get("announce_time_utc8")
            ann_ts = (
                int(dt.datetime.strptime(ann[:16], "%Y-%m-%d %H:%M").replace(tzinfo=TZ8).timestamp()) if ann else lt
            )
            out.append(
                mk(
                    row.get("exchange") or "Extra",
                    row["ticker"],
                    lt,
                    ann_ts,
                    region=row.get("region"),
                    pairs=row.get("pairs"),
                    url=row.get("source_url"),
                    title=row.get("title") or "extra",
                    flags=["extra"],
                )
            )
    return out


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="现货上交易所追踪")
    ap.add_argument("--start", help="窗口开始 YYYY-MM-DD (UTC+8)")
    ap.add_argument("--end", help="窗口结束 YYYY-MM-DD (UTC+8)")
    ap.add_argument("--now", help="固定 now (UTC+8)")
    ap.add_argument("--exchanges", default="upbit,bithumb,bitget,binance,okx", help=",".join(ALL_EX))
    ap.add_argument("--max-analyze", type=int, default=80, help="窗口内最多做价格分析条数（防 OKX 全量过慢）")
    ap.add_argument("--extra-events", help="补充 CSV: ticker,exchange,list_time_utc8,...")
    ap.add_argument("--id-map", action="append", default=[])
    ap.add_argument("--min-mcap", type=float, default=0)
    ap.add_argument("--out")
    ap.add_argument("--cache", default=os.path.join(HERE, "cache"))
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    return ap.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    real_now = int(time.time())
    now_ts = int(dt.datetime.strptime(a.now, "%Y-%m-%d %H:%M").replace(tzinfo=TZ8).timestamp()) if a.now else real_now
    lo = int(dt.datetime.strptime(a.start, "%Y-%m-%d").replace(tzinfo=TZ8).timestamp()) if a.start else now_ts - 30 * DAY
    hi = min(
        int(dt.datetime.strptime(a.end, "%Y-%m-%d").replace(tzinfo=TZ8).timestamp()) + DAY - 1 if a.end else now_ts,
        now_ts,
    )
    out_dir = a.out or os.path.join(os.getcwd(), "output", "latest")
    os.makedirs(out_dir, exist_ok=True)
    http = ut.Http(a.cache, a.refresh)
    meta = {
        "window_utc8": [fmt_ts(lo), fmt_ts(hi)],
        "now_utc8": fmt_ts(now_ts),
        "generated_utc8": fmt_ts(real_now),
        "args": vars(a),
        "sources": {},
        "errors": [],
        "unmapped": [],
    }
    fns = {"upbit": src_upbit, "bithumb": src_bithumb, "bitget": src_bitget, "okx": src_okx, "binance": src_binance}
    events = []
    for ex in [x.strip().lower() for x in a.exchanges.split(",") if x.strip()]:
        try:
            if ex in fns:
                src_notes = []
                if ex == "upbit":
                    evs = fns[ex](http, lo, now_ts, src_notes)
                else:
                    evs = fns[ex](http, lo, now_ts)
                src_meta = {"events_parsed": len(evs)}
                if ex == "upbit" and src_notes:
                    src_meta["notes"] = src_notes
                    meta["errors"].extend(src_notes)
                    if any("403" in n for n in src_notes):
                        src_meta["api_forbidden"] = True
            else:
                meta["errors"].append(f"unknown exchange {ex}")
                evs = []
                src_meta = {"events_parsed": 0}
            meta["sources"][ex] = src_meta
            events += evs
        except Exception as ex2:
            meta["errors"].append(f"{ex}: {type(ex2).__name__}: {ex2}")
            log(f"{ex} failed: {ex2}")
    seed = os.path.join(HERE, "kr_listings_seed.csv")
    extra_paths = [p for p in [a.extra_events, seed if os.path.isfile(seed) else None] if p]
    for path in extra_paths:
        extra_evs = src_extra(path)
        events += extra_evs
        meta["sources"]["extra"] = meta["sources"].get("extra") or {}
        meta["sources"]["extra"][os.path.basename(path)] = len(extra_evs)

    ded = {}
    for e in sorted(events, key=lambda x: (0 if "extra" in (x.get("flags") or []) else 1, x.get("ann_ts") or 0)):
        k = (e["exchange"].lower(), e["symbol"], e["list_ts"] // H if e["list_ts"] else None)
        ded[k] = e
    events = list(ded.values())
    analyze_all = [e for e in events if e["list_ts"] and lo <= e["list_ts"] <= hi and e["list_ts"] <= now_ts]
    analyze = select_for_analyze(events, lo, hi, now_ts, max(1, a.max_analyze))
    analyze = pin_extra_for_analyze(analyze, events, lo, hi, now_ts)
    if len(analyze_all) > len(analyze):
        meta["errors"].append(
            f"analyze capped at {a.max_analyze} (parsed in-window {len(analyze_all)}, selected {len(analyze)})"
        )
    kr = [e for e in analyze if e.get("exchange") in ("Upbit", "Bithumb")]
    meta["counts_kr_selected"] = len(kr)
    upcoming = [
        e
        for e in events
        if e["list_ts"] and e["list_ts"] > now_ts and e["list_ts"] <= now_ts + 14 * DAY
    ]
    meta["counts"] = {"parsed": len(events), "in_window": len(analyze), "upcoming": len(upcoming)}

    id_map = dict(x.split("=", 1) for x in a.id_map if "=" in x)
    bmap = {}
    try:
        bmap = ut.binance_symbols(http)
    except Exception as ex:
        meta["errors"].append(f"Binance exchangeInfo: {ex}")
    for e in analyze:
        e["gecko_id"], e["gecko_from"] = map_gecko(http, {"symbol": e["symbol"], "name": e.get("name"), "flags": []}, id_map)

    earliest = min([e["list_ts"] - 7 * DAY for e in analyze] + [now_ts - 8 * DAY])
    start_px = earliest - 2 * H
    btc = ut.binance_series(http, "BTCUSDT", start_px, now_ts)
    rows, daily = [], []
    for e in sorted(analyze, key=lambda x: (x["list_ts"], x["exchange"], x["symbol"])):
        ts, gid = e["list_ts"], e.get("gecko_id")
        flags = list(e.get("flags") or [])
        cgpts = []
        if gid:
            try:
                days = min(90, int((real_now - start_px) // DAY) + 2)
                d = http.get(
                    f"https://api.coingecko.com/api/v3/coins/{gid}/market_chart?vs_currency=usd&days={days}",
                    f"cg_chart_{gid}_{days}d.json",
                    ttl=20 * 60,
                )
                cgpts = [(int(p[0] // 1000), p[1]) for p in d.get("prices", []) if p[1] is not None]
            except Exception as ex:
                meta["errors"].append(f"CoinGecko {gid}: {ex}")
        pair = bmap.get(e["symbol"])
        pts, psrc = [], NA
        if pair:
            try:
                pts, psrc = ut.binance_series(http, pair, start_px, now_ts), f"Binance {pair} 1h"
            except Exception as ex:
                meta["errors"].append(f"Binance {pair}: {ex}")
        if not pts and cgpts:
            pts, psrc = cgpts, f"CoinGecko {gid} hourly"
        if not pts:
            flags.append("no_price_data")
        m = ut.window_metrics(pts, ts, now_ts) if pts else None
        b = ut.window_metrics(btc, ts, now_ts)
        g = (lambda k: m[k]) if m else (lambda k: None)
        b_post = b["post"]
        rows.append(
            {
                "ticker": e["symbol"],
                "exchange": e["exchange"],
                "region": e.get("region"),
                "pairs": e.get("pairs"),
                "announce_time_utc8": fmt_ts(e["ann_ts"]) if e.get("ann_ts") else None,
                "list_time_utc8": fmt_ts(ts),
                "list_utc8": fmt_ts(ts),
                "list_time_source": e.get("list_src"),
                "coingecko_id": gid,
                "coingecko_match": e.get("gecko_from"),
                "price_source": psrc,
                "pre7d_change_pct": g("pre7"),
                "post_listing_change_pct": g("post"),
                "btc_post_change_pct": b_post,
                "relative_vs_btc_post_pp": g("post") - b_post if g("post") is not None and b_post is not None else None,
                "post_window_hours": round((now_ts - ts) / H, 1),
                "flags": "; ".join(flags) or None,
                "source_url": e.get("url"),
                "title": e.get("title"),
            }
        )
        for lab, mm, src in ((e["symbol"], m, psrc), ("BTC", b, "Binance BTCUSDT 1h")):
            dr = {
                "ticker": e["symbol"],
                "exchange": e["exchange"],
                "series": lab,
                "list_time_utc8": fmt_ts(ts),
                "list_utc8": fmt_ts(ts),
                "D-7_start_utc8": fmt_ts(ts - 7 * DAY),
            }
            tot = 1.0
            for k in range(7, 0, -1):
                v = mm["daily"][f"D-{k}"] if mm else None
                dr[f"D-{k}_pct"] = v
                tot = tot * (1 + v / 100) if (v is not None and tot is not None) else None
            dr["total_7d_pct"] = (tot - 1) * 100 if tot is not None else None
            dr["price_source"] = src
            daily.append(dr)

    def wcsv(name, data):
        path = os.path.join(out_dir, name)
        with open(path, "w", newline="", encoding="utf-8") as f:
            if data:
                w = csv.DictWriter(f, fieldnames=list(data[0].keys()))
                w.writeheader()
                w.writerows([{k: csvv(v) for k, v in r.items()} for r in data])

    wcsv("spot_listings.csv", rows)
    wcsv("pre7d_daily.csv", daily)
    side = [
        {
            "exchange": e["exchange"],
            "ticker": e["symbol"],
            "region": e.get("region"),
            "announce_time_utc8": fmt_ts(e["ann_ts"]) if e.get("ann_ts") else None,
            "list_time_utc8": fmt_ts(e["list_ts"]),
            "pairs": e.get("pairs"),
            "flags": "; ".join(e.get("flags") or []) or None,
            "source_url": e.get("url"),
            "status": "scheduled",
        }
        for e in sorted(upcoming, key=lambda x: x["list_ts"])
    ]
    wcsv("other_events.csv", side)
    with open(os.path.join(out_dir, "run_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1, default=str)
    log(f"spot listings: analyze={len(rows)} upcoming={len(side)} -> {out_dir}")
    return rows


if __name__ == "__main__":
    main()
