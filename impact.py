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
    dict(key="hack", label="黑客/漏洞", boost=3, big=True, dir="利空", reason="资金被盗/漏洞被利用导致损失，恐慌抛售与流动性抽离",
         en=[r"hack(?:ed|er|ers|s)?", r"drain(?:ed|s)?", r"stolen", r"rug ?pull", r"compromised",
             r"exploit(?:ed|s)?\b.{0,40}\b(?:drain|steal|stolen|fund|million|\$|\d+\s*(?:ETH|BTC|USD))",
             r"(?:lost|lose[sd]?)\b.{0,30}\b(?:\$|\d+\s*(?:million|m\b|ETH|BTC))"],
         zh=[r"黑客", r"被盗", r"漏洞攻击", r"遭攻击", r"被攻击", r"盗取", r"跑路", r"资金损失"]),
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
    dict(key="etf_expected", label="ETF申请/预期", boost=2, big=False, dir="利好", reason="ETF申请或预期获批，尚未落地",
         en=[r"(?:ETF|ETP)s?\b.{0,50}\b(?:expected|expect(?:s|ed|ation)?|before\s+\d{4}|by\s+\d{4}|fil(?:es|ed|ing)|S-1|19b-4|application|seek(?:s|ing)?|proposal|could|may|might|soon(?:er)?)",
             r"(?:expected|expect(?:s|ed)|fil(?:es|ed|ing)|application|seek(?:s|ing)?|could|may|might)\b.{0,80}\b(?:ETF|ETP)s?",
             r"(?:ETF|ETP)s?\b.{0,60}\blaunch(?:es|ed)?\s+sooner"],
         zh=[r"ETF.{0,20}(?:预计|预期|有望|申请|提交|S-1)", r"(?:预计|预期|有望|将).{0,40}ETF", r"ETF.{0,30}(?:将|會).{0,20}(?:推出|上市)"]),
    dict(key="etf", label="ETF获批/上市", boost=3, big=False, dir="利好", reason="ETF获批/上市，打开机构资金入口",
         dir_rules=[(r"(?:reject(?:s|ed)?|拒绝|推迟|delay(?:s|ed)?)", "利空", "ETF被拒/推迟")],
         en=[r"(?:ETF|ETP)s?\b.{0,50}\b(?:approv(?:es|ed|al)|launch(?:es|ed)?|debut(?:s|ed)?|(?:begin|start)s?\s+trading|list(?:s|ed|ing)\b.{0,15}\b(?:on|at|trading))",
             r"(?:approv(?:es|ed|al)|launch(?:es|ed)?)\b.{0,50}\b(?:ETF|ETP)s?"],
         zh=[r"ETF.{0,20}(?:获批|批准|正式上市|上市交易)", r"(?:批准|获批|正式上市).{0,20}ETF"]),
    dict(key="perp_listing", label="新永续合约/高杠杆", boost=3, big=False, dir="利好", reason="新增合约流动性（高杠杆可能放大波动）",
         en=[r"(?:perp(?:etual)?s?|futures)\b.{0,30}\b(?:list(?:s|ed|ing)|launch(?:es|ed)?|go(?:es)? live|add(?:s|ed)?)", r"(?:list(?:s|ed|ing)|launch(?:es|ed)?|add(?:s|ed)?)\b.{0,30}\b(?:perp(?:etual)?s?|futures)", r"\d{3,4}x leverage", r"1000x"],
         zh=[r"(?:永续|合约).{0,10}(?:上线|上架|推出)", r"(?:上线|上架|推出).{0,15}(?:永续|U本位|币本位)合约", r"\d{3,4}\s*倍杠杆", r"1000x"]),
    dict(key="listing", label="交易所上币", boost=3, big=False, dir="利好", reason="新增交易所上币，流动性与曝光提升",
         en=[r"(?<!de)list(?:s|ed|ing)\b.{0,30}\b(?:on|at)\b.{0,15}(?:Binance|Coinbase|OKX|Upbit|Bybit|Bithumb|Kraken|Robinhood|Bitget|Gate|KuCoin|HTX|Huobi|MEXC|Hyperliquid|BingX|LBank)",
             r"(?:Binance|Coinbase|OKX|Upbit|Bybit|Bithumb|Kraken|Robinhood|Bitget|Gate|KuCoin|HTX|MEXC|Hyperliquid)\b.{0,20}\b(?:to list|lists|will list|adds?)\b"],
         zh=[r"(?:Binance|币安|OKX|Bybit|Coinbase|Upbit|Bithumb|Gate|Bitget|KuCoin|HTX|Huobi|MEXC|Kraken|Hyperliquid).{0,12}(?:上币|上架|上线|开放交易)",
             r"(?:上币|上架|上线).{0,12}(?:Binance|币安|OKX|Bybit|Coinbase|Upbit|Bithumb|Gate|Bitget|KuCoin|HTX|MEXC|Kraken)",
             r"(?:\$?[A-Z][A-Z0-9]{2,9}).{0,10}(?:上币|上架)", r"开放.{0,6}交易对"]),
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
    "injective": "INJ",
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
# 现货 ETF 基金代码（非链上代币）
NOT_TICKER |= {
    "IBIT", "FBTC", "FETH", "ETHA", "ETHW", "ETHE", "GBTC", "ARKB", "BITB", "EZBC", "HODL", "BTCW", "BRRR",
    "BTCO", "YBIT", "BSOL", "THYP", "SOLZ", "SOLT", "ETHV", "QETH", "CETH", "ETHB",
}

EXCHANGE_NAMES = re.compile(
    r"(?i)(?:Binance|币安|OKX|Bybit|Coinbase|Upbit|Bithumb|Gate\.?io|\bGate\b|Bitget|KuCoin|HTX|Huobi|MEXC|Kraken|Hyperliquid|Robinhood|BingX|LBank|CoinW|Crypto\.com)",
)
NON_CRYPTO_LISTING_CTX = re.compile(
    r"(?i)(?:Amazon|AWS|Bedrock|Azure|Google Cloud|GCP|Vertex AI|OpenAI|Anthropic|Claude|GPT|大模型|LLM|language model|model (?:marketplace|hub|store))",
)
# 交易所自有撮合/平台公测 ≠ 某代币上币
LISTING_INFRA_CTX = re.compile(
    r"(?i)(?:"
    r"公测|内测|public beta|open beta|"
    r"Exchange\s+OS|开放撮合|matching engine|"
    r"链上现货和永续|on-?chain spot and (?:perp|perpetual)|"
    r"trading (?:stack|infrastructure|platform)"
    r")"
)
LISTING_TOKEN_SIGNAL = re.compile(
    r"(?i)(?:"
    r"(?:to list|will list|lists|listing)\s+(?:\$?(?:[A-Z][A-Z0-9]{1,9}|[A-Za-z]{2,15})\b)|"
    r"list(?:s|ed|ing)\s+(?:the\s+)?\$[A-Za-z][A-Za-z0-9]+\b|"
    r"(?:上币|上架)\s*(?:\$|[A-Z][A-Z0-9]{2,9})|"
    r"(?:\$?[A-Z][A-Z0-9]{2,9}).{0,10}(?:上币|上架)|"
    r"上线.{0,12}(?:交易对|trading pair)|"
    r"开放.{0,6}交易对|"
    r"adds?\s+(?:support for\s+)?\$?[A-Z][A-Z0-9]{2,9}\b"
    r")"
)
CRYPTO_CONTEXT = re.compile(
    r"(?i)(?:crypto|cryptocurrency|blockchain|token|coin|DeFi|NFT|stablecoin|web3|on-?chain|"
    r"trading pair|spot market|perpetual|futures|memecoin|airdrop|ETF|ETP|"
    r"代币|区块链|加密|上币|交易所|现货|合约|链上)",
)

# 含 ETF 且为预期/申请语境时，不得命中 etf（获批/上市）
ETF_EXPECTATION = re.compile(
    r"(?i)(?:"
    r"预计|预期|有望|申请|提交|S-1|19b-4|"
    r"expects?|expected|could|may|might|fil(?:es|ed|ing)|application|seek(?:s|ing)?|proposal|"
    r"before\s+\d{4}|by\s+\d{4}|soon(?:er)?|"
    r"将.{0,20}(?:推出|上市|launch)|"
    r"(?:预计|预期|有望|将).{0,40}(?:ETF|ETP)"
    r")"
)

CASHTAG = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,11})(?![A-Za-z0-9])")
CAPS = re.compile(r"(?<![A-Za-z0-9$])([A-Z][A-Z0-9]{1,9})(?![A-Za-z0-9])")

# MicroStrategy / Strategy 优先股代码（非 Starknet 等 crypto STRK）
STRATEGY_PREF_TICKERS = frozenset({"STRF", "STRC", "STRK", "STRD"})
STRATEGY_PREF_CTX = re.compile(
    r"(?i)(?:"
    r"MicroStrategy|\bMSTR\b|"
    r"Strategy.{0,40}优先股|"
    r"优先股.{0,60}(?:STRF|STRC|STRK|STRD)|"
    r"(?:STRF|STRC|STRK|STRD)(?:\s*[,、／/]\s*(?:STRF|STRC|STRK|STRD)){1,}|"
    r"preferred\s+(?:stock|share|equity|note)s?"
    r")"
)


def strategy_preferred_blocks_ticker(text, ticker):
    """标题/摘要为 Strategy 优先股语境时，STRF/STRC/STRK/STRD 不作加密币种。"""
    t = (ticker or "").upper()
    if t not in STRATEGY_PREF_TICKERS:
        return False
    return bool(STRATEGY_PREF_CTX.search(text or ""))


# 改进提案/标准编号（EIP-1559、XIP-Exchange OS 等），非 $cashtag 时不作币种
IMP_PROPOSAL_ACRONYMS = frozenset(
    {"EIP", "ERC", "BIP", "XIP", "SIMD", "AIP", "TIP", "NEP", "CIP", "SIP", "KIP", "MIP", "OIP", "LIP"}
)
IMP_PROPOSAL_HYPHEN = re.compile(
    r"(?i)\b(" + "|".join(sorted(IMP_PROPOSAL_ACRONYMS, key=len, reverse=True)) + r")-(?:\d[\dA-Za-z]*|[A-Za-z][\w-]*)"
)
EXCHANGE_OS_PRODUCT = re.compile(r"(?i)Exchange\s+OS\b")


def improvement_proposal_blocks_ticker(text, ticker, *, from_cashtag=False):
    if from_cashtag:
        return False
    t = (ticker or "").upper()
    blob = text or ""
    if t == "OS" and EXCHANGE_OS_PRODUCT.search(blob):
        return True
    if t not in IMP_PROPOSAL_ACRONYMS:
        return False
    if IMP_PROPOSAL_HYPHEN.search(blob):
        return True
    return True


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


def _etf_expectation_context(text):
    if not re.search(r"(?i)ETF|ETP", text):
        return False
    return bool(ETF_EXPECTATION.search(text))


def _text_matches_category(c, text):
    if not c["rx"].search(text):
        return False
    if c["key"] == "etf" and _etf_expectation_context(text):
        return False
    if c["key"] == "listing":
        if NON_CRYPTO_LISTING_CTX.search(text):
            return False
        if not EXCHANGE_NAMES.search(text):
            return False
        if LISTING_INFRA_CTX.search(text) and not LISTING_TOKEN_SIGNAL.search(text):
            return False
        if not LISTING_TOKEN_SIGNAL.search(text):
            return False
    return True


def extract_coins(title, summary, tokens):
    """影响币种：关注列表命中 → $cashtag → 项目名映射 → 标题中的大写代码（需 crypto 语境）。"""
    out = []
    blob = title + " " + (summary or "")[:200]
    crypto_ctx = bool(CRYPTO_CONTEXT.search(blob))

    def add(t, from_cashtag=False):
        t = t.upper()
        if strategy_preferred_blocks_ticker(blob, t):
            return
        if improvement_proposal_blocks_ticker(blob, t, from_cashtag=from_cashtag):
            return
        if t and t not in out and t not in NOT_TICKER and not re.fullmatch(r"\d+[A-Z]?", t):
            out.append(t)

    for t in tokens or []:
        add(t)
    for m in CASHTAG.finditer(blob):
        add(m.group(1), from_cashtag=True)
    low = title.lower()
    for name, tk in NAME2TICKER.items():
        if (CJK.search(name) and name in low) or re.search(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", low):
            add(tk)
    if crypto_ctx or out:
        for m in CAPS.finditer(title):
            w = m.group(1)
            if len(w) >= 2 and not re.fullmatch(r"V?\d[\dA-Z]*", w) and not re.search(r"\d{3,}", w):
                add(w)
    return out[:6]


def classify(title, summary="", tokens=(), summary_factor=0.5):
    """返回 dict(impact, impact_label, direction, direction_reason, coins, impact_tag, boost, big_ok, where)。"""
    hits = []
    for i, c in enumerate(CATEGORIES):
        if _text_matches_category(c, title):
            hits.append((c["boost"], -i, c, "标题"))
        elif summary and _text_matches_category(c, summary):
            hits.append((c["boost"] * summary_factor, -i, c, "摘要"))
    coins = extract_coins(title, summary, tokens)
    if not hits:
        return dict(impact=None, impact_label=None, direction="中性", direction_reason="未识别到供给/流动性事件",
                    coins=coins, impact_tag=None, boost=0.0, big_ok=False, where=None, impacts=[])
    hits.sort(key=lambda x: (x[3] == "标题", x[0], x[1]), reverse=True)
    boost, _, c, where = hits[0]
    text = title if where == "标题" else summary
    if c["key"] == "etf" and _etf_expectation_context(text):
        alt = next((h for h in hits if h[2]["key"] == "etf_expected"), None)
        if alt:
            boost, _, c, where = alt
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
