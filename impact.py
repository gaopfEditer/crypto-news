"""impact: 流动性/供给类事件识别 + 方向（利好/利空/中性）+ 影响币种提取（规则/关键词，中英文，仅标准库）。

由 newscore.score() 调用；输出字段（全部为新增字段，data.json 向后兼容）：
  coins            影响币种代码列表（不含 $），如 ["ENA"]
  direction        "利好" | "利空" | "中性"
  direction_reason 简短理由
  impact           事件类别 key（如 unlock / otc_sell / mainnet），未命中为 None
  impact_label     类别中文名
  impact_tag       渲染用短标签，如 "【利空】$ENA 大额解锁"
"""
import re

CJK = re.compile(r"[\u4e00-\u9fff]")

# boost: 加到 score 的分数（标题全额、摘要减半）；big: 标题命中时允许升为「重大」
# dir: 默认方向；dir_rules: (正则, 方向, 理由) 覆盖默认方向（按顺序取首个命中）
CATEGORIES = [
    dict(key="hack", label="黑客/漏洞", boost=3, big=True, dir="利空", reason="资金被盗/漏洞，恐慌抛售与流动性抽离",
         en=[r"hack(?:ed|er|ers|s)?", r"exploit(?:ed|s)?", r"drain(?:ed|s)?", r"stolen", r"rug ?pull", r"compromised"],
         zh=[r"黑客", r"被盗", r"漏洞攻击", r"遭攻击", r"被攻击", r"盗取", r"跑路", r"安全事件"]),
    dict(key="otc_sell", label="机构OTC/大额抛售", boost=5, big=True, dir="利空", reason="团队/机构大额出售，潜在抛压",
         en=[r"OTC\b.{0,40}\b(?:sale|sell(?:s|ing)?|sold|deal)", r"(?:sell(?:s|ing)?|sold|offload(?:s|ed)?|dump(?:s|ed)?)\b.{0,50}\b(?:OTC|to institutions?|institutional buyers?)",
             r"team\b.{0,30}\b(?:sell(?:s|ing)?|sold|dump(?:s|ed)?)"],
         zh=[r"(?:OTC|场外).{0,12}(?:出售|卖出|抛售|转让|售出|卖给)", r"(?:出售|卖出|抛售|售出).{0,20}(?:OTC|场外)", r"团队.{0,10}(?:抛售|出售|卖出|套现)", r"(?:大额|巨额).{0,6}(?:抛售|出售)"]),
    dict(key="unlock", label="代币解锁", boost=4, big=True, big_need=r"(?:大额|巨额|large|massive|huge|\$\s?\d[\d.,]*\s*(?:[MB]|million|billion)\b|\d[\d.,]*\s*(?:亿|万|million|billion|[MB]\b)|\d+(?:\.\d+)?\s*%)",
         dir="利空", reason="解锁增加流通供给，短期抛压",
         en=[r"unlock(?:s|ed|ing)?", r"vesting", r"cliff"], zh=[r"解锁", r"释放.{0,6}代币"]),
    dict(key="tge", label="TGE/解除转让限制", boost=4, big=False, dir="利空", reason="开放转账/TGE，新增可流通筹码",
         en=[r"TGE", r"token generation event", r"transfer(?:ability)?\s*(?:restrictions?\s*(?:lift(?:ed|s)?|remov(?:ed|es))|enabled|unlock(?:ed)?)", r"(?:lift(?:s|ed)?|remov(?:es|ed))\b.{0,20}transfer restrictions?", r"tokens? (?:become|becomes|now) transferable"],
         zh=[r"TGE", r"解除.{0,6}(?:转让|转账)限制", r"开放(?:转账|转让)", r"(?:可|允许)(?:转账|转让)", r"代币生成事件"]),
    dict(key="delist", label="下架", boost=3, big=True, dir="利空", reason="交易所下架，流动性下降",
         en=[r"delist(?:s|ed|ing)?", r"cease trading", r"will remove .{0,20}trading pairs?"], zh=[r"下架", r"摘牌", r"终止交易"]),
    dict(key="inflation", label="通胀/质押率调整", boost=4, big=False, dir="中性", reason="通胀/质押参数调整",
         dir_rules=[(r"(?:下调|降低|降至|削减|减半|减少|cut|cuts|reduc(?:e|es|ed|tion)|lower(?:s|ed)?|halv(?:e|es|ed|ing)|from\s*\d+(?:\.\d+)?%\s*to\s*\d)", "利好", "通胀/增发下调，供给增速放缓"),
                    (r"(?:上调|提高|增发|rais(?:e|es|ed)|increas(?:e|es|ed))", "利空", "通胀/增发上调，供给增速加快")],
         en=[r"inflation(?: rate)?", r"staking (?:yield|rate|rewards?|APR|APY)", r"emissions?", r"issuance"],
         zh=[r"通胀率?", r"通货膨胀", r"质押(?:收益|奖励|年化|利率)", r"增发率?", r"发行率", r"排放"]),
    dict(key="burn_buyback", label="销毁/回购", boost=3, big=False, dir="利好", reason="销毁/回购减少流通供给",
         en=[r"burn(?:s|ed|ing)?", r"buy ?backs?", r"repurchas(?:e|es|ed)"], zh=[r"销毁", r"回购"]),
    dict(key="etf_flow", label="ETF资金流", boost=3, big=False, dir="中性", reason="ETF资金流向",
         dir_rules=[(r"(?:净流出|流出|outflows?)", "利空", "ETF资金净流出"), (r"(?:净流入|流入|inflows?)", "利好", "ETF资金净流入，增量买盘")],
         en=[r"ETFs?\b.{0,40}\b(?:inflows?|outflows?|flows)", r"(?:inflows?|outflows?)\b.{0,40}\bETFs?"], zh=[r"ETF.{0,20}(?:净流入|净流出|流入|流出)"]),
    dict(key="etf", label="ETF获批/上市", boost=3, big=False, dir="利好", reason="ETF获批/上市，打开机构资金入口",
         dir_rules=[(r"(?:reject(?:s|ed)?|拒绝|推迟|delay(?:s|ed)?)", "利空", "ETF被拒/推迟")],
         en=[r"(?:ETF|ETP)s?\b.{0,50}\b(?:approv(?:es|ed|al)|launch(?:es|ed)?|debut(?:s|ed)?|list(?:s|ed|ing)|reject(?:s|ed)?)", r"(?:approv(?:es|ed|al)|launch(?:es|ed)?)\b.{0,50}\b(?:ETF|ETP)s?"],
         zh=[r"ETF.{0,20}(?:获批|批准|上市|推出|拒绝)", r"(?:批准|获批|推出).{0,20}ETF"]),
    dict(key="perp_listing", label="新永续合约/高杠杆", boost=3, big=False, dir="利好", reason="新增合约流动性（高杠杆可能放大波动）",
         en=[r"(?:perp(?:etual)?s?|futures)\b.{0,30}\b(?:list(?:s|ed|ing)|launch(?:es|ed)?|go(?:es)? live|add(?:s|ed)?)", r"(?:list(?:s|ed|ing)|launch(?:es|ed)?|add(?:s|ed)?)\b.{0,30}\b(?:perp(?:etual)?s?|futures)", r"\d{3,4}x leverage", r"1000x"],
         zh=[r"(?:永续|合约).{0,10}(?:上线|上架|推出)", r"(?:上线|上架|推出).{0,15}(?:永续|U本位|币本位)合约", r"\d{3,4}\s*倍杠杆", r"1000x"]),
    dict(key="listing", label="交易所上币", boost=3, big=False, dir="利好", reason="新增交易所上币，流动性与曝光提升",
         en=[r"(?<!de)list(?:s|ed|ing)\b.{0,30}\b(?:on|at)\b.{0,15}(?:Binance|Coinbase|OKX|Upbit|Bybit|Bithumb|Kraken|Robinhood|Bitget|Gate|KuCoin|HTX)", r"(?:Binance|Coinbase|OKX|Upbit|Bybit|Bithumb|Kraken|Robinhood|Bitget)\b.{0,20}\b(?:to list|lists|will list|adds?)\b"],
         zh=[r"上线\s*\$?[A-Z][A-Z0-9]{1,11}(?![A-Za-z])", r"上线.{0,10}(?:现货|交易对|代币)", r"上币", r"上架", r"开放.{0,6}交易"]),
    dict(key="mainnet", label="主网上线", boost=4, big=True, dir="利好", reason="主网上线，基本面里程碑（注意预期兑现）",
         en=[r"mainnet\b.{0,20}\b(?:launch(?:es|ed)?|live|debut(?:s|ed)?|go(?:es)? live|goes live)", r"launch(?:es|ed)?\b.{0,20}\bmainnet", r"(?:L1|layer[- ]1|blockchain)\b.{0,15}\b(?:goes live|launch(?:es|ed)?)"],
         zh=[r"主网.{0,6}(?:上线|启动|发布|正式|推出)", r"(?:上线|启动|推出).{0,6}主网", r"L1.{0,6}(?:上线|启动)"]),
    dict(key="upgrade", label="网络升级", boost=3, big=False, dir="利好", reason="网络升级利好，注意预期兑现风险",
         dir_rules=[(r"(?:推迟|延期|delay(?:s|ed)?|postpon(?:e|es|ed)|bug|回滚|rollback)", "利空", "升级推迟/出现问题")],
         en=[r"upgrade[sd]?", r"hard ?fork", r"testnet", r"v\d+\.\d+(?:\.\d+)?\b", r"Glamsterdam", r"Fusaka", r"Pectra", r"Karst"],
         zh=[r"升级", r"硬分叉", r"测试网", r"Glamsterdam", r"Karst"]),
    dict(key="relaunch", label="协议重启/新版本", boost=3, big=False, dir="利好", reason="协议重启/新版上线",
         en=[r"relaunch(?:es|ed)?", r"V[2-9] (?:launch|live|goes live)", r"launch(?:es|ed)? V[2-9]"], zh=[r"重启", r"重新上线", r"V[2-9]\s*(?:版本)?(?:上线|发布|推出)"]),
    dict(key="migration", label="代币迁移", boost=3, big=False, dir="中性", reason="代币迁移/换币，注意操作与交易所支持风险",
         en=[r"token (?:migration|swap)", r"migrat(?:e|es|ed|ion)\b.{0,30}\btokens?", r"rebrand(?:s|ed|ing)?", r"redenominat\w+"],
         zh=[r"代币迁移", r"迁移.{0,8}代币", r"换币", r"代币.{0,6}更名", r"品牌升级"]),
    dict(key="presale", label="预售/募资", boost=2, big=False, dir="中性", reason="预售/公募，关注后续解锁抛压",
         en=[r"pre-?sale", r"public sale", r"ICO", r"token sale"], zh=[r"预售", r"公募", r"代币销售", r"ICO"]),
    dict(key="ai_pivot", label="AI 转型", boost=2, big=False, dir="利好", reason="转型 AI/Agent 叙事，短期情绪利好",
         en=[r"pivot(?:s|ed|ing)?\b.{0,20}\b(?:AI|agent)", r"rebrand(?:s|ed)?\b.{0,20}\bAI"], zh=[r"(?:转型|转向|进军).{0,8}(?:AI|人工智能|Agent|智能体)"]),
    dict(key="macro", label="宏观/美联储", boost=2, big=False, dir="中性", reason="宏观事件，影响整体风险偏好",
         dir_rules=[(r"(?:降息|rate cuts?|cuts? rates?|dovish|鸽派)", "利好", "降息/鸽派，流动性预期改善"),
                    (r"(?:加息|rate hikes?|hikes? rates?|hawkish|鹰派)", "利空", "加息/鹰派，流动性收紧")],
         en=[r"FOMC", r"Fed\b", r"Federal Reserve", r"Powell", r"CPI", r"nonfarm payrolls?", r"rate (?:cut|hike|decision)s?", r"minutes"],
         zh=[r"FOMC", r"美联储", r"鲍威尔", r"会议纪要", r"CPI", r"非农", r"降息", r"加息", r"利率决议"]),
    dict(key="conference", label="生态大会", boost=1, big=False, dir="中性", reason="生态大会/峰会，可能伴随发布消息",
         en=[r"summit", r"basecamp", r"conference", r"Token2049", r"Devcon", r"Breakpoint", r"hackathon"], zh=[r"峰会", r"大会", r"开发者大会", r"黑客松"]),
]

# 项目名 → 代币代码（标题未写代码时用来补全影响币种）
NAME2TICKER = {
    "ethena": "ENA", "hyperliquid": "HYPE", "starknet": "STRK", "monad": "MON", "sui": "SUI", "near": "NEAR",
    "derive": "DRV", "ethereum": "ETH", "以太坊": "ETH", "bitcoin": "BTC", "比特币": "BTC", "solana": "SOL",
    "arbitrum": "ARB", "optimism": "OP", "aptos": "APT", "celestia": "TIA", "avalanche": "AVAX", "polygon": "POL",
    "chainlink": "LINK", "uniswap": "UNI", "aave": "AAVE", "pump.fun": "PUMP", "worldcoin": "WLD", "berachain": "BERA",
    "ripple": "XRP", "dogecoin": "DOGE", "toncoin": "TON", "ethos": "ETHOS",
}

# 不是币种的常见大写缩写
NOT_TICKER = set("""ETF ETP SEC CFTC DOJ FCA CEO CTO CFO COO USD USDT USDC EUR CNY HKD JPY GBP FOMC FED OTC TVL AI L1 L2 L3 CPI PPI PCE GDP DEX CEX NFT
DAO API TGE IPO ICO IDO RWA DEFI DEPIN KYC AML APR APY ATH ATL UTC PT ET US UK EU UAE HK SAR MEV ZK EVM SVM VM GPU CPU IT PR AMA Q1 Q2 Q3 Q4 H1 H2
V1 V2 V3 V4 V5 X OK NO HTX OKX MEXC GATE BYBIT KRAKEN UPBIT COINW LBANK BINGX POOLX OCC OFAC IMF ECB BOJ PBOC YES TOP NEW BREAKING CNBC BBC WSJ NYSE NASDAQ CME MSCI BLS LLC INC LTD CORP PLC SPAC M B K BTCFI FBI IRS ESMA MICA SOL NYDFS""".split()) - {"SOL"}
NOT_TICKER |= {"SEC", "THE", "AND", "FOR"}

CASHTAG = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,11})(?![A-Za-z0-9])")
CAPS = re.compile(r"(?<![A-Za-z0-9$])([A-Z][A-Z0-9]{1,9})(?![A-Za-z0-9])")


def _rx(en, zh):
    # 中文规则里以 ASCII 字母开头/结尾的片段也加词边界，避免 "TGE" 命中 "Bitget"
    zh2 = [(r"(?<![A-Za-z])" if re.match(r"[A-Za-z]", p) else "") + p + (r"(?![A-Za-z])" if re.search(r"[A-Za-z]$", p) else "") for p in zh]
    pats = [r"(?<![A-Za-z])" + p + r"(?![A-Za-z])" for p in en] + zh2
    return re.compile("|".join("(?:" + p + ")" for p in pats), re.I) if pats else None


for _c in CATEGORIES:
    _c["rx"] = _rx(_c.get("en", []), _c.get("zh", []))
    _c["rx_big"] = re.compile(_c["big_need"], re.I) if _c.get("big_need") else None
    _c["rx_dir"] = [(re.compile(p, re.I), d, r) for p, d, r in _c.get("dir_rules", [])]

META = [{"key": c["key"], "label": c["label"], "dir": c["dir"], "big": bool(c["big"])} for c in CATEGORIES]


def extract_coins(title, summary, tokens):
    """影响币种：关注列表命中 → $cashtag → 项目名映射 → 标题中的大写代码（排除常见缩写）。"""
    out = []

    def add(t):
        t = t.upper()
        if t and t not in out and t not in NOT_TICKER and not re.fullmatch(r"\d+[A-Z]?", t):
            out.append(t)

    for t in tokens or []:
        add(t)
    for m in CASHTAG.finditer(title + " " + (summary or "")[:200]):
        add(m.group(1))
    low = title.lower()
    for name, tk in NAME2TICKER.items():
        if (CJK.search(name) and name in low) or re.search(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", low):
            add(tk)
    for m in CAPS.finditer(title):
        w = m.group(1)
        if len(w) >= 2 and not re.fullmatch(r"V?\d[\dA-Z]*", w) and not re.search(r"\d{3,}", w):
            add(w)
    return out[:6]


def classify(title, summary="", tokens=(), summary_factor=0.5):
    """返回 dict(impact, impact_label, direction, direction_reason, coins, impact_tag, boost, big_ok, where)。"""
    hits = []
    for i, c in enumerate(CATEGORIES):
        if c["rx"].search(title):
            hits.append((c["boost"], -i, c, "标题"))
        elif summary and c["rx"].search(summary):
            hits.append((c["boost"] * summary_factor, -i, c, "摘要"))
    coins = extract_coins(title, summary, tokens)
    if not hits:
        return dict(impact=None, impact_label=None, direction="中性", direction_reason="未识别到供给/流动性事件",
                    coins=coins, impact_tag=None, boost=0.0, big_ok=False, where=None, impacts=[])
    hits.sort(key=lambda x: (x[3] == "标题", x[0], x[1]), reverse=True)
    boost, _, c, where = hits[0]
    text = title if where == "标题" else summary
    direction, reason = c["dir"], c["reason"]
    for rx, d, r in c["rx_dir"]:
        if rx.search(text):
            direction, reason = d, r
            break
    big_ok = bool(c["big"]) and where == "标题" and (c["rx_big"] is None or bool(c["rx_big"].search(title)))
    label = c["label"]
    if c["key"] == "unlock" and big_ok:
        label = "大额解锁"
    coin_txt = " ".join("$" + x for x in coins[:3]) if coins else ("全市场" if c["key"] in ("macro", "etf_flow") else "")
    tag = "【%s】%s%s" % (direction, (coin_txt + " ") if coin_txt else "", label)
    return dict(impact=c["key"], impact_label=label, direction=direction, direction_reason=reason, coins=coins,
                impact_tag=tag, boost=round(boost, 2), big_ok=big_ok, where=where,
                impacts=[h[2]["key"] for h in hits])
