#!/usr/bin/env python3
"""GitHub Actions 新闻抓取脚本（静态生成）"""
import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import sys
import time

import newscore as nc

# 公开字段（输出到前端的 data.json）
PUBLIC = ("id", "first_seen", "updated", "ts", "title", "alt_title", "url", "source", "label", 
          "summary", "score", "level", "big", "event", "event_label", "events", "tokens", 
          "tok_in_title", "reasons", "sources", "multi", "prices", "seeded", "level_at", "big_at")

MEMBER_PUBLIC = ("source", "label", "url", "title", "ts", "score")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def load_state():
    """加载之前的状态（如果存在）"""
    if os.path.exists("state.json"):
        try:
            with open("state.json", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log(f"⚠ 状态文件读取失败: {e}")
    return {
        "seen": {},
        "sources": {},
        "stories": [],
        "seeded_at": None
    }


def save_state(state):
    """保存状态到 state.json"""
    with open("state.json", "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def recompute_story(s, members, cfg, now):
    """重新计算故事的分数和级别"""
    lead = max(members, key=lambda m: (m["score"], -m["ts"]))
    labels = sorted({m["label"] for m in members})
    
    # 独立来源验证：Odaily+PANews不算多源，需要英文媒体
    english_outlets = {"CoinDesk", "The Block"}
    has_english = any(m["label"] in english_outlets for m in members)
    cn_only = all(m["label"] in ("Odaily", "PANews") for m in members)
    
    # 真正的多源确认
    is_multi_source = len(labels) > 1 and has_english and not cn_only
    bonus = cfg.get("cross_confirm_bonus", 1) if is_multi_source else 0
    
    toks = list(lead["tokens"]) + [t for m in members for t in m["tokens"] if t not in lead["tokens"]]
    toks = list(dict.fromkeys(toks))
    
    reasons = list(lead["reasons"]) + (["多源确认+%g" % bonus] if bonus else [])
    score = round(lead["score"] + bonus, 2)
    
    big = any(m["big"] for m in members)
    # 等级必须基于最终分数重新计算
    if big and score >= cfg["big_threshold"]:
        level = "big"
    elif toks and score >= cfg["threshold"]:
        level = "hit"
    else:
        level = "low"
    
    zh = bool(nc.CJK.search(lead["title"]))
    alt = next((m["title"] for m in members if bool(nc.CJK.search(m["title"])) != zh), None)
    
    old_level = s.get("level")
    s.update(
        title=lead["title"],
        url=lead["url"],
        source=lead["source"],
        label=lead["label"],
        summary=lead.get("summary", ""),
        score=score,
        level=level,
        big=level == "big",
        event=lead["event"],
        event_label=lead["event_label"],
        events=lead.get("events", []),
        tokens=toks,
        tok_in_title=lead["tok_in_title"],
        reasons=reasons,
        sources=labels,
        multi=len(labels) > 1,
        alt_title=alt,
        ts=min(m["ts"] for m in members),
        members=members
    )
    
    rank = {"low": 0, "hit": 1, "big": 2}
    if old_level is None or rank[level] > rank.get(old_level, 0):
        s["level_at"] = now
        if level == "big":
            s["big_at"] = now


def new_story(item_id, now, seeded):
    """创建新故事"""
    sid = hashlib.sha1(item_id.encode()).hexdigest()[:12]
    return {
        "id": sid,
        "first_seen": now,
        "updated": now,
        "members": [],
        "prices": {},
        "seeded": seeded
    }


def make_member(it):
    """从条目创建成员记录"""
    return {k: it.get(k) for k in ("id", "source", "label", "url", "title", "ts", "summary", 
                                    "score", "big", "event", "event_label", "events", "tokens", 
                                    "tok_in_title", "reasons", "sig", "ents")}


def fetch_source(key, sc, ss):
    """抓取单个数据源"""
    t0 = time.time()
    try:
        got = nc.SOURCES[sc["type"]](key, sc, ss)
        return key, ss, got, None, time.time() - t0
    except Exception as e:
        return key, ss, [], e, time.time() - t0


def fill_prices(stories, toks, cfg):
    """填充价格数据"""
    want = {}
    for s in stories:
        if s["level"] == "low":
            continue
        for t in (s["tok_in_title"] or s["tokens"])[:2]:
            if t not in s["prices"] and t in toks:
                want[t] = want.get(t, False) or s["level"] != "low"
    
    if not want:
        return
    
    try:
        syms = nc.binance_syms(".")
    except Exception as e:
        log(f"⚠ 无法获取 Binance 交易对: {e}")
        syms = set()
    
    with cf.ThreadPoolExecutor(4) as ex:
        res = dict(zip(want, ex.map(
            lambda t: nc.price_1h(t, toks[t], syms, allow_gecko=want[t]), 
            want
        )))
    
    now = time.time()
    for s in stories:
        for t in (s["tok_in_title"] or s["tokens"])[:2]:
            if t not in s["prices"] and res.get(t):
                s["prices"] = dict(s["prices"], **{t: res[t]})
                s["updated"] = now


def public_story(s):
    """转换为公开格式"""
    d = {k: s.get(k) for k in PUBLIC}
    d["prices"] = dict(s.get("prices") or {})
    d["members"] = [
        {k: m.get(k) for k in MEMBER_PUBLIC} 
        for m in sorted(s["members"], key=lambda m: m["ts"])
    ]
    d["time_utc8"] = nc.utc8(s["ts"])
    d["first_seen_utc8"] = nc.utc8(s["first_seen"], "%Y-%m-%d %H:%M:%S")
    return d


def main():
    parser = argparse.ArgumentParser(description="抓取加密新闻")
    parser.add_argument("--seed", action="store_true", help="首次播种（回填近12小时）")
    args = parser.parse_args()
    
    # 加载配置
    with open("config.json", encoding="utf-8") as f:
        cfg = json.load(f)
    
    # 加载代币和规则
    toks, tok_info = nc.load_tokens(cfg, ".")
    ev, nz = nc.compile_rules(cfg)
    
    log(f"✓ 加载了 {len(toks)} 个代币 (配置: {tok_info['config']}, 自动: {tok_info['auto_status']})")
    
    # 加载状态
    state = load_state()
    now = time.time()
    # 只有明确的--seed或真正空状态才播种
    is_empty_state = not state.get("seeded_at") and len(state.get("stories", [])) == 0
    seeding = args.seed or is_empty_state
    
    if seeding:
        log("🌱 首次播种模式（回填近12小时）")
    
    # 准备数据源任务
    jobs = {}
    for key, sc in cfg["sources"].items():
        if sc.get("enabled"):
            jobs[key] = (sc, dict(state["sources"].get(key, {})))
    
    # 并行抓取所有数据源
    log(f"📡 开始抓取 {len(jobs)} 个数据源...")
    with cf.ThreadPoolExecutor(max(1, len(jobs))) as ex:
        results = list(ex.map(lambda k: fetch_source(k, *jobs[k]), list(jobs)))
    
    # 处理结果
    seen = state["seen"]
    stories = state["stories"]
    retain_hours = cfg.get("dashboard", {}).get("retain_hours", 48)
    seed_hours = cfg.get("dashboard", {}).get("seed_hours", 12)
    window = now - (seed_hours if seeding else retain_hours) * 3600
    
    fresh = []
    new_total = 0
    source_stats = {}
    
    for key, ss, got, err, secs in results:
        ss["last_ms"] = int(secs * 1000)
        if err is not None:
            ss["fails"] = ss.get("fails", 0) + 1
            ss["last_fail"] = int(now)
            ss["last_error"] = f"{type(err).__name__}: {err}"[:300]
            log(f"⚠ {key} 失败 ({ss['fails']}次): {err}")
            source_stats[key] = {"status": "error", "error": str(err)}
        else:
            ss.update(fails=0, last_ok=int(now), last_count=len(got))
            log(f"✓ {key}: {len(got)} 条")
            source_stats[key] = {"status": "ok", "count": len(got)}
        
        n_new = 0
        for it in got:
            keys = [it["id"], "u:" + it["url"], nc.tkey(it["title"])]
            unseen = not any(k in seen for k in keys)
            for k in keys:
                seen[k] = int(now)
            
            if unseen and it["title"] and window <= it["ts"] <= now + 600:
                fresh.append(nc.score(it, toks, ev, nz, cfg))
                n_new += 1
        
        if err is None:
            ss["last_new"] = n_new
            new_total += n_new
        
        state["sources"][key] = ss
    
    log(f"✓ 新增 {new_total} 条原始条目")
    
    # 去重和聚类
    fresh.sort(key=lambda x: (-x["score"], x["ts"]))
    cluster_hours = cfg.get("cluster_hours", 6)
    stale_check_hours = 72  # 72小时陈旧新闻检查
    recent = [s for s in stories if s["ts"] >= now - (retain_hours + cluster_hours) * 3600]
    stale_window = [s for s in stories if s["ts"] >= now - stale_check_hours * 3600]
    touched = []
    
    for it in fresh:
        tgt = None
        # 检查URL去重
        for s in recent:
            if any(m["url"] == it["url"] for m in s["members"]):
                tgt = "dup"
                break
        
        if tgt == "dup":
            continue
        
        # 72小时陈旧新闻检查：相同主体实体+事件类型
        for s in stale_window:
            if (it.get("event") and it.get("event") == s.get("event") and
                s["ts"] < it["ts"] - 12 * 3600 and  # 至少12小时前的老故事
                any(nc.same_story(m, it, cfg) for m in s["members"])):
                # 这是陈旧新闻重新出现，附加到老故事而不是创建新故事
                tgt = s
                log(f"⚠ 陈旧新闻: {it['title'][:40]} 附加到 {s.get('first_seen_utc8', 'old')} 的故事")
                break
        
        # 常规聚类
        if tgt is None:
            for s in recent:
                if any(nc.same_story(m, it, cfg) for m in s["members"]):
                    tgt = s
                    break
        
        if tgt is None:
            tgt = new_story(it["id"], now, seeding)
            stories.append(tgt)
            recent.append(tgt)
        
        tgt["members"].append(make_member(it))
        tgt["updated"] = now
        recompute_story(tgt, tgt["members"], cfg, now)
        if tgt not in touched:
            touched.append(tgt)
    
    log(f"✓ 更新了 {len(touched)} 个故事")
    
    # 填充价格
    try:
        log("💰 获取价格数据...")
        fill_prices(touched, toks, cfg)
    except Exception as e:
        log(f"⚠ 价格获取失败: {e}")
    
    # 清理旧数据
    cut = now - retain_hours * 3600
    old_count = len(stories)
    stories = [s for s in stories if max(m["ts"] for m in s["members"]) >= cut]
    state["stories"] = stories
    
    # 清理旧的 seen 记录
    state["seen"] = {k: v for k, v in seen.items() if v >= now - 7 * 86400}
    
    if seeding:
        state["seeded_at"] = int(now)
    state["last_poll"] = int(now)
    
    log(f"✓ 保留 {len(stories)} 个故事（清理了 {old_count - len(stories)} 个）")
    
    # 保存状态
    save_state(state)
    
    # 生成公开数据
    levels = {"big": 0, "hit": 0, "low": 0}
    for s in stories:
        levels[s["level"]] += 1
    
    data = {
        "updated": now,
        "updated_utc8": nc.utc8(now, "%Y-%m-%d %H:%M:%S"),
        "retain_hours": retain_hours,
        "stories": [public_story(s) for s in stories],
        "sources": {
            key: {
                "label": cfg["sources"][key].get("label", key),
                "enabled": bool(cfg["sources"][key].get("enabled")),
                "health": "ok" if ss.get("fails", 0) == 0 and ss.get("last_ok") 
                         else "warn" if ss.get("fails", 0) < 3 
                         else "down",
                "last_ok": ss.get("last_ok"),
                "last_ok_utc8": nc.utc8(ss.get("last_ok"), "%m-%d %H:%M:%S"),
                "last_fail": ss.get("last_fail"),
                "last_fail_utc8": nc.utc8(ss.get("last_fail"), "%m-%d %H:%M:%S"),
                "last_error": ss.get("last_error") if ss.get("fails", 0) else None,
                "last_count": ss.get("last_count"),
                "last_new": ss.get("last_new"),
                "fails": ss.get("fails", 0)
            }
            for key, ss in state["sources"].items()
        },
        "meta": {
            "threshold": cfg["threshold"],
            "big_threshold": cfg["big_threshold"],
            "tokens": [
                {"ticker": t, "origin": v["origin"], "weight": v["weight"]} 
                for t, v in toks.items()
            ],
            "events": [
                {"key": e["key"], "label": e["label"], "big": bool(e.get("big"))} 
                for e in cfg["events"]
            ],
            "sources": [
                {"key": k, "label": v.get("label", k), "enabled": bool(v.get("enabled"))} 
                for k, v in cfg["sources"].items()
            ]
        },
        "stats": {
            "total": len(stories),
            "big": levels["big"],
            "hit": levels["hit"],
            "low": levels["low"]
        }
    }
    
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    
    log(f"✓ 生成 data.json: {len(stories)} 个故事 (重大 {levels['big']}, 命中 {levels['hit']}, 低分 {levels['low']})")
    
    # 生成摘要
    summary = f"""
**统计**: {len(stories)} 个故事 (重大 {levels['big']}, 命中 {levels['hit']}, 低分 {levels['low']})  
**新增**: {new_total} 条原始条目，更新 {len(touched)} 个故事  
**数据源状态**:
"""
    for key, stat in source_stats.items():
        if stat["status"] == "ok":
            summary += f"- ✓ {cfg['sources'][key].get('label', key)}: {stat['count']} 条\n"
        else:
            summary += f"- ⚠ {cfg['sources'][key].get('label', key)}: {stat['error']}\n"
    
    with open("summary.md", "w", encoding="utf-8") as f:
        f.write(summary)
    
    # 输出给 GitHub Actions
    print(f"new_items={new_total}")
    print(f"stories={len(stories)}")
    print(f"big={levels['big']}")
    print(f"summary=+{new_total}条/共{len(stories)}个故事")


if __name__ == "__main__":
    main()
