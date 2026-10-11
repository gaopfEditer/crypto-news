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
    names_zh = {k.upper(): v for k, v in (data.get("names_zh") or {}).items()}
    return [s.upper() for s in symbols if s], names_zh


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


def _pct_chg(series, bars_back):
    if series is None or len(series) <= bars_back:
        return None
    try:
        last = float(series.iloc[-1])
        base = float(series.iloc[-1 - bars_back])
        if base <= 0:
            return None
        return (last / base - 1.0) * 100.0
    except (TypeError, ValueError, IndexError):
        return None


def _build_market_context(symbol, hist, info, qqq_close, as_of_utc8):
    """近期行情摘要：Yahoo 日收盘价，便于财报前后对照 QQQ。"""
    if hist is None or hist.empty or "Close" not in hist:
        return None
    close = hist["Close"].dropna()
    if len(close) < 2:
        return None
    price = float(close.iloc[-1])
    chg_5d = _pct_chg(close, 5)
    chg_21d = _pct_chg(close, 21)
    chg_63d = _pct_chg(close, 63)
    vs_qqq_21d = None
    if qqq_close is not None and len(qqq_close) > 22:
        q21 = _pct_chg(qqq_close, 21)
        if chg_21d is not None and q21 is not None:
            vs_qqq_21d = chg_21d - q21
    hi52 = info.get("fiftyTwoWeekHigh") if info else None
    lo52 = info.get("fiftyTwoWeekLow") if info else None
    dist_hi = None
    if hi52 and hi52 > 0:
        try:
            dist_hi = (price / float(hi52) - 1.0) * 100.0
        except (TypeError, ValueError):
            dist_hi = None
    parts = []
    if chg_21d is not None:
        parts.append(f"近21交易日 {'+' if chg_21d >= 0 else ''}{chg_21d:.1f}%")
    if vs_qqq_21d is not None:
        parts.append(f"较 QQQ {'+' if vs_qqq_21d >= 0 else ''}{vs_qqq_21d:.1f}pp")
    if dist_hi is not None:
        parts.append(f"距52周高点 {dist_hi:+.1f}%")
    if chg_5d is not None:
        parts.append(f"近5日 {'+' if chg_5d >= 0 else ''}{chg_5d:.1f}%")
    summary = "；".join(parts) if parts else "近期行情数据不足"
    methodology = (
        "日收盘价；5日=最近5个交易日环比；21日≈1个月(21个交易日)；"
        "63日≈3个月；较QQQ为同窗口累计收益差(百分点)；52周高低来自 Yahoo 基本面字段。"
    )
    return {
        "as_of_utc8": as_of_utc8,
        "price_usd": round(price, 4) if price < 1000 else round(price, 2),
        "change_5d_pct": round(chg_5d, 2) if chg_5d is not None else None,
        "change_21d_pct": round(chg_21d, 2) if chg_21d is not None else None,
        "change_63d_pct": round(chg_63d, 2) if chg_63d is not None else None,
        "vs_qqq_21d_pp": round(vs_qqq_21d, 2) if vs_qqq_21d is not None else None,
        "fifty_two_week_high": hi52,
        "fifty_two_week_low": lo52,
        "pct_from_52w_high": round(dist_hi, 2) if dist_hi is not None else None,
        "data_source": "Yahoo Finance (yfinance)",
        "methodology_zh": methodology,
        "summary_zh": summary,
    }


def _yf_symbol_bundle(symbol, qqq_close, as_of_utc8):
    try:
        ticker = yf.Ticker(symbol)
        dates = _parse_earnings_dates(ticker.calendar)
        info = {}
        try:
            info = ticker.info or {}
        except Exception:
            info = {}
        name = info.get("shortName") or info.get("longName")
        hist = ticker.history(period="6mo", interval="1d")
        ctx = _build_market_context(symbol, hist, info, qqq_close, as_of_utc8)
        if not dates:
            return None, name, ctx
        return dates, name, ctx
    except Exception as e:
        log(f"yfinance {symbol} 失败: {e}")
        return None, None, None


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
    symbols, names_zh = load_watchlist(watchlist_path)
    if not symbols:
        log("⚠ earnings_watchlist.json 为空，跳过财报抓取")
        return []

    ny = ZoneInfo("America/New_York")
    bj = ZoneInfo("Asia/Shanghai")
    as_of_utc8 = datetime.now(bj).strftime("%Y-%m-%d %H:%M")
    qqq_close = None
    try:
        qqq_hist = yf.Ticker("QQQ").history(period="6mo", interval="1d")
        if qqq_hist is not None and not qqq_hist.empty:
            qqq_close = qqq_hist["Close"].dropna()
    except Exception as e:
        log(f"QQQ 基准行情失败: {e}")

    today = datetime.now(ny).date()
    lo = today - timedelta(days=days_back)
    hi = today + timedelta(days=days_ahead)

    # 1) Yahoo Finance：各标的下一次/多次公布日
    symbol_dates = {}
    yf_names = {}
    market_ctx = {}
    for sym in symbols:
        dates, name, ctx = _yf_symbol_bundle(sym, qqq_close, as_of_utc8)
        if not dates:
            continue
        if name:
            yf_names[sym] = name
        if ctx:
            market_ctx[sym] = ctx
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
                ev["name_zh"] = names_zh.get(sym) or ""
                ev["company_name_en"] = yf_names.get(sym) or ev.get("type")
                if market_ctx.get(sym):
                    ev["market_context"] = market_ctx[sym]
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
