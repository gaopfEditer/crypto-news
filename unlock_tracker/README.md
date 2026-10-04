# unlock_tracker：代币解锁 + 解锁前后价格表现

用法：给定一个日期窗口（UTC+8），找出窗口内**已经发生**的代币解锁，并计算每个代币：
- 解锁前 7 天逐日涨跌（D-7…D-1，每天是锚定解锁时刻的 24h 桶，7 个值连乘等于"解锁前7天合计"）
- 解锁时价格、解锁前 7 天的价格、解锁后 24h 的价格、当前价格；解锁至今涨跌；解锁后最高/最低相对解锁价
- 同一时间窗口的 BTC 涨跌，以及相对 BTC 的超额（pp）

只用 Python 标准库（Python ≥ 3.9），不需要 pip 安装。

## 运行

```bash
cd /workspace/trading-watch/unlock_tracker
# 默认：最近 7 天（截至现在），数据源 = DefiLlama + CoinMarketCap 快照
python3 unlock_tracker.py

# 指定窗口 + 加入新闻源（PANews 等"下周解锁"快讯，数据来自 Token Unlocks/Tokenomist）
python3 unlock_tracker.py --start 2026-09-24 --end 2026-10-01 \
  --news-url https://www.panewslab.com/zh/articles/01a0bea7-77b7-742c-8612-2806cba8b182 \
  --news-url https://www.panewslab.com/zh/articles/01a0e2d7-22b1-76f5-9456-18803fa3ef69

# 复现某个时刻的结果（价格只用该时刻之前的数据）
python3 unlock_tracker.py --start 2026-09-24 --end 2026-10-01 --now "2026-10-01 12:42"
```

## 参数

| 参数 | 说明 |
|---|---|
| `--start / --end` | 窗口日期 YYYY-MM-DD（UTC+8）。start 从当天 00:00 算起，end 算到当天 23:59，但不会超过"现在"。不传时默认窗口 = 现在往前 7 天 |
| `--now "YYYY-MM-DD HH:MM"` | 把"现在"固定在某个时刻，用来复现历史结果 |
| `--min-usd` (默认 5e6) | 解锁价值阈值（美元） |
| `--min-pct` (默认 1) | 占流通比阈值（%）。价值或占流通比满足任一个即保留 |
| `--min-usd-floor` (默认 5e5) | 只靠占流通比入选的事件，价值也不能低于这个数，用来过滤极小盘 |
| `--sources` (默认 `defillama,cmc`) | 自动数据源，逗号分隔；传 `none` 表示只用新闻 / 手工源 |
| `--news-url URL` | 解析新闻快讯里的解锁条目，可多次传入。支持 PANews 中文/英文、火星财经等常见句式，例如"Plasma（XPL）将于北京时间9月25日晚上8点解锁约17.6亿枚代币，与流通量的比值约为63.20%，价值约1.58亿美元" |
| `--extra-events CSV` | 手工补充事件，格式见 `extra_events_example.csv` |
| `--max-tokens` (默认 40) | 按价值排序，最多保留多少个事件 |
| `--out DIR` | 输出目录，默认 `./output/<start>_<end>/` |
| `--cache DIR` | 缓存目录，默认 `./cache` |
| `--refresh` | 忽略缓存，全部重新抓取 |
| `--quiet` | 不在 stdout 打印 markdown 摘要 |

## 输出（在 `--out` 目录下）
- `unlocks.csv`：每个解锁事件一行。包括数量、来源报的价值、按解锁时价格算的价值、占流通比、接收方、来源、各来源报的时间和数量、标记，以及所有价格和涨跌指标。缺失值写 `N/A`。
- `pre7d_daily.csv`：每个事件两行（代币一行、BTC 一行），D-7…D-1 逐日涨跌和 7 天合计。
- `summary.md`：中文摘要（三张表 + 只根据数字自动生成的观察 + 未映射/异常 + 数据来源），同时打印到 stdout。
- `run_meta.json`：本次运行的参数、各数据源统计、错误、未映射代币。

## 数据源与逻辑
1. **DefiLlama**：解析 `https://defillama.com/unlocks` 页面里内嵌的 `__NEXT_DATA__`（约 370 个协议，含历史和未来事件）。`api.llama.fi/emissions` 是付费接口（HTTP 402），所以不用。同一代币 12 小时内的多笔事件合并成一次解锁。
2. **CoinMarketCap**：`api.coinmarketcap.com/data-api/v3/token-unlock/listing`（公开，无需登录）。这个接口**只给每个代币的"下一次"解锁**，所以程序每次运行会存一份快照到 `cache/cmc_snapshots/`，之后查询过去的窗口时会用历史快照。**需要定期运行（比如每天一次，用 cron）才能覆盖过去的窗口**；第一次运行时它对过去窗口没有贡献。
3. **新闻 / 手工源**：Tokenomist 的历史数据需要登录，CryptoRank 被 Cloudflare 拦截（403），所以 Tokenomist 的数据通过 PANews 等转载的快讯（`--news-url`）或手工 CSV 引入。
4. **合并去重**：同一 symbol、不同来源、时间相差 ≤48h 的事件视为同一次解锁。合并后的主字段按优先级取：新闻/手工 > CMC > DefiLlama。会打以下标记：
   - `single_source`：只有一个来源
   - `date_conflict(Nh)`：来源之间时间相差超过 6h
   - `amount_conflict`：来源之间数量相差超过 25%
   - `short_post_window`：解锁后不足 24h
   - `no_price_data`：没拿到价格
   - 只统计已经发生的事件；未来的事件不参与合并
5. **价格**：
   - 优先用 Binance 现货 1h K 线（`data-api.binance.vision`，因为 `api.binance.com` 在本机返回 451）。但只有当 Binance 上同名 USDT 交易对的现价与 CoinGecko（或 DefiLlama）现价相差 ≤10% 时才采用，防止同名不同币。
   - 否则用 CoinGecko `/coins/{id}/market_chart` 的小时数据。CoinGecko id 优先用数据源里给的，否则用 CoinGecko search 查找，要求 symbol 完全一致且名称匹配。
   - 都找不到就列在"未映射"里，**不猜**。
   - 锚点价 = 该时刻或之前最近的一个价格点；如果这个点比锚点早超过 3h，记为 N/A。BTC 基准统一用 Binance BTCUSDT。
6. **礼貌请求**：所有原始 JSON/HTML 都缓存在 `cache/raw/`（DefiLlama 3h、价格 20min、CoinGecko 搜索 7 天）。CoinGecko 请求间隔约 6.5 秒，遇到 429/5xx 会退避重试。

## 已知局限
- DefiLlama 覆盖不全，部分代币的排期过时或和 Tokenomist 不一致：2026-09-24~10-01 这个窗口里，SOSO、BIGTIME、KMNO、STBL、CARDS 没有，H 的时间早约 35h，FF、SUI 的数量不同。想要全面就加 `--news-url` 或 `--extra-events`。
- 新闻里的价值是发稿时按当时价格估算的；`value_usd_at_unlock` 是按解锁时价格重新算的。
- CoinGecko 小时数据只覆盖最近 90 天，更早的窗口如果不在 Binance 上会是 N/A。
- 新闻解析依赖固定句式，媒体换了写法可能解析不到（会在 stderr 打印每个 URL 解析出几条）。
