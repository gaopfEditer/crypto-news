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
    
    # 显示时间应该是最早成员的时间
    display_ts = min(m["ts"] for m in members)
    
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
        multi=is_multi_source,
        alt_title=alt,
        ts=display_ts,
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
    
    # === 关键修复：每次运行都从所有成员重新聚类 ===
    # 1. 提取所有保留的历史成员
    cluster_hours = cfg.get("cluster_hours", 6)
    retain_cut = now - retain_hours * 3600
    cluster_cut = now - (retain_hours + cluster_hours) * 3600
    
    all_members = []
    old_story_metadata = {}  # story_id -> metadata
    
    for s in stories:
        # 只保留时间窗口内的故事
        max_member_ts = max((m["ts"] for m in s["members"]), default=0)
        if max_member_ts < retain_cut:
            continue
        
        # 保存故事级元数据,用故事ID索引
        old_story_metadata[s["id"]] = {
            "first_seen": s.get("first_seen"),
            "seeded": s.get("seeded", False),
            "prices": s.get("prices", {})
        }
        
        for m in s["members"]:
            all_members.append(m)
    
    log(f"✓ 从旧状态提取了 {len(all_members)} 个历史成员")
    
    # 2. 合并新抓取的条目
    url_set = {m["url"] for m in all_members}
    for it in fresh:
        if it["url"] not in url_set:
            all_members.append(make_member(it))
            url_set.add(it["url"])
        # 如果URL重复，跳过（已在all_members中）
    
    log(f"✓ 合并后共 {len(all_members)} 个成员，开始重新评分和聚类...")
    
    # 3. 对所有成员重新评分（应用当前规则）
    for m in all_members:
        # 重建完整的item以便重新评分
        item = {
            "id": m["id"],
            "source": m["source"],
            "label": m["label"],
            "url": m["url"],
            "title": m["title"],
            "ts": m["ts"],
            "summary": m.get("summary", ""),
            "tags": []
        }
        # 重新评分（应用当前的代币、事件、降权规则）
        nc.score(item, toks, ev, nz, cfg)
        # 更新member数据
        m.update({
            "score": item["score"],
            "tokens": item["tokens"],
            "tok_in_title": item["tok_in_title"],
            "event": item["event"],
            "event_label": item["event_label"],
            "events": item["events"],
            "big": item["big"],
            "reasons": item["reasons"],
            "sig": item["sig"],
            "ents": item["ents"]
        })
    
    # 4. 清空stories，从零开始重新聚类
    stories = []
    all_members.sort(key=lambda m: (-m.get("score", 0), m["ts"]))
    
    for m in all_members:
        
        # 在时间窗口内查找匹配的故事
        tgt = None
        for s in stories:
            # 使用故事的第一个成员时间来判断时间窗口
            s_ts = s["members"][0]["ts"] if s["members"] else m["ts"]
            if abs(m["ts"] - s_ts) > cluster_hours * 3600:
                continue
            if any(nc.same_story(existing, m, cfg) for existing in s["members"]):
                tgt = s
                break
        
        if tgt is None:
            # 创建新故事
            # 计算新故事的ID(与new_story函数一致)
            story_id = hashlib.sha1(m["id"].encode()).hexdigest()[:12]
            # 查找旧故事元数据(如果这个ID之前存在)
            meta = old_story_metadata.get(story_id, {})
            # 如果找到旧元数据,使用它;否则这是新故事或拆分出的子故事
            if meta:
                tgt = new_story(m["id"], meta.get("first_seen", now), meta.get("seeded", False))
                tgt["prices"] = dict(meta.get("prices", {}))
            else:
                # 新故事或拆分出的子故事:first_seen=第一个成员ts,seeded=False
                tgt = new_story(m["id"], m["ts"], False)
            stories.append(tgt)
        
        tgt["members"].append(m)
        tgt["updated"] = now
    
    log(f"✓ 重新聚类完成，生成 {len(stories)} 个故事")
    
    # 4. 对所有故事重新计算分数、等级、多源等属性
    for s in stories:
        recompute_story(s, s["members"], cfg, now)
        
        # 清除错误的seeded标记：只有真正冷启动时创建的故事才保留
        if s.get("seeded"):
            seeded_at = state.get("seeded_at")
            if seeded_at and s.get("first_seen", 0) > seeded_at + 600:
                s["seeded"] = False
            elif not seeded_at and not seeding:
                # 没有冷启动记录且当前不是冷启动
                s["seeded"] = False
    
    # 5. 填充价格（只针对需要的故事）
    try:
        log("💰 获取价格数据...")
        fill_prices(stories, toks, cfg)
    except Exception as e:
        log(f"⚠ 价格获取失败: {e}")
    
    # 6. 最终清理：只保留retain_hours内的故事
    old_count = len(stories)
    stories = [s for s in stories if s["ts"] >= retain_cut]
    
    log(f"✓ 最终保留 {len(stories)} 个故事（清理了 {old_count - len(stories)} 个）")
    
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
**新增**: {new_total} 条原始条目，重新聚类 {len(all_members)} 个成员  
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
