#!/usr/bin/env python3
"""日历事件处理脚本：转换时区、获取实际值"""
import json
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fetch_earnings import fetch_earnings_events

def log(*a):
    print(*a, file=sys.stderr, flush=True)


def is_dst(dt_et):
    """判断给定的ET时间是否在夏令时（DST）期间
    2026年美国夏令时：3月8日 - 11月1日
    """
    from datetime import timedelta
    year = dt_et.year
    # DST开始：3月第二个周日 2:00 AM
    march_1 = datetime(year, 3, 1, tzinfo=ZoneInfo("America/New_York"))
    days_to_first_sunday = (6 - march_1.weekday() + 7) % 7
    dst_start = march_1 + timedelta(days=days_to_first_sunday + 7)  # 第二个周日
    # DST结束：11月第一个周日 2:00 AM
    nov_1 = datetime(year, 11, 1, tzinfo=ZoneInfo("America/New_York"))
    days_to_sunday = (6 - nov_1.weekday()) % 7
    dst_end = nov_1 + timedelta(days=days_to_sunday)  # 第一个周日
    return dst_start <= dt_et < dst_end


def et_to_beijing(time_et_str):
    """将美国东部时间转换为北京时间（UTC+8）
    Args:
        time_et_str: "YYYY-MM-DD HH:MM" 格式的ET时间
    Returns:
        (beijing_datetime, utc_datetime, is_dst_flag)
    """
    try:
        dt_naive = datetime.strptime(time_et_str, "%Y-%m-%d %H:%M")
        et_tz = ZoneInfo("America/New_York")
        dt_et = dt_naive.replace(tzinfo=et_tz)
        
        # 转换到UTC
        dt_utc = dt_et.astimezone(timezone.utc)
        
        # 转换到北京时间
        beijing_tz = ZoneInfo("Asia/Shanghai")
        dt_beijing = dt_utc.astimezone(beijing_tz)
        
        dst_flag = is_dst(dt_et)
        
        return dt_beijing, dt_utc, dst_flag
    except Exception as e:
        log(f"时间转换错误 {time_et_str}: {e}")
        return None, None, False


def utc_to_beijing(time_utc_str):
    """将UTC时间转换为北京时间
    Args:
        time_utc_str: "YYYY-MM-DD HH:MM:SS" 格式的UTC时间
    Returns:
        beijing_datetime
    """
    try:
        dt_utc = datetime.strptime(time_utc_str, "%Y-%m-%d %H:%M:%S")
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        
        beijing_tz = ZoneInfo("Asia/Shanghai")
        dt_beijing = dt_utc.astimezone(beijing_tz)
        
        return dt_beijing
    except Exception as e:
        log(f"UTC时间转换错误 {time_utc_str}: {e}")
        return None


def fetch_bls_actual(event_id, symbol):
    """尝试从BLS获取实际值（如果已发布）
    注意：BLS API需要注册密钥，这里仅作为框架，实际需要密钥时留空
    """
    # TODO: 实现BLS API调用（需要API key）
    # 当前返回None，表示未获取
    return None


def generate_initial_claims(start_date, days=28):
    """生成初请失业金事件（每周四，节假日自动调整）
    
    Args:
        start_date: 开始日期（datetime对象）
        days: 生成未来多少天的数据
    Returns:
        list of events
    """
    from datetime import timedelta
    
    events = []
    current = start_date
    end_date = start_date + timedelta(days=days)
    
    # 美国联邦假期（2026年）
    us_holidays = {
        datetime(2026, 11, 26).date(): "Thanksgiving",  # 感恩节，周四 → 周三
    }
    
    while current <= end_date:
        # 找到下一个周四（weekday=3）
        if current.weekday() == 3:  # Thursday
            event_date = current
            # 检查是否是节假日
            if current.date() in us_holidays:
                # 感恩节周四 → 移到周三
                event_date = current - timedelta(days=1)
                note = f"每周四发布（{us_holidays[current.date()]}假期调整至周三）"
            else:
                note = "每周四发布"
            
            event_id = f"initial-claims-{event_date.strftime('%Y-%m-%d')}"
            events.append({
                "id": event_id,
                "category": "macro",
                "type": "初请失业金人数",
                "symbol": "CLAIMS",
                "anchor": "BTC",
                "importance": "big",
                "time_et": event_date.strftime("%Y-%m-%d 08:30"),
                "expected": "",
                "previous": "",
                "actual": "",
                "source_url": "https://www.dol.gov/ui/data.pdf",
                "confirmed": True,
                "note": note
            })
        current += timedelta(days=1)
    
    return events


def validate_events(events):
    """验证事件数据
    
    Returns:
        list of validation errors
    """
    errors = []
    
    for event in events:
        # 验证：初请失业金必须是周四或节假日调整的周三
        if event.get("symbol") == "CLAIMS":
            if "time_et" in event:
                try:
                    dt = datetime.strptime(event["time_et"], "%Y-%m-%d %H:%M")
                    weekday = dt.weekday()
                    if weekday not in (2, 3):  # 周三或周四
                        errors.append(f"初请失业金 {event['id']} 在错误的星期 {['周一','周二','周三','周四','周五','周六','周日'][weekday]}（必须是周四或假期调整的周三）")
                except Exception as e:
                    errors.append(f"初请失业金 {event['id']} 时间格式错误: {e}")
        
        # 验证：宏观事件必须是重大
        if event.get("category") == "macro" and event.get("importance") != "big":
            errors.append(f"宏观事件 {event['id']} importance={event.get('importance')}，应为 'big'")
        
        # 验证：有数据的必须有来源
        if any(event.get(k) for k in ("expected", "actual", "previous")) and not event.get("source_url"):
            errors.append(f"事件 {event['id']} 有数据但缺少 source_url")
    
    return errors


def process_calendar():
    """处理日历数据：转换时区、获取实际值、生成周期事件"""
    try:
        with open("calendar_seed.json", encoding="utf-8") as f:
            seed_data = json.load(f)
    except FileNotFoundError:
        log("⚠ calendar_seed.json 不存在，创建空日历")
        seed_data = {"events": []}
    
    now_utc = datetime.now(timezone.utc)
    now_beijing = now_utc.astimezone(ZoneInfo("Asia/Shanghai"))
    
    # 过滤掉将由程序生成/拉取的数据
    base_events = [
        e
        for e in seed_data.get("events", [])
        if e.get("symbol") != "CLAIMS" and e.get("category") != "earnings"
    ]

    earnings_events = fetch_earnings_events()
    
    # 生成初请失业金事件（未来28天的所有周四）
    ny_tz = ZoneInfo("America/New_York")
    today_et = datetime.now(ny_tz).date()
    claims_events = generate_initial_claims(datetime(today_et.year, today_et.month, today_et.day), days=28)
    
    # 合并事件
    all_events = base_events + claims_events + earnings_events
    
    # 强制所有宏观事件为重大
    for event in all_events:
        if event.get("category") == "macro":
            event["importance"] = "big"
    
    # 验证
    validation_errors = validate_events(all_events)
    if validation_errors:
        log("❌ 验证失败:")
        for err in validation_errors:
            log(f"  - {err}")
        sys.exit(1)
    
    processed_events = []
    
    for event in all_events:
        processed = dict(event)
        
        # 转换时间
        if "time_et" in event and event["time_et"]:
            # 美东时间转北京时间
            dt_beijing, dt_utc, is_dst_flag = et_to_beijing(event["time_et"])
            if dt_beijing:
                processed["time_beijing"] = dt_beijing.strftime("%Y-%m-%d %H:%M")
                processed["time_utc"] = dt_utc.strftime("%Y-%m-%d %H:%M:%S")
                processed["weekday"] = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][dt_beijing.weekday()]
                processed["is_dst"] = is_dst_flag
                
                # 判断是否已过期
                processed["is_past"] = dt_utc < now_utc
        elif "time_utc" in event and event["time_utc"]:
            # UTC时间转北京时间
            dt_beijing = utc_to_beijing(event["time_utc"])
            if dt_beijing:
                processed["time_beijing"] = dt_beijing.strftime("%Y-%m-%d %H:%M")
                processed["weekday"] = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][dt_beijing.weekday()]
                
                dt_utc = datetime.strptime(event["time_utc"], "%Y-%m-%d %H:%M:%S")
                dt_utc = dt_utc.replace(tzinfo=timezone.utc)
                processed["is_past"] = dt_utc < now_utc
        
        # 尝试获取实际值（仅对已过期的宏观事件）
        if processed.get("is_past") and processed.get("category") == "macro":
            if not processed.get("actual"):
                actual = fetch_bls_actual(event.get("id"), event.get("symbol"))
                if actual:
                    processed["actual"] = actual
        
        # 计算相对预期
        if processed.get("actual") and processed.get("expected"):
            try:
                actual_val = float(str(processed["actual"]).rstrip("%K"))
                expected_val = float(str(processed["expected"]).rstrip("%K"))
                if actual_val > expected_val * 1.01:
                    processed["vs_expected"] = "high"
                elif actual_val < expected_val * 0.99:
                    processed["vs_expected"] = "low"
                else:
                    processed["vs_expected"] = "inline"
            except (ValueError, TypeError):
                pass
        
        processed_events.append(processed)
    
    # 按时间排序
    processed_events.sort(key=lambda x: x.get("time_utc", x.get("time_beijing", "")))
    
    # 分为未来和已公布
    upcoming = [e for e in processed_events if not e.get("is_past", False)]
    past = [e for e in processed_events if e.get("is_past", False)]
    
    output = {
        "updated": now_utc.timestamp(),
        "updated_utc8": now_beijing.strftime("%Y-%m-%d %H:%M:%S"),
        "upcoming": upcoming,
        "past": past[-50:],  # 只保留最近50个已公布事件
        "stats": {
            "total": len(processed_events),
            "upcoming": len(upcoming),
            "past": len(past)
        }
    }
    
    with open("calendar.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=1)
    
    log(f"✓ 日历处理完成: {len(upcoming)} 个即将发生, {len(past)} 个已公布")
    log(f"  更新时间: {output['updated_utc8']} (UTC+8)")
    
    # 输出摘要
    print(f"upcoming={len(upcoming)}")
    print(f"past={len(past)}")


if __name__ == "__main__":
    process_calendar()
