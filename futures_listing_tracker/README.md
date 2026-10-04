# futures_listing_tracker — 新上线 USDT 永续合约追踪

找出窗口内**已开放交易**的加密货币 USDT 本位永续合约（剔除 TradFi/股票/Pre-IPO/外汇/交割），按代币分组，以最早上线的交易所为锚点，计算：
- 上合约前 7 天逐日（D-7..D-1，24h 桶，锚定上合约时刻）+ 7 天复利合计；新币无 7 天前历史记 N/A
- 上合约至今、上线后最低/最高、公告→上线涨跌
- BTC 同窗口（Binance BTCUSDT）、相对 BTC
- 价值：CoinGecko 上合约时刻市值 + 24h 成交额（CoinGecko 市值为 0/缺失记 N/A）
- 最高杠杆、该交易所当时是否已有现货；同一代币其他交易所的后续上线单列

只用标准库。复用 `../unlock_tracker/unlock_tracker.py`（HTTP缓存、Binance/CoinGecko 取价、window_metrics、格式化）和 `../delist_tracker/delist_tracker.py`（html_text、parse_en_dt、binance_body_text、map_gecko），**均未修改**。

## 运行
```bash
cd /workspace/trading-watch/futures_listing_tracker
python3 futures_listing_tracker.py                     # 默认最近7天，binance,okx,bybit,bitget
# 复现 2026-10-01 报告
python3 futures_listing_tracker.py --start 2026-09-24 --end 2026-10-01 --now "2026-10-01 17:30" \
  --extra-events extra_events_example.csv --include-tradfi --out output/test_2026-09-24_2026-10-01
python3 compare_with_manual.py output/test_2026-09-24_2026-10-01   # 与人工结果逐项对比
```
| 参数 | 说明 |
|---|---|
| `--start/--end` | 窗口 YYYY-MM-DD（UTC+8，end 含当日且不超过 now），默认 now-7 天 ~ now |
| `--now` | 固定"现在"（UTC+8）用于复现 |
| `--exchanges` | binance,okx,bybit,bitget |
| `--extra-events` | 补充/覆盖 CSV：`exchange,symbol,launch_time_utc8,announce_time_utc8,announce_note,max_leverage,coingecko_id,source_url`。主要用于填 OKX 公告时间 |
| `--id-map SYM=gecko-id` | 手工指定 CoinGecko id |
| `--alias EX:CODE=SYM` | 合约代码别名（内置 `OKX:QUANT=QNT`：OKX 的 QUANT-USDT-SWAP 是 Quant，而 QNT-USDT-SWAP 是股票合约） |
| `--include-tradfi` | 在 other_events/summary 中列出被剔除的 TradFi 合约 |
| `--min-mcap` | 锚点市值低于该值不展示（市值 N/A 的保留） |
| `--out/--cache/--refresh/--quiet` | 输出目录 / 缓存 / 忽略缓存 / 静默 |

## 输出
- `listings.csv`：每个（代币, 交易所）上线事件一行，role=anchor/secondary，含公告/上线时间及来源、杠杆、现货状态、CoinGecko id 与匹配方式、价格源、市值/成交额、各项涨跌、BTC、flags、链接
- `pre7d_daily.csv`：D-7..D-1 逐日（代币与 BTC）
- `other_events.csv`：已公告未上线（upcoming）、被剔除的 TradFi 合约
- `summary.md`：中文报告（表1锚点、后续上线、表2逐日+BTC合计、BTC逐日、自动观察、未上线、剔除、异常、来源）
- `run_meta.json`：参数、各源解析数、错误、未映射、计数

## 数据源
| 交易所 | 上线事件 | 上线时间 | 公告时间 | 现货状态 |
|---|---|---|---|---|
| Binance | `www.binance.com/fapi/v1/exchangeInfo`（fapi.binance.com 从本机 451）+ CMS 公告 catalogId=48 | 公告时间行 / onboardDate，1m K线核验 | 公告发布时间 | data-api exchangeInfo |
| OKX | `/api/v5/public/instruments?instType=SWAP`（instCategory=1 为加密） | listTime，1m K线核验首笔成交 | **不可自动获取**（公告 API 按 IP 地区过滤，本机为美国站），需 extra CSV | SPOT instruments listTime（含集合竞价开始） |
| Bybit | announcements.bybit.com new_crypto 列表 + 文章（api.bybit.com 被 CloudFront 屏蔽） | "Trading is now open" 类 = 公告时间；否则解析标题/正文 UTC 时间 | 文章 date | 无法核实 |
| Bitget | 公告 coin_listings(annSubType=futures) + mix 合约列表 | 首笔 1m K线（合约 API 的 launchTime 为空） | 公告 cTime | spot symbols openTime |

价格规则：代币在 Binance 有现货 → Binance 1h；否则 CoinGecko 小时价；CoinGecko 在上合约时刻无数据 → OKX 现货 1h（XDP 即此情况）。同一代币所有交易所使用同一条价格序列（由锚点决定）。"现在"价格 = 不晚于 now 的最后一个价格点（小时级，固定 now=17:30 时实际为 17:00 点）。

## 测试结果（2026-09-24 00:00 ~ 2026-10-01 17:30）
- 找到 4 个代币 / 9 个上线事件（KII、XDP、CT、QNT），与人工 Part A（`../perp_listings_2026-10-01.csv`、`../perp_listings_pre7d_daily_2026-10-01.csv`）**逐项一致**（`compare_with_manual.py`：0 差异，含逐日 D-7..D-1）。
- 程序还自动发现了 Bybit CTUSDT（10-01 17:26，在人工初稿后发布），已补入 Part A。
- 剔除 23 个 TradFi/股票合约（Binance 12、Bybit 3、Bitget 8）。
- 不带 `--extra-events` 时 OKX 公告时间与公告→上线为 N/A；不带 `--now` 时"至今"按实时价格计算，会与固定 now 的结果不同。

## 已知限制
- OKX 合约公告无法从本机获取（地区过滤），公告时间依赖 extra CSV（如 Tokenearly/新闻转录，可能有几分钟误差）。
- Bybit API 被屏蔽：只能靠公告页，无法核验首笔成交和现货状态；没有公告的上线会漏掉；列表只看前 4 页。
- Binance 期货元数据走 www.binance.com/fapi 镜像，若被封需改源；已下架合约不在 exchangeInfo（靠公告补）。
- Bitget 公告接口只覆盖近期；上线时间依赖 1m K线（公告后 3h 内）。
- CoinGecko：新币市值常为 0（记 N/A）；小时粒度，同一小时内的多次上线取到同一价格（如 CT Bitget/Binance）；数据可能晚于上线开始（改用 OKX 现货）。
- 符号冲突（QUANT/QNT 等）需 alias/id-map；仅符号匹配有误配风险（已 flag）。
- TradFi 判定基于 contractType/instCategory/isRwa/标题关键词的启发式；非 ASCII 合约代码（如 龙虾USDT）跳过。
- 依赖 `../unlock_tracker/` 与 `../delist_tracker/` 的相对位置。
