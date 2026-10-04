# delist_tracker — 交易所现货下架追踪（与 unlock_tracker 同风格）

找出指定窗口内**已实际停止交易**的现货下架（整币下架；仅移除部分交易对的单独列出），并计算：
- 下架前 7 天逐日（D-7..D-1，24h 桶，锚定下架时刻）涨跌 + 7 天复利合计
- 下架至今涨跌、下架后最低/最高、下架后 24h
- 公告时刻 → 下架时刻涨跌、公告后 24h（公告是否落在下架前 7 天内）
- 同窗口 BTC（Binance BTCUSDT）对比、相对 BTC（pp）
- 价值：CoinGecko market_chart 下架时刻的市值与 24h 成交额

只用标准库。公共代码（HTTP缓存、Binance/CoinGecko 取价、window_metrics、格式化）从 `../unlock_tracker/unlock_tracker.py` 导入，**未改动** unlock_tracker。

## 运行
```bash
cd /workspace/trading-watch/delist_tracker
# 默认：最近7天，交易所 binance,okx,bybit,bitget,upbit,coinbase
python3 delist_tracker.py
# 复现 2026-10-01 报告
python3 delist_tracker.py --start 2026-09-24 --end 2026-10-01 --now "2026-10-01 17:00" \
  --extra-events extra_events_example.csv \
  --exchanges binance,okx,bybit,bitget,upbit,coinbase,gate \
  --out output/test_extra
```
| 参数 | 说明 |
|---|---|
| `--start/--end` | 窗口 YYYY-MM-DD（UTC+8，end 含当日且不超过 now），默认 now-7 天 ~ now |
| `--now` | 固定"现在"`'YYYY-MM-DD HH:MM'`(UTC+8)，用于复现 |
| `--exchanges` | 逗号分隔：binance,okx,bybit,bitget,upbit,coinbase,gate |
| `--news-url` | Coinbase 停牌新闻 URL（可多次），用正则解析 "suspend trading … (SYM) … on DATE … around H PM ET" |
| `--extra-events` | 手工补充事件 CSV（见 `extra_events_example.csv`），**优先级最高**，同币同所时覆盖自动结果 |
| `--id-map SYM=gecko-id` | 手工指定 CoinGecko id（可多次） |
| `--include-pairs` | 仅移除部分交易对的事件也做价格分析 |
| `--min-mcap` | 下架时市值低于该值（美元）不展示 |
| `--out` / `--cache` / `--refresh` / `--quiet` | 输出目录（默认 `output/<start>_<end>/`）/ 缓存目录 / 忽略缓存 / 静默 |

## 输出
- `delistings.csv`：每条已执行整币下架一行（公告/下架时间、间隔天数、CoinGecko id 与匹配方式、价格源、市值/成交额、各项涨跌、BTC、flags、来源 URL）
- `pre7d_daily.csv`：D-7..D-1 逐日（代币与 BTC）
- `other_events.csv`：仅交易对移除 / 已推迟 / 尚未执行的事件
- `summary.md`：中文报告（表1、表2、BTC 同窗、自动观察、其他事件、未映射/错误、来源）
- `run_meta.json`：参数、各源解析数、错误、未映射、计数

## 数据源与解析
| 交易所 | 方式 | 说明 |
|---|---|---|
| Binance | `www.binance.com/bapi/composite/v1/public/cms/article/list/query?catalogId=161` + 文章详情 | 解析 "Binance Will Delist … on …"（排除 Margin/Futures/Loan/Alpha 等）与 "Notice of Removal of Spot Trading Pairs"（kind=pair） |
| OKX | `www.okx.com/api/v5/support/announcements?annType=announcements-delistings` + 帮助页 | 解析表格 `SYM/QUOTE Month D, YYYY, HH:MM - HH:MM UTC`；币的最后一个交易对时间=整币下架，更早的为仅交易对 |
| Bybit | `announcements.bybit.com` 列表页 `__NEXT_DATA__`（1-4 页）+ 文章页 | api.bybit.com 从本机 403（CloudFront），故抓官网公告页 |
| Bitget | `api.bitget.com/api/v2/public/annoucements?annType=symbol_delisting`（cursor 翻页）+ 文章 | 识别后续 "delay the delisting" 公告并标记为已推迟 |
| Upbit | `api-manager.upbit.com/api/v1/announcements?category=trade` + 详情 | 解析 "거래지원 종료" 公告中的 이름(SYM)、대상 페어、종료 예정일 KST |
| Coinbase | 无公告 API：`--news-url` 新闻解析 + `api.exchange.coinbase.com/products/{SYM}-USD` 状态核验 | 公告时间=新闻发稿时间（近似）；推荐用 extra CSV 填准确时间 |
| Gate | 本机访问 www.gate.com 返回 403 | 仅通过 `--extra-events` |

价格规则：下架所≠Binance 且 Binance 仍有 USDT 对、Binance 现价与 CoinGecko 偏差≤10% → Binance 1h K线（data-api.binance.vision）；否则 CoinGecko 聚合小时价。CoinGecko id：给定 id → `--id-map` → 名称+符号搜索 → 仅符号搜索（唯一或排名领先≥3倍才接受，flag `gecko_by_symbol`），否则列入"未映射"。

## 测试结果（2026-09-24 ~ 2026-10-01 17:00）
与人工 Part A（`../delistings_2026-10-01.csv`、`../delist_pre7d_daily_2026-10-01.csv`）对比：
- 识别出同样 12 条已执行整币下架（IOTX/BADGER/STORJ@Coinbase 需 `--news-url` 或 extra CSV）；仅交易对、Bitget 推迟、OKX 10/03 待执行等也一致。
- 市值、成交额、下架前 7 天逐日与合计、BTC 同窗**完全一致**。
- "下架至今"有 0.0–1.4pp 差异（如 ICX −21.8% vs −23.2%），原因是"现在"价格取数时刻不同（人工 16:59/16:00 的 CoinGecko 点、Binance 未收盘 K 线 vs 程序后取的 17:00 点）。
- 公告→下架：程序取 90 天 CoinGecko 数据，能算出人工版因只取 16 天而为 N/A 的值（如 IOTX、SNX、BADGER、STORJ）；仅用 `--news-url` 时 Coinbase 公告时间为新闻发稿时间（如 TradingView 08-29 19:55），与实际公告日（08-28）不同。

## 已知限制
- Bybit API、Gate 网站从本机被拦（403）；Bybit 改抓官网页面，Gate 只能手工补充。
- Coinbase 无公告 API，依赖新闻/手工；公告精确时刻常未知。
- Bitget 公告接口只返回最近约 30 天；公告回看默认 60 天，更早公告的长间隔下架（如 Upbit 提前 1 个月公告）若超出回看可能漏掉。
- CoinGecko 小时级数据仅约 90 天内，且公共 API 限速（~6.5 秒/次），首次运行约 3 分钟；有缓存后很快。
- 仅符号匹配 CoinGecko 可能误配（已 flag），必要时用 `--id-map`。
- 解析依赖公告标题/正文格式（正则），交易所改版会失效；无法解析的公告会在 stderr 和 run_meta 中提示。
- 依赖 `../unlock_tracker/unlock_tracker.py`，两目录需保持相对位置。
