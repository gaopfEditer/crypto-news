# 加密新闻过滤看板 🪙📰

实时抓取和过滤加密货币新闻，自动打分、去重、聚类，通过 GitHub Pages 提供静态网页访问。

**特性**：
- 🔄 每 15 分钟自动更新（GitHub Actions 定时任务）
- 🎯 智能打分：关注代币 + 重大事件（漏洞/被盗/下架/ETF/监管等）
- 🔗 跨源聚类：自动合并同一事件的不同报道
- 💰 实时价格：显示相关代币的当前价格和 1 小时涨跌
- 🌙 暗色主题：交易终端风格 UI
- 🔔 浏览器通知：重大新闻推送
- 📱 响应式设计：支持桌面和移动设备

## 📊 数据源

- [CoinDesk](https://www.coindesk.com/) RSS
- [The Block](https://www.theblock.co/) RSS
- [Odaily](https://www.odaily.news/) 快讯
- [PANews](https://www.panewslab.com/) API

## 🚀 使用方法

### 访问看板

访问 GitHub Pages 部署的页面：`https://你的用户名.github.io/你的仓库名/`

（部署成功后会显示在仓库的 Settings → Pages 中）

### 筛选功能

- **排序**：按最新时间 / 最重要程度
- **代币筛选**：选择关注的代币（带条目计数）
- **事件类型**：漏洞/被盗、下架、ETF、监管等（★ 表示重大类）
- **来源筛选**：按数据源过滤
- **最低分滑块**：调整显示阈值（默认 5 分）
- **只看重大**：只显示重大新闻
- **搜索框**：全文搜索标题和摘要

### 自定义配置

编辑仓库中的 `config.json` 文件：

#### 添加关注代币

在 `tokens` 中添加：

```json
"ARB": {
  "names": ["Arbitrum"],
  "weight": 4,
  "gecko": "arbitrum"
}
```

- `names`: 代币的常见名称（用于匹配新闻标题）
- `weight`: 权重（1-10，影响打分）
- `gecko`: CoinGecko ID（用于获取价格，可选）

#### 调整事件关键词

在 `events` 中修改：

```json
{
  "key": "hack",
  "label": "漏洞/被盗",
  "weight": 10,
  "big": true,
  "en": ["hack", "exploit", "stolen"],
  "zh": ["漏洞", "被盗", "黑客"]
}
```

- `big: true` 表示重大事件类型（不需要命中代币也算重大）
- `weight` 影响打分

#### 修改更新频率

编辑 `.github/workflows/update-news.yml`，修改 cron 表达式：

```yaml
schedule:
  - cron: '*/15 * * * *'  # 每 15 分钟（改为 */30 即为 30 分钟）
```

**注意**：GitHub Actions 的定时任务可能会延迟几分钟执行。

#### 调整数据保留时间

在 `config.json` 的 `dashboard` 部分修改：

```json
"dashboard": {
  "retain_hours": 48,  # 保留最近 48 小时
  "seed_hours": 12     # 首次回填 12 小时
}
```

## 🔧 工作原理

1. **GitHub Actions 定时运行**：每 15 分钟执行一次 `fetch_news.py`
2. **抓取新闻**：从 4 个数据源获取最新新闻
3. **打分和过滤**：
   - 关注代币权重：标题命中满分，摘要命中 × 0.3
   - 事件权重：取最高的事件 + 第二高的 × 0.3
   - 多源确认：+1 分
   - 噪音降权：观点文章、播客、日报等
4. **去重和聚类**：
   - 按 URL、标题去重
   - 相似标题、共同实体、相同代币+事件 → 合并为一个故事
5. **获取价格**：从 Binance 或 CoinGecko 获取相关代币价格
6. **生成静态文件**（只写到 `gh-pages`，不提交到 `main`）：
   - `data.json`：前端读取的数据
   - `state.json`：保存状态供下次运行使用
   - `calendar.json`：日历事件
7. **部署到 GitHub Pages**：workflow 推到 `gh-pages` 分支；若站点从 `main` 发布，前端会从 `gh-pages` 的 raw 地址取最新数据

## 📝 评分规则

### 流动性/供给事件优先（impact.py）

`impact.py` 按中英文关键词识别会影响代币流动性/供给的事件，命中后在原有分数上加分（标题全额、摘要减半），并可在标题命中时升为「重大」：
代币解锁（大额解锁可升重大）、机构 OTC/团队抛售、TGE/解除转让限制、下架、黑客/漏洞、通胀/质押率调整、销毁/回购、ETF 获批/资金流、新永续合约/高杠杆、交易所上币、主网上线、网络升级（含测试网）、协议重启/新版本、代币迁移、预售、AI 转型、宏观/FOMC、生态大会。

每条新闻（data.json `stories[]`，均为**新增字段**，旧字段不变）：
- `coins`：影响币种（不含 `$`），`direction`：`利好`/`利空`/`中性`，`direction_reason`：简短理由
- `impact` / `impact_label`：事件类别，`impact_tag`：如 `【利空】$ENA 大额解锁`
- 页面在徽章与卡片描述中展示；方向规则：解锁/OTC/开放转账/下架/黑客=利空；主网/升级/上币/销毁回购/ETF 流入/通胀下调=利好（升级注明预期兑现风险）；大会/宏观=中性（降息/加息等明确时例外）
- 有流动性事件且识别到币种时，即使不在关注列表也可达到「命中」；`config.json` 设 `"impact_priority": false` 可关闭加分

### 等级划分

- **重大**（红色）：重大事件（漏洞/被盗/下架/ETF/监管）出现在标题中，且分数 ≥ 7
- **命中**（黄色）：命中关注代币，且分数 ≥ 7
- **低分**（灰色）：其他条目

### 示例

| 新闻标题 | 代币 | 事件 | 分数 | 等级 |
|---------|------|------|------|------|
| "Hyperliquid 遭遇漏洞攻击，损失 960 万美元" | HYPE (4分) | 漏洞/被盗 (10分) | 14 | 重大 |
| "SEC 批准比特币现货 ETF" | BTC (2分) | ETF (7分) | 9 | 重大 |
| "NEAR Protocol 发布新路线图" | NEAR (4分) | 无 | 4 | 命中 |
| "比特币价格分析" | BTC (2分) | 价格波动 (3分)，降权 -4 | 1 | 低分 |

## 🛠️ 本地开发

虽然这个版本设计为 GitHub Pages 静态部署，但你也可以在本地运行：

```bash
# 克隆仓库
git clone https://github.com/你的用户名/你的仓库名.git
cd 你的仓库名

# 运行一次抓取
python3 fetch_news.py --seed

# 启动本地服务器
python3 -m http.server 8000

# 访问 http://localhost:8000/
```

## ⚙️ 手动触发更新

1. 访问仓库的 **Actions** 标签
2. 点击左侧的 **更新加密新闻数据**
3. 点击右上角的 **Run workflow**
4. （可选）勾选"强制重新播种"以回填近 12 小时数据
5. 点击绿色的 **Run workflow** 按钮

## 📊 状态监控

在看板顶部可以看到：

- **健康灯**：各数据源的状态
  - 🟢 绿色：正常
  - 🟡 黄色：有失败记录或数据偏旧
  - 🔴 红色：连续失败 3 次以上
- **最后更新时间**（UTC+8）
- **查看工作流** 链接：查看 Actions 运行日志

## 📅 经济日历

除了新闻流，看板还提供 **经济日历** 功能，显示影响 BTC 和 QQQ 价格的预定事件。

### 访问日历

点击顶部导航的 **「日历」** 标签，或访问 `calendar.html`。

### 三大类别

1. **宏观数据**（锚定 BTC + 美股）
   - 非农就业 + 失业率（每月首个周五 20:30 UTC+8）
   - CPI、PPI、PCE 通胀数据
   - FOMC 决议 + 点阵图（SEP 会议）
   - GDP、ECI 就业成本指数
   - 初请失业金（每周四 20:30 UTC+8）
   - 数据来源：BLS、BEA、美联储官网

2. **财报季**（锚定 QQQ）
   - Mag7：AAPL、MSFT、NVDA、AMZN、GOOGL、META、TSLA
   - 芯片股：MU、AMD、AVGO、AMAT、LRCX
   - 标记「已确认」（来自公司 IR）或「预估」（来自财报日历）

3. **币圈日历**（锚定 BTC）
   - 代币解锁日期（≥1% 供应量或 ≥$50M）
   - Deribit/CME 期权期货到期（月度/季度）
   - 主要链升级
   - Spot ETF SEC 决议截止日期

### 卡片信息

每个事件卡片显示：
- 时间（UTC+8，标注时区）
- 类型、标的、锚定资产（BTC/QQQ）
- 重要性：**重大**（宏观数据、Mag7+AVGO 财报、大型币圈事件）或 **中**
- 预期值、前值（发布前）
- 实际值、相对预期（发布后：高于/低于/符合预期）
- 数据来源 URL

### 筛选和视图

- **即将发生**：默认显示未来 14 天
- **已公布**：保留历史数据，显示实际值
- 按分类筛选：宏观/财报/币圈
- 按锚定筛选：BTC/QQQ

### 手动编辑事件

编辑仓库中的 `calendar_seed.json` 文件：

```json
{
  "events": [
    {
      "id": "unique-id",
      "category": "macro",
      "type": "CPI 消费者物价指数",
      "symbol": "CPI",
      "anchor": "BTC",
      "importance": "big",
      "time_et": "2026-10-14 08:30",
      "expected": "2.3%",
      "previous": "2.5%",
      "actual": "",
      "source_url": "https://www.bls.gov/...",
      "confirmed": true,
      "note": "9月CPI数据"
    }
  ]
}
```

**字段说明**：
- `category`: `macro` | `earnings` | `crypto`
- `importance`: `big` | `medium`
- `time_et`: 美东时间（宏观/财报），格式 `YYYY-MM-DD HH:MM`
- `time_utc`: UTC 时间（币圈事件），格式 `YYYY-MM-DD HH:MM:SS`
- `confirmed`: `true`=官方确认, `false`=预估日期
- `anchor`: `BTC` | `QQQ`

**时间转换**：
- 美东时间会自动转换为北京时间（UTC+8）
- DST 期间（3月-11月）：20:30 北京时间
- 非 DST 期间（11月-3月）：21:30 北京时间
- FOMC 决议：14:00 ET = 次日 02:00/03:00 北京时间

### 自动刷新

GitHub Actions 工作流会：
1. 每 15 分钟处理日历数据
2. 自动转换时区（ET → UTC+8）
3. 尝试从 BLS API 获取实际值（已发布的数据）
4. 计算相对预期（高于/低于/符合）
5. 生成 `calendar.json` 部署到 gh-pages

### 添加财报日期

```json
{
  "id": "nvda-q4-2026",
  "category": "earnings",
  "type": "财报",
  "symbol": "NVDA",
  "anchor": "QQQ",
  "importance": "big",
  "time_et": "2026-11-20 16:30",
  "expected": "",
  "previous": "",
  "actual": "",
  "source_url": "https://investor.nvidia.com/",
  "confirmed": false,
  "note": "NVIDIA Q4 FY2026财报，预估日期"
}
```

### 添加币圈事件

```json
{
  "id": "arb-unlock-2026-11",
  "category": "crypto",
  "type": "ARB 代币解锁",
  "symbol": "ARB",
  "anchor": "BTC",
  "importance": "big",
  "time_utc": "2026-11-15 00:00:00",
  "expected": "1.2B ARB (约8%)",
  "previous": "",
  "actual": "",
  "source_url": "https://docs.arbitrum.foundation/...",
  "confirmed": true,
  "note": "团队 + 投资人解锁"
}
```

编辑后提交到 `main` 分支，下次工作流运行时会自动更新日历。

## 🔔 浏览器通知

1. 勾选顶部的"重大通知"复选框
2. 浏览器会请求通知权限，点击"允许"
3. 当出现新的重大新闻时，会弹出系统通知
4. 可选：勾选"声音"以启用提示音

**注意**：
- 需要保持浏览器标签页打开（可以在后台）
- 只有页面打开后出现的重大新闻才会通知
- 启动回填的历史数据不会触发通知

## ⚠️ 局限性

1. **关键词匹配**：基于正则表达式，可能有误报或漏报
2. **定时延迟**：GitHub Actions 的 cron 可能延迟几分钟
3. **数据源限制**：
   - CoinDesk RSS 约 25 条
   - The Block RSS 约 19 条
   - PANews API 50 条
   - Odaily 按 ID 增量抓取
4. **价格数据**：仅显示检测时的价格快照（不是新闻发布时的价格）
5. **运行环境**：GitHub Runners 可能无法访问某些被墙的网站（会标记为红色但不中断工作流）

## 📄 许可证

MIT License

## 🙏 致谢

数据源：CoinDesk、The Block、Odaily、PANews  
价格数据：Binance Data API、CoinGecko API
