#!/usr/bin/env python3
"""futures_listing_tracker: 新上线 USDT 本位永续合约追踪 + 上合约前后价格表现 (stdlib only).

复用 ../unlock_tracker/unlock_tracker.py (HTTP缓存/取价/指标/格式化) 和 ../delist_tracker/delist_tracker.py
(html_text / parse_en_dt / binance_body_text / map_gecko)，只 import，不修改它们。

上线事件来源:
  binance : www.binance.com/fapi/v1/exchangeInfo (onboardDate, fapi.binance.com 从本机 451) + CMS 公告 catalogId=48
            ("Binance Futures Will Launch ... Perpetual Contract") + 1m K线核验首笔成交时间
  okx     : /api/v5/public/instruments?instType=SWAP (listTime, lever, instCategory=1 加密) + 1m K线核验;
            OKX 公告 API 按 IP 地区过滤(美国站无合约公告) -> 公告时间需 --extra-events
  bybit   : announcements.bybit.com new_crypto 列表 __NEXT_DATA__ + 文章 (api.bybit.com CloudFront 地区屏蔽)
  bitget  : /api/v2/public/annoucements?annType=coin_listings (annSubType=futures) + 合约列表 + 1m K线核验首笔成交
默认剔除 TradFi/股票/Pre-IPO/外汇/交割合约 (--include-tradfi 可保留到 other_events)。
"""
import argparse, csv, datetime as dt, hashlib, json, os, re, sys, time, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "unlock_tracker"))
sys.path.insert(0, os.path.join(HERE, "..", "delist_tracker"))
import unlock_tracker as ut  # noqa: E402
import delist_tracker as dlt  # noqa: E402
from unlock_tracker import TZ8, NA, H, DAY, fmt_ts, fp, fmt_usd, csvv, pc, at  # noqa: E402
from delist_tracker import html_text, parse_en_dt, binance_body_text, map_gecko  # noqa: E402

UTC = dt.timezone.utc
ALL_EX = ["binance", "okx", "bybit", "bitget"]
LOOKBACK_ANN = 14 * DAY
TRADFI_RE = re.compile(r"TradFi|Stock|Pre-IPO|Pre IPO|\bFX\b|Equity|ETF|Delivery|Quarterly|bStocks|Commodit|Gold|Silver", re.I)
# 交易所合约代码 -> 通用代币符号 (OKX 用 QUANT 表示 Quant(QNT)，因为 QNT-USDT-SWAP 是另一只股票合约)
KNOWN_ALIASES = {("OKX", "QUANT"): "QNT"}
MULT_RE = re.compile(r"^(1000000|100000|10000|1000|100|1M|1K)(?=[A-Z])")


def log(*a):
    print("[futures_listing_tracker]", *a, file=sys.stderr, flush=True)


def mk(exchange, contract, base, launch_ts, ann_ts, **kw):
    e = dict(exchange=exchange, contract=contract, base=base.upper(), symbol=base.upper(), launch_ts=launch_ts, ann_ts=ann_ts,
             launch_src=None, ann_note=None, leverage=None, url=None, title=None, category="crypto", name=None, gecko_id=None,
             status="launched", flags=[])
    e.update(kw)
    return e


def norm_symbol(exchange, base, aliases):
    b = base.upper()
    if (exchange, b) in aliases:
        return aliases[(exchange, b)], f"alias {b}->{aliases[(exchange, b)]}"
    if b in aliases.get(("*", "*"), {}):
        return aliases[("*", "*")][b], f"alias {b}"
    m = MULT_RE.match(b)
    if m:
        return b[m.end():], f"multiplier {m.group(1)}"
    return b, None


# ---------------------------------------------------------------- Binance
def src_binance(http, lo, hi, meta):
    out = []
    ex = http.get("https://www.binance.com/fapi/v1/exchangeInfo", "binance_fapi_exchangeInfo.json", ttl=30 * 60)
    info = {s["symbol"]: s for s in ex["symbols"]}
    arts = []
    for page in range(1, 5):
        d = http.get(f"https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1&catalogId=48&pageNo={page}&pageSize=50",
                     f"binance_newlist_p{page}.json", ttl=30 * 60)
        a = d["data"]["catalogs"][0]["articles"]
        arts += a
        if not a or a[-1]["releaseDate"] / 1000 < lo - LOOKBACK_ANN:
            break
    ann = {}
    for a in arts:
        rel, title = a["releaseDate"] // 1000, a["title"]
        if rel < lo - LOOKBACK_ANN or rel > hi or not re.search(r"Binance Futures Will (Launch|List)", title) or "Perpetual" not in title:
            continue
        url = f"https://www.binance.com/en/support/announcement/detail/{a['code']}"
        tradfi = bool(TRADFI_RE.search(title))
        d = http.get(f"https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query?articleCode={a['code']}", f"binance_article_{a['code']}.json", ttl=7 * DAY)
        text = binance_body_text(d["data"]["body"])
        names = {sym: nm.strip() for nm, sym in re.findall(r"Underlying Asset\s*([^\n()]{2,60}?)\s*\(([A-Z0-9]{1,20})\)", text)}
        for ds, tm, sym, lev in re.findall(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s*\(UTC\)\s*:\s*([A-Z0-9]+USDT)\s+Perpetual Contract(?:[^\n]*?up to\s*(\d+)x)?", text):
            ann.setdefault(sym, (parse_en_dt(ds, tm), rel, int(lev) if lev else None, url, title, tradfi, names.get(sym[:-4])))
    seen = set()
    for sym, s in info.items():
        if s.get("quoteAsset") != "USDT" or s.get("contractType") not in ("PERPETUAL", "TRADIFI_PERPETUAL"):
            continue
        ob = s["onboardDate"] // 1000
        a = ann.get(sym)
        lt = a[0] if a else ob
        if not (lo - DAY <= lt <= hi + 30 * DAY):
            continue
        cat = "crypto" if s.get("contractType") == "PERPETUAL" and s.get("underlyingType") in ("COIN", None) and not (a and a[5]) else "tradfi"
        e = mk("Binance", sym, s["baseAsset"], lt, a[1] if a else None, leverage=a[2] if a else None, url=a[3] if a else None,
               title=a[4] if a else None, category=cat, launch_src="announcement" if a else "exchangeInfo onboardDate", name=a[6] if a else None)
        if a and abs(a[0] - ob) > 120:
            e["flags"].append(f"onboardDate={fmt_ts(ob)}")
        if s.get("underlyingSubType"):
            e["flags"].append("subType=" + "/".join(s["underlyingSubType"]))
        out.append(e)
        seen.add(sym)
    for sym, a in ann.items():  # announced but not in exchangeInfo (delisted already / upcoming not yet loaded)
        if sym not in seen and lo - DAY <= a[0] <= hi + 30 * DAY:
            out.append(mk("Binance", sym, sym[:-4], a[0], a[1], leverage=a[2], url=a[3], title=a[4], category="tradfi" if a[5] else "crypto",
                          launch_src="announcement", flags=["not_in_exchangeInfo"]))
    for e in out:  # verify first trade with 1m klines
        if e["launch_ts"] <= hi and e["category"] == "crypto":
            try:
                k = http.get(f"https://www.binance.com/fapi/v1/klines?symbol={e['contract']}&interval=1m&limit=5&startTime={(e['launch_ts'] - 2 * H) * 1000}",
                             f"binance_fk_{e['contract']}_{e['launch_ts']}.json", ttl=7 * DAY)
                if k:
                    e["first_trade_ts"] = k[0][0] // 1000
            except Exception as ex2:
                meta["errors"].append(f"Binance klines {e['contract']}: {ex2}")
    return out


# ---------------------------------------------------------------- OKX
def okx_first_trade(http, inst, list_ts):
    d = http.get(f"https://www.okx.com/api/v5/market/history-candles?instId={inst}&bar=1m&limit=100&after={(list_ts + 90 * 60) * 1000}",
                 f"okx_fk_{inst}_{list_ts}.json", ttl=7 * DAY)["data"]
    return min(int(x[0]) // 1000 for x in d) if d else None


def src_okx(http, lo, hi, meta):
    out = []
    d = http.get("https://www.okx.com/api/v5/public/instruments?instType=SWAP", "okx_swap_instruments.json", ttl=30 * 60)["data"]
    for s in d:
        if s.get("settleCcy") != "USDT" or s.get("ctType") != "linear" or not s.get("listTime"):
            continue
        lt = int(s["listTime"]) // 1000
        if not (lo - DAY <= lt <= hi + 30 * DAY):
            continue
        base = s["instFamily"].split("-")[0]
        cat = "crypto" if s.get("instCategory") == "1" else "tradfi"
        e = mk("OKX", s["instId"], base, lt, None, leverage=int(float(s["lever"])) if s.get("lever") else None, category=cat,
               launch_src="instruments listTime", ann_note="OKX公告API按IP地区过滤，公告时间需 --extra-events",
               url=f"https://www.okx.com/trade-swap/{s['instId'].lower()}", status="launched" if s.get("state") == "live" else s.get("state"))
        if s.get("ruleType") == "pre_market":
            e["flags"].append("pre_market")
        if cat == "crypto" and lt <= hi:
            try:
                ft = okx_first_trade(http, s["instId"], lt)
                if ft:
                    e["first_trade_ts"] = ft
            except Exception as ex2:
                meta["errors"].append(f"OKX candles {s['instId']}: {ex2}")
        out.append(e)
    return out


# ---------------------------------------------------------------- Bybit
def src_bybit(http, lo, hi, meta):
    out, items = [], []
    for page in range(1, 5):
        h = http.get(f"https://announcements.bybit.com/en/?category=new_crypto&page={page}", f"bybit_newcrypto_p{page}.html", ttl=30 * 60, as_json=False)
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', h, re.S)
        lst = json.loads(m.group(1))["props"]["pageProps"]["articleInitEntity"]["list"] if m else []
        items += lst
        if not lst or min(x["publish_time"] for x in lst) < lo - LOOKBACK_ANN:
            break
    for a in items:
        title = a["title"]
        rel = a.get("date_timestamp") or a["publish_time"]
        if rel < lo - LOOKBACK_ANN or rel > hi or "Perpetual" not in title or "USDT" not in title:
            continue
        tradfi = bool(TRADFI_RE.search(title))
        url = "https://announcements.bybit.com/en" + a["url"]
        h = http.get(url, "bybit_art_" + hashlib.sha1(url.encode()).hexdigest()[:12] + ".html", ttl=7 * DAY, as_json=False)
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', h, re.S)
        det = json.loads(m.group(1))["props"]["pageProps"].get("articleDetail", {}) if m else {}
        text = re.sub(r"\s+", " ", html_text(det.get("content_html") or h))
        if det.get("date"):
            rel = int(dt.datetime.fromisoformat(det["date"].replace("Z", "+00:00")).timestamp())
        lev = re.search(r"up to (\d+)x", title + " " + text)
        nm = re.search(r"Underlying asset\s+([^()]{2,60}?)\s*\(([A-Z0-9]{1,20})\)", text)
        syms = re.findall(r"\b([A-Z0-9\u4e00-\u9fff]{1,20})USDT\b", title)
        launch, src = None, None
        if re.search(r"Trading is now open|has listed", text):
            launch, src = rel, "announcement(已开放交易)"
        else:
            mm = re.search(r"(?:on|at)\s+([A-Z][a-z]+ \d{1,2}, \d{4}),?\s*(\d{1,2}(?::\d{2})?\s*[AP]M)\s*UTC", title + " " + text)
            if mm:
                launch, src = parse_en_dt(mm.group(1), mm.group(2)), "announcement"
        if not launch or not syms:
            log(f"bybit: cannot parse {title}")
            continue
        for s in dict.fromkeys(syms):
            if not re.fullmatch(r"[A-Z0-9]+", s):  # 非 ASCII 代码 (如 龙虾USDT) 无法映射价格
                meta["errors"].append(f"Bybit {s}USDT: 非ASCII合约代码，跳过价格映射")
            out.append(mk("Bybit", s + "USDT", s, launch, rel, leverage=int(lev.group(1)) if lev else None, url=url, title=title,
                          category="tradfi" if tradfi else "crypto", launch_src=src,
                          ann_note="公告即上线" if src.startswith("announcement(") else None,
                          name=nm.group(1).strip() if nm and nm.group(2) == s else None))
    return out


# ---------------------------------------------------------------- Bitget
def bitget_first_trade(http, sym, ann_ts):
    d = http.get(f"https://api.bitget.com/api/v2/mix/market/history-candles?symbol={sym}&productType=USDT-FUTURES&granularity=1m&limit=200"
                 f"&startTime={(ann_ts - H) * 1000}&endTime={(ann_ts + 3 * H) * 1000}", f"bitget_fk_{sym}_{ann_ts}.json", ttl=7 * DAY)
    rows = d.get("data") or []
    return min(int(x[0]) // 1000 for x in rows) if rows else None


def src_bitget(http, lo, hi, meta):
    out, items, cursor = [], [], None
    for page in range(8):
        d = http.get("https://api.bitget.com/api/v2/public/annoucements?language=en_US&annType=coin_listings" + (f"&cursor={cursor}" if cursor else ""),
                     f"bitget_listing_{cursor or 'first'}.json", ttl=30 * 60)
        lst = d.get("data") or []
        items += lst
        if len(lst) < 10 or int(lst[-1]["cTime"]) // 1000 < lo - LOOKBACK_ANN:
            break
        cursor = lst[-1]["annId"]
    contracts = {s["symbol"]: s for s in http.get("https://api.bitget.com/api/v2/mix/market/contracts?productType=USDT-FUTURES", "bitget_usdt_contracts.json", ttl=30 * 60)["data"]}
    for a in items:
        rel, title = int(a["cTime"]) // 1000, a["annTitle"]
        if a.get("annSubType") != "futures" or rel < lo - LOOKBACK_ANN or rel > hi:
            continue
        tradfi = bool(TRADFI_RE.search(title)) or "perps" in title.lower() and "stock" in title.lower()
        syms = re.findall(r"\b([A-Z0-9]{1,20})USDT\b", title)
        if not syms:
            continue
        url = a["annUrl"].replace("/en/support", "/support")
        text = ""
        if not tradfi:
            text = re.sub(r"\s+", " ", html_text(http.get(a["annUrl"], f"bitget_art_{a['annId']}.html", ttl=7 * DAY, as_json=False)))
        lev = re.search(r"maximum leverage of (\d+)", text)
        mm = re.search(r"will launch .*? on ([A-Z][a-z]+ \d{1,2}, \d{4}),?\s*(\d{1,2}:\d{2})\s*\(UTC\+8\)", text)
        for s in dict.fromkeys(syms):
            c = contracts.get(s + "USDT")
            if not tradfi and c is not None and c.get("isRwa") == "YES":
                tradfi = True
            launch, src = rel, "announcement"
            if mm:
                launch = int(dt.datetime.strptime(mm.group(1) + " " + mm.group(2), "%B %d, %Y %H:%M").replace(tzinfo=TZ8).timestamp())
            e = mk("Bitget", s + "USDT", s, launch, rel, leverage=int(lev.group(1)) if lev else (int(c["maxLever"]) if c else None), url=url, title=title,
                   category="tradfi" if tradfi else "crypto", launch_src=src)
            if not tradfi and launch <= hi:
                try:
                    ft = bitget_first_trade(http, s + "USDT", rel)
                    if ft:
                        e["first_trade_ts"] = ft
                except Exception as ex2:
                    meta["errors"].append(f"Bitget candles {s}: {ex2}")
            out.append(e)
    return out


# ---------------------------------------------------------------- spot status
def spot_status(http, meta):
    st = {}
    try:
        for s in http.get("https://data-api.binance.vision/api/v3/exchangeInfo?permissions=SPOT", "binance_exchangeInfo.json", ttl=DAY)["symbols"]:
            if s["status"] == "TRADING":
                st.setdefault(("Binance", s["baseAsset"].upper()), []).append((None, s["symbol"]))
    except Exception as ex:
        meta["errors"].append(f"Binance spot exchangeInfo: {ex}")
    try:
        for s in http.get("https://www.okx.com/api/v5/public/instruments?instType=SPOT", "okx_spot_instruments.json", ttl=30 * 60)["data"]:
            st.setdefault(("OKX", s["baseCcy"].upper()), []).append((int(s["listTime"]) // 1000 if s.get("listTime") else None, s["instId"]))
    except Exception as ex:
        meta["errors"].append(f"OKX spot instruments: {ex}")
    try:
        for s in http.get("https://api.bitget.com/api/v2/spot/public/symbols", "bitget_spot_symbols.json", ttl=30 * 60)["data"]:
            if s.get("status") == "online":
                st.setdefault(("Bitget", s["baseCoin"].upper()), []).append((int(s["openTime"]) // 1000 if s.get("openTime") else None, s["symbol"]))
    except Exception as ex:
        meta["errors"].append(f"Bitget spot symbols: {ex}")
    return st


def spot_text(st, e):
    if e["exchange"] == "Bybit":
        return "N/A(Bybit API被屏蔽，无法核实)"
    lst = st.get((e["exchange"], e["base"])) or st.get((e["exchange"], e["symbol"]))
    if not lst:
        return f"否({e['exchange']}无现货)"
    ts = [t for t, _ in lst if t]
    first = min(ts) if ts else None
    if first is None:
        return "是"
    if first > e["launch_ts"]:
        return f"否(现货晚于合约，{fmt_ts(first)[5:]})"
    lab = "listTime" if e["exchange"] == "OKX" else "openTime"
    if e["launch_ts"] - first < 2 * DAY:
        return f"是(现货{lab} {fmt_ts(first)[5:]}，新开)"
    return f"是(现货自 {fmt_ts(first)[:10]})"


def src_extra(path):
    """补充/覆盖: exchange,symbol(合约代码或代币),launch_time_utc8,announce_time_utc8,announce_note,max_leverage,coingecko_id,source_url"""
    out = []
    p = lambda s: int(dt.datetime.strptime(s.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=TZ8).timestamp()) if s and s.strip() not in ("", NA) else None
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("exchange") and r.get("symbol"):
                out.append(dict(exchange=r["exchange"].strip(), symbol=r["symbol"].strip().upper(), launch_ts=p(r.get("launch_time_utc8")),
                                ann_ts=p(r.get("announce_time_utc8")), ann_note=r.get("announce_note") or None,
                                leverage=int(r["max_leverage"]) if (r.get("max_leverage") or "").strip().isdigit() else None,
                                gecko_id=r.get("coingecko_id") or None, url=r.get("source_url") or None))
    return out


def apply_extra(events, extra, lo, hi):
    for x in extra:
        hit = [e for e in events if e["exchange"].lower() == x["exchange"].lower() and x["symbol"] in (e["contract"], e["base"], e["symbol"], e["base"] + "USDT")]
        if not hit and x["launch_ts"]:
            base = x["symbol"][:-4] if x["symbol"].endswith("USDT") else x["symbol"]
            hit = [mk(x["exchange"], base + "USDT", base, x["launch_ts"], None, launch_src="extra")]
            events += hit
        for e in hit:
            for k in ("ann_ts", "ann_note", "leverage", "gecko_id", "url"):
                if x.get(k):
                    if k == "url" and e.get("url"):
                        e["ann_url"] = x["url"]
                    else:
                        e[k] = x[k]
            if x.get("launch_ts"):
                e["launch_ts"], e["launch_src"] = x["launch_ts"], "extra"
            e["flags"].append("extra")


# ---------------------------------------------------------------- CoinGecko mapping
def map_token(http, e, id_map):
    """given -> --id-map -> 名称+符号 -> 仅符号(有排名: 唯一或领先3倍) -> 仅符号(无排名: 24h成交额唯一或领先5倍)"""
    gid, how = map_gecko(http, e, id_map)
    if gid:
        return gid, how
    try:
        d = http.get("https://api.coingecko.com/api/v3/search?query=" + urllib.parse.quote(e["symbol"]), f"cg_search_{e['symbol']}.json", ttl=7 * DAY)
    except Exception:
        return None, how
    c = [x["id"] for x in d.get("coins", []) if (x.get("symbol") or "").upper() == e["symbol"]][:10]
    if not c:
        return None, how
    try:
        v = http.get("https://api.coingecko.com/api/v3/simple/price?vs_currencies=usd&include_24hr_vol=true&ids=" + ",".join(c),
                     f"cg_vol_{e['symbol']}.json", ttl=6 * H)
    except Exception:
        return None, how
    vs = sorted(((v.get(i, {}).get("usd_24h_vol") or 0, i) for i in c), reverse=True)
    if vs and vs[0][0] > 0 and (len(vs) == 1 or vs[0][0] >= 5 * vs[1][0]):
        return vs[0][1], f"search(symbol-only, by 24h volume ${vs[0][0]:,.0f})"
    return None, how or ("ambiguous symbol: " + ",".join(c[:4]))


# ---------------------------------------------------------------- prices
def okx_spot_series(http, inst, start_ts, now_ts):
    rows, after = [], ""
    for _ in range(8):
        d = http.get(f"https://www.okx.com/api/v5/market/history-candles?instId={inst}&bar=1H&limit=100" + (f"&after={after}" if after else ""),
                     f"okx_{inst}_1H_{after or 'latest'}.json", ttl=20 * 60)["data"]
        rows += d
        if len(d) < 100 or int(d[-1][0]) // 1000 < start_ts:
            break
        after = d[-1][0]
    pts = {}
    for k in rows:
        o = int(k[0]) // 1000
        if o <= now_ts:
            pts[o] = float(k[1])
        if o + H <= now_ts and k[8] == "1":
            pts[o + H] = float(k[4])
        elif o <= now_ts and abs(now_ts - time.time()) < 120:
            pts[now_ts] = float(k[4])
    return sorted(pts.items())


def choose_series(http, sym, e, ts, bmap, cgpts, gid, st, start_px, now_ts, meta):
    pair = bmap.get(sym)
    if pair:
        return ut.binance_series(http, pair, start_px, now_ts), f"Binance {pair} 现货1h"
    if cgpts and at(cgpts, ts) is not None:
        return cgpts, f"CoinGecko {gid} 小时"
    okx_inst = [i for t, i in st.get(("OKX", e["base"]), []) + st.get(("OKX", sym), []) if i.endswith("-USDT")]
    if okx_inst:
        try:
            pts = okx_spot_series(http, okx_inst[0], start_px, now_ts)
            if pts:
                return pts, f"OKX {okx_inst[0]} 现货1h(CoinGecko上合约时无数据)"
        except Exception as ex:
            meta["errors"].append(f"OKX spot {okx_inst[0]}: {ex}")
    return (cgpts, f"CoinGecko {gid} 小时") if cgpts else ([], NA)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="新上线USDT永续合约追踪: 找出窗口内已上线的加密货币USDT永续合约，计算上合约前7天逐日/上合约后表现 (含BTC基准)")
    ap.add_argument("--start", help="窗口开始 YYYY-MM-DD (UTC+8)，默认 now-7天")
    ap.add_argument("--end", help="窗口结束 YYYY-MM-DD (UTC+8，含当日)，不超过 now")
    ap.add_argument("--now", help="固定'现在' 'YYYY-MM-DD HH:MM' (UTC+8) 用于复现")
    ap.add_argument("--exchanges", default=",".join(ALL_EX), help="逗号分隔: " + ",".join(ALL_EX))
    ap.add_argument("--extra-events", help="补充/覆盖CSV (公告时间等，见 extra_events_example.csv)")
    ap.add_argument("--id-map", action="append", default=[], help="手工指定 CoinGecko id: SYM=gecko-id，可多次")
    ap.add_argument("--alias", action="append", default=[], help="合约代码别名 EXCHANGE:CODE=SYM 或 CODE=SYM，可多次 (内置 OKX:QUANT=QNT)")
    ap.add_argument("--include-tradfi", action="store_true", help="在 other_events 中列出 TradFi/股票/Pre-IPO/外汇 合约")
    ap.add_argument("--min-mcap", type=float, default=0, help="上合约时 CoinGecko 市值低于此值不展示(市值N/A的保留)")
    ap.add_argument("--out", help="输出目录，默认 ./output/<start>_<end>/")
    ap.add_argument("--cache", default=os.path.join(HERE, "cache"))
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    return ap.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    real_now = int(time.time())
    now_ts = int(dt.datetime.strptime(a.now, "%Y-%m-%d %H:%M").replace(tzinfo=TZ8).timestamp()) if a.now else real_now
    lo = int(dt.datetime.strptime(a.start, "%Y-%m-%d").replace(tzinfo=TZ8).timestamp()) if a.start else now_ts - 7 * DAY
    hi = min(int(dt.datetime.strptime(a.end, "%Y-%m-%d").replace(tzinfo=TZ8).timestamp()) + DAY - 1 if a.end else now_ts, now_ts)
    out_dir = a.out or os.path.join(os.getcwd(), "output", f"{fmt_ts(lo)[:10]}_{fmt_ts(hi)[:10]}")
    os.makedirs(out_dir, exist_ok=True)
    http = ut.Http(a.cache, a.refresh)
    meta = {"window_utc8": [fmt_ts(lo), fmt_ts(hi)], "now_utc8": fmt_ts(now_ts), "generated_utc8": fmt_ts(real_now), "args": vars(a),
            "sources": {}, "errors": [], "unmapped": [], "price_notes": []}
    aliases = dict(KNOWN_ALIASES)
    for x in a.alias:
        k, _, v = x.partition("=")
        exn, _, code = k.rpartition(":")
        aliases[((exn or "*").strip(), code.strip().upper())] = v.strip().upper()
    star = {c: v for (exn, c), v in aliases.items() if exn == "*"}
    aliases[("*", "*")] = star
    aliases = {(({"okx": "OKX", "binance": "Binance", "bybit": "Bybit", "bitget": "Bitget"}.get(k[0].lower(), k[0])), k[1]) if k != ("*", "*") else k: v
               for k, v in aliases.items()}
    fns = {"binance": src_binance, "okx": src_okx, "bybit": src_bybit, "bitget": src_bitget}
    events = []
    for ex in [x.strip().lower() for x in a.exchanges.split(",") if x.strip()]:
        if ex not in fns:
            meta["errors"].append(f"unknown/unsupported exchange {ex}")
            continue
        try:
            evs = fns[ex](http, lo, now_ts, meta)
            meta["sources"][ex] = {"events_parsed": len(evs)}
            events += evs
        except Exception as ex2:
            meta["errors"].append(f"{ex}: {type(ex2).__name__}: {ex2}")
            log(f"{ex} failed: {ex2}")
    if a.extra_events:
        extra = src_extra(a.extra_events)
        apply_extra(events, extra, lo, hi)
        meta["sources"]["extra"] = {"file": a.extra_events, "rows": len(extra)}
    for e in events:
        e["symbol"], note = norm_symbol(e["exchange"], e["base"], aliases)
        if note:
            e["flags"].append(note)
        ft = e.get("first_trade_ts")
        if ft and e.get("launch_src") != "extra":
            if abs(ft - e["launch_ts"]) > 120:
                e["flags"].append(f"first_trade={fmt_ts(ft)}")
                if e["launch_src"] in ("announcement", "instruments listTime", "exchangeInfo onboardDate") and ft > e["launch_ts"]:
                    e["launch_ts"], e["launch_src"] = ft, e["launch_src"] + "+首笔K线"
        if e["launch_ts"] > now_ts:
            e["status"] = "upcoming"
    st = spot_status(http, meta)
    crypto = [e for e in events if e["category"] == "crypto"]
    launched = [e for e in crypto if lo <= e["launch_ts"] <= hi and e["status"] == "launched"]
    upcoming = [e for e in crypto if e["launch_ts"] > now_ts or e["status"] not in ("launched",)]
    tradfi = [e for e in events if e["category"] != "crypto" and lo <= e["launch_ts"] <= hi + 30 * DAY]
    # group by token; anchor = earliest launch inside window
    groups = {}
    for e in sorted(launched, key=lambda e: e["launch_ts"]):
        groups.setdefault(e["symbol"], []).append(e)
    for sym, lst in groups.items():
        for i, e in enumerate(lst):
            e["role"] = "anchor" if i == 0 else "secondary"
            e["others"] = ", ".join(f"{x['exchange']} {fmt_ts(x['launch_ts'])[5:]}" for x in lst if x is not e)
    meta["counts"] = {"parsed": len(events), "crypto_launched_in_window": len(launched), "tokens": len(groups), "upcoming": len(upcoming),
                      "tradfi_excluded": len(tradfi)}
    log(f"parsed={len(events)} crypto-launched={len(launched)} tokens={len(groups)} upcoming={len(upcoming)} tradfi-excluded={len(tradfi)}")

    id_map = dict(x.split("=", 1) for x in a.id_map if "=" in x)
    for sym, lst in groups.items():
        nm = next((x["name"] for x in lst if x.get("name")), None)
        for x in lst:
            x["name"] = x.get("name") or nm
    bmap = ut.binance_symbols(http)
    for e in launched:
        if e.get("gecko_from"):
            continue
        if not re.fullmatch(r"[A-Z0-9]+", e["symbol"]):
            e["gecko_id"], e["gecko_from"] = None, "非ASCII代码"
            continue
        if not e.get("gecko_id"):
            same = [x for x in groups[e["symbol"]] if x.get("gecko_id")]
            if same:
                e["gecko_id"], e["gecko_from"] = same[0]["gecko_id"], "same token"
                continue
        e["gecko_id"], e["gecko_from"] = map_token(http, e, id_map)
        for x in groups[e["symbol"]]:
            if not x.get("gecko_id") and e["gecko_id"]:
                x["gecko_id"], x["gecko_from"] = e["gecko_id"], e["gecko_from"]
    start_px = min([e["launch_ts"] for e in launched] + [now_ts]) - 7 * DAY - 2 * H
    start_px = min(start_px, min([e["ann_ts"] for e in launched if e.get("ann_ts")] + [start_px]))
    btc = ut.binance_series(http, "BTCUSDT", start_px, now_ts)
    cg_cache, series_cache, rows, daily = {}, {}, [], []
    for e in sorted(launched, key=lambda e: (e["symbol"] not in groups or groups[e["symbol"]][0]["launch_ts"], e["symbol"], e["launch_ts"])):
        ts, gid, sym = e["launch_ts"], e.get("gecko_id"), e["symbol"]
        flags = list(e["flags"])
        if not gid:
            meta["unmapped"].append(f"{sym} ({e['exchange']} {fmt_ts(ts)}): {e.get('gecko_from') or '无CoinGecko匹配'}")
        mc = vol = cgpts = []
        if gid:
            if gid not in cg_cache:
                try:
                    days = min(90, int((real_now - start_px) // DAY) + 2)
                    cg_cache[gid] = http.get(f"https://api.coingecko.com/api/v3/coins/{gid}/market_chart?vs_currency=usd&days={days}", f"cg_chart_{gid}_{days}d.json", ttl=20 * 60)
                except Exception as ex:
                    cg_cache[gid] = {}
                    meta["errors"].append(f"CoinGecko {gid}: {ex}")
            d = cg_cache[gid]
            cgpts = [(int(p[0] // 1000), p[1]) for p in d.get("prices", []) if p[1] is not None and p[0] // 1000 <= now_ts]
            mc = [(int(p[0] // 1000), p[1]) for p in d.get("market_caps", []) if p[1] and p[0] // 1000 <= now_ts]
            vol = [(int(p[0] // 1000), p[1]) for p in d.get("total_volumes", []) if p[1] and p[0] // 1000 <= now_ts]
        if sym in series_cache:  # 同一代币所有交易所用同一条价格序列(由锚点决定)
            pts, psrc = series_cache[sym]
        else:
            pts, psrc = choose_series(http, sym, e, ts, bmap, cgpts, gid, st, start_px, now_ts, meta)
            series_cache[sym] = (pts, psrc)
        if not pts:
            flags.append("no_price_data")
        m = ut.window_metrics(pts, ts, now_ts) if pts else None
        b = ut.window_metrics(btc, ts, now_ts)
        if m and (m["t_now"] is None or m["t_now"] <= ts):  # 上线后尚无价格点
            m = dict(m, post=None, lo=None, hi=None, p_now=None)
            flags.append("no_post_price_point")
        g = (lambda k: m[k]) if m else (lambda k: None)
        first = pts[0][0] if pts else None
        if first and first > ts - 7 * DAY:
            flags.append(f"price_history_from={fmt_ts(first)}")
        ann = e.get("ann_ts")
        pa = at(pts, ann) if (pts and ann and ann < ts and first is not None and first <= ann - H) else None
        post_h = (now_ts - ts) / H
        if post_h < 24:
            flags.append(f"short_post_window({post_h:.1f}h)")
        if (e.get("gecko_from") or "").startswith("search(symbol-only"):
            flags.append("gecko_by_symbol")
        rows.append({
            "token": sym, "contract": e["contract"], "exchange": e["exchange"], "role": e.get("role"), "other_listings": e.get("others") or None,
            "announce_time_utc8": fmt_ts(ann) if ann else None, "announce_note": e.get("ann_note"),
            "launch_time_utc8": fmt_ts(ts), "launch_time_source": e.get("launch_src"),
            "gap_hours": round((ts - ann) / H, 2) if ann else None, "max_leverage": e.get("leverage"),
            "spot_on_exchange": spot_text(st, e), "coingecko_id": gid, "coingecko_match": e.get("gecko_from"), "price_source": psrc,
            "mcap_usd_at_launch_cg": at(mc, ts) if mc else None, "vol24h_usd_at_launch_cg": at(vol, ts) if vol else None,
            "price_7d_before": g("p_7"), "price_at_launch": g("p_u"), "price_now": g("p_now"),
            "price_now_time_utc8": fmt_ts(m["t_now"]) if m and m["t_now"] else None,
            "pre7d_change_pct": g("pre7"), "post_launch_change_pct": g("post"), "post_low_pct": g("lo"), "post_high_pct": g("hi"),
            "announce_to_launch_change_pct": pc(pa, g("p_u")),
            "btc_pre7d_change_pct": b["pre7"], "btc_post_change_pct": b["post"],
            "relative_vs_btc_post_pp": g("post") - b["post"] if g("post") is not None and b["post"] is not None else None,
            "post_window_hours": round(post_h, 1), "flags": "; ".join(flags) or None, "source_url": e.get("url"),
            "announce_source_url": e.get("ann_url"), "title": e.get("title"),
        })
        for lab, mm, src in ((sym, m, psrc), ("BTC", b, "Binance BTCUSDT 1h")):
            dr = {"token": sym, "exchange": e["exchange"], "role": e.get("role"), "series": lab, "launch_time_utc8": fmt_ts(ts), "D-7_start_utc8": fmt_ts(ts - 7 * DAY)}
            tot = 1.0
            for k in range(7, 0, -1):
                v = mm["daily"][f"D-{k}"] if mm else None
                dr[f"D-{k}_pct"] = v
                tot = tot * (1 + v / 100) if (v is not None and tot is not None) else None
            dr["total_7d_pct"] = (tot - 1) * 100 if tot is not None else None
            dr["price_source"] = src
            daily.append(dr)
    if a.min_mcap:
        drop = {r["token"] for r in rows if r["role"] == "anchor" and r["mcap_usd_at_launch_cg"] is not None and r["mcap_usd_at_launch_cg"] < a.min_mcap}
        rows = [r for r in rows if r["token"] not in drop]
        daily = [d for d in daily if d["token"] not in drop]

    def wcsv(name, data, fields=None):
        with open(os.path.join(out_dir, name), "w", newline="", encoding="utf-8") as f:
            if data or fields:
                w = csv.DictWriter(f, fieldnames=fields or list(data[0].keys()))
                w.writeheader()
                w.writerows([{k: csvv(v) for k, v in r.items()} for r in data])
    wcsv("listings.csv", rows)
    wcsv("pre7d_daily.csv", daily)
    side = []
    for e in sorted(upcoming, key=lambda e: e["launch_ts"]):
        side.append({"type": "upcoming", "exchange": e["exchange"], "contract": e["contract"], "token": e["symbol"], "category": e["category"],
                     "announce_time_utc8": fmt_ts(e["ann_ts"]) if e.get("ann_ts") else None, "launch_time_utc8": fmt_ts(e["launch_ts"]),
                     "max_leverage": e.get("leverage"), "flags": "; ".join(e["flags"]) or None, "source_url": e.get("url")})
    for e in sorted(tradfi, key=lambda e: e["launch_ts"]):
        side.append({"type": "excluded_tradfi", "exchange": e["exchange"], "contract": e["contract"], "token": e["symbol"], "category": e["category"],
                     "announce_time_utc8": fmt_ts(e["ann_ts"]) if e.get("ann_ts") else None, "launch_time_utc8": fmt_ts(e["launch_ts"]),
                     "max_leverage": e.get("leverage"), "flags": "; ".join(e["flags"]) or None, "source_url": e.get("url")})
    wcsv("other_events.csv", side, ["type", "exchange", "contract", "token", "category", "announce_time_utc8", "launch_time_utc8", "max_leverage", "flags", "source_url"])
    md = render_md(rows, daily, side, meta, a)
    with open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8") as f:
        f.write(md)
    with open(os.path.join(out_dir, "run_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1, default=str)
    if not a.quiet:
        print(md)
    log(f"outputs written to {out_dir}")
    return rows, daily, meta


def render_md(rows, daily, side, meta, a):
    anchors = [r for r in rows if r["role"] == "anchor"]
    L = [f"# 新上线 USDT 永续合约追踪 {meta['window_utc8'][0]} ~ {meta['window_utc8'][1]} (UTC+8)\n",
         f"生成 {meta['generated_utc8']}；价格截至 {meta['now_utc8']}。只统计窗口内已开放交易的加密货币 USDT 本位永续（剔除 TradFi/股票/Pre-IPO/外汇）。"
         f"共 {len(anchors)} 个代币、{len(rows)} 个上线事件；同一代币以最早上线为锚点。\n",
         "## 表1 上合约与价格表现（锚点）\n",
         "| 代币 | 交易所(锚点) | 其他交易所 | 公告时间 | 上合约时间 | 最高杠杆 | 该所已有现货 | 市值/24h额(CG) | 上合约前7天 | 上合约至今 | 上线后最低/最高 | 同期BTC 前7天/至今 | 公告→上线 | 标记 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in anchors:
        ann = r["announce_time_utc8"][5:] if r["announce_time_utc8"] else NA
        L.append(f"| {r['token']} | {r['exchange']} | {r['other_listings'] or '-'} | {ann} | {r['launch_time_utc8'][5:]} | {str(r['max_leverage']) + 'x' if r['max_leverage'] else NA} | "
                 f"{r['spot_on_exchange']} | {fmt_usd(r['mcap_usd_at_launch_cg'])} / {fmt_usd(r['vol24h_usd_at_launch_cg'])} | {fp(r['pre7d_change_pct'])} | {fp(r['post_launch_change_pct'])} | "
                 f"{fp(r['post_low_pct'])} / {fp(r['post_high_pct'])} | {fp(r['btc_pre7d_change_pct'])} / {fp(r['btc_post_change_pct'])} | {fp(r['announce_to_launch_change_pct'])} | {r['flags'] or ''} |")
    sec = [r for r in rows if r["role"] != "anchor"]
    if sec:
        L += ["\n### 同一代币在其他交易所的后续上线\n", "| 代币 | 交易所 | 公告时间 | 上合约时间 | 最高杠杆 | 该所已有现货 | 上合约至今 | 同期BTC | 上线后最低/最高 | 公告→上线 |", "|---|---|---|---|---|---|---|---|---|---|"]
        for r in sec:
            ann = r["announce_time_utc8"][5:] if r["announce_time_utc8"] else NA
            L.append(f"| {r['token']} | {r['exchange']} | {ann} | {r['launch_time_utc8'][5:]} | {str(r['max_leverage']) + 'x' if r['max_leverage'] else NA} | {r['spot_on_exchange']} | "
                     f"{fp(r['post_launch_change_pct'])} | {fp(r['btc_post_change_pct'])} | {fp(r['post_low_pct'])} / {fp(r['post_high_pct'])} | {fp(r['announce_to_launch_change_pct'])} |")
    L.append("\n口径：市值/24h额=CoinGecko market_chart 在上合约时刻（或之前3h内最近点），CoinGecko 市值为0/缺失记 N/A；价格优先 Binance 现货1h，否则 CoinGecko 聚合小时价，"
             "CoinGecko 在上合约时无数据则用 OKX 现货1h；上线前不足7天历史的记 N/A；公告→上线需公告前已有价格，否则 N/A。\n")
    L += ["## 表2 上合约前7天逐日涨跌（24h桶，锚定上合约时刻，锚点交易所）\n",
          "| 代币 | 交易所 | 上合约时间 | D-7 | D-6 | D-5 | D-4 | D-3 | D-2 | D-1 | 7天合计 | BTC合计 |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    bt = {(d["token"], d["exchange"]): d for d in daily if d["series"] == "BTC"}
    for d in daily:
        if d["series"] != "BTC" and d["role"] == "anchor":
            L.append(f"| {d['token']} | {d['exchange']} | {d['launch_time_utc8'][5:]} | " + " | ".join(fp(d[f'D-{k}_pct']) for k in range(7, 0, -1)) +
                     f" | {fp(d['total_7d_pct'], 2)} | {fp(bt[(d['token'], d['exchange'])]['total_7d_pct'], 2)} |")
    L += ["\n## BTC 同窗口逐日\n", "| 对应 | D-7 | D-6 | D-5 | D-4 | D-3 | D-2 | D-1 | 合计 |", "|---|---|---|---|---|---|---|---|---|"]
    for d in daily:
        if d["series"] == "BTC" and d["role"] == "anchor":
            L.append(f"| {d['token']}@{d['exchange']} {d['launch_time_utc8'][5:]} | " + " | ".join(fp(d[f'D-{k}_pct']) for k in range(7, 0, -1)) + f" | {fp(d['total_7d_pct'])} |")
    obs = []
    hot = [r for r in anchors if r["pre7d_change_pct"] is not None and r["pre7d_change_pct"] >= 20]
    if hot:
        obs.append("上合约前7天涨幅≥20%：" + "、".join(f"{r['token']}({r['pre7d_change_pct']:+.0f}%)" for r in hot))
    newt = [r for r in anchors if r["pre7d_change_pct"] is None]
    if newt:
        obs.append("无7天前价格历史(新币，现货与合约几乎同时上线)：" + "、".join(r["token"] for r in newt))
    mv = [r for r in rows if r["post_launch_change_pct"] is not None and r["post_window_hours"] >= 24]
    if mv:
        obs.append("上合约至今(≥24h)：" + "、".join(f"{r['token']}@{r['exchange']}({r['post_launch_change_pct']:+.1f}%, 相对BTC {r['relative_vs_btc_post_pp']:+.1f}pp)" for r in mv))
    if obs:
        L.append("\n## 自动观察（仅基于表内数字）\n")
        L += [f"- {o}" for o in obs]
    up = [s for s in side if s["type"] == "upcoming"]
    L.append("\n## 已公告未上线（加密货币）\n")
    if up:
        L += ["| 交易所 | 合约 | 公告 | 预定上线 | 杠杆 |", "|---|---|---|---|---|"]
        L += [f"| {s['exchange']} | {s['contract']} | {(s['announce_time_utc8'] or NA)[5:]} | {s['launch_time_utc8'][5:]} | {s['max_leverage'] or NA} |" for s in up]
    else:
        L.append("- 自动源中未发现（OKX 公告不可得，需另行确认）。")
    tf = [s for s in side if s["type"] == "excluded_tradfi"]
    if tf:
        by = {}
        for s in tf:
            by.setdefault(s["exchange"], []).append(s["contract"])
        L.append("\n## 已剔除的 TradFi/股票/Pre-IPO/外汇 合约\n")
        L += [f"- {k}（{len(v)}）：" + ", ".join(v) for k, v in by.items()]
    if meta["unmapped"] or meta["errors"] or meta["price_notes"]:
        L.append("\n## 未映射 / 异常\n")
        L += [f"- 未映射价格：{x}" for x in meta["unmapped"]] + [f"- {x}" for x in meta["price_notes"]] + [f"- 错误/限制：{x}" for x in meta["errors"]]
    L.append("\n## 数据来源\n")
    L.append("- 合约：Binance www.binance.com/fapi/v1/exchangeInfo + CMS公告(catalogId=48) + fapi 1m K线；OKX /api/v5/public/instruments(SWAP) + 1m K线；"
             "announcements.bybit.com new_crypto；Bitget 公告(coin_listings/futures) + mix 合约/1m K线；--extra-events。各行链接见 listings.csv。")
    L.append("- 现货状态：Binance data-api exchangeInfo；OKX SPOT instruments listTime；Bitget spot symbols openTime；Bybit 无法核实。")
    L.append("- 价格：Binance data-api.binance.vision 1h / CoinGecko market_chart / OKX 现货1h；BTC 基准 Binance BTCUSDT。")
    L.append("- 各源解析数：" + json.dumps(meta["sources"], ensure_ascii=False))
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
