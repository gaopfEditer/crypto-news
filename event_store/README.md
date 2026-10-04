# 事件库（`events/` on gh-pages）

四类 tracker 输出经 `event_store/merge.py` 合并为 `events/{unlock,delist,perp_listing,burn}.json` + `index.json`，由 `.github/workflows/update-events.yml` 部署到 **gh-pages**。

## 数据 URL（外部读取）

| 用途 | 推荐 URL |
|------|-----------|
| **程序/API（稳定）** | `https://raw.githubusercontent.com/gaopfEditer/crypto-news/gh-pages/events/unlock.json` |
| **GitHub Pages 站点** | `https://gaopfEditer.github.io/crypto-news/events/unlock.json` |

Workflow 在 `repo/` 下合并到 **`publish/events/`**（与 `peaceiris/actions-gh-pages` 的 `publish_dir: ./repo/publish` 一致）。勿使用 `../publish`，否则会写到仓库外、部署仍用旧 gh-pages 快照。

`events.html` 会同时请求相对路径 `./events/unlock.json` 与 raw；取 `updated` 较新的一份。若 Pages 相对路径偶发 404（缓存或首次部署），raw 仍可用。

## 解锁库后处理

`merge unlock` 结束后运行 `unlock_postprocess.py`：

1. 合并 `event_store/unlock_manual.json`（`data_source=manual`）
2. 应用 `overrides`（如 ENA 数量更正）
3. 同一 ticker、解锁时刻 **±1 小时** 去重；**同一 UTC+8 日历日**的多源重复也会合并（保留 `amount_conflict`）
4. 冲突标记、`pct_circ_basis`、**impact_score**

### `impact_score`（0–100）

对每条解锁（含未来 scheduled）计算，写入 `impact_score` 与 `fields.impact_score`：

```
base = 0
+ min(40, pct_circ × 2)           # 占流通 %，>100% 视为异常不计入
+ min(30, log10(value_usd+1) × 4.5)  # 美元价值（value_usd_at_unlock / value_usd_source）
+ min(15, (value_usd / vol24h_usd) × 12)   # 仅当 vol24h_usd 可取时
× recipient_weight × unlock_type_weight
→ round，clamp 0–100
```

**recipient_weight**：团队/投资人/私募/Insider ≈ 1.0；生态/社区/挖矿 ≈ 0.72；国库/储备 ≈ 0.45；未知 ≈ 0.62  

**unlock_type_weight**：含 `cliff` ≈ 1.0；含 `linear` ≈ 0.68；其他 ≈ 0.78  

### 冲突与占流通

- `amount_conflict`：多源数量相差 **>5%**
- `pct_conflict`：多源占流通相差 **>5%**
- `date_conflict`：多源时间相差 >6h，或 `source_times_utc8` / 公告不一致
- `source_conflict`：仅单一来源且与 Tokenomist 等“已全部解锁”类信息矛盾
- `pct_circ_basis`：`cmc_circulating` / `defillama_circulating` / `coingecko_circulating` / `tokenomist` 等
- `pct_circ_display`：展示用；原值 **>100%** 显示「异常」并标 `pct_circ_anomaly`

### 手工补充

编辑 `event_store/unlock_manual.json`：

- `events[]`：新增行（必填 `ticker`, `unlock_time_utc8`, `sources` 含 `manual`, `source_urls`）
- `overrides[]`：按 `match.ticker` + `date_prefix` 修补库内已有事件
