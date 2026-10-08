#!/usr/bin/env python3
"""财报日历：Yahoo Finance (yfinance) 公布日 + Nasdaq 日历补充盘前/盘后与公司信息。"""
import json
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf

NASDAQ_EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings?date={date}"
NASDAQ_STOCK_EARNINGS = "https://www.nasdaq.com/market-activity/stocks/{symbol}/earnings"
CURL_UA = "Mozilla/5.0 (compatible; crypto-news-calendar/1.0)"

SESSION_MAP = {
    "time-pre-market": ("08:00", "盘前", True),
    "time-after-hours": ("16:30", "盘后", True),
    "time-not-supplied": ("16:00", "公布时段待确认", False),
}

# 未来约一个季度 + 少量回看（已公布列表）
DEFAULT_DAYS_AHEAD = 95
DEFAULT_DAYS_BACK = 7


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def load_watchlist(path="earnings_watchlist.json"):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    symbols = data.get("symbols") or []
    return [s.upper() for s in symbols if s]


def _fetch_nasdaq_day(date_str, retries=3):
    """Nasdaq API 在部分环境 urllib 易挂起，用 curl 更稳。"""
    url = NASDAQ_EARNINGS_URL.format(date=date_str)
    for attempt in range(retries):
        try:
            proc = subprocess.run(
                [
                    "curl",
                    "-sS",
                    "-m",
                    "25",
                    url,
                    "-H",
                    f"User-Agent: {CURL_UA}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or f"curl exit {proc.returncode}")
            payload = json.loads(proc.stdout)
            block = payload.get("data") or {}
            return block.get("rows") or []
        except (json.JSONDecodeError, RuntimeError, OSError) as e:
            if attempt + 1 >= retries:
                log(f"Nasdaq 财报日历 {date_str} 失败: {e}")
                return None
            time.sleep(0.6 * (attempt + 1))
    return None


def _parse_earnings_dates(calendar_obj):
    if calendar_obj is None:
        return []
    if isinstance(calendar_obj, dict):
        raw = calendar_obj.get("Earnings Date")
    else:
        try:
            raw = calendar_obj.get("Earnings Date")
        except Exception:
            raw = None
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = [raw]
    out = []
    for item in items:
        if isinstance(item, datetime):
            out.append(item.date())
        elif isinstance(item, date):
            out.append(item)
        elif hasattr(item, "date"):
            out.append(item.date())
    return out


def _yf_next_earnings(symbol):
    try:
        ticker = yf.Ticker(symbol)
        dates = _parse_earnings_dates(ticker.calendar)
        if not dates:
            return None, None
        name = None
        try:
            info = ticker.info or {}
            name = info.get("shortName") or info.get("longName")
        except Exception:
            name = None
        return dates, name
    except Exception as e:
        log(f"yfinance {symbol} 失败: {e}")
        return None, None


def _row_to_event(row, report_date, fallback_name=None):
    symbol = (row.get("symbol") or "").upper()
    if not symbol:
        return None

    session = row.get("time") or "time-not-supplied"
    time_et_hm, session_note, confirmed = SESSION_MAP.get(
        session, SESSION_MAP["time-not-supplied"]
    )
    time_et = f"{report_date} {time_et_hm}"

    name = (row.get("name") or fallback_name or symbol).strip().rstrip(",")
    fiscal = (row.get("fiscalQuarterEnding") or "").strip()
    eps = (row.get("epsForecast") or "").strip()

    note_parts = [session_note]
    if fiscal:
        note_parts.append(f"财季 {fiscal}")
    note = " · ".join(note_parts)

    return {
        "id": f"earnings-{symbol.lower()}-{report_date}",
        "category": "earnings",
        "type": name,
        "symbol": symbol,
        "anchor": "QQQ",
        "importance": "big",
        "time_et": time_et,
        "expected": eps,
        "previous": "",
        "actual": "",
        "source_url": NASDAQ_STOCK_EARNINGS.format(symbol=symbol.lower()),
        "confirmed": confirmed,
        "note": note,
        "earnings_session": session,
        "report_date": report_date,
    }


def _fallback_event(symbol, report_date, company_name=None):
    time_et_hm, session_note, confirmed = SESSION_MAP["time-not-supplied"]
    time_et = f"{report_date} {time_et_hm}"
    name = (company_name or symbol).strip()
    return {
        "id": f"earnings-{symbol.lower()}-{report_date}",
        "category": "earnings",
        "type": name,
        "symbol": symbol,
        "anchor": "QQQ",
        "importance": "big",
        "time_et": time_et,
        "expected": "",
        "previous": "",
        "actual": "",
        "source_url": f"https://finance.yahoo.com/quote/{symbol}/",
        "confirmed": confirmed,
        "note": f"{session_note} · 日期来源 Yahoo Finance",
        "earnings_session": "time-not-supplied",
        "report_date": report_date,
    }


def fetch_earnings_events(
    days_ahead=DEFAULT_DAYS_AHEAD,
    days_back=DEFAULT_DAYS_BACK,
    watchlist_path="earnings_watchlist.json",
):
    symbols = load_watchlist(watchlist_path)
    if not symbols:
        log("⚠ earnings_watchlist.json 为空，跳过财报抓取")
        return []

    ny = ZoneInfo("America/New_York")
    today = datetime.now(ny).date()
    lo = today - timedelta(days=days_back)
    hi = today + timedelta(days=days_ahead)

    # 1) Yahoo Finance：各标的下一次/多次公布日
    symbol_dates = {}
    yf_names = {}
    for sym in symbols:
        dates, name = _yf_next_earnings(sym)
        if not dates:
            continue
        if name:
            yf_names[sym] = name
        in_window = [d for d in dates if lo <= d <= hi]
        if in_window:
            symbol_dates[sym] = sorted(set(in_window))
        time.sleep(0.08)

    if not symbol_dates:
        log("⚠ 窗口内未从 Yahoo Finance 获取到财报日期")
        return []

    unique_dates = sorted({d for ds in symbol_dates.values() for d in ds})

    # 2) Nasdaq：按公布日拉取，补充盘前/盘后与公司行
    nasdaq_by_date = {}
    for day in unique_dates:
        day_str = day.strftime("%Y-%m-%d")
        rows = _fetch_nasdaq_day(day_str)
        if rows is None:
            continue
        nasdaq_by_date[day_str] = {
            (r.get("symbol") or "").upper(): r for r in rows if r.get("symbol")
        }
        time.sleep(0.15)

    events = {}
    for sym, dates in symbol_dates.items():
        for d in dates:
            day_str = d.strftime("%Y-%m-%d")
            row = (nasdaq_by_date.get(day_str) or {}).get(sym)
            if row:
                ev = _row_to_event(row, day_str, fallback_name=yf_names.get(sym))
            else:
                ev = _fallback_event(sym, day_str, yf_names.get(sym))
            if ev:
                events[ev["id"]] = ev

    result = sorted(events.values(), key=lambda e: (e.get("time_et") or "", e.get("symbol") or ""))
    log(
        f"✓ 财报: watchlist {len(symbols)} 只, Yahoo 命中 {len(symbol_dates)} 只, "
        f"输出 {len(result)} 条 (Nasdaq 补全 {len(nasdaq_by_date)} 个公布日)"
    )
    return result


if __name__ == "__main__":
    for e in fetch_earnings_events():
        print(f"{e['symbol']}\t{e['report_date']}\t{e['note']}\t{e['type']}")
