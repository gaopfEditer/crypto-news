#!/usr/bin/env python3
"""burn_tracker: 代币销毁(一次性/定期/回购销毁)追踪 + 销毁前后价格表现 (stdlib only).

复用 ../unlock_tracker/unlock_tracker.py (Http缓存/取价/window_metrics/格式化) 和 ../delist_tracker/delist_tracker.py
(map_gecko)，只 import，不修改。

事件来源 (--sources):
  hyperliquid : Hyperliquid 援助基金(AF, 0xfefe…fe) 在窗口内的 HYPE 买单成交 (api.hyperliquid.xyz/info userFillsByTime)
                -> 一行"持续回购销毁(窗口累计)"，锚点=窗口起点
  pump        : pump.fun/pump-token 官方看板内嵌的每日 buyback 数据 (日桶) -> 一行"持续回购销毁(窗口累计)"
  panews      : universal-api.panewslab.com/articles?type=NEWS 分页回溯至窗口起点 (快讯全文摘要)
  odaily      : www.odaily.news/zh-CN/newsflash/<id> 逐 ID 扫描 (二分定位窗口起点 ID; 标题+摘要+datePublished; 索引缓存)
  extra       : --extra-events CSV (主要的可靠输入: 链上时间、tx、官方金额、单一来源标记、即将销毁、手工剔除)
新闻只做候选: 含"销毁/burn"且能解析出"数量+枚+代码"的已完成销毁才进入主表; 将来时(将/拟/计划/就绪)→upcoming;
已有连接器覆盖的持续计划(HYPE/PUMP)的日报→other_events。窗口外参考: 窗口起点前 --ref-hours 内执行的销毁也计算指标并标注。
"""
import argparse, concurrent.futures as cf, csv, datetime as dt, html as htmlmod, json, os, re, sys, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "unlock_tracker"))
sys.path.insert(0, os.path.join(HERE, "..", "delist_tracker"))
import unlock_tracker as ut  # noqa: E402
import delist_tracker as dlt  # noqa: E402
from unlock_tracker import TZ8, NA, H, DAY, fmt_ts, fp, fmt_usd, csvv, pc, at  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) burn_tracker/1.0", "Content-Type": "application/json"}
ALL_SRC = ["hyperliquid", "pump", "panews", "odaily"]
HL_AF = "0xfefefefefefefefefefefefefefefefefefefefe"
PROGRAM_SYMS = {"hyperliquid": ("HYPE", "hyperliquid"), "pump": ("PUMP", "pump-fun")}
BURN_RE = re.compile(r"销毁|burn", re.I)
NONCRYPTO_RE = re.compile(r"股票|股份|国债|美债|逆回购|优先股|STRC")
FUTURE_RE = re.compile(r"将(?:再|全部|永久|于|在)?\S{0,6}销毁|拟销毁|计划销毁|提议|提案|投票|即将|待销毁|已就绪|准备就绪|可触发|或考虑|考虑")
DONE_RE = re.compile(r"已(?:完成)?(?:永久)?销毁|完成(?:永久)?销毁|销毁了|已永久|回购并销毁|买入并销毁|并销毁")
AMT_RE = re.compile(r"([\d][\d,]*(?:\.\d+)?)\s*(万|亿)?\s*枚\s*\$?([A-Za-z][A-Za-z0-9]{1,11})")
USD_RE = [re.compile(r"价值(?:约)?\s*([\d][\d,]*(?:\.\d+)?)\s*(万|亿)?\s*美元"), re.compile(r"([\d][\d,]*(?:\.\d+)?)\s*(万|亿)?\s*美元用于回购")]
CTX_RE = re.compile(r"上线|上架|下架|解锁|Coinbase|Binance|币安|Upbit|Robinhood|ETF|空投|融资|黑客|攻击|存入|转入|抛售|增持")
DIGEST_RE = re.compile(r"早讯|午讯|晚讯|今日热议|日报|周报|一周|要闻|盘点|回顾")
MULT = {None: 1, "": 1, "万": 1e4, "亿": 1e8}


def log(*a):
    print("[burn_tracker]", *a, file=sys.stderr, flush=True)


def num(s, unit):
    return float(s.replace(",", "")) * MULT[unit]


def T(s):
    s = s.strip()
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(dt.datetime.strptime(s, f).replace(tzinfo=TZ8).timestamp())
        except ValueError:
            pass
    raise ValueError(f"bad time {s!r} (want YYYY-MM-DD HH:MM, UTC+8)")


def iso_ts(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def mk(symbol, ts, **kw):
    e = dict(symbol=symbol.upper().lstrip("$"), ts=ts, gecko_id=None, type=None, ts_basis=None, ann=None, amount=None, usd=None, usd_basis=None,
             tx=None, single_source=None, sources=[], outlets=set(), flags=[], status="done", origin=None, note=None, title=None)
    e.update(kw)
    return e


def post_json(http, url, body, cache_name, ttl):
    path = http._path(cache_name)
    if not http.refresh and os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl:
        return json.load(open(path, encoding="utf-8"))
    err = None
    for i in range(4):
        try:
            req = urllib.request.Request(url, json.dumps(body).encode(), UA)
            d = json.load(urllib.request.urlopen(req, timeout=60))
            json.dump(d, open(path, "w", encoding="utf-8"))
            return d
        except Exception as ex:  # noqa: BLE001
            err = ex
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"POST {url} failed: {err}")


# ---------------------------------------------------------------- programme connectors
def src_hyperliquid(http, lo, hi, now_ts, meta):
    end = min(hi, now_ts)
    fills, t = [], lo * 1000
    for page in range(100):
        d = post_json(http, "https://api.hyperliquid.xyz/info", {"type": "userFillsByTime", "user": HL_AF, "startTime": t, "endTime": end * 1000, "aggregateByTime": True},
                      f"hl_af_fills_{t}_{end}.json", ttl=(10 ** 9 if end < time.time() - 3600 else 600))
        if not d:
            break
        fills += d
        t = max(f["time"] for f in d) + 1
        if len(d) < 2000:
            break
    seen, buys = set(), []
    for f in fills:
        if f.get("coin") == "@107" and f.get("side") == "B" and lo * 1000 <= f["time"] <= end * 1000 and (f["tid"], f["time"]) not in seen:
            seen.add((f["tid"], f["time"]))
            buys.append(f)
    if not buys:
        return [], []
    amt = sum(float(f["sz"]) for f in buys)
    usd = sum(float(f["sz"]) * float(f["px"]) for f in buys)
    meta["sources"]["hyperliquid"] = {"af_buy_fills": len(buys), "hype": round(amt, 4), "usd": round(usd, 2)}
    daily = {}
    for f in buys:
        k = fmt_ts(f["time"] // 1000)[:10]
        a = daily.setdefault(k, [0.0, 0.0])
        a[0] += float(f["sz"])
        a[1] += float(f["sz"]) * float(f["px"])
    side = [dict(type="programme_daily", token="HYPE", time_utc8=k, amount=v[0], usd_value=v[1], detail="援助基金当日(UTC+8)买入HYPE合计(视同销毁)",
                 source_url="https://api.hyperliquid.xyz/info userFillsByTime") for k, v in sorted(daily.items())]
    e = mk("HYPE", lo, gecko_id="hyperliquid", type="持续回购销毁(窗口累计)", ts_basis="窗口起点锚定(援助基金窗口内连续买入)", amount=amt, usd=usd,
           usd_basis="援助基金实际成交额(Hyperliquid API userFillsByTime 窗口内买单合计)", tx=f"HyperCore援助基金地址 {HL_AF}",
           single_source="否(链上成交)", origin="hyperliquid", sources=["https://api.hyperliquid.xyz/info (userFillsByTime)"],
           ann=f"{len(daily)}天有成交; 首笔 {fmt_ts(min(f['time'] for f in buys) // 1000)} 末笔 {fmt_ts(max(f['time'] for f in buys) // 1000)}")
    return [e], side


def src_pump(http, lo, hi, now_ts, meta):
    t = http.get("https://pump.fun/pump-token", "pump_token_page.html", ttl=(10 ** 9 if min(hi, now_ts) < time.time() - 2 * DAY else 3600), as_json=False).replace('\\"', '"')
    rows = {}
    for m in re.finditer(r'\{"date":"(\d{4}-\d\d-\d\d)","stableRevenue":[^{}]*?"buybacksUsd":([\d.]+)[^{}]*?"pumpTokensBought":([\d.]+)[^{}]*?"cumulativeUsd":([\d.]+)\}', t):
        rows[m.group(1)] = (float(m.group(2)), float(m.group(3)))
    d0, d1 = fmt_ts(lo)[:10], fmt_ts(min(hi, now_ts))[:10]
    days = [k for k in sorted(rows) if d0 <= k <= d1]
    meta["sources"]["pump"] = {"days_on_page": len(rows), "days_in_window": days, "last_day_on_page": max(rows) if rows else None}
    if not days:
        return [], []
    amt, usd = sum(rows[k][1] for k in days), sum(rows[k][0] for k in days)
    side = [dict(type="programme_daily", token="PUMP", time_utc8=k, amount=rows[k][1], usd_value=rows[k][0], detail="官方看板日桶 buybacksUsd / pumpTokensBought",
                 source_url="https://pump.fun/pump-token") for k in days]
    partial = " (末日为部分日)" if d1 == fmt_ts(now_ts)[:10] else ""
    e = mk("PUMP", lo, gecko_id="pump-fun", type="持续回购销毁(窗口累计)", ts_basis="窗口起点锚定(官方日度回购, 看板日桶" + partial + ")", amount=amt, usd=usd,
           usd_basis=f"官方看板buybacksUsd合计({days[0][5:]}~{days[-1][5:]})", tx="N/A(官方看板未给tx)", single_source="官方看板", origin="pump",
           sources=["https://pump.fun/pump-token"], ann="官方看板每日更新")
    return [e], side


# ---------------------------------------------------------------- news
def news_panews(http, lo, now_ts, meta):
    items, skip = [], 0
    while skip < 5000:
        d = http.get(f"https://universal-api.panewslab.com/articles?type=NEWS&take=100&skip={skip}", f"panews_news_{skip}.json", ttl=20 * 60)
        if not d:
            break
        for x in d:
            ts = iso_ts(x["publishedAt"])
            items.append(dict(outlet="PANews", ts=ts, title=x.get("title") or "", desc=x.get("desc") or "",
                              url=f"https://www.panewslab.com/zh/articles/{x['id']}"))
        skip += 100
        if iso_ts(d[-1]["publishedAt"]) < lo - DAY:
            break
    meta["sources"]["panews"] = {"items": len(items), "oldest": fmt_ts(min(i["ts"] for i in items)) if items else None}
    return items


def _od_fetch(i):
    u = f"https://www.odaily.news/zh-CN/newsflash/{i}"
    for a in range(3):
        try:
            t = urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=25).read().decode("utf-8", "ignore")
            ti = re.search(r"<title>(.*?)</title>", t, re.S)
            dp = re.search(r'"datePublished"\s*:\s*"([^"]+)"', t)
            de = re.search(r'<meta name="description" content="([^"]*)"', t)
            if not (ti and dp):
                return i, None
            return i, [htmlmod.unescape(ti.group(1)).replace(" - Odaily", "").strip(), dp.group(1), htmlmod.unescape(de.group(1)) if de else ""]
        except urllib.error.HTTPError as ex:
            if ex.code == 404:
                return i, "404"
            time.sleep(2 * (a + 1))
        except Exception:  # noqa: BLE001
            time.sleep(2 * (a + 1))
    return i, "ERR"


def news_odaily(http, lo, now_ts, meta, threads=12):
    idx_path = os.path.join(http.cache_dir, "..", "odaily_index.json")
    idx = json.load(open(idx_path, encoding="utf-8")) if os.path.exists(idx_path) and not http.refresh else {}
    page = http.get("https://www.odaily.news/zh-CN/newsflash", "odaily_newsflash_list.html", ttl=600, as_json=False)
    ids = [int(x) for x in re.findall(r"newsflash/(\d{5,})", page)]
    if not ids:
        raise RuntimeError("odaily: no newsflash ids on list page")
    latest = max(ids)

    def get(i):
        k = str(i)
        if k in idx and idx[k] not in ("ERR",):
            return idx[k]
        _, v = _od_fetch(i)
        if v != "ERR" and not (v == "404" and i > latest - 50):
            idx[k] = v
        return v

    def date_near(i):
        for j in range(i, i + 15):
            v = get(j)
            if isinstance(v, list):
                return iso_ts(v[1])
        return None
    a, b = latest - 4000, latest
    while date_near(a) is not None and date_near(a) > lo - DAY:
        a -= 4000
    while b - a > 20:  # binary search: first id published >= lo - 1 day
        mid = (a + b) // 2
        dn = date_near(mid)
        if dn is None or dn < lo - DAY:
            a = mid
        else:
            b = mid
    todo = [i for i in range(a, latest + 1) if str(i) not in idx or idx[str(i)] == "ERR"]
    log(f"odaily: scanning ids {a}..{latest} ({len(todo)} uncached)")
    with cf.ThreadPoolExecutor(threads) as ex:
        for i, v in ex.map(_od_fetch, todo):
            if v != "ERR" and not (v == "404" and i > latest - 50):
                idx[str(i)] = v
    json.dump(idx, open(idx_path, "w", encoding="utf-8"), ensure_ascii=False)
    items = []
    for i in range(a, latest + 1):
        v = idx.get(str(i))
        if isinstance(v, list):
            items.append(dict(outlet="Odaily", ts=iso_ts(v[1]), title=v[0], desc=v[2] or "", url=f"https://www.odaily.news/zh-CN/newsflash/{i}"))
    meta["sources"]["odaily"] = {"id_range": [a, latest], "items": len(items), "errors": sum(1 for i in range(a, latest + 1) if idx.get(str(i)) in (None, "ERR"))}
    return items


def news_urls(http, urls, meta):
    items = []
    for u in urls:
        try:
            h = http.get(u, "news_" + re.sub(r"\W", "_", u)[-80:], ttl=7 * DAY, as_json=False)
        except Exception as ex:  # noqa: BLE001
            meta["errors"].append(f"news-url {u}: {ex}")
            continue
        dp = re.search(r'"datePublished"\s*:\s*"([^"]+)"', h) or re.search(r'article:published_time"\s+content="([^"]+)"', h)
        ti = re.search(r"<title>(.*?)</title>", h, re.S)
        if not dp:
            meta["errors"].append(f"news-url {u}: 无发布时间，跳过")
            continue
        items.append(dict(outlet="news-url", ts=iso_ts(dp.group(1)), title=htmlmod.unescape(ti.group(1)) if ti else "", desc=dlt.html_text(h)[:3000], url=u))
    return items


def parse_news(items, lo, hi, now_ts, program_syms):
    """-> (burn events, upcoming events, programme reports, unparsed candidates)"""
    done, upcoming, reports, cands = [], [], [], []
    for it in items:
        if not (lo - 3 * DAY <= it["ts"] <= now_ts):
            continue
        text = (it["title"] + "。" + it["desc"])
        if not BURN_RE.search(text) or NONCRYPTO_RE.search(it["title"]):
            continue
        if DIGEST_RE.search(it["title"]):  # 早/午/晚讯、热议等汇总帖: 只记候选, 不解析(会与原快讯重复且时态混杂)
            cands.append(dict(outlet=it["outlet"], time_utc8=fmt_ts(it["ts"]), title=it["title"], url=it["url"], status="digest(汇总帖,跳过)"))
            continue
        sent = [s for s in re.split(r"[。；;！!\n]", text) if BURN_RE.search(s)]
        hit = None
        for s in sent:
            for m in AMT_RE.finditer(s):
                pre = s[max(0, m.start() - 6):m.start()]
                hit = (m, s, "累计" in pre or "超过" in pre or "超" in pre[-2:])
                if not hit[2]:
                    break
            if hit and not hit[2]:
                break
        usd = None
        for rx in USD_RE:
            mu = rx.search(text)
            if mu:
                usd = num(mu.group(1), mu.group(2))
                break
        fut = None
        cand = dict(outlet=it["outlet"], time_utc8=fmt_ts(it["ts"]), title=it["title"], url=it["url"])
        if not hit:
            cands.append(dict(cand, status="unparsed(无'数量+枚+代码')"))
            continue
        m, s, cumul = hit
        fut = (bool(FUTURE_RE.search(it["title"])) and not DONE_RE.search(it["title"])) or (bool(FUTURE_RE.search(s)) and not DONE_RE.search(s))
        sym, amt = m.group(3).upper(), num(m.group(1), m.group(2))
        cand.update(token=sym, amount=amt, usd=usd)
        if sym in program_syms:
            reports.append(dict(cand, status="programme_report(连接器已覆盖)", cumulative=cumul))
        elif cumul:
            cands.append(dict(cand, status="cumulative_only(累计值,非单次)"))
        elif fut:
            upcoming.append(dict(cand, status="upcoming(news)"))
        else:
            e = mk(sym, it["ts"], type="一次性/定期销毁(新闻)", ts_basis="新闻发布时间(无链上时间)", ann=fmt_ts(it["ts"]), amount=amt, usd=usd,
                   usd_basis="新闻所载金额" if usd else None, origin="news", sources=[it["url"]], outlets={it["outlet"]}, title=it["title"])
            done.append(e)
            cands.append(dict(cand, status="burn_event"))
    # merge duplicates (same token, amount within 3%, within 72h)
    merged = []
    for e in sorted(done, key=lambda e: e["ts"]):
        for m in merged:
            if m["symbol"] == e["symbol"] and abs(m["amount"] - e["amount"]) <= 0.03 * m["amount"] and abs(m["ts"] - e["ts"]) <= 3 * DAY:
                m["sources"] += e["sources"]
                m["outlets"] |= e["outlets"]
                m["usd"] = m["usd"] or e["usd"]
                break
        else:
            merged.append(e)
    for e in merged:
        e["single_source"] = "是(仅" + "/".join(sorted(e["outlets"])) + ")" if len(e["outlets"]) == 1 else "否(" + "/".join(sorted(e["outlets"])) + ")"
    return merged, upcoming, reports, cands


# ---------------------------------------------------------------- extra events
EXTRA_FIELDS = ["kind", "symbol", "coingecko_id", "type", "burn_time_utc8", "burn_time_basis", "announce_utc8", "amount", "usd_value", "usd_basis",
                "tx", "single_source", "source_url", "note"]


def read_extra(path):
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if not (r.get("symbol") or "").strip() or (r.get("kind") or "").startswith("#"):
                continue
            rows.append({k: (r.get(k) or "").strip() for k in EXTRA_FIELDS})
    return rows


def fnum(s):
    try:
        return float(s.replace(",", "")) if s else None
    except ValueError:
        return None


def apply_extra(events, extra, side):
    for r in extra:
        sym, kind, amt = r["symbol"].upper(), (r["kind"] or "burn").lower(), fnum(r["amount"])
        match = [e for e in events if e["symbol"] == sym and (amt is None or e["amount"] is None or abs(e["amount"] - amt) <= 0.03 * max(amt, 1))]
        if kind == "exclude":
            for e in match:
                e["status"] = "excluded"
                e["note"] = r["note"] or "extra: exclude"
            continue
        if kind in ("upcoming", "context"):
            side.append(dict(type=("upcoming(extra)" if kind == "upcoming" else "context(extra)"), token=sym, time_utc8=r["burn_time_utc8"] or None,
                             amount=amt, usd_value=fnum(r["usd_value"]), detail=(r["type"] + " " + r["note"]).strip(), source_url=r["source_url"]))
            continue
        e = match[0] if match else None
        if e is None:
            if not r["burn_time_utc8"]:
                continue
            e = mk(sym, T(r["burn_time_utc8"]), origin="extra", outlets=set())
            events.append(e)
        if r["burn_time_utc8"]:
            nt = T(r["burn_time_utc8"])
            if e["origin"] != "extra" and nt != e["ts"]:
                e["flags"].append(f"时间被extra覆盖(原{fmt_ts(e['ts'])})")
            e["ts"] = nt
        for k_e, k_r, conv in (("gecko_id", "coingecko_id", str), ("type", "type", str), ("ts_basis", "burn_time_basis", str), ("ann", "announce_utc8", str),
                               ("amount", "amount", fnum), ("usd", "usd_value", fnum), ("usd_basis", "usd_basis", str), ("tx", "tx", str),
                               ("single_source", "single_source", str), ("note", "note", str)):
            if r[k_r]:
                e[k_e] = conv(r[k_r])
        if r["source_url"]:
            e["sources"] = [u.strip() for u in r["source_url"].split(";") if u.strip()] + [u for u in e["sources"] if u not in r["source_url"]]
        e["flags"].append("extra")


# ---------------------------------------------------------------- pricing
def choose_series(http, e, ts, bmap, start_px, now_ts):
    pair = bmap.get(e["symbol"])
    pts = ut.binance_series(http, pair, start_px, now_ts) if pair else []
    if pts and at(pts, ts - 7 * DAY) is not None:
        return pts, f"Binance {pair} 现货1h"
    note = f"(Binance {pair} 首根K线 {fmt_ts(pts[0][0])}, 不足7天)" if pts else ""
    if e.get("gecko_id"):
        days = min(90, int((time.time() - start_px) // DAY) + 2)
        d = http.get(f"https://api.coingecko.com/api/v3/coins/{e['gecko_id']}/market_chart?vs_currency=usd&days={days}", f"cg_chart_{e['gecko_id']}_{days}d.json", ttl=20 * 60)
        cg = [(int(p[0] // 1000), p[1]) for p in d.get("prices", []) if p[1] and p[0] // 1000 <= now_ts]
        if cg:
            return cg, f"CoinGecko {e['gecko_id']} 小时" + note
    return pts, (f"Binance {pair} 现货1h" + note) if pts else None


def supply(http, gid, cache):
    if gid not in cache:
        try:
            md = http.get(f"https://api.coingecko.com/api/v3/coins/{gid}?localization=false&tickers=false&community_data=false&developer_data=false",
                          f"cg_coin_{gid}.json", ttl=6 * H)["market_data"]
            cache[gid] = (md.get("circulating_supply"), md.get("total_supply"))
        except Exception:  # noqa: BLE001
            cache[gid] = (None, None)
    return cache[gid]


# ---------------------------------------------------------------- main
def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="代币销毁追踪: 窗口内已执行的销毁/回购销毁 + 销毁前7天逐日/销毁后表现 (含BTC基准)")
    ap.add_argument("--start", help="窗口开始 YYYY-MM-DD (UTC+8)，默认 now-7天")
    ap.add_argument("--end", help="窗口结束 YYYY-MM-DD (UTC+8，含当日)，不超过 now")
    ap.add_argument("--now", help="固定'现在' 'YYYY-MM-DD HH:MM' (UTC+8) 用于复现")
    ap.add_argument("--sources", default=",".join(ALL_SRC), help="逗号分隔: " + ",".join(ALL_SRC) + " (extra 由 --extra-events 决定)")
    ap.add_argument("--exchanges", help=argparse.SUPPRESS)  # 兼容其他 tracker 的写法; 本工具忽略
    ap.add_argument("--extra-events", help="补充/覆盖CSV (链上时间/tx/官方金额/即将销毁/剔除, 见 extra_events_example.csv)")
    ap.add_argument("--news-url", action="append", default=[], help="额外新闻/公告页面URL(需含 datePublished)，可多次")
    ap.add_argument("--id-map", action="append", default=[], help="手工指定 CoinGecko id: SYM=gecko-id，可多次")
    ap.add_argument("--min-usd", type=float, default=1e6, help="入表阈值: 销毁价值(美元) ≥ 此值，或")
    ap.add_argument("--min-pct", type=float, default=0.1, help="入表阈值: 占流通 ≥ 此百分比 (默认0.1)")
    ap.add_argument("--ref-hours", type=float, default=24, help="窗口起点前多少小时内执行的销毁作为'窗口外参考'计算指标 (0=不计算)")
    ap.add_argument("--max-context", type=int, default=40, help="每个代币最多列出多少条同期相关新闻")
    ap.add_argument("--odaily-threads", type=int, default=12)
    ap.add_argument("--out", help="输出目录，默认 ./output/<start>_<end>/")
    ap.add_argument("--cache", default=os.path.join(HERE, "cache"))
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    return ap.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    real_now = int(time.time())
    now_ts = T(a.now) if a.now else real_now
    lo = T(a.start) if a.start else now_ts - 7 * DAY
    hi = min(T(a.end) + DAY - 1 if a.end else now_ts, now_ts)
    out_dir = a.out or os.path.join(os.getcwd(), "output", f"{fmt_ts(lo)[:10]}_{fmt_ts(hi)[:10]}")
    os.makedirs(out_dir, exist_ok=True)
    http = ut.Http(a.cache, a.refresh)
    meta = {"window_utc8": [fmt_ts(lo), fmt_ts(hi)], "now_utc8": fmt_ts(now_ts), "generated_utc8": fmt_ts(real_now), "args": vars(a),
            "sources": {}, "errors": [], "unmapped": [], "counts": {}}
    srcs = [s.strip().lower() for s in a.sources.split(",") if s.strip()]
    events, side, items = [], [], []
    for s in srcs:
        try:
            if s in ("hyperliquid", "pump"):
                ev, sd = (src_hyperliquid if s == "hyperliquid" else src_pump)(http, lo, hi, now_ts, meta)
                events += ev
                side += sd
            elif s == "panews":
                items += news_panews(http, lo, now_ts, meta)
            elif s == "odaily":
                items += news_odaily(http, lo, now_ts, meta, a.odaily_threads)
            else:
                meta["errors"].append(f"unknown source {s}")
        except Exception as ex:  # noqa: BLE001
            meta["errors"].append(f"{s}: {type(ex).__name__}: {ex}")
            log(f"{s} failed: {ex}")
    if a.news_url:
        items += news_urls(http, a.news_url, meta)
    prog = {PROGRAM_SYMS[s][0] for s in srcs if s in PROGRAM_SYMS}
    nev, nup, nrep, cands = parse_news(items, lo, hi, now_ts, prog)
    events += nev
    for r in nrep:
        side.append(dict(type="programme_report(news)", token=r["token"], time_utc8=r["time_utc8"], amount=r["amount"], usd_value=r.get("usd"),
                         detail=("累计值 " if r["cumulative"] else "") + f"[{r['outlet']}] {r['title']}", source_url=r["url"]))
    if a.extra_events:
        extra = read_extra(a.extra_events)
        apply_extra(events, extra, side)
        meta["sources"]["extra"] = {"file": a.extra_events, "rows": len(extra)}
    for u in nup:  # 将来时新闻: 若已有同币同数量的已执行事件(±3天) -> 预告(已执行)
        done_match = [e for e in events if e["symbol"] == u["token"] and e["amount"] and abs(e["amount"] - u["amount"]) <= 0.03 * e["amount"]
                      and abs(e["ts"] - T(u["time_utc8"])) <= 3 * DAY]
        side.append(dict(type="pre_announcement(已执行)" if done_match else "upcoming(news)", token=u["token"], time_utc8=u["time_utc8"], amount=u["amount"],
                         usd_value=u.get("usd"), detail=f"[{u['outlet']}] {u['title']}", source_url=u["url"]))
    id_map = dict(x.split("=", 1) for x in a.id_map if "=" in x)
    for e in events:
        if not e["gecko_id"]:
            e["gecko_id"], how = dlt.map_gecko(http, dict(symbol=e["symbol"], name=None, gecko_id=None), id_map)
            if not e["gecko_id"]:
                meta["unmapped"].append(f"{e['symbol']}: {how or '无CoinGecko匹配'}")
            elif how and how.startswith("search(symbol-only"):
                e["flags"].append("gecko_by_symbol")
    ref_lo = lo - int(a.ref_hours * H)
    calc = [e for e in events if e["status"] == "done" and ref_lo <= e["ts"] <= hi]
    for e in events:
        if e not in calc:
            why = "excluded(extra)" if e["status"] == "excluded" else ("outside_window" if e["ts"] < ref_lo or e["ts"] > hi else e["status"])
            side.append(dict(type=why, token=e["symbol"], time_utc8=fmt_ts(e["ts"]), amount=e["amount"], usd_value=e["usd"], detail=(e.get("note") or e.get("title") or e["type"] or ""),
                             source_url="; ".join(e["sources"])))
    bmap = ut.binance_symbols(http)
    start_px = min([e["ts"] for e in calc] + [now_ts]) - 7 * DAY - 2 * H
    btc = ut.binance_series(http, "BTCUSDT", start_px, now_ts)
    sup, rows, daily = {}, [], []
    for e in sorted(calc, key=lambda e: (e["ts"] < lo, e["ts"], e["symbol"])):
        ts = e["ts"]
        pts, psrc = choose_series(http, e, ts, bmap, start_px, now_ts)
        m = ut.window_metrics(pts, ts, now_ts) if pts else None
        b = ut.window_metrics(btc, ts, now_ts)
        if m and (m["t_now"] is None or m["t_now"] <= ts):
            m = dict(m, post=None, lo=None, hi=None, p_now=None)
        g = (lambda k: m[k]) if m else (lambda k: None)
        cs, tsup = supply(http, e["gecko_id"], sup) if e["gecko_id"] else (None, None)
        usd, ub = e["usd"], e["usd_basis"]
        if usd is None and e["amount"] is not None and g("p_u") is not None:
            usd, ub = e["amount"] * g("p_u"), "数量×销毁时刻价格(" + (psrc or "") + ")"
        noncirc = "非流通" in (e["type"] or "")
        pct_c = (e["amount"] / cs * 100) if (cs and e["amount"] is not None and not noncirc) else None
        pct_t = (e["amount"] / (tsup + e["amount"]) * 100) if (tsup and e["amount"] is not None) else None
        inw = lo <= ts <= hi
        qualifies = (usd is not None and usd >= a.min_usd) or (pct_c is not None and pct_c >= a.min_pct) or (noncirc and pct_t is not None and pct_t >= a.min_pct)
        flags = list(e["flags"])
        if not pts:
            flags.append("no_price_data")
        if not qualifies:
            side.append(dict(type="below_threshold", token=e["symbol"], time_utc8=fmt_ts(ts), amount=e["amount"], usd_value=usd,
                             detail=f"价值{fmt_usd(usd)} 占流通{fp(pct_c, 3)} < 阈值 {fmt_usd(a.min_usd)}/{a.min_pct}%", source_url="; ".join(e["sources"])))
            continue
        r = dict(token=e["symbol"], coingecko_id=e["gecko_id"], type=e["type"], in_window="是" if inw else "否(窗口外参考)", burn_utc8=fmt_ts(ts),
                 burn_time_basis=e["ts_basis"], announce_utc8=e["ann"], amount=e["amount"], usd_value=usd, usd_basis=ub, pct_circulating=pct_c, pct_total_preburn=pct_t,
                 supply_basis="CoinGecko /coins 当前快照; 占总量=数量/(当前总量+数量)", tx=e["tx"], price_source=psrc,
                 price_7d_before=g("p_7"), price_at_burn=g("p_u"), price_now=g("p_now"), price_now_time=fmt_ts(m["t_now"]) if m and m["t_now"] else None,
                 pre7d_pct=g("pre7"), post_pct=g("post"), post_low_pct=g("lo"), post_high_pct=g("hi"), btc_pre7d_pct=b["pre7"], btc_post_pct=b["post"],
                 post_hours=round((now_ts - ts) / H, 1), single_source=e["single_source"], flags="; ".join(flags) or None, source_url=" ; ".join(e["sources"]))
        rows.append(r)
        for lab, mm in ((e["symbol"], m), ("BTC", b)):
            dr = dict(token=e["symbol"], series=lab, burn_utc8=fmt_ts(ts), in_window=r["in_window"], D7_start_utc8=fmt_ts(ts - 7 * DAY))
            tot = 1.0
            for k in range(7, 0, -1):
                v = mm["daily"][f"D-{k}"] if mm else None
                dr[f"D-{k}"] = v
                tot = tot * (1 + v / 100) if (v is not None and tot is not None) else None
            dr["total_7d"] = (tot - 1) * 100 if tot is not None else None
            daily.append(dr)
    # context news for table tokens (listings, unlocks, whale deposits...)
    toks = {r["token"]: T(r["burn_utc8"]) for r in rows}
    ctx_n = {}
    for it in sorted(items, key=lambda x: x["ts"]):
        for sym, ts in toks.items():
            if ctx_n.get(sym, 0) >= a.max_context:
                continue
            if ts - 7 * DAY <= it["ts"] <= now_ts and re.search(rf"(?<![A-Za-z]){re.escape(sym)}(?![A-Za-z])", it["title"]) and CTX_RE.search(it["title"]) and not BURN_RE.search(it["title"]):
                ctx_n[sym] = ctx_n.get(sym, 0) + 1
                side.append(dict(type="context_news", token=sym, time_utc8=fmt_ts(it["ts"]), amount=None, usd_value=None, detail=f"[{it['outlet']}] {it['title']}", source_url=it["url"]))
    meta["counts"] = {"news_items": len(items), "news_burn_candidates": len(cands), "events": len(events), "rows": len(rows),
                      "in_window_rows": sum(1 for r in rows if r["in_window"] == "是")}
    log("counts", meta["counts"])

    def wcsv(name, data, fields=None):
        with open(os.path.join(out_dir, name), "w", newline="", encoding="utf-8") as f:
            if data or fields:
                w = csv.DictWriter(f, fieldnames=fields or list(data[0].keys()), extrasaction="ignore")
                w.writeheader()
                w.writerows([{k: csvv(v) for k, v in r.items()} for r in data])
    wcsv("burns.csv", rows, None if rows else ["token"])
    wcsv("pre7d_daily.csv", daily, None if daily else ["token"])
    sf = ["type", "token", "time_utc8", "amount", "usd_value", "detail", "source_url"]
    wcsv("other_events.csv", side, sf)
    wcsv("news_candidates.csv", cands, ["outlet", "time_utc8", "status", "token", "amount", "usd", "title", "url"])
    md = render_md(rows, daily, side, meta, a)
    open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8").write(md)
    json.dump(meta, open(os.path.join(out_dir, "run_meta.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    if not a.quiet:
        print(md)
    log(f"outputs written to {out_dir}")
    return rows, daily, meta


HDR1 = "| 代币 | 类型 | 销毁时间 | 销毁数量/价值 | 占流通 | 销毁前7天 | 销毁至今 | 销毁后最低/最高 | 同期BTC 前7天/至今 | 单一来源 |"
SEP1 = "|---|---|---|---|---|---|---|---|---|---|"


def render_md(rows, daily, side, meta, a):
    inw = [r for r in rows if r["in_window"] == "是"]
    ref = [r for r in rows if r["in_window"] != "是"]
    L = [f"# 代币销毁追踪 {meta['window_utc8'][0]} ~ {meta['window_utc8'][1]} (UTC+8)\n",
         f"生成 {meta['generated_utc8']}；价格截至 {meta['now_utc8']}。入表阈值：价值≥{fmt_usd(a.min_usd)} 或 占流通≥{a.min_pct}%。"
         f"窗口内 {len(inw)} 项；窗口外参考(起点前{a.ref_hours:g}h内) {len(ref)} 项。持续回购计划按窗口累计一行、锚点=窗口起点。\n",
         "## 表1 销毁与价格表现\n", HDR1, SEP1]

    def line(r):
        return (f"| {r['token']} | {r['type']} | {r['burn_utc8'][5:]} | {ut.fmt_amt(r['amount'])} / {fmt_usd(r['usd_value'])} | {fp(r['pct_circulating'], 3)} | "
                f"{fp(r['pre7d_pct'])} | {fp(r['post_pct'])} | {fp(r['post_low_pct'])} / {fp(r['post_high_pct'])} | {fp(r['btc_pre7d_pct'])} / {fp(r['btc_post_pct'])} | {r['single_source'] or NA} |")
    L += [line(r) for r in inw] or ["| (无) |||||||||"]
    if ref:
        L += ["\n### 窗口外参考（起点前执行，不计入窗口）\n", HDR1, SEP1] + [line(r) for r in ref]
    L.append("\n口径：销毁时间=链上tx时间(有则用)，否则公告/新闻时间；价值优先官方/链上成交额，否则数量×销毁时刻价格；占流通=数量/CoinGecko当前流通(非流通储备的销毁记N/A，见占总量列)；"
             "价格优先 Binance 现货1h(需覆盖销毁前7天)，否则 CoinGecko 小时价；缺失记 N/A。\n")
    L += ["## 表2 销毁前7天逐日涨跌（24h桶，锚定销毁时间）\n", "| 代币 | 销毁时间 | D-7 | D-6 | D-5 | D-4 | D-3 | D-2 | D-1 | 7天合计 | BTC合计 |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    bt = {(d["token"], d["burn_utc8"]): d for d in daily if d["series"] == "BTC"}
    for d in daily:
        if d["series"] != "BTC":
            L.append(f"| {d['token']}{'' if d['in_window'] == '是' else '(参考)'} | {d['burn_utc8'][5:]} | " + " | ".join(fp(d[f'D-{k}']) for k in range(7, 0, -1)) +
                     f" | {fp(d['total_7d'], 2)} | {fp(bt[(d['token'], d['burn_utc8'])]['total_7d'], 2)} |")
    L += ["\n## BTC 同窗口逐日\n", "| 对应 | D-7 | D-6 | D-5 | D-4 | D-3 | D-2 | D-1 | 合计 |", "|---|---|---|---|---|---|---|---|---|"]
    for d in daily:
        if d["series"] == "BTC":
            L.append(f"| {d['token']} {d['burn_utc8'][5:]} | " + " | ".join(fp(d[f'D-{k}']) for k in range(7, 0, -1)) + f" | {fp(d['total_7d'])} |")
    obs = []
    rel = [r for r in inw if r["pre7d_pct"] is not None and r["btc_pre7d_pct"] is not None]
    if rel:
        obs.append("销毁前7天相对BTC：" + "、".join(f"{r['token']} {r['pre7d_pct'] - r['btc_pre7d_pct']:+.1f}pp" for r in rel))
    po = [r for r in inw if r["post_pct"] is not None and r["btc_post_pct"] is not None]
    if po:
        obs.append("销毁至今相对BTC：" + "、".join(f"{r['token']} {r['post_pct'] - r['btc_post_pct']:+.1f}pp" for r in po))
    if obs:
        L.append("\n## 自动观察（仅基于表内数字）\n")
        L += [f"- {o}" for o in obs]
    groups = [("即将/计划中的销毁", lambda s: s["type"].startswith("upcoming")), ("持续计划逐日明细", lambda s: s["type"] == "programme_daily"),
              ("同期相关新闻(可能干扰)", lambda s: s["type"] == "context_news"), ("低于阈值 / 剔除 / 窗口外 / 已执行的预告", lambda s: s["type"] in ("below_threshold", "excluded(extra)", "outside_window", "pre_announcement(已执行)"))]
    for title, fn in groups:
        ss = [s for s in side if fn(s)]
        if not ss:
            continue
        L += [f"\n## {title}\n", "| 类型 | 代币 | 时间 | 数量 | 价值 | 说明 |", "|---|---|---|---|---|---|"]
        L += [f"| {s['type']} | {s['token']} | {s['time_utc8'] or NA} | {ut.fmt_amt(s['amount'])} | {fmt_usd(s['usd_value'])} | {(s['detail'] or '')[:90]} |" for s in ss]
    if meta["unmapped"] or meta["errors"]:
        L.append("\n## 未映射 / 异常\n")
        L += [f"- 未映射价格：{x}" for x in meta["unmapped"]] + [f"- 错误/限制：{x}" for x in meta["errors"]]
    L.append("\n## 数据来源\n")
    L.append("- 销毁：Hyperliquid API(援助基金成交)；pump.fun/pump-token 官方看板；PANews 快讯 API；Odaily 快讯页(逐ID)；--news-url；--extra-events（链上 tx 见 burns.csv 的 tx 列）。")
    L.append("- 价格：Binance data-api.binance.vision 1h / CoinGecko market_chart 小时；流通/总量：CoinGecko /coins；BTC 基准 Binance BTCUSDT。")
    L.append("- 各源解析数：" + json.dumps(meta["sources"], ensure_ascii=False))
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
