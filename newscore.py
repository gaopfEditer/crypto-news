"""newscore: news_watch 的抓取/打分/聚类/取价核心（vendored，独立运行，仅标准库，Python 3.8+）。

由 /workspace/trading-watch/news_watch/news_watch.py 复制改写；运行时不依赖原目录。
"""
import concurrent.futures as cf, csv, datetime as dt, email.utils, glob, gzip, hashlib, html, json, os, re, sys, time
import urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

TZ8 = dt.timezone(dt.timedelta(hours=8))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
CJK = re.compile(r"[\u4e00-\u9fff]")


def log(*a):
    print(time.strftime("%H:%M:%S"), "[news_dashboard]", *a, file=sys.stderr, flush=True)


def utc8(ts, f="%Y-%m-%d %H:%M"):
    return dt.datetime.fromtimestamp(ts, TZ8).strftime(f) if ts else None


def parse_iso(s):
    """ISO8601 -> unix ts；兼容 Z、任意位数小数秒（Python 3.8 的 fromisoformat 较严格）。"""
    s = s.strip().replace("Z", "+00:00")
    m = re.match(r"(\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d)?)(?:\.(\d+))?(.*)$", s)
    if not m:
        raise ValueError(s)
    base, frac, tz = m.groups()
    d = dt.datetime.fromisoformat(base + (tz or ""))
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp())


def get(url, timeout=20, accept="*/*", retries=2, as_json=False):
    err, redirects = None, 0
    for i in range(retries + 4):
        if i - redirects > retries:
            break
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept, "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                b = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    b = gzip.decompress(b)
            t = b.decode("utf-8", "replace")
            return json.loads(t) if as_json else t
        except urllib.error.HTTPError as e:
            err = e
            if e.code == 404:
                raise
            # Python < 3.11 的 urllib 不跟随 307/308（CoinDesk 会 308 到去掉末尾 / 的地址），手动跟随
            loc = e.headers.get("Location") if e.code in (307, 308) else None
            if loc and redirects < 3:
                url, redirects = urllib.parse.urljoin(url, loc), redirects + 1
                continue
        except Exception as e:  # noqa: BLE001
            err = e
            if "CERTIFICATE_VERIFY_FAILED" in str(e):
                raise RuntimeError("SSL 证书校验失败：python.org 版 Python 请先运行 /Applications/Python 3.x/Install Certificates.command") from e
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{url}: {err}")


def strip_html(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def item(source, label, sid, url, title, ts, summary="", tags=()):
    return dict(source=source, label=label, id=f"{source}:{sid}", url=url, title=strip_html(title), ts=ts,
                summary=strip_html(summary)[:600], tags=list(tags))


# ---------------------------------------------------------------- sources
def src_rss(key, cfg, st):
    t = get(cfg["url"], accept="application/rss+xml,application/xml;q=0.9,*/*;q=0.8")
    if "<item" not in t:
        raise RuntimeError(f"no <item> in response ({len(t)} bytes)")
    root = ET.fromstring(t.encode("utf-8"))
    out = []
    for it in root.iter("item"):
        g = lambda tag: (it.findtext(tag) or "").strip()  # noqa: E731
        link, title = g("link"), g("title")
        try:
            ts = int(email.utils.parsedate_to_datetime(g("pubDate")).timestamp())
        except Exception:  # noqa: BLE001
            continue
        tags = [c.text.strip() for c in it.findall("category") if c.text]
        out.append(item(key, cfg["label"], g("guid") or link, link, title, ts, g("description"), tags))
    return out


def _od_one(i):
    try:
        t = get(f"https://www.odaily.news/zh-CN/newsflash/{i}", retries=1)
    except urllib.error.HTTPError:
        return i, "404"
    except Exception:  # noqa: BLE001
        return i, None
    ti = re.search(r"<title>(.*?)</title>", t, re.S)
    dp = re.search(r'"datePublished"\s*:\s*"([^"]+)"', t)
    de = re.search(r'<meta name="description" content="([^"]*)"', t)
    if not (ti and dp):
        return i, None
    title = html.unescape(ti.group(1)).replace(" - Odaily", "").strip()
    return i, (title, parse_iso(dp.group(1)), html.unescape(de.group(1)) if de else "")


def src_odaily(key, cfg, st):
    """列表页取最新 id，再逐 id 抓详情。st['odaily_last_id'] 为增量进度；只推进到连续成功/404 处。"""
    page = get("https://www.odaily.news/zh-CN/newsflash")
    ids = [int(x) for x in re.findall(r"newsflash/(\d{5,})", page)]
    if not ids:
        raise RuntimeError("list page has no newsflash ids")
    latest = max(ids)
    last = st.get("odaily_last_id")
    if not last:  # 首次（播种）：回扫 seed_ids 个 id，约覆盖 12h
        lo = latest - cfg.get("seed_ids", cfg.get("first_run_ids", 60))
    else:
        lo = max(last + 1, latest - cfg.get("max_ids_per_run", 150))
    out, missing = [], []
    with cf.ThreadPoolExecutor(6) as ex:
        for i, v in ex.map(_od_one, range(lo, latest + 1)):
            if isinstance(v, tuple):
                out.append(item(key, cfg["label"], i, f"https://www.odaily.news/zh-CN/newsflash/{i}", v[0], v[1], v[2]))
            elif v is None:
                missing.append(i)
    st["odaily_last_id"] = (min(missing) - 1) if missing else latest
    return out


def src_panews(key, cfg, st):
    d = get(f"https://universal-api.panewslab.com/articles?type=NEWS&take={cfg.get('take', 50)}&skip=0", as_json=True)
    return [item(key, cfg["label"], x["id"], f"https://www.panewslab.com/zh/articles/{x['id']}", x.get("title") or "",
                 parse_iso(x["publishedAt"]), x.get("desc") or "") for x in d]


SOURCES = {"rss": src_rss, "odaily": src_odaily, "panews": src_panews}


# ---------------------------------------------------------------- watchlist
def _latest_csv(root, tracker, fname, max_age_days):
    best = None
    for p in glob.glob(os.path.join(root, tracker, "output", "*", fname)):
        m = os.path.getmtime(p)
        if time.time() - m <= max_age_days * 86400 and (best is None or m > best[0]):
            best = (m, p)
    return best[1] if best else None


def load_tokens(cfg, base_dir):
    """返回 (tokens, info)。auto_tokens 可选：root 为空/不存在时跳过，不报错。"""
    toks, info = {}, {"config": 0, "auto": {}, "auto_root": None, "auto_status": "disabled"}
    for t, v in cfg["tokens"].items():
        toks[t.upper()] = dict(ticker=t.upper(), names=v.get("names", []), weight=v.get("weight", 4), gecko=v.get("gecko"), origin="config")
    info["config"] = len(toks)
    a = cfg.get("auto_tokens", {})
    root = (os.environ.get("NEWS_DASH_TRACKER_ROOT") or a.get("root") or "").strip()
    if a.get("enabled") and root:
        root = os.path.expanduser(root if os.path.isabs(os.path.expanduser(root)) else os.path.join(base_dir, root))
        info["auto_root"] = root
        if not os.path.isdir(root):
            info["auto_status"] = "root 不存在，已跳过"
        else:
            excl = {x.upper() for x in a.get("exclude", [])}
            for tracker, fname in a.get("trackers", {}).items():
                p = _latest_csv(root, tracker, fname, a.get("max_age_days", 14))
                n = 0
                if p:
                    try:
                        for r in csv.DictReader(open(p, encoding="utf-8")):
                            t = (r.get("ticker") or r.get("token") or "").strip().upper()
                            if not t or t in excl or t in toks or not re.fullmatch(r"[A-Z0-9]{2,12}", t):
                                continue
                            nm = (r.get("name") or "").strip()
                            toks[t] = dict(ticker=t, names=[nm] if nm and nm.upper() != t and len(nm) >= 4 else [], weight=a.get("weight", 3),
                                           gecko=(r.get("coingecko_id") or "").strip() or None, origin=tracker)
                            n += 1
                    except Exception as e:  # noqa: BLE001
                        log(f"auto_tokens {p}: {e}")
                info["auto"][tracker] = n
            info["auto_status"] = "ok"
    elif a.get("enabled"):
        info["auto_status"] = "未配置 root，已跳过"
    amb = {x.upper() for x in cfg.get("ambiguous_tickers", [])}
    ctx = "|".join(re.escape(w) for w in cfg.get("context_words", []))
    for t, v in toks.items():
        v["ambiguous"] = t in amb or len(t) <= 2
        v["rx_t"] = re.compile(r"(?<![A-Za-z0-9])(\$?)" + re.escape(t) + r"(?![A-Za-z0-9])")
        v["rx_ctx"] = re.compile(r"(?<![A-Za-z0-9])" + re.escape(t) + r"\W{0,3}(?:" + ctx + r")|(?:" + ctx + r")\W{0,3}" + re.escape(t) + r"(?![A-Za-z0-9])", re.I) if ctx else None
        v["rx_n"] = [re.compile(re.escape(n) if CJK.search(n) else r"(?<![A-Za-z0-9])" + re.escape(n) + r"(?![A-Za-z0-9])",
                                re.I if n.lower() not in ("ether",) else 0) for n in v["names"]]
    return toks, info


def match_tokens(text, toks):
    hits = {}
    zh = bool(CJK.search(text))
    for t, v in toks.items():
        if any(rx.search(text) for rx in v["rx_n"]):
            hits[t] = "名称"
            continue
        m = v["rx_t"].search(text)
        if not m:
            continue
        if not v["ambiguous"]:
            hits[t] = "代码"
        elif m.group(1) == "$":
            hits[t] = "$代码"
        elif v["rx_ctx"] and v["rx_ctx"].search(text):
            hits[t] = "代码+上下文"
        elif zh:
            hits[t] = "代码(中文语境)"
    return hits


# ---------------------------------------------------------------- scoring
def compile_rules(cfg):
    def rx(en, zh):
        pats = [r"(?<![A-Za-z])" + p + r"(?![A-Za-z])" for p in en] + list(zh)
        return re.compile("|".join("(?:" + p + ")" for p in pats), re.I) if pats else None
    ev = [dict(e, rx=rx(e.get("en", []), e.get("zh", []))) for e in cfg["events"]]
    nz = [dict(n, rx=rx(n.get("en", []), n.get("zh", []))) for n in cfg["noise"]]
    return ev, nz


def score(it, toks, ev, nz, cfg):
    title, summ = it["title"], it["summary"]
    tags = " ".join(it.get("tags", []))
    sf = cfg.get("summary_factor", 0.5)
    th, sh = match_tokens(title, toks), match_tokens(summ, toks)
    tokens = list(th) + [t for t in sh if t not in th]
    tok_pts, reasons = 0.0, []
    if tokens:
        best = max(tokens, key=lambda t: (t in th, toks[t]["weight"]))
        w = toks[best]["weight"] * (1 if best in th else sf)
        tok_pts = w
        reasons.append("关注代币 %s(%s%s)+%g" % ("/".join(tokens[:4]), th.get(best) or sh.get(best), "" if best in th else ",摘要", w))
    evs = []
    for e in ev:
        if e["rx"] and e["rx"].search(title):
            evs.append((e, e["weight"], "标题"))
        elif e["rx"] and summ and e["rx"].search(summ):
            evs.append((e, e["weight"] * sf, "摘要"))
    evs.sort(key=lambda x: -x[1])
    ev_pts = 0.0
    if evs:
        ev_pts = evs[0][1] + (evs[1][1] * 0.3 if len(evs) > 1 else 0)
        reasons.append("事件 " + "、".join("%s(%g,%s)" % (e["label"], w, where) for e, w, where in evs[:3]))
    noise_pts = 0.0
    for n in nz:
        if n["rx"] and (n["rx"].search(title) or n["rx"].search(tags)):
            noise_pts += n["weight"]
            reasons.append("降权 %s(%s)" % (n["label"], n["weight"]))
    if re.search(r"breaking", tags, re.I):
        noise_pts += cfg.get("breaking_tag_bonus", 1)
        reasons.append("Breaking标签+%s" % cfg.get("breaking_tag_bonus", 1))
    s = tok_pts + ev_pts + noise_pts
    big = bool(evs) and bool(evs[0][0].get("big")) and evs[0][2] == "标题"
    it.update(score=round(s, 2), tokens=tokens, event=evs[0][0]["key"] if evs else None, event_label=evs[0][0]["label"] if evs else None,
              events=[e["key"] for e, _, _ in evs], big=big, reasons=reasons, tok_in_title=[t for t in tokens if t in th],
              sig=sorted(sig(title)), ents=sorted(entities(title, cfg)))
    return it


def is_hit(it, cfg):
    if it["big"] and it["score"] >= cfg["big_threshold"]:
        return True
    return bool(it["tokens"]) and it["score"] >= cfg["threshold"]


# ---------------------------------------------------------------- dedupe / cluster
def norm_title(t):
    t = re.sub(r"https?://\S+", "", t.lower())
    t = re.sub(r"[^\w\u4e00-\u9fff]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def tkey(t):
    return "t:" + hashlib.sha1(norm_title(t).encode()).hexdigest()[:16]


STOP = set("the a an of to in on for and or is are was be by with as at from after over its it this that new says say will has have".split())


def sig(t):
    n = norm_title(t)
    if CJK.search(n):
        s = re.sub(r"\s+", "", n)
        return {s[i:i + 2] for i in range(len(s) - 1)}
    return {w for w in n.split() if w not in STOP and len(w) > 1}


def entities(title, cfg):
    ents = {w.lower() for w in re.findall(r"(?<![A-Za-z0-9])([A-Z][A-Za-z0-9.]{1,}|[A-Z]{2,})(?![A-Za-z0-9])", title)
            if w.lower() not in STOP and w.lower() not in ("the", "us", "u.s.", "new")}
    low = title.lower()
    for concept, words in cfg.get("cluster_glossary", {}).items():
        for w in words:
            if (CJK.search(w) and w in low) or (not CJK.search(w) and re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", low)):
                ents.add("#" + concept)
                break
    return ents


def nums(t):
    out = set()
    for x in re.findall(r"(?<![\d.])\d[\d,]*(?:\.\d+)?", t):
        x = x.replace(",", "").rstrip(".")
        digits = x.replace(".", "").lstrip("0")
        if len(digits) >= 3 and not re.fullmatch(r"(19|20)\d\d", x):
            out.add(x)
    return out


def same_story(a, b, cfg):
    """改进的聚类：要求实体+事件+相似度三重验证，减少误合并"""
    if abs(a["ts"] - b["ts"]) > cfg["cluster_hours"] * 3600:
        return False
    
    ea, eb = set(a.get("ents") or []), set(b.get("ents") or [])
    shared = ea & eb
    proper_nouns = {e for e in shared if not e.startswith("#")}
    
    sa, sb = set(a.get("sig") or []), set(b.get("sig") or [])
    zh_a, zh_b = bool(CJK.search(a["title"])), bool(CJK.search(b["title"]))
    
    # 计算标题相似度
    if sa and sb:
        title_sim = len(sa & sb) / len(sa | sb)
    else:
        title_sim = 0
    
    # 规则1: 同事件类型 + 至少2个共同专有名词 + 相似度阈值
    if (a.get("event") and a.get("event") == b.get("event") and 
        len(proper_nouns) >= 2 and title_sim >= 0.3):
        return True
    
    # 规则2: 同语种 + 高相似度（提高阈值以减少误合并）
    if zh_a == zh_b and sa and sb and title_sim >= (0.5 if zh_a else 0.6):
        return True
    
    # 规则3: 特征数字 + 事件类型 + 共同实体 + 相似度
    common_nums = nums(a["title"]) & nums(b["title"])
    if (zh_a == zh_b and sa and sb and common_nums and 
        a.get("event") and a.get("event") == b.get("event") and
        len(proper_nouns) >= 1 and title_sim >= 0.25):
        return True
    
    # 规则4: 同代币 + 同事件（非价格波动）+ 高相似度
    ta, tb = set(a.get("tok_in_title") or []), set(b.get("tok_in_title") or [])
    if (ta & tb and a.get("event") and a.get("event") == b.get("event") and 
        a.get("event") not in ("price_move",) and title_sim >= 0.4):
        return True
    
    return False


# ---------------------------------------------------------------- prices
def binance_syms(cache_dir):
    p = os.path.join(cache_dir, "binance_symbols.json")
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < 86400:
        return set(json.load(open(p)))
    d = get("https://data-api.binance.vision/api/v3/exchangeInfo?permissions=SPOT", as_json=True, timeout=30)
    s = sorted({x["baseAsset"] for x in d["symbols"] if x["quoteAsset"] == "USDT" and x["status"] == "TRADING"})
    json.dump(s, open(p, "w"))
    return set(s)


def price_1h(t, tokinfo, syms, allow_gecko=True):
    """Binance 1m K 线(61 根)算 1h 涨跌；不在 Binance 时用 CoinGecko markets 兜底。失败返回 None（不编造）。"""
    try:
        if t in syms:
            k = get(f"https://data-api.binance.vision/api/v3/klines?symbol={t}USDT&interval=1m&limit=61", as_json=True, retries=1)
            if k:
                p0, p1 = float(k[0][1]), float(k[-1][4])
                return dict(price=p1, chg1h=round((p1 / p0 - 1) * 100, 3), src="Binance", at=int(time.time()))
        if allow_gecko and tokinfo.get("gecko"):
            d = get(f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids={tokinfo['gecko']}&price_change_percentage=1h",
                    as_json=True, retries=1)
            if d:
                c = d[0].get("price_change_percentage_1h_in_currency")
                return dict(price=d[0]["current_price"], chg1h=None if c is None else round(c, 3), src="CoinGecko", at=int(time.time()))
    except Exception as e:  # noqa: BLE001
        log(f"price {t}: {e}")
    return None
