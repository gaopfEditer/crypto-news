# spot_listing_tracker

现货 **上交易所** 事件（韩国 Upbit、Bitget、OKX、Binance 等）与上线前后价格表现。

## 输出

- `spot_listings.csv` → 合并为 `events/spot_listing.json`
- `pre7d_daily.csv`：D-7…D-1 逐日涨跌
- `other_events.csv`：未来 14 天内计划上线（scheduled）

## 数据源

| 交易所 | 方式 |
|--------|------|
| Upbit | `api-manager.upbit.com` 交易类公告（신규 거래지원 / 디지털 자산 추가） |
| Bitget | `annType=coin_listings` 且 `annSubType=spot` |
| OKX | `GET /api/v5/public/instruments?instType=SPOT` 的 `listTime` |
| Binance | CMS `catalogId=48` 标题含 *Will List*（排除合约/杠杆/Alpha） |
| Bithumb | 无稳定 API → `--extra-events` 手工 CSV |

## 运行

```bash
python3 spot_listing_tracker/spot_listing_tracker.py --quiet --out spot_listing_tracker/output/latest --cache spot_listing_tracker/cache
python3 event_store/merge.py merge spot_listing spot_listing_tracker/output/latest publish/events
```

## extra-events CSV 列

`ticker,exchange,list_time_utc8,announce_time_utc8,region,pairs,source_url,title`
