#!/usr/bin/env python3
"""Binance USDT 24h 涨跌幅榜 + 异动因素（新闻/事件/量能/合约），输出 movers.json。"""
import argparse
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

TZ8 = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (compatible; crypto-news-movers/1.0)"
FAPI = "https://fapi.binance.com"
SPOT = "https://data-api.binance.vision"

STABLE_BASES = {
    "USDT", "USDC", "BUSD", "FDUSD", "TUSD", "DAI", "USDP", "USDD", "EUR", "AEUR",
    "USD1", "XUSD", "USTC", "PAXG", "EURT", "SUSD", "GUSD", "FRAX", "LUSD", "CUSD",
}
EVENT_FILES = {
    "unlock": ("unlock.json", "代币解锁", lambda f: f.get("ticker")),
    "delist": ("delist.json", "下架", lambda f: f.get("ticker")),
    "perp_listing": ("perp_listing.json", "永续合约上线", lambda f: f.get("token")),
    "burn": ("burn.json", "销毁/回购", lambda f: f.get("token")),
}
EVENT_WINDOW = 7 * 86400
NEWS_WINDOW = 48 * 3600
MIN_QUOTE_VOL_DEFAULT = 5_000_000
VOL_SPIKE_RATIO = 1.5


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def utc8(ts, fmt="%Y-%m-%d %H:%M:%S"):
    return datetime.fromtimestamp(ts, TZ8).strftime(fmt) if ts else None


def http_get(url, timeout=25, as_json=True):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            text = raw.decode("utf-8", "replace")
            return json.loads(text) if as_json else text
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {url}") from e


def base_asset(symbol):
    if symbol.endswith("USDT"):
        return symbol[:-4]
    return symbol


def is_leveraged_token(base):
    if len(base) >= 6 and base.endswith("UP") and not base.endswith("SUP"):
        return True
    if len(base) >= 8 and base.endswith("DOWN"):
        return True
    if base.endswith("BULL") or base.endswith("BEAR"):
        return True
    return False


def fetch_tickers():
    try:
        rows = http_get(f"{FAPI}/fapi/v1/ticker/24hr")
        src = "fapi_usdt_perp"
        market = "usdt_perpetual"
    except RuntimeError as e:
        if "451" not in str(e) and "403" not in str(e):
            log(f"⚠ fapi 不可用 ({e})，改用 spot")
        rows = http_get(f"{SPOT}/api/v3/ticker/24hr")
        src = "spot_usdt"
        market = "spot_usdt"
    out = []
    for row in rows:
        sym = row.get("symbol") or ""
        if not sym.endswith("USDT"):
            continue
        base = base_asset(sym)
        if base in STABLE_BASES:
            continue
        if is_leveraged_token(base):
            continue
        try:
            qv = float(row.get("quoteVolume") or 0)
            pct = float(row.get("priceChangePercent") or 0)
            price = float(row.get("lastPrice") or row.get("weightedAvgPrice") or 0)
        except (TypeError, ValueError):
            continue
        if not re.fullmatch(r"[A-Z0-9]{2,20}", base):
            continue
        out.append({"symbol": sym, "base": base, "price": price, "change_pct": pct, "quote_volume": qv})
    return out, src, market


def filter_and_rank(tickers, min_qv, top_n=10):
    eligible = [t for t in tickers if t["quote_volume"] >= min_qv]
    gainers = sorted(eligible, key=lambda x: x["change_pct"], reverse=True)[:top_n]
    losers = sorted(eligible, key=lambda x: x["change_pct"])[:top_n]
    return gainers, losers


def load_json_path(path):
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_events(events_dir):
    events_by_ticker = {}
    if not events_dir or not os.path.isdir(events_dir):
        return events_by_ticker
    for etype, (fname, label, get_tk) in EVENT_FILES.items():
        doc = load_json_path(os.path.join(events_dir, fname))
        if not doc:
            continue
        for ev in doc.get("events") or []:
            fields = ev.get("fields") or {}
            tk = get_tk(fields)
            if not tk:
                continue
            tk = str(tk).upper()
            events_by_ticker.setdefault(tk, []).append(
                {
                    "type": etype,
                    "label": label,
                    "event_ts": ev.get("event_ts"),
                    "event_time_utc8": ev.get("event_time_utc8"),
                    "is_future": ev.get("is_future"),
                    "fields": fields,
                }
            )
    return events_by_ticker


def story_matches_base(story, base):
    base_u = base.upper()
    coins = [c.upper() for c in (story.get("coins") or [])]
    if base_u in coins:
        return True
    for t in story.get("tokens") or []:
        if str(t).upper() == base_u:
            return True
    title = (story.get("title") or "") + " " + (story.get("alt_title") or "")
    if re.search(r"(?<![A-Za-z0-9$])" + re.escape(base_u) + r"(?![A-Za-z0-9])", title, re.I):
        return True
    if re.search(r"\$" + re.escape(base_u) + r"(?![A-Za-z0-9])", title, re.I):
        return True
    return False


def match_news(stories, base, now_ts):
    cut = now_ts - NEWS_WINDOW
    hits = []
    for s in stories:
        ts = s.get("ts") or s.get("first_seen") or 0
        if ts < cut:
            continue
        if not story_matches_base(s, base):
            continue
        hits.append(s)
    hits.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    return hits[:3]


def match_events(events_by_ticker, base, now_ts):
    lst = events_by_ticker.get(base.upper()) or []
    out = []
    for ev in lst:
        ets = ev.get("event_ts")
        if not ets:
            continue
        if abs(ets - now_ts) > EVENT_WINDOW:
            continue
        out.append(ev)
    out.sort(key=lambda x: abs((x.get("event_ts") or 0) - now_ts))
    return out[:3]


def kline_base_url(market):
    if market == "usdt_perpetual":
        return f"{FAPI}/fapi/v1/klines"
    return f"{SPOT}/api/v3/klines"


def volume_factor(symbol, market, quote_vol_24h):
    try:
        url = kline_base_url(market) + "?" + urllib.parse.urlencode(
            {"symbol": symbol, "interval": "1d", "limit": 8}
        )
        kl = http_get(url)
        if not kl or len(kl) < 2:
            return None
        daily_qv = [float(k[7]) for k in kl[:-1]][-7:]
        if not daily_qv:
            return None
        avg = sum(daily_qv) / len(daily_qv)
        if avg <= 0:
            return None
        ratio = quote_vol_24h / avg
        if ratio < VOL_SPIKE_RATIO:
            return None
        return {
            "kind": "volume",
            "text": f"24h 成交额约为近7日日均的 {ratio:.1f} 倍（约 ${quote_vol_24h/1e6:.1f}M vs 均 ${avg/1e6:.1f}M）",
        }
    except Exception as e:
        log(f"  klines {symbol}: {e}")
        return None


def futures_factors(symbol, fapi_ok):
    if not fapi_ok:
        return []
    out = []
    try:
        prem = http_get(f"{FAPI}/fapi/v1/premiumIndex?" + urllib.parse.urlencode({"symbol": symbol}))
        fr = float(prem.get("lastFundingRate") or 0)
        if abs(fr) >= 0.0001:
            bias = "多头支付费率" if fr > 0 else "空头支付费率"
            out.append({"kind": "futures", "text": f"资金费率 {fr*100:.3f}%（{bias}，或反映合约侧情绪）"})
    except Exception as e:
        log(f"  funding {symbol}: {e}")
    try:
        hist = http_get(
            f"{FAPI}/futures/data/openInterestHist?"
            + urllib.parse.urlencode({"symbol": symbol, "period": "1d", "limit": 2})
        )
        if isinstance(hist, list) and len(hist) >= 2:
            o0 = float(hist[-2].get("sumOpenInterestValue") or hist[-2].get("sumOpenInterest") or 0)
            o1 = float(hist[-1].get("sumOpenInterestValue") or hist[-1].get("sumOpenInterest") or 0)
            if o0 > 0:
                chg = (o1 - o0) / o0 * 100
                if abs(chg) >= 3:
                    direction = "增仓" if chg > 0 else "减仓"
                    out.append({"kind": "futures", "text": f"合约持仓（OI）近1日{direction}约 {abs(chg):.1f}%"})
    except Exception as e:
        log(f"  OI {symbol}: {e}")
    return out


def event_factor_text(ev):
    label = ev["label"]
    when = ev.get("event_time_utc8") or utc8(ev.get("event_ts"), "%m-%d %H:%M")
    fut = "（计划中）" if ev.get("is_future") else ""
    extra = ""
    f = ev.get("fields") or {}
    if ev["type"] == "unlock" and f.get("pct_circ"):
        extra = f"，约流通 {f.get('pct_circ')}%"
    if ev["type"] == "delist" and f.get("exchange"):
        extra = f"，{f.get('exchange')}"
    if ev["type"] == "burn" and f.get("amount"):
        extra = f"，数量 {f.get('amount')}"
    return f"事件库：{label}{fut} {when}{extra}"


def news_factor_text(st):
    dir_ = st.get("direction") or ""
    imp = st.get("impact_label") or st.get("event_label") or ""
    title = (st.get("title") or "")[:80]
    bits = [title]
    if imp:
        bits.append(f"({imp})")
    if dir_ and dir_ != "中性":
        bits.append(f"[{dir_}]")
    return " · ".join(bits)


def direction_note(side, factors, change_pct):
    kinds = {f.get("kind") for f in factors}
    has_bull_news = any(
        f.get("kind") == "news" and ("利好" in (f.get("text") or "") or "[利好]" in (f.get("text") or ""))
        for f in factors
    )
    has_bear_news = any(
        f.get("kind") == "news" and ("利空" in (f.get("text") or "") or "[利空]" in (f.get("text") or ""))
        for f in factors
    )
    if side == "gainer":
        if has_bull_news:
            return "涨幅与近期利好新闻方向一致，注意短线过热。"
        if "volume" in kinds and change_pct > 15:
            return "放量大涨，可能有消息或资金推动，追高风险较高。"
        if not factors or all(f.get("kind") == "fallback" for f in factors):
            return "偏多但缺乏明确催化剂，更像资金/情绪推动。"
        return "短线偏强，结合下方因素判断持续性。"
    if has_bear_news:
        return "跌幅与近期利空/风险事件一致，反弹需确认。"
    if "volume" in kinds and change_pct < -10:
        return "放量下跌，警惕恐慌抛售或杠杆清算。"
    if not factors or all(f.get("kind") == "fallback" for f in factors):
        return "偏空但未见明确新闻，疑似资金流出或板块拖累。"
    return "短线偏弱，关注是否超卖反弹。"


def prepare_context(data_json, events_dir, now_ts):
    stories = []
    if data_json:
        stories = data_json.get("stories") or []
    events_by_ticker = load_events(events_dir)
    news_map = {}
    for s in stories:
        ts = s.get("ts") or s.get("first_seen") or 0
        if ts < now_ts - NEWS_WINDOW:
            continue
        for base in set(list(s.get("coins") or []) + list(s.get("tokens") or [])):
            news_map.setdefault(str(base).upper(), []).append(s)
        m = re.findall(r"\$([A-Z][A-Z0-9]{1,9})\b", (s.get("title") or "") + " " + (s.get("alt_title") or ""))
        for b in m:
            news_map.setdefault(b.upper(), []).append(s)
    for k in list(news_map.keys()):
        seen = set()
        dedup = []
        for s in news_map[k]:
            sid = s.get("id")
            if sid in seen:
                continue
            seen.add(sid)
            dedup.append(s)
        dedup.sort(key=lambda x: x.get("ts") or 0, reverse=True)
        news_map[k] = dedup[:5]
    ev_map = {}
    for tk, evs in events_by_ticker.items():
        matched = match_events(events_by_ticker, tk, now_ts)
        if matched:
            ev_map[tk] = matched
    return {"news": news_map, "events": ev_map}


def enrich_mover(item, side, ctx):
    base = item["base"]
    news_hits = match_news(ctx.get("_stories") or [], base, ctx["now_ts"])
    if not news_hits:
        news_hits = ctx["news"].get(base.upper(), [])[:3]
    item_ctx = {
        **ctx,
        "news": {base.upper(): news_hits},
        "events": {base.upper(): ctx["events"].get(base.upper(), [])},
    }
    factors = []
    for st in news_hits:
        factors.append({"kind": "news", "text": news_factor_text(st), "url": st.get("url")})
    for ev in item_ctx["events"].get(base.upper(), []):
        factors.append({"kind": "event", "text": event_factor_text(ev)})
    vf = volume_factor(item["symbol"], ctx["market"], item["quote_volume"])
    if vf:
        factors.append(vf)
    time.sleep(0.12)
    for ff in futures_factors(item["symbol"], ctx["fapi_ok"]):
        factors.append(ff)
    if not factors:
        factors.append({"kind": "fallback", "text": "未找到明确消息，疑似资金/情绪驱动"})
    note = direction_note(side, factors, item["change_pct"])
    return {
        "symbol": item["symbol"],
        "base": base,
        "price": item["price"],
        "change_pct": round(item["change_pct"], 2),
        "quote_volume": round(item["quote_volume"], 2),
        "direction_note": note,
        "factors": factors,
    }


def run(args):
    now_ts = int(time.time())
    log("拉取 Binance 24h ticker…")
    tickers, price_source, market = fetch_tickers()
    fapi_ok = price_source == "fapi_usdt_perp"
    gainers_raw, losers_raw = filter_and_rank(tickers, args.min_volume, args.top)
    log(f"来源 {price_source}，候选 {len(tickers)}，涨幅榜 {len(gainers_raw)} / 跌幅榜 {len(losers_raw)}")

    data_json = load_json_path(args.data_json)
    events_dir = args.events_dir
    ctx = prepare_context(data_json, events_dir, now_ts)
    ctx.update({"now_ts": now_ts, "market": market, "fapi_ok": fapi_ok, "_stories": (data_json or {}).get("stories") or []})

    gainers, losers = [], []
    for i, it in enumerate(gainers_raw):
        log(f"  分析涨幅 #{i+1} {it['symbol']}…")
        gainers.append(enrich_mover(it, "gainer", ctx))
    for i, it in enumerate(losers_raw):
        log(f"  分析跌幅 #{i+1} {it['symbol']}…")
        losers.append(enrich_mover(it, "loser", ctx))

    doc = {
        "updated": now_ts,
        "updated_utc8": utc8(now_ts),
        "price_source": price_source,
        "market": market,
        "min_quote_volume_usd": args.min_volume,
        "gainers": gainers,
        "losers": losers,
    }
    out_path = args.out
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    log(f"✓ 写入 {out_path}（{len(gainers)}↑ / {len(losers)}↓）")
    return doc


def main():
    ap = argparse.ArgumentParser(description="Binance movers + factor analysis → movers.json")
    ap.add_argument("--data-json", default="data.json", help="news data.json（可选，用于匹配新闻）")
    ap.add_argument("--events-dir", default="events", help="events 目录（unlock/delist/…）")
    ap.add_argument("--out", default="movers.json")
    ap.add_argument("--min-volume", type=float, default=MIN_QUOTE_VOL_DEFAULT)
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()
    try:
        run(args)
    except Exception as e:
        log(f"✗ {e}")
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
