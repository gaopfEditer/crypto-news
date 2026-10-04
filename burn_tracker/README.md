# burn_tracker：代币销毁追踪 + 销毁前后价格表现

只用 Python 标准库。复用 `../unlock_tracker/unlock_tracker.py`（HTTP 缓存、Binance/CoinGecko 取价、`window_metrics`、格式化）和 `../delist_tracker/delist_tracker.py`（`map_gecko`、`html_text`）。这两个模块只 import，不修改，所以其他 tracker 不受影响。

## 用法

```bash
cd /workspace/trading-watch/burn_tracker
# 复现 2026-09-24 ~ 2026-10-01 窗口（与 Part A 手工结果一致）
python3 burn_tracker.py --start 2026-09-24 --end 2026-10-01 --now "2026-10-01 18:00" \
    --extra-events extra_events_example.csv --out output/test_2026-09-24_2026-10-01
python3 compare_with_manual.py output/test_2026-09-24_2026-10-01   # 与 ../burns_2026-10-01.csv 逐字段对比

# 日常：最近 7 天
python3 burn_tracker.py --extra-events my_burns.csv
```

| 参数 | 说明 |
|---|---|
| `--start/--end` | 窗口，按 UTC+8 日期，end 当天包含在内，且不超过 now |
| `--now` | 固定"现在"（UTC+8），用于复现 |
| `--sources` | 默认 `hyperliquid,pump,panews,odaily`。为兼容其他 tracker 也接受 `--exchanges`，但本工具会忽略它 |
| `--extra-events` | 补充/覆盖 CSV，**是最可靠的输入**，格式见下 |
| `--news-url` | 额外的新闻/公告页，页面需带 `datePublished`，可多次传入 |
| `--id-map SYM=gecko-id` | 手工指定 CoinGecko id |
| `--min-usd / --min-pct` | 入表阈值：价值 ≥ 100 万美元，或占流通 ≥ 0.1%，满足其一即可（默认值） |
| `--ref-hours` | 窗口起点前多少小时内执行的销毁算作"窗口外参考"，也计算指标（默认 24） |
| `--out/--cache/--refresh/--quiet` | 与其他 tracker 相同 |

## 事件来源与口径

1. **hyperliquid**：Hyperliquid 援助基金（`0xfefe…fe`）在窗口内买入 HYPE 的成交，取自 `userFillsByTime`，属于链上数据。按"持续回购销毁（窗口累计）"汇总成一行，锚点为窗口起点。逐日明细写入 other_events。
2. **pump**：pump.fun/pump-token 官方看板内嵌的每日回购数据（日桶），汇总方式同上。
3. **panews**：PANews 快讯 API，分页回溯到窗口起点。
4. **odaily**：Odaily 快讯，逐 ID 扫描。先用二分查找定位窗口起点对应的 ID；标题、摘要和发布时间缓存在 `cache/odaily_index.json`。首次运行约 3 分钟，之后只补扫新 ID。
5. 新闻只作为候选，按以下规则处理：
   - 含"销毁/burn"且能解析出"数量 + 枚 + 代码"的已完成销毁，才进入主表。销毁时间记为新闻发布时间，并标注单一来源。
   - 将来时（将/拟/计划/就绪/提议）的新闻归入 upcoming；如果同币、同数量的销毁已经执行，则标为"预告（已执行）"。
   - 累计值、早/午/晚讯汇总帖，以及股票/国债回购，一律跳过，只写入 `news_candidates.csv`。
   - 已有连接器覆盖的计划（HYPE、PUMP）的日报，归入 other_events。
6. **extra-events**：可以覆盖时间、数量、价值、tx、单一来源标记和 CoinGecko id，也可以新增事件。`kind` 字段的取值：
   - `burn`：已执行的销毁。按同币且数量相差 ≤3% 与新闻事件匹配后覆盖；匹配不到就新增。
   - `upcoming`：即将进行的销毁。
   - `context`：背景事件。
   - `exclude`：剔除对应事件。
   - 例：新闻报道 POL 销毁的时间是 9/24 11:24，extra 用链上 tx 时间 9/23 22:41 覆盖后，这笔销毁落到窗口外，列为参考行。
7. **指标**：与 unlock/delist/perp 三个 tracker 相同。D-7..D-1 为 24h 桶，锚定销毁时间；另算 7 天合计、销毁至今涨跌、销毁后最低/最高，以及同窗口的 BTC。价格源：
   - Binance 现货 1h，但要求覆盖销毁前 7 天。例如 HYPE 在 Binance 现货 9/24 19:00 才上线，就会退回 CoinGecko 小时价。
   - 其余情况用 CoinGecko 小时价，缺失记 N/A。
8. **价值与占比**：
   - 价值优先取官方或链上成交额，否则用数量 × 销毁时刻价格。
   - 占流通 = 数量 / CoinGecko 当前流通量。基金会、储备等非流通部分的销毁，占流通记 N/A，改看占总量（销毁前）。

## 输出（`--out` 目录）

- `burns.csv`：主表，含窗口外参考行（`in_window` 列为"否"）
- `pre7d_daily.csv`：逐日涨跌，代币与 BTC 各一行
- `other_events.csv`：upcoming、已执行的预告、持续计划逐日明细、计划日报、同期相关新闻（上币/ETF/巨鲸转入等可能的干扰）、低于阈值、窗口外、剔除
- `news_candidates.csv`：所有含"销毁"的新闻及其处理结果
- `summary.md`：中文报告（表 1、表 2、BTC 逐日、自动观察、upcoming、来源）
- `run_meta.json`：参数、各源解析数和错误

## 局限（重要）

- 销毁数据非常分散，没有统一的公开 API。本工具实质上由新闻和 extra-events 驱动，加上两个持续计划的连接器（HYPE、PUMP）。其他项目的回购计划（如 SKY、INJ 社区回购、SUN、交易所平台币季度销毁）需要手工补充 extra。
- **新闻覆盖有限**：
  - 只接了 PANews 和 Odaily 两家中文快讯；BlockBeats API 和 Odaily RSS 不可用。
  - 新闻里没有"数量 + 枚 + 代码"格式的销毁（比如只写百分比，或用英文）不会被解析。
  - 解析出的时间是新闻发布时间，不是链上时间。不加 extra 时，POL 会被误判为窗口内（9/24 11:24）。
- 没有 Etherscan/Solscan API key，不做通用的链上核验。Part A 中 POL 的 tx 和 SANC 的总量是用公共 RPC 手工核验的，结果以 extra 形式提供。
- pump 看板按日桶统计，末日是部分日，无法按 `--now` 截断。复现历史窗口时需要当时缓存的页面（`cache/raw/pump_token_page.html`）。
- HYPE 援助基金的买入"视同销毁"（社区投票认定），并不是转入 0x0 地址。
- 流通量和总量取当前的 CoinGecko 快照，不是销毁时点的数据。CoinGecko 小时价的时间戳不是整点，按 3 小时以内的最近点取值。
- 持续计划锚定在窗口起点，"销毁前 7 天"即窗口前一周，这是一个口径选择。
- 依赖相邻目录 `../unlock_tracker`、`../delist_tracker`。
