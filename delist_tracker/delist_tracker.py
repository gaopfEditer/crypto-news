#!/usr/bin/env python3
"""delist_tracker: 交易所现货下架追踪 + 下架前后价格表现 (stdlib only).

复用 ../unlock_tracker/unlock_tracker.py 里的 HTTP缓存/价格/指标/格式化函数 (只 import, 不修改它).
交易所公告源:
  binance : www.binance.com CMS 公告 API (catalogId=161 Delisting)
  okx     : www.okx.com /api/v5/support/announcements (announcements-delistings) + 帮助中心文章表格
  bybit   : announcements.bybit.com 列表页 __NEXT_DATA__ + 文章 (api.bybit.com 被 CloudFront 地区屏蔽)
  bitget  : api.bitget.com /api/v2/public/annoucements (symbol_delisting, 仅最近30天) + 文章
  upbit   : api-manager.upbit.com 公告 API (category=trade, "거래지원 종료")
  coinbase: 无公开公告API -> --news-url 解析 Coinbase 停牌新闻句式 + Exchange products 状态校验 + --extra-events
  gate    : 本机访问 403 -> 仅 --extra-events
"""
import argparse, csv, datetime as dt, hashlib, html as htmlmod, json, os, re, sys, time, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "unlock_tracker"))
import unlock_tracker as ut  # noqa: E402  (shared helpers)
from unlock_tracker import TZ8, NA, H, DAY, fmt_ts, fp, fmt_usd, csvv, pc, at  # noqa: E402


def log(*a):
    print("[delist_tracker]", *a, file=sys.stderr, flush=True)

UTC = dt.timezone.utc
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
QUOTES = ("USDT", "USDC", "FDUSD", "USD", "EUR", "BTC", "ETH", "TRY", "BRL", "KRW", "USDE", "BNB")
ALL_EX = ["binance", "okx", "bybit", "bitget", "upbit", "coinbase", "gate"]
LOOKBACK_ANN = 60 * DAY   # announcements up to 60d before window start can schedule a delisting inside the window


# ---------------------------------------------------------------- helpers
def html_text(h):
    h = re.sub(r"<script.*?</script>|<style.*?</style>", " ", h, flags=re.S)
    t = htmlmod.unescape(re.sub(r"<[^>]+>", "\n", h))
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n", t))


def parse_en_dt(date_s, time_s=None, tz=UTC):
    """'Sep 24, 2026' / 'September 24, 2026' / '24 September 2026' / '2026-09-24' + '8:00AM' / '10:00 AM' / '08:00' / '08:00:00'."""
    date_s = date_s.strip().rstrip(",")
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", date_s)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.match(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", date_s) or None
        if m:
            mo, d, y = MONTHS[m.group(1)[:3].lower()], int(m.group(2)), int(m.group(3))
        else:
            m = re.match(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", date_s)
            if not m:
                raise ValueError(f"bad date {date_s!r}")
            d, mo, y = int(m.group(1)), MONTHS[m.group(2)[:3].lower()], int(m.group(3))
    hh = mi = 0
    if time_s:
        m = re.match(r"\s*(\d{1,2})(?::(\d{2}))?(?::\d{2})?\s*([AaPp]\.?[Mm]\.?)?", time_s)
        hh, mi = int(m.group(1)), int(m.group(2) or 0)
        ap = (m.group(3) or "").lower().replace(".", "")
        if ap == "pm" and hh < 12:
            hh += 12
        if ap == "am" and hh == 12:
            hh = 0
    return int(dt.datetime(y, mo, d, hh, mi, tzinfo=tz).timestamp())


def us_eastern(y, mo, d):
    """EDT(-4) between 2nd Sunday of March and 1st Sunday of November, else EST(-5)."""
    def nth_sunday(month, n):
        first = dt.date(y, month, 1)
        return first + dt.timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))
    day = dt.date(y, mo, d)
    return dt.timezone(dt.timedelta(hours=-4 if nth_sunday(3, 2) <= day < nth_sunday(11, 1) else -5))


def mk(exchange, symbol, delist_ts, ann_ts, kind="token", **kw):
    e = dict(exchange=exchange, symbol=symbol.upper().strip(), name=None, delist_ts=delist_ts, ann_ts=ann_ts, kind=kind,
             pairs=None, url=None, title=None, status="scheduled", ann_note=None, gecko_id=None, flags=[])
    e.update(kw)
    return e


def split_syms(s):
    return [x.strip().upper() for x in re.split(r"[,，、;&]|\band\b", s) if x.strip() and re.fullmatch(r"[A-Za-z0-9/]{1,20}", x.strip())]


def base_of(pair):
    p = pair.replace("/", "").replace("_", "").replace("-", "").upper()
    for q in sorted(QUOTES, key=len, reverse=True):
        if p.endswith(q) and len(p) > len(q):
            return p[: -len(q)]
    return p


# ---------------------------------------------------------------- exchange sources
def src_binance(http, lo, hi):
    out, arts = [], []
    for page in range(1, 8):
        d = http.get(f"https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1&catalogId=161&pageNo={page}&pageSize=50",
                     f"binance_delist_list_p{page}.json", ttl=30 * 60)
        a = d["data"]["catalogs"][0]["articles"]
        arts += a
        if not a or a[-1]["releaseDate"] / 1000 < lo - LOOKBACK_ANN:
            break
    for a in arts:
        rel = a["releaseDate"] // 1000
        title = a["title"]
        if rel < lo - LOOKBACK_ANN or rel > hi:
            continue
        is_token = re.match(r"Binance Will Delist .+ on \d{4}-\d{2}-\d{2}", title) and not re.search(r"Margin|Futures|Loan|Alpha|Options", title)
        is_pair = title.startswith("Notice of Removal of Spot Trading Pairs")
        if not (is_token or is_pair):
            continue
        d = http.get(f"https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query?articleCode={a['code']}",
                     f"binance_article_{a['code']}.json", ttl=7 * DAY)
        text = binance_body_text(d["data"]["body"])
        url = f"https://www.binance.com/en/support/announcement/detail/{a['code']}"
        if is_token:
            m = re.search(r"following token\(s\)\s*at\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s*\(UTC\)\s*:?(.*?)(?:Please note|$)", text, re.S)
            if not m:
                log(f"binance: cannot parse {title}")
                continue
            ts = parse_en_dt(m.group(1), m.group(2))
            for nm, sym in re.findall(r"([^\n()]{1,60}?)\s*\(([A-Za-z0-9]{1,15})\)", m.group(3)):
                out.append(mk("Binance", sym, ts, rel, name=nm.strip(), pairs="all spot pairs", url=url, title=title))
        else:
            for dd, tt, plist in re.findall(r"At\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s*\(UTC\)\s*:\s*([^\n]+)", text):
                ts = parse_en_dt(dd, tt)
                for p in re.findall(r"[A-Z0-9]{1,15}/[A-Z0-9]{2,6}", plist):
                    out.append(mk("Binance", base_of(p), ts, rel, kind="pair", pairs=p, url=url, title=title))
    return out


def binance_body_text(body):
    try:
        tree = json.loads(body)
    except (ValueError, TypeError):
        return html_text(body or "")
    buf = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("node") == "text":
                buf.append(n.get("text", ""))
            for c in n.get("child") or []:
                walk(c)
            if n.get("tag") in ("p", "li", "tr", "h1", "h2", "h3", "br", "div"):
                buf.append("\n")
        elif isinstance(n, list):
            for c in n:
                walk(c)
    walk(tree)
    return htmlmod.unescape("".join(buf))


def src_okx(http, lo, hi):
    out, items = [], []
    for page in range(1, 6):
        d = http.get(f"https://www.okx.com/api/v5/support/announcements?annType=announcements-delistings&page={page}", f"okx_delist_p{page}.json", ttl=30 * 60)
        det = (d.get("data") or [{}])[0].get("details") or []
        items += det
        if not det or int(det[-1]["pTime"]) / 1000 < lo - LOOKBACK_ANN:
            break
    for a in items:
        rel = int(a.get("businessPTime") or a["pTime"]) // 1000
        if rel < lo - LOOKBACK_ANN or rel > hi or "delist" not in a["title"].lower() or "spot" not in a["title"].lower():
            continue
        page = http.get(a["url"], "okx_art_" + hashlib.sha1(a["url"].encode()).hexdigest()[:12] + ".html", ttl=7 * DAY, as_json=False)
        text = html_text(page)
        per = {}
        for b, q, ds, tm in re.findall(r"\b([A-Z0-9]{1,15})/([A-Z]{2,6})\s+([A-Z][a-z]+ \d{1,2}, \d{4}),?\s+(\d{1,2}:\d{2})\s*-\s*\d{1,2}:\d{2}\s*UTC", text):
            per.setdefault(b, []).append((parse_en_dt(ds, tm), f"{b}/{q}"))
        whole = set(split_syms(re.sub(r"(?i)^OKX to delist|spot trading pairs?$", "", a["title"]).strip())) if "selected" not in a["title"].lower() else set()
        for b, lst in per.items():
            lst.sort()
            last_ts = lst[-1][0]
            if b in whole:  # token-level: trading on OKX fully stops when its last pair is removed
                out.append(mk("OKX", b, last_ts, rel, pairs=",".join(p for _, p in lst), url=a["url"], title=a["title"]))
                lst = [x for x in lst if x[0] < last_ts]
            for ts, p in lst:
                out.append(mk("OKX", b, ts, rel, kind="pair", pairs=p, url=a["url"], title=a["title"]))
    return out


def src_bybit(http, lo, hi):
    out, items = [], []
    for page in range(1, 5):
        h = http.get(f"https://announcements.bybit.com/en/?category=delistings&page={page}", f"bybit_delist_p{page}.html", ttl=30 * 60, as_json=False)
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', h, re.S)
        lst = json.loads(m.group(1))["props"]["pageProps"]["articleInitEntity"]["list"] if m else []
        items += lst
        if not lst or lst[-1]["publish_time"] < lo - LOOKBACK_ANN:
            break
    for a in items:
        rel, title = a["publish_time"], a["title"]
        if rel < lo - LOOKBACK_ANN or rel > hi or re.search(r"Perpetual|Futures|Alpha|Collateral|Margin|Loan|Earn", title, re.I):
            continue
        url = "https://announcements.bybit.com/en" + a["url"]
        text = html_text(http.get(url, "bybit_art_" + hashlib.sha1(url.encode()).hexdigest()[:12] + ".html", ttl=7 * DAY, as_json=False))
        m = re.search(r"Trading of\s+([A-Z0-9/, ]+?)\s+(?:pairs?\s+)?on Bybit.{0,3}s Spot platform will end after\s+([A-Z][a-z]+ \d{1,2}, \d{4}),?\s*(\d{1,2}(?::\d{2})?\s*[AP]M)\s*UTC", text)
        if m:
            ts = parse_en_dt(m.group(2), m.group(3))
            pairs = split_syms(m.group(1))
            mt = re.search(r"Delisted tokens:\s*([A-Z0-9][A-Z0-9 ,]*)", text)
            toks = split_syms(mt.group(1)) if mt else sorted({base_of(p) for p in pairs})
            for t in toks:
                out.append(mk("Bybit", t, ts, rel, pairs=",".join(p for p in pairs if base_of(p) == t) or None, url=url, title=title))
            continue
        m = re.search(r"delist the following Spot trading pairs on\s+(.+?)\s*UTC\s*:\s*([A-Z0-9][A-Z0-9 ,]*)", text)
        if m:
            ds = m.group(1)
            mm = re.match(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", ds) or re.match(r"([A-Z][a-z]+ \d{1,2}, \d{4}),?\s*(\d{1,2}:\d{2}\s*[AP]M)", ds)
            ts = parse_en_dt(mm.group(1), mm.group(2))
            for p in split_syms(m.group(2)):
                out.append(mk("Bybit", base_of(p), ts, rel, kind="pair", pairs=p, url=url, title=title))
            continue
        log(f"bybit: cannot parse {title}")
    return out


def src_bitget(http, lo, hi):
    out, items, cursor = [], [], None
    for page in range(6):
        url = "https://api.bitget.com/api/v2/public/annoucements?language=en_US&annType=symbol_delisting" + (f"&cursor={cursor}" if cursor else "")
        d = http.get(url, f"bitget_delist_{cursor or 'first'}.json", ttl=30 * 60)
        lst = d.get("data") or []
        items += lst
        if len(lst) < 10:
            break
        cursor = lst[-1]["annId"]
    postpone = []
    for a in items:
        rel, title = int(a["cTime"]) // 1000, a["annTitle"]
        m = re.search(r"delay the delisting of (.+?) from spot", title, re.I)
        if m:
            postpone.append((rel, set(split_syms(m.group(1))), a["annUrl"]))
    for a in items:
        rel, title = int(a["cTime"]) // 1000, a["annTitle"]
        if rel > hi or not re.search(r"Delisting .*Spot Trading Pairs|delist .* spot trading", title, re.I) or "delay" in title.lower():
            continue
        page = http.get(a["annUrl"], f"bitget_art_{a['annId']}.html", ttl=7 * DAY, as_json=False)
        text = html_text(page)
        for ds, tm, plist in re.findall(r"Bitget will delist the following trading pairs from the unified account \(spot trading\) on\s+"
                                        r"([A-Z][a-z]+ \d{1,2}, \d{4}),?\s*(\d{1,2}:\d{2}\s*[AP]M)\s*\(UTC\)\s*:\s*([A-Z0-9/;，, \n]+)", text):
            ts = parse_en_dt(ds, tm)
            for p in re.findall(r"[A-Z0-9]{1,15}/[A-Z]{2,6}", plist):
                e = mk("Bitget", base_of(p), ts, rel, pairs=p, url=a["annUrl"].replace("/en/support", "/support"), title=title)
                for prel, syms, purl in postpone:
                    if prel > rel and e["symbol"] in syms:
                        e["status"] = "postponed"
                        e["flags"].append(f"postponed@{fmt_ts(prel)}")
                out.append(e)
    return out


def src_upbit(http, lo, hi):
    out, items = [], []
    for page in range(1, 10):
        d = http.get(f"https://api-manager.upbit.com/api/v1/announcements?os=web&page={page}&per_page=20&category=trade", f"upbit_trade_p{page}.json", ttl=30 * 60)
        lst = d["data"]["notices"]
        items += lst
        if not lst or dt.datetime.fromisoformat(lst[-1]["listed_at"]).timestamp() < lo - LOOKBACK_ANN:
            break
    for a in items:
        rel = int(dt.datetime.fromisoformat(a["first_listed_at"] or a["listed_at"]).timestamp())
        if rel < lo - LOOKBACK_ANN or rel > hi or "거래지원 종료" not in a["title"]:
            continue
        d = http.get(f"https://api-manager.upbit.com/api/v1/announcements/{a['id']}", f"upbit_ann_{a['id']}.json", ttl=7 * DAY)
        body = d["data"].get("body") or ""
        url = f"https://upbit.com/service_center/notice?id={a['id']}"
        found = False
        for nm, sym, pairs, ds, tm in re.findall(r"(?:^|\n)[ \t]*(?:\*\*)?([^*\r\n(]{1,40}?)\s*\(([A-Z0-9]{1,15})\)(?:\*\*)?\s*"
                                                 r"(?:-\s*대상 페어\s*:\s*([^\r\n]+)\s*)?-\s*거래지원 종료 예정일\s*:\s*(\d{4}-\d{2}-\d{2})\([^)]*\)\s*(\d{1,2}:\d{2})\s*KST", body):
            ts = int(dt.datetime.strptime(f"{ds} {tm}", "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone(dt.timedelta(hours=9))).timestamp())
            kind = "pair" if "마켓 거래지원 종료" in a["title"] else "token"
            out.append(mk("Upbit", sym, ts, rel, kind=kind, name=nm.strip(), pairs=(pairs or "").strip() or None, url=url, title=a["title"]))
            found = True
        if not found:
            log(f"upbit: cannot parse {a['title']}")
    return out


CB_RE = re.compile(r"suspend(?:ed)?\s+trading\s+(?:for|of|in)\s+(?P<list>[^.;]{2,200}?)\s+on\s+(?P<date>\d{1,2}\s+[A-Z][a-z]+\s+\d{4}|[A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4}),?\s*"
                   r"(?:on or )?(?:at )?(?:approximately |around |about )?(?P<h>\d{1,2})(?::(?P<mi>\d{2}))?\s*(?P<ap>[AaPp]\.?\s?[Mm]\.?)\s*(?:ET|Eastern)")


def src_coinbase_news(http, urls):
    out = []
    for url in urls:
        try:
            page = http.get(url, "news_" + hashlib.sha1(url.encode()).hexdigest()[:16] + ".html", ttl=7 * DAY, as_json=False)
        except Exception as e:
            log(f"news fetch failed {url}: {e}")
            continue
        if "coinbase" not in page.lower():
            continue
        pm = re.search(r'"datePublished"\s*:\s*"([^"]+)"', page)
        pub = int(dt.datetime.fromisoformat(pm.group(1).replace("Z", "+00:00")).timestamp()) if pm else None
        text = re.sub(r"\s+", " ", html_text(page))
        seen = set()
        for m in CB_RE.finditer(text):
            ds = m.group("date")
            y = int(re.search(r"\d{4}", ds).group(0))
            mon = re.search(r"[A-Za-z]+", ds).group(0)[:3].lower()
            day = int(re.search(r"\b(\d{1,2})\b", ds).group(1))
            tz = us_eastern(y, MONTHS[mon], day)
            ts = parse_en_dt(ds, f"{m.group('h')}:{m.group('mi') or '00'} {m.group('ap').replace(' ', '')}", tz=tz)
            syms = re.findall(r"\(([A-Z0-9]{2,12})\)", m.group("list")) + re.findall(r"\b([A-Z0-9]{2,12})USD\b", m.group("list"))
            for s in syms:
                if (s, ts) in seen:
                    continue
                seen.add((s, ts))
                ann_ts = pub if pub and pub < ts else None
                out.append(mk("Coinbase", s, ts, ann_ts, pairs="all", url=url, title=None,
                              ann_note="公告时间=新闻发稿时间(近似)" if ann_ts else "公告时间未知"))
        log(f"coinbase news {url}: {len(seen)} events")
    return out


def coinbase_verify(http, events):
    for e in events:
        if e["exchange"] != "Coinbase":
            continue
        try:
            d = http.get(f"https://api.exchange.coinbase.com/products/{e['symbol']}-USD", f"cb_product_{e['symbol']}-USD.json", ttl=3 * H)
            st = d.get("status")
            e["flags"].append(f"coinbase_status={st}" + (",trading_disabled" if d.get("trading_disabled") else ""))
        except Exception as ex:
            e["flags"].append(f"coinbase_status=N/A({type(ex).__name__})")


def src_extra(path):
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if not (r.get("symbol") and r.get("exchange") and r.get("delist_time_utc8")):
                continue
            p = lambda s: int(dt.datetime.strptime(s.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=TZ8).timestamp()) if s and s.strip() not in ("", NA) else None
            out.append(mk(r["exchange"], r["symbol"], p(r["delist_time_utc8"]), p(r.get("announce_time_utc8")), kind=r.get("kind") or "token",
                          name=r.get("name") or None, pairs=r.get("pairs") or None, url=r.get("source_url") or None,
                          ann_note=r.get("announce_note") or None, gecko_id=r.get("coingecko_id") or None,
                          title="extra:" + (r.get("source") or os.path.basename(path))))
    return out


# ---------------------------------------------------------------- mapping
def map_gecko(http, e, id_map):
    if e.get("gecko_id"):
        return e["gecko_id"], "given"
    if e["symbol"] in id_map:
        return id_map[e["symbol"]], "--id-map"
    gid = ut.cg_search(http, e.get("name"), e["symbol"]) if e.get("name") else None
    if gid:
        return gid, "search(name+symbol)"
    try:
        d = http.get("https://api.coingecko.com/api/v3/search?query=" + urllib.parse.quote(e["symbol"]), f"cg_search_{e['symbol']}.json", ttl=7 * DAY)
    except Exception:
        return None, None
    c = [x for x in d.get("coins", []) if (x.get("symbol") or "").upper() == e["symbol"] and x.get("market_cap_rank")]
    c.sort(key=lambda x: x["market_cap_rank"])
    if len(c) == 1 or (len(c) > 1 and c[0]["market_cap_rank"] * 3 <= c[1]["market_cap_rank"]):
        return c[0]["id"], f"search(symbol-only, rank {c[0]['market_cap_rank']})"
    return None, ("ambiguous symbol: " + ",".join(x["id"] for x in c[:4])) if c else None


# ---------------------------------------------------------------- main
def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="交易所现货下架追踪: 找出窗口内已实际停止交易的下架，并计算下架前7天逐日/下架后表现 (含BTC基准)")
    ap.add_argument("--start", help="窗口开始 YYYY-MM-DD (UTC+8)，默认 now-7天")
    ap.add_argument("--end", help="窗口结束 YYYY-MM-DD (UTC+8，含当日)，不超过 now")
    ap.add_argument("--now", help="固定'现在' 'YYYY-MM-DD HH:MM' (UTC+8) 用于复现")
    ap.add_argument("--exchanges", default="binance,okx,bybit,bitget,upbit,coinbase", help="逗号分隔: " + ",".join(ALL_EX))
    ap.add_argument("--news-url", action="append", default=[], help="Coinbase 停牌新闻/推文转载URL，可多次")
    ap.add_argument("--extra-events", help="手工补充事件CSV (见 extra_events_example.csv)")
    ap.add_argument("--id-map", action="append", default=[], help="手工指定 CoinGecko id: SYM=gecko-id，可多次")
    ap.add_argument("--include-pairs", action="store_true", help="把仅移除部分交易对(代币仍在该所交易)的事件也做价格分析")
    ap.add_argument("--min-mcap", type=float, default=0, help="下架时 CoinGecko 市值低于此值(美元)的不展示，默认0")
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
    exs = [x.strip().lower() for x in a.exchanges.split(",") if x.strip()]
    fns = {"binance": src_binance, "okx": src_okx, "bybit": src_bybit, "bitget": src_bitget, "upbit": src_upbit}
    events = []
    for ex in exs:
        try:
            if ex in fns:
                evs = fns[ex](http, lo, now_ts)
            elif ex == "coinbase":
                evs = src_coinbase_news(http, a.news_url)
                if not a.news_url:
                    meta["errors"].append("Coinbase: 无公开公告API，未提供 --news-url，仅能靠 --extra-events")
            elif ex == "gate":
                meta["errors"].append("Gate: www.gate.com 从本机访问返回403，未自动抓取；请用 --extra-events")
                evs = []
            else:
                meta["errors"].append(f"unknown exchange {ex}")
                evs = []
            meta["sources"][ex] = {"events_parsed": len(evs)}
            events += evs
        except Exception as ex2:
            meta["errors"].append(f"{ex}: {type(ex2).__name__}: {ex2}")
            log(f"{ex} failed: {ex2}")
    if a.extra_events:
        evs = src_extra(a.extra_events)
        events += evs
        meta["sources"]["extra"] = {"file": a.extra_events, "events_parsed": len(evs)}
    # dedupe (same exchange+symbol+kind+delist time); news+extra duplicates collapse, extra wins
    ded = {}
    for e in sorted(events, key=lambda e: (0 if (e.get("title") or "").startswith("extra:") else 1, e.get("ann_ts") or 9e12)):
        k = (e["exchange"].lower(), e["symbol"], e["kind"], e["delist_ts"] // H if e["delist_ts"] else None)
        if k not in ded:
            ded[k] = e
    events = list(ded.values())
    coinbase_verify(http, events)
    in_win = [e for e in events if e["delist_ts"] and lo <= e["delist_ts"] <= hi]
    happened = [e for e in in_win if e["delist_ts"] <= now_ts and e["status"] == "scheduled"]
    postponed = [e for e in in_win if e["status"] != "scheduled"]
    pairs_only = [e for e in happened if e["kind"] == "pair"]
    analyze = [e for e in happened if e["kind"] == "token" or a.include_pairs]
    upcoming = [e for e in events if e["delist_ts"] and e["delist_ts"] > now_ts and e["kind"] == "token" and e["status"] == "scheduled"
                and (e["ann_ts"] or 0) >= lo - 14 * DAY]
    meta["counts"] = {"parsed": len(events), "in_window": len(in_win), "analyzed": len(analyze), "pair_only": len(pairs_only),
                      "postponed": len(postponed), "upcoming_token_delists": len(upcoming)}
    log(f"parsed={len(events)} in-window={len(in_win)} analyze={len(analyze)} pair-only={len(pairs_only)} postponed={len(postponed)}")

    # ---- prices
    id_map = dict(x.split("=", 1) for x in a.id_map if "=" in x)
    bmap = {}
    try:
        bmap = ut.binance_symbols(http)
    except Exception as ex:
        meta["errors"].append(f"Binance exchangeInfo: {ex}")
    for e in analyze:
        e["gecko_id"], e["gecko_from"] = map_gecko(http, e, id_map)
    cgp = ut.cg_simple_prices(http, [e["gecko_id"] for e in analyze])
    try:
        blast = ut.binance_last_prices(http, {bmap[e["symbol"]] for e in analyze if e["symbol"] in bmap} | {"BTCUSDT"})
    except Exception as ex:
        blast = {}
        meta["errors"].append(f"Binance ticker: {ex}")
    earliest = min([e["delist_ts"] - 7 * DAY for e in analyze] + [now_ts - 8 * DAY])
    earliest_ann = min([e["ann_ts"] for e in analyze if e.get("ann_ts")] + [earliest])
    earliest_ann = max(earliest_ann, real_now - 88 * DAY)
    start_px = min(earliest, earliest_ann) - 2 * H
    btc = ut.binance_series(http, "BTCUSDT", start_px, now_ts)
    rows, daily = [], []
    for e in sorted(analyze, key=lambda e: (e["delist_ts"], e["exchange"], e["symbol"])):
        ts, gid = e["delist_ts"], e.get("gecko_id")
        flags = list(e["flags"])
        if not gid:
            meta["unmapped"].append(f"{e['symbol']} ({e['exchange']} {fmt_ts(ts)}): {e.get('gecko_from') or '无CoinGecko匹配'}")
        mc = vol = cgpts = []
        if gid:
            try:
                days = min(90, int((real_now - start_px) // DAY) + 2)
                d = http.get(f"https://api.coingecko.com/api/v3/coins/{gid}/market_chart?vs_currency=usd&days={days}", f"cg_chart_{gid}_{days}d.json", ttl=20 * 60)
                cgpts = [(int(p[0] // 1000), p[1]) for p in d.get("prices", []) if p[1] is not None and p[0] // 1000 <= now_ts]
                mc = [(int(p[0] // 1000), p[1]) for p in d.get("market_caps", []) if p[1] and p[0] // 1000 <= now_ts]
                vol = [(int(p[0] // 1000), p[1]) for p in d.get("total_volumes", []) if p[1] and p[0] // 1000 <= now_ts]
            except Exception as ex:
                meta["errors"].append(f"CoinGecko {gid}: {ex}")
        pair = bmap.get(e["symbol"])
        pts, psrc = [], NA
        if pair and e["exchange"] != "Binance" and pair in blast and cgp.get(gid) and abs(blast[pair] / cgp[gid] - 1) <= ut.PRICE_VERIFY_TOL:
            try:
                pts, psrc = ut.binance_series(http, pair, start_px, now_ts), f"Binance {pair} 1h"
            except Exception as ex:
                meta["errors"].append(f"Binance {pair}: {ex}")
        elif pair and e["exchange"] != "Binance" and pair in blast and cgp.get(gid):
            meta["price_notes"].append(f"{e['symbol']}: Binance {pair} 现价与CoinGecko偏差>10%，改用CoinGecko")
        if not pts and cgpts:
            pts, psrc = cgpts, f"CoinGecko {gid} hourly"
        if not pts:
            flags.append("no_price_data")
        m = ut.window_metrics(pts, ts, now_ts) if pts else None
        b = ut.window_metrics(btc, ts, now_ts)
        g = (lambda k: m[k]) if m else (lambda k: None)
        ann = e.get("ann_ts")
        pa = at(pts, ann) if (pts and ann) else None
        pa24 = at(pts, ann + DAY) if (pts and ann and ann + DAY <= now_ts) else None
        post_h = (now_ts - ts) / H
        if post_h < 24:
            flags.append(f"short_post_window({post_h:.1f}h)")
        if e.get("gecko_from", "").startswith("search(symbol-only"):
            flags.append("gecko_by_symbol")
        if e["kind"] == "pair":
            flags.append("pair_only")
        b_post = b["post"]
        rows.append({
            "ticker": e["symbol"], "name": e.get("name"), "exchange": e["exchange"], "kind": e["kind"], "pairs": e.get("pairs"),
            "announce_time_utc8": fmt_ts(ann) if ann else None, "announce_note": e.get("ann_note"),
            "delist_time_utc8": fmt_ts(ts), "gap_days": round((ts - ann) / DAY, 1) if ann else None,
            "announce_in_pre7d": ("yes" if ann and ts - 7 * DAY <= ann < ts else "no") if ann else None,
            "coingecko_id": gid, "coingecko_match": e.get("gecko_from"), "price_source": psrc,
            "mcap_usd_at_delist_cg": at(mc, ts) if mc else None, "vol24h_usd_at_delist_cg": at(vol, ts) if vol else None,
            "price_7d_before": g("p_7"), "price_at_delist": g("p_u"), "price_24h_after": g("p_24"), "price_now": g("p_now"),
            "price_now_time_utc8": fmt_ts(m["t_now"]) if m and m["t_now"] else None,
            "pre7d_change_pct": g("pre7"), "post_delist_change_pct": g("post"), "change_24h_after_pct": pc(g("p_u"), g("p_24")),
            "post_low_pct": g("lo"), "post_high_pct": g("hi"),
            "announce_24h_change_pct": pc(pa, pa24), "announce_to_delist_change_pct": pc(pa, g("p_u")),
            "btc_pre7d_change_pct": b["pre7"], "btc_post_change_pct": b_post,
            "relative_vs_btc_post_pp": g("post") - b_post if g("post") is not None and b_post is not None else None,
            "post_window_hours": round(post_h, 1), "flags": "; ".join(flags) or None, "source_url": e.get("url"), "title": e.get("title"),
        })
        for lab, mm, src in ((e["symbol"], m, psrc), ("BTC", b, "Binance BTCUSDT 1h")):
            dr = {"ticker": e["symbol"], "exchange": e["exchange"], "series": lab, "delist_time_utc8": fmt_ts(ts), "D-7_start_utc8": fmt_ts(ts - 7 * DAY)}
            tot = 1.0
            for k in range(7, 0, -1):
                v = mm["daily"][f"D-{k}"] if mm else None
                dr[f"D-{k}_pct"] = v
                tot = tot * (1 + v / 100) if (v is not None and tot is not None) else None
            dr["total_7d_pct"] = (tot - 1) * 100 if tot is not None else None
            dr["price_source"] = src
            daily.append(dr)
    if a.min_mcap:
        keep = {(r["ticker"], r["exchange"]) for r in rows if (r["mcap_usd_at_delist_cg"] or 0) >= a.min_mcap}
        rows = [r for r in rows if (r["ticker"], r["exchange"]) in keep]
        daily = [d for d in daily if (d["ticker"], d["exchange"]) in keep]

    def wcsv(name, data):
        path = os.path.join(out_dir, name)
        with open(path, "w", newline="", encoding="utf-8") as f:
            if data:
                w = csv.DictWriter(f, fieldnames=list(data[0].keys()))
                w.writeheader()
                w.writerows([{k: csvv(v) for k, v in r.items()} for r in data])
    wcsv("delistings.csv", rows)
    wcsv("pre7d_daily.csv", daily)
    side = [{"exchange": e["exchange"], "ticker": e["symbol"], "kind": e["kind"], "status": e["status"], "pairs": e.get("pairs"),
             "announce_time_utc8": fmt_ts(e["ann_ts"]) if e.get("ann_ts") else None, "delist_time_utc8": fmt_ts(e["delist_ts"]),
             "flags": "; ".join(e["flags"]) or None, "source_url": e.get("url")}
            for e in sorted((pairs_only if not a.include_pairs else []) + postponed + upcoming, key=lambda e: e["delist_ts"])]
    wcsv("other_events.csv", side)
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
    L = [f"# 交易所现货下架追踪 {meta['window_utc8'][0]} ~ {meta['window_utc8'][1]} (UTC+8)\n",
         f"生成 {meta['generated_utc8']}；价格截至 {meta['now_utc8']}。只统计窗口内**已实际停止交易**的整币下架（{'含' if a.include_pairs else '不含'}仅移除部分交易对）。共 {len(rows)} 条。\n",
         "## 下架与价格表现\n",
         "| 代币 | 交易所 | 公告时间 | 下架时间 | 价值(CG市值@下架) | 24h成交额(CG) | 下架前7天 | 下架至今 | 同期BTC | 相对BTC | 公告→下架 | 标记 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        rel = NA if r["relative_vs_btc_post_pp"] is None else f"{r['relative_vs_btc_post_pp']:+.1f}pp"
        ann = (r["announce_time_utc8"] or NA)[5:] if r["announce_time_utc8"] else NA
        if r["announce_in_pre7d"] == "yes":
            ann += "（在7天窗口内）"
        L.append(f"| {r['ticker']} | {r['exchange']} | {ann} | {r['delist_time_utc8'][5:]} | {fmt_usd(r['mcap_usd_at_delist_cg'])} | {fmt_usd(r['vol24h_usd_at_delist_cg'])} | "
                 f"{fp(r['pre7d_change_pct'])} | {fp(r['post_delist_change_pct'])} | {fp(r['btc_post_change_pct'])} | {rel} | {fp(r['announce_to_delist_change_pct'])} | {r['flags'] or ''} |")
    L.append("\n价值口径：CoinGecko market_chart 在下架时刻（或之前最近点）的市值；价格优先用仍在交易的 Binance 1h K线（且下架所≠Binance、与CoinGecko现价偏差≤10%），否则 CoinGecko 聚合小时价。公告→下架 = 公告时刻价到下架时刻价（数据不足为 N/A）。\n")
    L += ["## 下架前7天逐日涨跌（24h桶，锚定下架时刻）\n", "| 代币 | 交易所 | 下架时间 | D-7 | D-6 | D-5 | D-4 | D-3 | D-2 | D-1 | 7天合计 |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for d in daily:
        if d["series"] != "BTC":
            L.append(f"| {d['ticker']} | {d['exchange']} | {d['delist_time_utc8'][5:]} | " + " | ".join(fp(d[f'D-{k}_pct']) for k in range(7, 0, -1)) + f" | {fp(d['total_7d_pct'], 2)} |")
    L += ["\n## BTC 同窗口逐日涨跌\n", "| 对应代币 | D-7 | D-6 | D-5 | D-4 | D-3 | D-2 | D-1 | 合计 |", "|---|---|---|---|---|---|---|---|---|"]
    grp = {}
    for d in daily:
        if d["series"] == "BTC":
            grp.setdefault(d["delist_time_utc8"], []).append(d)
    for t, ds in grp.items():
        d = ds[0]
        L.append(f"| {' / '.join(x['ticker'] + '@' + x['exchange'] for x in ds)} | " + " | ".join(fp(d[f'D-{k}_pct']) for k in range(7, 0, -1)) + f" | {fp(d['total_7d_pct'])} |")
    obs = []
    weak = [r for r in rows if r["relative_vs_btc_post_pp"] is not None and r["relative_vs_btc_post_pp"] <= -5]
    if weak:
        obs.append("下架后跑输BTC≥5pp：" + "、".join(f"{r['ticker']}@{r['exchange']}({r['relative_vs_btc_post_pp']:+.1f}pp)" for r in weak))
    pumps = [r for r in rows if r["pre7d_change_pct"] is not None and r["pre7d_change_pct"] >= 20]
    if pumps:
        obs.append("下架前7天涨幅≥20%：" + "、".join(f"{r['ticker']}({r['pre7d_change_pct']:+.0f}%)" for r in pumps))
    inwin = [r for r in rows if r["announce_in_pre7d"] == "yes" and r["announce_24h_change_pct"] is not None]
    if inwin:
        obs.append("公告落在下架前7天内的，公告后24h涨跌：" + "、".join(f"{r['ticker']}({r['announce_24h_change_pct']:+.1f}%)" for r in inwin))
    if obs:
        L.append("\n## 自动观察（仅基于表内数字）\n")
        L += [f"- {o}" for o in obs]
    if side:
        L += ["\n## 其他事件（不做价格分析）\n", "| 交易所 | 代币 | 类型 | 状态 | 交易对 | 公告 | 下架时间 |", "|---|---|---|---|---|---|---|"]
        for s in side:
            typ = "仅交易对" if s["kind"] == "pair" else "整币"
            st = {"scheduled": "已执行" if s["delist_time_utc8"] <= meta["now_utc8"] else "待执行", "postponed": "已推迟"}.get(s["status"], s["status"])
            L.append(f"| {s['exchange']} | {s['ticker']} | {typ} | {st} | {s['pairs'] or ''} | {(s['announce_time_utc8'] or NA)[5:]} | {s['delist_time_utc8'][5:]} |")
    if meta["unmapped"] or meta["errors"] or meta["price_notes"]:
        L.append("\n## 未映射 / 异常\n")
        L += [f"- 未映射价格：{x}" for x in meta["unmapped"]] + [f"- {x}" for x in meta["price_notes"]] + [f"- 错误/限制：{x}" for x in meta["errors"]]
    L.append("\n## 数据来源\n")
    L.append("- 公告：Binance CMS API (catalogId=161)；OKX /api/v5/support/announcements；announcements.bybit.com；Bitget /api/v2/public/annoucements；Upbit api-manager 公告API；Coinbase 新闻(--news-url)+Exchange products 状态；--extra-events。各行 source_url 见 delistings.csv。")
    L.append("- 价格：Binance data-api.binance.vision 1h K线 / CoinGecko market_chart（含市值、成交额）；BTC 基准 Binance BTCUSDT。")
    L.append("- 各源解析数：" + json.dumps(meta["sources"], ensure_ascii=False))
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
