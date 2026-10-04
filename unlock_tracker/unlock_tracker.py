#!/usr/bin/env python3
"""unlock_tracker: 代币解锁追踪 + 解锁前后价格表现分析 (stdlib only).

Unlock sources
  defillama : defillama.com/unlocks 页面内嵌的 __NEXT_DATA__ (api.llama.fi/emissions 需付费, 402)
  cmc       : CoinMarketCap token-unlock listing (只给每个币的"下一次"解锁 -> 每次运行存快照, 定期运行才能覆盖过去窗口)
  news      : --news-url 传入的 PANews 等"下周解锁"快讯 (Token Unlocks/Tokenomist 数据), 正则解析
  extra     : --extra-events CSV 手工补充
Prices: Binance data-api.binance.vision 1h klines (经 CoinGecko 现价校验), 否则 CoinGecko market_chart 小时数据.
"""
import argparse, csv, datetime as dt, gzip, hashlib, html as htmlmod, json, math, os, re, sys, time
import urllib.error, urllib.parse, urllib.request
from collections import Counter, defaultdict

TZ8 = dt.timezone(dt.timedelta(hours=8))
UA = "Mozilla/5.0 (X11; Linux x86_64) unlock_tracker/1.0"
BASE = os.path.dirname(os.path.abspath(__file__))
NA = "N/A"
H = 3600
DAY = 86400
MERGE_TOL = 48 * H          # cross-source same-event tolerance
CLUSTER_GAP = 12 * H        # within-source: events of one token within 12h -> one unlock
MAX_ANCHOR_STALE = 3 * H    # price point older than this vs anchor -> N/A
PRICE_VERIFY_TOL = 0.10     # Binance vs CoinGecko/DefiLlama current price tolerance
SRC_PREF = {"news": 0, "extra": 0, "CoinMarketCap": 1, "DefiLlama": 2}


def log(*a):
    print("[unlock_tracker]", *a, file=sys.stderr, flush=True)


def fmt_ts(ts):
    return dt.datetime.fromtimestamp(ts, TZ8).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- HTTP + cache
class Http:
    INTERVAL = {"api.coingecko.com": 6.5, "api.coinmarketcap.com": 1.0}

    def __init__(self, cache_dir, refresh=False):
        self.cache_dir = os.path.join(cache_dir, "raw")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.refresh = refresh
        self.last = {}

    def _path(self, name):
        return os.path.join(self.cache_dir, re.sub(r"[^A-Za-z0-9._-]", "_", name))

    def get(self, url, cache_name, ttl, as_json=True, retries=5):
        path = self._path(cache_name)
        if not self.refresh and ttl and os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl:
            with open(path, encoding="utf-8") as f:
                return json.load(f) if as_json else f.read()
        host = urllib.parse.urlparse(url).netloc
        err = None
        for attempt in range(retries):
            wait = self.INTERVAL.get(host, 0.4) - (time.time() - self.last.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            self.last[host] = time.time()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip", "Accept": "*/*"})
                with urllib.request.urlopen(req, timeout=90) as r:
                    data = r.read()
                    if r.headers.get("Content-Encoding") == "gzip":
                        data = gzip.decompress(data)
                text = data.decode("utf-8", "replace")
                obj = json.loads(text) if as_json else text
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                return obj
            except urllib.error.HTTPError as e:
                err = e
                if e.code in (429, 500, 502, 503, 504, 520, 522):
                    ra = e.headers.get("Retry-After")
                    back = float(ra) if ra and ra.isdigit() else (20 if host == "api.coingecko.com" else 3) * (attempt + 1)
                    log(f"HTTP {e.code} {host}, retry in {back:.0f}s")
                    time.sleep(back)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
                err = e
                log(f"{type(e).__name__} {host}: {e}; retry")
                time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"GET failed after {retries} tries: {url}: {err}")


# ---------------------------------------------------------------- sources
def mk_event(**kw):
    ev = dict(source=None, kind=None, symbol=None, name=None, gecko_id=None, ts=None, amount=None,
              value_usd_src=None, est_value_usd=None, pct_circ=None, recipients=None, unlock_type=None,
              url=None, ref_price=None)
    ev.update(kw)
    ev["symbol"] = (ev["symbol"] or "").strip().upper().lstrip("$")
    return ev


def src_defillama(http, lo, hi):
    html = http.get("https://defillama.com/unlocks", "defillama_unlocks.html", ttl=3 * H, as_json=False)
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("DefiLlama: __NEXT_DATA__ not found (page layout changed?)")
    pp = json.loads(m.group(1))["props"]["pageProps"]
    meta = {"generatedAt": fmt_ts(pp["generatedAtSec"]) if pp.get("generatedAtSec") else NA, "protocols": len(pp.get("data") or [])}
    out = []
    for x in pp.get("data") or []:
        evs = sorted((e for e in (x.get("events") or []) if e.get("timestamp") and lo <= e["timestamp"] <= hi), key=lambda e: e["timestamp"])
        clusters = []
        for e in evs:
            if clusters and e["timestamp"] - clusters[-1][0]["timestamp"] <= CLUSTER_GAP:
                clusters[-1].append(e)
            else:
                clusters.append([e])
        price, circ = x.get("tPrice"), x.get("circSupply")
        for cl in clusters:
            cats, types, amt = Counter(), set(), 0.0
            for e in cl:
                n = sum(v for v in (e.get("noOfTokens") or []) if isinstance(v, (int, float)))
                amt += n
                cats[e.get("category") or "?"] += n
                types.add(e.get("unlockType") or "?")
            if amt <= 0:
                continue
            out.append(mk_event(
                source="DefiLlama", kind="DefiLlama", symbol=x.get("tSymbol"), name=x.get("name"), gecko_id=x.get("gecko_id"),
                ts=cl[0]["timestamp"], amount=amt, est_value_usd=amt * price if price else None,
                pct_circ=amt / circ * 100 if circ else None,
                recipients="; ".join(f"{k}:{fmt_amt(v)}" for k, v in cats.most_common() if v),
                unlock_type="/".join(sorted(types)), ref_price=price,
                url=f"https://defillama.com/unlocks/{x.get('protocolSlug') or ''}"))
    return out, meta


def cmc_take_snapshot(http, cache_dir, max_age=6 * H):
    sdir = os.path.join(cache_dir, "cmc_snapshots")
    os.makedirs(sdir, exist_ok=True)
    snaps = sorted(os.listdir(sdir))
    if snaps and not http.refresh and time.time() - os.path.getmtime(os.path.join(sdir, snaps[-1])) < max_age:
        return os.path.join(sdir, snaps[-1]), False
    rows = []
    for page in range(1, 40):
        url = ("https://api.coinmarketcap.com/data-api/v3/token-unlock/listing?"
               f"start={page}&limit=100&sort=next_unlocked_date&direction=desc")
        d = http.get(url, f"cmc_listing_p{page}.json", ttl=0)
        lst = (d.get("data") or {}).get("tokenUnlockList") or []
        if not lst:
            break
        for x in lst:
            rows.append({k: x.get(k) for k in ("cryptoId", "symbol", "slug", "name", "circulatingSupply", "nextUnlocked", "nextUnlockedDetail")})
    path = os.path.join(sdir, dt.datetime.now(TZ8).strftime("%Y%m%dT%H%M") + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"takenAt": int(time.time()), "rows": rows}, f)
    return path, True


def src_cmc(cache_dir, lo, hi):
    sdir = os.path.join(cache_dir, "cmc_snapshots")
    best = {}
    files = sorted(os.listdir(sdir)) if os.path.isdir(sdir) else []
    for fn in files:
        with open(os.path.join(sdir, fn), encoding="utf-8") as f:
            snap = json.load(f)
        for x in snap["rows"]:
            nu = x.get("nextUnlocked") or {}
            if not nu.get("date"):
                continue
            ts = int(nu["date"] // 1000)
            if not (lo <= ts <= hi) or ts < snap["takenAt"] - H:  # only events that were still upcoming at snapshot time
                continue
            amt = nu.get("tokenAmount")
            det = x.get("nextUnlockedDetail") or []
            circ = x.get("circulatingSupply")
            best[(x["symbol"], ts)] = mk_event(
                source="CoinMarketCap", kind="CoinMarketCap", symbol=x["symbol"], name=x.get("name"), ts=ts, amount=amt,
                value_usd_src=nu.get("tokenAmountUsd"), pct_circ=amt / circ * 100 if amt and circ else None,
                recipients="; ".join(f"{a.get('allocationName')}:{fmt_amt(a.get('tokenAmount'))}" for a in det),
                unlock_type="/".join(sorted({a.get("vestingType") or "?" for a in det})) or None,
                ref_price=(nu["tokenAmountUsd"] / amt) if amt and nu.get("tokenAmountUsd") else None,
                url=f"https://coinmarketcap.com/currencies/{x.get('slug')}/")
    return list(best.values()), {"snapshots": len(files)}


def src_cmc_live(cache_dir, lo, hi):
    sdir = os.path.join(cache_dir, "cmc_snapshots")
    if not os.path.isdir(sdir):
        return []
    files = sorted(os.listdir(sdir))
    if not files:
        return []
    with open(os.path.join(sdir, files[-1]), encoding="utf-8") as f:
        snap = json.load(f)
    out = []
    for x in snap.get("rows") or []:
        nu = x.get("nextUnlocked") or {}
        if not nu.get("date"):
            continue
        ts = int(nu["date"] // 1000)
        if not (lo <= ts <= hi):
            continue
        amt = nu.get("tokenAmount")
        det = x.get("nextUnlockedDetail") or []
        circ = x.get("circulatingSupply")
        out.append(mk_event(
            source="CoinMarketCap", kind="CoinMarketCap", symbol=x["symbol"], name=x.get("name"), ts=ts, amount=amt,
            value_usd_src=nu.get("tokenAmountUsd"), pct_circ=amt / circ * 100 if amt and circ else None,
            recipients="; ".join(f"{a.get('allocationName')}:{fmt_amt(a.get('tokenAmount'))}" for a in det),
            unlock_type="/".join(sorted({a.get("vestingType") or "?" for a in det})) or None,
            ref_price=(nu["tokenAmountUsd"] / amt) if amt and nu.get("tokenAmountUsd") else None,
            url=f"https://coinmarketcap.com/currencies/{x.get('slug')}/"))
    return out


ZH_RE = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9 .\-&'’]{0,40}?)\s*[（(]\s*(?P<sym>\$?[A-Za-z0-9]{1,15})\s*[）)]\s*将于\s*(?:北京时间)?\s*"
    r"(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日\s*(?P<ap>凌晨|早上|上午|中午|下午|傍晚|晚上|晚)?\s*"
    r"(?P<h>\d{1,2})\s*(?:[:：]\s*(?P<mi>\d{2})|点\s*(?:(?P<mi2>\d{1,2})\s*分|(?P<half>半))?|时)\s*"
    r"(?:左右\s*)?解锁\s*约?\s*(?P<amt>[\d.,]+)\s*(?P<unit>亿|万)?\s*枚"
    r"(?:[^；;。\n]{0,30}?(?:比值|比例|占)[^；;。\d\n]{0,8}(?P<pct>[\d.]+)\s*%)?"
    r"(?:[^；;。\n]{0,30}?价值约?\s*(?P<val>[\d.,]+)\s*(?P<vunit>亿|万)?\s*美元)?")
EN_RE = re.compile(
    r"(?P<name>[A-Z][A-Za-z0-9 .\-&']{0,40}?)\s*\(\s*(?P<sym>\$?[A-Za-z0-9]{1,15})\s*\)\s*will unlock\s*(?:about|approximately|around)?\s*"
    r"(?P<amt>[\d.,]+)\s*(?P<unit>thousand|million|billion)?\s*tokens at\s*(?P<h>\d{1,2})(?::(?P<mi>\d{2}))?\s*(?P<ap>am|pm|a\.m\.|p\.m\.)\s*"
    r"Beijing time on\s*(?P<mon>[A-Z][a-z]+)\s*(?P<d>\d{1,2})"
    r"(?:[^;\n]{0,60}?(?P<pct>[\d.]+)%\s*of (?:the )?(?:current )?circulating supply)?"
    r"(?:[^;\n]{0,40}?worth (?:about|approximately|around)?\s*\$(?P<val>[\d.,]+)\s*(?P<vunit>thousand|million|billion)?)?", re.I)
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
UNIT = {None: 1, "": 1, "万": 1e4, "亿": 1e8, "thousand": 1e3, "million": 1e6, "billion": 1e9}


def _num(s, unit):
    return float(s.replace(",", "")) * UNIT[(unit or "").lower() if unit and unit.isascii() else unit]


def parse_news_text(text, pub_year, pub_month, url):
    seen = {}
    for rx, lang in ((ZH_RE, "zh"), (EN_RE, "en")):
        for m in rx.finditer(text):
            g = m.groupdict()
            try:
                if lang == "zh":
                    mon, day, hr = int(g["m"]), int(g["d"]), int(g["h"])
                    mi = int(g.get("mi") or g.get("mi2") or (30 if g.get("half") else 0))
                    if g["ap"] in ("下午", "傍晚", "晚上", "晚") and hr < 12:
                        hr += 12
                else:
                    mon = MONTHS.get(g["mon"][:3].lower())
                    if not mon:
                        continue
                    day, hr, mi = int(g["d"]), int(g["h"]), int(g["mi"] or 0)
                    ap = g["ap"].lower().replace(".", "")
                    if ap == "pm" and hr < 12:
                        hr += 12
                    if ap == "am" and hr == 12:
                        hr = 0
                year = pub_year + (1 if mon < pub_month - 6 else -1 if mon > pub_month + 6 else 0)
                ts = int(dt.datetime(year, mon, day, hr % 24, mi, tzinfo=TZ8).timestamp())
                sym = g["sym"].upper().lstrip("$")
                ev = mk_event(
                    source="news:" + urllib.parse.urlparse(url).netloc, kind="news", symbol=sym, name=g["name"].strip(), ts=ts,
                    amount=_num(g["amt"], g["unit"]), value_usd_src=_num(g["val"], g["vunit"]) if g.get("val") else None,
                    pct_circ=float(g["pct"]) if g.get("pct") else None, url=url)
                score = (ev["pct_circ"] is not None) + (ev["value_usd_src"] is not None)
                if (sym, ts) not in seen or score > seen[(sym, ts)][0]:
                    seen[(sym, ts)] = (score, ev)
            except (ValueError, KeyError, TypeError):
                continue
    return [v[1] for v in seen.values()]


def src_news(http, urls):
    out = []
    for url in urls:
        try:
            page = http.get(url, "news_" + hashlib.sha1(url.encode()).hexdigest()[:16] + ".html", ttl=7 * DAY, as_json=False)
        except Exception as e:
            log(f"news fetch failed {url}: {e}")
            continue
        pm = re.search(r'"datePublished"\s*:\s*"(\d{4})-(\d{2})', page)
        now = dt.datetime.now(TZ8)
        py, pmon = (int(pm.group(1)), int(pm.group(2))) if pm else (now.year, now.month)
        text = re.sub(r"<style.*?</style>", " ", page, flags=re.S)
        text = htmlmod.unescape(re.sub(r"<[^>]+>", "\n", text))
        evs = parse_news_text(text, py, pmon, url)
        log(f"news {url}: {len(evs)} unlock lines parsed (published {py}-{pmon:02d})")
        out += evs
    return out


def src_extra(path):
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if not (r.get("symbol") and r.get("unlock_time_utc8")):
                continue
            ts = int(dt.datetime.strptime(r["unlock_time_utc8"].strip(), "%Y-%m-%d %H:%M").replace(tzinfo=TZ8).timestamp())
            fl = lambda k: float(r[k]) if r.get(k) not in (None, "", NA) else None
            out.append(mk_event(source="extra:" + (r.get("source") or os.path.basename(path)), kind="extra", symbol=r["symbol"],
                                name=r.get("name") or r["symbol"], gecko_id=r.get("coingecko_id") or None, ts=ts, amount=fl("amount"),
                                value_usd_src=fl("value_usd"), pct_circ=fl("pct_circ"), recipients=r.get("recipients") or None,
                                unlock_type=r.get("unlock_type") or None, url=r.get("source_url") or None))
    return out


# ---------------------------------------------------------------- merge / filter
def merge_events(events):
    by_sym = defaultdict(list)
    for e in events:
        if e["symbol"] and e["ts"]:
            by_sym[e["symbol"]].append(e)
    merged = []
    for sym, evs in by_sym.items():
        clusters = []
        for e in sorted(evs, key=lambda e: (SRC_PREF.get(e["kind"], 9), e["ts"])):
            best, bestd = None, None
            for c in clusters:
                if any(x["source"] == e["source"] for x in c):
                    continue
                if e["gecko_id"] and any(x["gecko_id"] and x["gecko_id"] != e["gecko_id"] for x in c):
                    continue
                d = min(abs(x["ts"] - e["ts"]) for x in c)
                if d <= MERGE_TOL and (bestd is None or d < bestd):
                    best, bestd = c, d
            (best.append(e) if best is not None else clusters.append([e]))
        for c in clusters:
            c.sort(key=lambda e: (SRC_PREF.get(e["kind"], 9), e["ts"]))
            p = dict(c[0])
            for k in ("gecko_id", "recipients", "unlock_type", "pct_circ", "ref_price", "est_value_usd"):
                if p.get(k) is None:
                    p[k] = next((x[k] for x in c if x.get(k) is not None), None)
            # DefiLlama gives the long name; prefer it when the primary name is just a ticker
            p["members"] = c
            p["sources"] = sorted({x["source"] for x in c})
            p["kinds"] = sorted({x["kind"] for x in c})
            flags = []
            if len(p["kinds"]) == 1:
                flags.append("single_source")
            spread = max(x["ts"] for x in c) - min(x["ts"] for x in c)
            if spread > 6 * H:
                flags.append(f"date_conflict({spread / H:.0f}h)")
            amts = [x["amount"] for x in c if x.get("amount")]
            if len(amts) > 1 and max(amts) / min(amts) > 1.25:
                flags.append("amount_conflict")
            p["flags"] = flags
            merged.append(p)
    return merged


def size_filter(ev, a):
    v = ev["value_usd_src"] if ev["value_usd_src"] is not None else ev["est_value_usd"]
    if v is None and ev.get("amount") and ev.get("ref_price"):
        v = ev["amount"] * ev["ref_price"]
    pct = ev["pct_circ"]
    ev["filter_value_usd"] = v
    if v is None and pct is None:
        ev["flags"].append("size_unknown")
        return True
    if v is not None and v >= a.min_usd:
        return True
    return pct is not None and pct >= a.min_pct and (v is None or v >= a.min_usd_floor)


# ---------------------------------------------------------------- prices
def binance_symbols(http):
    d = http.get("https://data-api.binance.vision/api/v3/exchangeInfo?permissions=SPOT", "binance_exchangeInfo.json", ttl=DAY)
    return {s["baseAsset"].upper(): s["symbol"] for s in d["symbols"] if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"}


def binance_last_prices(http, syms):
    if not syms:
        return {}
    url = "https://data-api.binance.vision/api/v3/ticker/price?symbols=" + urllib.parse.quote(json.dumps(sorted(syms), separators=(",", ":")))
    return {x["symbol"]: float(x["price"]) for x in http.get(url, "binance_ticker_now.json", ttl=0)}


def cg_search(http, name, sym):
    for q in [name, sym]:
        if not q:
            continue
        try:
            d = http.get("https://api.coingecko.com/api/v3/search?query=" + urllib.parse.quote(q), f"cg_search_{q}.json", ttl=7 * DAY)
        except Exception as e:
            log(f"CoinGecko search failed for {q}: {e}")
            continue
        cands = [c for c in d.get("coins", []) if (c.get("symbol") or "").upper() == sym]
        nm = (name or "").casefold()
        for c in cands:
            cn = (c.get("name") or "").casefold()
            if nm and (cn == nm or cn in nm or nm in cn):
                return c["id"]
    return None


def cg_simple_prices(http, ids):
    out = {}
    ids = sorted(set(i for i in ids if i))
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            d = http.get("https://api.coingecko.com/api/v3/simple/price?vs_currencies=usd&ids=" + ",".join(chunk),
                         f"cg_simple_{hashlib.sha1(','.join(chunk).encode()).hexdigest()[:10]}.json", ttl=10 * 60)
            out.update({k: v.get("usd") for k, v in d.items()})
        except Exception as e:
            log(f"CoinGecko simple/price failed: {e}")
    return out


def binance_series(http, pair, start_ts, now_ts):
    rows, cur = [], (start_ts // H) * H * 1000
    while True:
        url = f"https://data-api.binance.vision/api/v3/klines?symbol={pair}&interval=1h&startTime={cur}&limit=1000"
        d = http.get(url, f"binance_{pair}_1h_{cur}.json", ttl=20 * 60)
        rows += d
        if len(d) < 1000 or d[-1][0] // 1000 >= now_ts:
            break
        cur = d[-1][0] + H * 1000
    pts = []
    for k in rows:
        o, c = k[0] // 1000, (k[6] + 1) // 1000
        if o <= now_ts:
            pts.append((o, float(k[1])))
        if c <= now_ts:
            pts.append((c, float(k[4])))
        elif o <= now_ts and abs(now_ts - time.time()) < 120:  # live run: in-progress candle's latest price = "now"
            pts.append((now_ts, float(k[4])))
    pts = sorted(dict(pts).items())
    return pts


def cg_series(http, gid, start_ts, now_ts):
    days = math.ceil((time.time() - start_ts) / DAY) + 1
    if days > 90:
        raise RuntimeError("CoinGecko hourly data only available for the last 90 days")
    d = http.get(f"https://api.coingecko.com/api/v3/coins/{gid}/market_chart?vs_currency=usd&days={days}",
                 f"cg_chart_{gid}_{days}d.json", ttl=20 * 60)
    return [(int(p[0] // 1000), p[1]) for p in d.get("prices", []) if p[1] is not None and p[0] // 1000 <= now_ts]


def at(pts, ts):
    """last price point at/before ts; None if missing or stale."""
    lo, hi = 0, len(pts)
    while lo < hi:
        mid = (lo + hi) // 2
        if pts[mid][0] <= ts:
            lo = mid + 1
        else:
            hi = mid
    if lo == 0:
        return None
    t, p = pts[lo - 1]
    return p if ts - t <= MAX_ANCHOR_STALE else None


def pc(a, b):
    return None if a is None or b is None or a == 0 else (b / a - 1) * 100


def window_metrics(pts, ts, now_ts):
    anchors = [at(pts, ts - k * DAY) for k in range(8)]
    daily = {f"D-{d}": pc(anchors[d], anchors[d - 1]) for d in range(7, 0, -1)}
    p_u, p_7 = anchors[0], anchors[7]
    p_now = pts[-1][1] if pts else None
    t_now = pts[-1][0] if pts else None
    if t_now is not None and now_ts - t_now > MAX_ANCHOR_STALE:
        p_now = None
    after = [p for t, p in pts if t >= ts]
    return dict(
        anchors=anchors, daily=daily, p_7=p_7, p_u=p_u, p_now=p_now, t_now=t_now,
        p_24=at(pts, ts + DAY) if now_ts >= ts + DAY else None,
        pre7=pc(p_7, p_u), post=pc(p_u, p_now),
        lo=pc(p_u, min(after)) if after and p_u else None, hi=pc(p_u, max(after)) if after and p_u else None)


# ---------------------------------------------------------------- formatting
def fmt_amt(v):
    if v is None:
        return NA
    a = abs(v)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{v / div:.2f}{suf}"
    return f"{v:.2f}"


def fmt_usd(v):
    return NA if v is None else "$" + fmt_amt(v)


def fp(v, d=1):
    return NA if v is None else f"{v:+.{d}f}%"


def csvv(v):
    if v is None:
        return NA
    if isinstance(v, float):
        return f"{v:.10g}"
    return v


# ---------------------------------------------------------------- main
def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="代币解锁追踪: 找出窗口内已发生的解锁并计算解锁前7天逐日/解锁后表现 (含BTC基准)")
    ap.add_argument("--start", help="窗口开始日期 YYYY-MM-DD (UTC+8, 含当日00:00). 默认=now-7天")
    ap.add_argument("--end", help="窗口结束日期 YYYY-MM-DD (UTC+8, 含当日到23:59, 不超过now). 默认=now")
    ap.add_argument("--now", help="把'现在'固定为某时刻 'YYYY-MM-DD HH:MM' (UTC+8), 用于复现; 价格只取该时刻前的数据")
    ap.add_argument("--min-usd", type=float, default=5e6, help="解锁价值阈值(美元), 默认5e6")
    ap.add_argument("--min-pct", type=float, default=1.0, help="占流通比阈值(%%), 默认1; 满足任一阈值即保留")
    ap.add_argument("--min-usd-floor", type=float, default=5e5, help="仅凭占流通比入选时的最低价值(美元), 默认5e5")
    ap.add_argument("--sources", default="defillama,cmc", help="逗号分隔: defillama,cmc (news/extra 由下面参数启用)")
    ap.add_argument("--news-url", action="append", default=[], help="PANews 等'解锁快讯'文章URL, 可多次指定")
    ap.add_argument("--extra-events", help="手工补充事件CSV (见 extra_events_example.csv)")
    ap.add_argument("--max-tokens", type=int, default=40, help="最多保留多少个解锁事件(按价值排序), 默认40")
    ap.add_argument("--out", help="输出目录, 默认 ./output/<start>_<end>/")
    ap.add_argument("--cache", default=os.path.join(BASE, "cache"), help="缓存目录")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存强制重新抓取")
    ap.add_argument("--quiet", action="store_true", help="不在stdout打印markdown摘要")
    ap.add_argument("--future-days", type=int, default=14, help="写入 future_unlocks.csv 的未来天数(UTC+8)")
    return ap.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    real_now = int(time.time())
    now_ts = int(dt.datetime.strptime(a.now, "%Y-%m-%d %H:%M").replace(tzinfo=TZ8).timestamp()) if a.now else real_now
    lo = int(dt.datetime.strptime(a.start, "%Y-%m-%d").replace(tzinfo=TZ8).timestamp()) if a.start else now_ts - 7 * DAY
    hi = int(dt.datetime.strptime(a.end, "%Y-%m-%d").replace(tzinfo=TZ8).timestamp()) + DAY - 1 if a.end else now_ts
    hi = min(hi, now_ts)
    if lo >= hi:
        sys.exit("empty window")
    tag = f"{fmt_ts(lo)[:10]}_{fmt_ts(hi)[:10]}"
    out_dir = a.out or os.path.join(os.getcwd(), "output", tag)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(a.cache, exist_ok=True)
    http = Http(a.cache, a.refresh)
    meta = {"window_utc8": [fmt_ts(lo), fmt_ts(hi)], "now_utc8": fmt_ts(now_ts), "generated_utc8": fmt_ts(real_now),
            "args": vars(a), "sources": {}, "errors": [], "unmapped": [], "price_notes": []}

    # ---- collect events (look +-48h beyond window so cross-source date conflicts still merge)
    future_hi = now_ts + int(getattr(a, "future_days", 14) or 14) * DAY
    qlo, qhi = lo - MERGE_TOL, max(hi, future_hi) + MERGE_TOL
    events = []
    srcs = [s.strip().lower() for s in a.sources.split(",") if s.strip()]
    if "defillama" in srcs:
        try:
            evs, m = src_defillama(http, qlo, qhi)
            events += evs
            meta["sources"]["DefiLlama"] = {**m, "events_raw": len(evs), "url": "https://defillama.com/unlocks"}
        except Exception as e:
            meta["errors"].append(f"DefiLlama: {e}")
            log("DefiLlama failed:", e)
    if "cmc" in srcs:
        try:
            path, fresh = cmc_take_snapshot(http, a.cache)
            evs, m = src_cmc(a.cache, qlo, qhi)
            events += evs
            meta["sources"]["CoinMarketCap"] = {**m, "events_raw": len(evs), "latest_snapshot": os.path.basename(path), "new_snapshot": fresh,
                                                "url": "https://coinmarketcap.com/token-unlocks/"}
        except Exception as e:
            meta["errors"].append(f"CoinMarketCap: {e}")
            log("CMC failed:", e)
    if a.news_url:
        evs = src_news(http, a.news_url)
        events += evs
        meta["sources"]["news"] = {"urls": a.news_url, "events_raw": len(evs)}
    if a.extra_events:
        evs = src_extra(a.extra_events)
        events += evs
        meta["sources"]["extra"] = {"file": a.extra_events, "events_raw": len(evs)}

    # only unlocks that already happened; future (e.g. CMC "next unlock") events must not merge with past ones
    n_raw = len(events)
    events = [e for e in events if e["ts"] <= now_ts]
    merged = [e for e in merge_events(events) if lo <= e["ts"] <= hi]
    kept = [e for e in merged if size_filter(e, a)]
    kept.sort(key=lambda e: -(e.get("filter_value_usd") or 0))
    dropped_cap = kept[a.max_tokens:]
    kept = kept[:a.max_tokens]
    meta["counts"] = {"raw_events": n_raw, "raw_events_already_happened": len(events), "merged_in_window": len(merged), "kept": len(kept), "dropped_by_cap": len(dropped_cap)}
    log(f"events raw={n_raw} happened={len(events)} merged-in-window={len(merged)} kept={len(kept)}")

    # ---- map to price sources
    bmap = {}
    try:
        bmap = binance_symbols(http)
    except Exception as e:
        meta["errors"].append(f"Binance exchangeInfo: {e}")
    for e in kept:
        if not e.get("gecko_id"):
            e["gecko_id"] = cg_search(http, e.get("name"), e["symbol"])
            if e["gecko_id"]:
                e["gecko_id_from"] = "coingecko_search"
    cgp = cg_simple_prices(http, [e.get("gecko_id") for e in kept])
    blast = {}
    try:
        blast = binance_last_prices(http, {bmap[e["symbol"]] for e in kept if e["symbol"] in bmap} | {"BTCUSDT"})
    except Exception as ex:
        meta["errors"].append(f"Binance ticker: {ex}")
    for e in kept:
        pair = bmap.get(e["symbol"])
        ref = cgp.get(e.get("gecko_id")) or (e.get("ref_price") if e["kind"] == "DefiLlama" or "DefiLlama" in e["kinds"] else None)
        e["price_src"] = None
        if pair and pair in blast and ref:
            if abs(blast[pair] / ref - 1) <= PRICE_VERIFY_TOL:
                e["price_src"] = ("binance", pair)
            else:
                meta["price_notes"].append(f"{e['symbol']}: Binance {pair}={blast[pair]:.6g} vs 参考价 {ref:.6g} 偏差>10%, 不用Binance")
        if not e["price_src"] and e.get("gecko_id"):
            e["price_src"] = ("coingecko", e["gecko_id"])
        if not e["price_src"]:
            why = "Binance有同名交易对但无法校验(无CoinGecko id/参考价)" if pair else "无Binance交易对且无CoinGecko id"
            meta["unmapped"].append(f"{e['symbol']} ({e.get('name')}, {fmt_ts(e['ts'])}): {why}")

    # ---- prices + metrics
    earliest = min([e["ts"] for e in kept] + [now_ts]) - 7 * DAY - 2 * H
    btc = binance_series(http, "BTCUSDT", earliest, now_ts)
    rows, daily_rows = [], []
    for e in sorted(kept, key=lambda e: e["ts"]):
        ts = e["ts"]
        pts, psrc_label = [], NA
        if e["price_src"]:
            kind, ident = e["price_src"]
            try:
                pts = binance_series(http, ident, earliest, now_ts) if kind == "binance" else cg_series(http, ident, earliest, now_ts)
                psrc_label = f"Binance {ident} 1h" if kind == "binance" else f"CoinGecko {ident} hourly"
            except Exception as ex:
                meta["errors"].append(f"price {e['symbol']}: {ex}")
                if kind == "binance" and e.get("gecko_id"):
                    try:
                        pts = cg_series(http, e["gecko_id"], earliest, now_ts)
                        psrc_label = f"CoinGecko {e['gecko_id']} hourly"
                    except Exception as ex2:
                        meta["errors"].append(f"price fallback {e['symbol']}: {ex2}")
        tm = window_metrics(pts, ts, now_ts) if pts else None
        bm = window_metrics(btc, ts, now_ts)
        post_h = (now_ts - ts) / H
        flags = list(e["flags"])
        if post_h < 24:
            flags.append(f"short_post_window({post_h:.1f}h)")
        if not pts:
            flags.append("no_price_data")
        g = (lambda k: tm[k]) if tm else (lambda k: None)
        b_post = pc(bm["p_u"], bm["p_now"])
        val_at = e["amount"] * g("p_u") if e.get("amount") and g("p_u") else None
        row = {
            "ticker": e["symbol"], "name": e.get("name"), "unlock_time_utc8": fmt_ts(ts), "unlock_type": e.get("unlock_type"),
            "amount": e.get("amount"), "value_usd_source": e.get("value_usd_src"), "value_usd_at_unlock": val_at,
            "pct_circ": e.get("pct_circ"), "recipients": e.get("recipients"),
            "sources": " | ".join(e["sources"]), "n_sources": len(e["kinds"]), "single_source": "yes" if "single_source" in flags else "no",
            "flags": "; ".join(flags) or None,
            "source_times_utc8": " | ".join(f"{x['source']}@{fmt_ts(x['ts'])}" for x in e["members"]),
            "source_amounts": " | ".join(f"{x['source']}={fmt_amt(x.get('amount'))}" for x in e["members"]),
            "source_urls": " | ".join(sorted({x["url"] for x in e["members"] if x.get("url")})) or None,
            "coingecko_id": e.get("gecko_id"), "price_source": psrc_label,
            "price_7d_before": g("p_7"), "price_at_unlock": g("p_u"), "price_24h_after": g("p_24"), "price_now": g("p_now"),
            "price_now_time_utc8": fmt_ts(tm["t_now"]) if tm and tm["t_now"] else None,
            "pre7d_change_pct": g("pre7"), "post_unlock_change_pct": g("post"),
            "change_24h_after_pct": pc(g("p_u"), g("p_24")),
            "post_low_vs_unlock_pct": g("lo"), "post_high_vs_unlock_pct": g("hi"),
            "btc_pre7d_change_pct": bm["pre7"], "btc_post_change_pct": b_post,
            "relative_vs_btc_post_pp": (g("post") - b_post) if g("post") is not None and b_post is not None else None,
            "post_window_hours": round(post_h, 1),
        }
        rows.append(row)
        for label, mm, src in ((e["symbol"], tm, psrc_label), ("BTC", bm, "Binance BTCUSDT 1h")):
            dr = {"ticker": e["symbol"], "series": label, "unlock_time_utc8": fmt_ts(ts), "D-7_start_utc8": fmt_ts(ts - 7 * DAY)}
            tot = 1.0
            for d in range(7, 0, -1):
                v = mm["daily"][f"D-{d}"] if mm else None
                dr[f"D-{d}_pct"] = v
                tot = tot * (1 + v / 100) if (v is not None and tot is not None) else None
            dr["total_7d_pct"] = (tot - 1) * 100 if tot is not None else None
            dr["price_source"] = src
            daily_rows.append(dr)

    # ---- future unlocks (next N days)
    future_days = int(getattr(a, "future_days", 14) or 14)
    future_hi = now_ts + future_days * DAY
    future_pool = []
    if "defillama" in srcs:
        try:
            fev, _ = src_defillama(http, now_ts - MERGE_TOL, future_hi + MERGE_TOL)
            future_pool += [e for e in fev if now_ts < e["ts"] <= future_hi]
        except Exception as e:
            meta["errors"].append(f"DefiLlama future: {e}")
    if "cmc" in srcs:
        try:
            future_pool += src_cmc_live(a.cache, now_ts + 1, future_hi)
        except Exception as e:
            meta["errors"].append(f"CMC future: {e}")
    future_merged = merge_events(future_pool)
    future_kept = [e for e in future_merged if size_filter(e, a)]
    future_kept.sort(key=lambda e: -(e.get("filter_value_usd") or 0))
    future_kept = future_kept[:a.max_tokens]
    if not bmap:
        try:
            bmap = binance_symbols(http)
        except Exception:
            pass
    if not blast:
        try:
            syms = {bmap[e["symbol"]] for e in future_kept if e["symbol"] in bmap} | {"BTCUSDT"}
            blast = binance_last_prices(http, syms)
        except Exception:
            blast = {}
    for e in future_kept:
        if not e.get("gecko_id"):
            e["gecko_id"] = cg_search(http, e.get("name"), e["symbol"])
    cgp2 = cg_simple_prices(http, [e.get("gecko_id") for e in future_kept])
    for e in future_kept:
        pair = bmap.get(e["symbol"])
        ref = cgp2.get(e.get("gecko_id")) or e.get("ref_price")
        e["price_src"] = None
        if pair and pair in blast and ref and abs(blast[pair] / ref - 1) <= PRICE_VERIFY_TOL:
            e["price_src"] = ("binance", pair)
        elif e.get("gecko_id"):
            e["price_src"] = ("coingecko", e["gecko_id"])
    future_rows, future_daily_rows = [], []
    for e in sorted(future_kept, key=lambda e: e["ts"]):
        ts = e["ts"]
        pts, psrc_label = [], NA
        if e["price_src"]:
            kind, ident = e["price_src"]
            try:
                pts = binance_series(http, ident, now_ts - 7 * DAY - 2 * H, now_ts) if kind == "binance" else cg_series(http, ident, now_ts - 7 * DAY - 2 * H, now_ts)
                psrc_label = f"Binance {ident} 1h" if kind == "binance" else f"CoinGecko {ident} hourly"
            except Exception as ex:
                meta["errors"].append(f"future price {e['symbol']}: {ex}")
        tm = window_metrics(pts, ts, now_ts) if pts else None
        bm = window_metrics(btc, ts, now_ts) if btc else None
        g = (lambda k: tm[k]) if tm else (lambda k: None)
        p_now = g("p_now") or (pts[-1][1] if pts else None) or (cgp2.get(e.get("gecko_id")) if e.get("gecko_id") else None)
        val_est = e["amount"] * p_now if e.get("amount") and p_now else e.get("value_usd_src") or e.get("est_value_usd")
        flags = list(e.get("flags") or [])
        flags.append("scheduled")
        days_until = round((ts - now_ts) / DAY, 1)
        row = {
            "ticker": e["symbol"], "name": e.get("name"), "unlock_time_utc8": fmt_ts(ts), "unlock_type": e.get("unlock_type"),
            "amount": e.get("amount"), "value_usd_source": e.get("value_usd_src"), "value_usd_at_unlock": val_est,
            "pct_circ": e.get("pct_circ"), "recipients": e.get("recipients"),
            "sources": " | ".join(e.get("sources") or []), "n_sources": len(e.get("kinds") or []),
            "single_source": "yes" if "single_source" in flags else "no",
            "flags": "; ".join(flags), "status": "scheduled", "days_until": days_until,
            "source_times_utc8": " | ".join(f"{x['source']}@{fmt_ts(x['ts'])}" for x in e.get("members") or [e]),
            "source_amounts": " | ".join(f"{x['source']}={fmt_amt(x.get('amount'))}" for x in e.get("members") or [e]),
            "source_urls": " | ".join(sorted({x.get("url") for x in (e.get("members") or [e]) if x.get("url")})) or None,
            "coingecko_id": e.get("gecko_id"), "price_source": psrc_label,
            "price_7d_before": g("p_7"), "price_at_unlock": None, "price_24h_after": None, "price_now": p_now,
            "price_now_time_utc8": fmt_ts(tm["t_now"]) if tm and tm.get("t_now") else fmt_ts(now_ts),
            "pre7d_change_pct": g("pre7"), "post_unlock_change_pct": None, "change_24h_after_pct": None,
            "post_low_vs_unlock_pct": None, "post_high_vs_unlock_pct": None,
            "btc_pre7d_change_pct": bm["pre7"] if bm else None, "btc_post_change_pct": None,
            "relative_vs_btc_post_pp": None, "post_window_hours": None,
        }
        future_rows.append(row)
        for label, mm, src in ((e["symbol"], tm, psrc_label), ("BTC", bm, "Binance BTCUSDT 1h")):
            dr = {"ticker": e["symbol"], "series": label, "unlock_time_utc8": fmt_ts(ts), "D-7_start_utc8": fmt_ts(ts - 7 * DAY)}
            tot = 1.0
            for d in range(7, 0, -1):
                v = mm["daily"][f"D-{d}"] if mm else None
                dr[f"D-{d}_pct"] = v
                tot = tot * (1 + v / 100) if (v is not None and tot is not None) else None
            dr["total_7d_pct"] = (tot - 1) * 100 if tot is not None else None
            dr["price_source"] = src
            future_daily_rows += [dr]
    meta["counts"]["future_kept"] = len(future_rows)

    # ---- write outputs
    def wcsv(path, data):
        if not data:
            open(path, "w").close()
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(data[0].keys()))
            w.writeheader()
            w.writerows([{k: csvv(v) for k, v in r.items()} for r in data])
    wcsv(os.path.join(out_dir, "unlocks.csv"), rows)
    wcsv(os.path.join(out_dir, "pre7d_daily.csv"), daily_rows)
    wcsv(os.path.join(out_dir, "future_unlocks.csv"), future_rows)
    if future_daily_rows:
        wcsv(os.path.join(out_dir, "future_pre7d_daily.csv"), future_daily_rows)
    md = render_md(rows, daily_rows, meta, a)
    with open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8") as f:
        f.write(md)
    with open(os.path.join(out_dir, "run_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1, default=str)
    if not a.quiet:
        print(md)
    log(f"outputs written to {out_dir}")
    return rows, daily_rows, meta


def render_md(rows, daily_rows, meta, a):
    L = []
    L.append(f"# 代币解锁追踪 {meta['window_utc8'][0]} ~ {meta['window_utc8'][1]} (UTC+8)\n")
    L.append(f"生成时间 {meta['generated_utc8']} UTC+8；价格截至 {meta['now_utc8']}。阈值：价值≥${a.min_usd:,.0f} 或 占流通≥{a.min_pct}%（且价值≥${a.min_usd_floor:,.0f}）。共 {len(rows)} 个解锁事件。\n")
    L.append("## 解锁与价格表现\n")
    L.append("| 代币 | 解锁时间 | 解锁数量/价值 | 占流通比 | 解锁前7天涨跌 | 解锁日至今涨跌 | 同期BTC涨跌 | 相对BTC | 来源 | 标记 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        val = r["value_usd_source"] if r["value_usd_source"] is not None else r["value_usd_at_unlock"]
        src = "+".join(sorted({s.split(":")[0] for s in r["sources"].split(" | ")}))
        rel = NA if r["relative_vs_btc_post_pp"] is None else f"{r['relative_vs_btc_post_pp']:+.1f}pp"
        pct = NA if r["pct_circ"] is None else f"{r['pct_circ']:.2f}%"
        L.append(f"| {r['ticker']} | {r['unlock_time_utc8'][5:]} | {fmt_amt(r['amount'])} / {fmt_usd(val)} | {pct} | {fp(r['pre7d_change_pct'])} | "
                 f"{fp(r['post_unlock_change_pct'])} | {fp(r['btc_post_change_pct'])} | {rel} | {src} | {r['flags'] or ''} |")
    L.append("\n价值列优先用来源报的价值，否则 = 数量 × 解锁时价格。标记：single_source=只有一个来源；date_conflict/amount_conflict=来源间日期/数量不一致；short_post_window=解锁后不足24h。\n")
    L.append("## 解锁前7天逐日涨跌（24h桶，锚定解锁时刻）\n")
    L.append("| 代币 | 解锁时间 | D-7 | D-6 | D-5 | D-4 | D-3 | D-2 | D-1 | 7天合计 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for d in daily_rows:
        if d["series"] != "BTC":
            L.append(f"| {d['ticker']} | {d['unlock_time_utc8'][5:]} | " + " | ".join(fp(d[f'D-{k}_pct']) for k in range(7, 0, -1)) + f" | {fp(d['total_7d_pct'], 2)} |")
    L.append("\n## BTC 同窗口逐日涨跌\n")
    L.append("| 对应代币 | D-7 | D-6 | D-5 | D-4 | D-3 | D-2 | D-1 | 合计 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    seen = {}
    for d in daily_rows:
        if d["series"] == "BTC":
            seen.setdefault(d["unlock_time_utc8"], []).append(d)
    for t, ds in seen.items():
        d = ds[0]
        L.append(f"| {' / '.join(x['ticker'] for x in ds)} | " + " | ".join(fp(d[f'D-{k}_pct']) for k in range(7, 0, -1)) + f" | {fp(d['total_7d_pct'])} |")
    # purely computed observations
    obs = []
    weak = [r for r in rows if r["relative_vs_btc_post_pp"] is not None and r["relative_vs_btc_post_pp"] <= -5 and r["post_window_hours"] >= 24]
    strong = [r for r in rows if r["relative_vs_btc_post_pp"] is not None and r["relative_vs_btc_post_pp"] >= 5 and r["post_window_hours"] >= 24]
    if weak:
        obs.append("解锁后跑输BTC≥5pp：" + "、".join(f"{r['ticker']}({r['relative_vs_btc_post_pp']:+.1f}pp)" for r in weak))
    if strong:
        obs.append("解锁后跑赢BTC≥5pp：" + "、".join(f"{r['ticker']}({r['relative_vs_btc_post_pp']:+.1f}pp)" for r in strong))
    tok_daily = {(d["ticker"], d["unlock_time_utc8"]): d for d in daily_rows if d["series"] != "BTC"}
    big_d1 = [k[0] for k, d in tok_daily.items() if d["D-1_pct"] is not None and abs(d["D-1_pct"]) >= 8]
    if big_d1:
        obs.append("D-1（解锁前最后24h）单日波动≥8%：" + "、".join(big_d1))
    if obs:
        L.append("\n## 自动观察（仅基于上表数字）\n")
        L += [f"- {o}" for o in obs]
    if meta["unmapped"] or meta["price_notes"] or meta["errors"]:
        L.append("\n## 未映射 / 异常\n")
        L += [f"- 未映射价格：{u}" for u in meta["unmapped"]]
        L += [f"- {n}" for n in meta["price_notes"]]
        L += [f"- 错误：{x}" for x in meta["errors"]]
    L.append("\n## 数据来源\n")
    for k, v in meta["sources"].items():
        L.append(f"- {k}: " + json.dumps(v, ensure_ascii=False))
    L.append("- 价格：Binance 现货 1h K线 https://data-api.binance.vision/api/v3/klines（经CoinGecko现价校验）；否则 CoinGecko /coins/{id}/market_chart 小时数据；BTC 基准 = Binance BTCUSDT 同窗口。")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
