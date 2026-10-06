#!/usr/bin/env python3
"""Regression tests for impact.py keyword tightening (stdlib only)."""
import json
import re
import sys

import impact


def _event_title_matches(cfg, key, title):
    ev = next(e for e in cfg["events"] if e["key"] == key)
    pats = ev.get("en", []) + ev.get("zh", [])
    rx = re.compile("|".join("(?:" + p + ")" for p in pats), re.I)
    return bool(rx.search(title))


def run():
    fails = []

    def check(name, cond, detail=""):
        if not cond:
            fails.append("%s: %s" % (name, detail or "failed"))

    # 1. Cloud AI 上架 ≠ 交易所上币
    t1 = "智谱 GLM-5.3 上架 Amazon Bedrock"
    r1 = impact.classify(t1, "", [])
    check("1-no-listing", r1.get("impact") != "listing", r1)
    check("1-no-glm-coin", "GLM" not in (r1.get("coins") or []), r1.get("coins"))

    # 2. ETF 预期 ≠ 获批
    t2 = "INJ ETF expected before 2027 (CEO)"
    r2 = impact.classify(t2, "", [])
    check("2-not-approved-etf", r2.get("impact") != "etf", r2)
    check("2-expected-or-none", r2.get("impact") in (None, "etf_expected"), r2)

    # 3. 银行结算 ≠ 监管执法（config events）
    cfg = json.load(open("config.json", encoding="utf-8"))
    t3 = "Kraken parent Payward + bank 24/7 institutional settlement"
    check("3-no-regulation-event", not _event_title_matches(cfg, "regulation", t3), t3)

    # 4. ETF 基金代码不作 coin
    t4 = "Fidelity FETH outflow hits $50M"
    r4 = impact.classify(t4, "", [])
    check("4-no-feth", "FETH" not in (r4.get("coins") or []), r4.get("coins"))

    # 5. 暂停/attack risk ≠ 黑客
    t5 = "Arbitrum pauses Stylus over attack risk"
    r5 = impact.classify(t5, "", [])
    check("5-no-hack-impact", r5.get("impact") != "hack", r5)

    # 6. NEAR 预防性暂停（同 #5）
    t6 = "NEAR pauses bridge withdrawals after suspicious activity, no funds lost"
    r6 = impact.classify(t6, "", [])
    check("6-no-hack-near", r6.get("impact") != "hack", r6)
    check("6-no-hack-config", not _event_title_matches(cfg, "hack", t6), t6)

    # 正例：真交易所上币仍命中
    t_ok = "Binance will list SOL spot trading pair"
    ro = impact.classify(t_ok, "", [])
    check("ok-listing", ro.get("impact") == "listing", ro)

    if fails:
        print("FAIL")
        for f in fails:
            print(" ", f)
        return 1
    print("OK (7 checks)")
    return 0


if __name__ == "__main__":
    sys.exit(run())
