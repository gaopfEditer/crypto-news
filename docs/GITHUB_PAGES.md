# GitHub Pages 发布源

本仓库的 JSON 与静态页由 Actions 写入 **`gh-pages` 分支**（`peaceiris/actions-gh-pages`，`force_orphan: false`，在现有提交上增量更新）。

## 根因：站点 404 / 数据不更新

若 **Settings → Pages** 的发布源仍是 **`main` 分支 /`/`**，则：

- `https://<user>.github.io/crypto-news/movers.json`、`data.json` 会 **404**（这些文件不在 `main` 上）；
- `index.html` 的 **Last-Modified** 停留在 `main` 上最后一次提交，与 `gh-pages` 上的行情快照无关；
- 前端只能靠 `raw.githubusercontent.com/.../gh-pages/...` 回退读取。

可用 API 确认当前配置（需仓库管理员权限）：

```bash
gh api repos/gaopfEditer/crypto-news/pages --jq '{html_url, build_type, source}'
```

修复前典型输出：`"source": {"branch": "main", "path": "/"}`。

## 合入修复后请执行（一次性）

1. 打开 **Settings → Pages → Build and deployment**。
2. **Source** 选 **Deploy from a branch**。
3. **Branch** 选 **`gh-pages`**，文件夹 **`/ (root)`**，保存。
4. 等待 1–3 分钟后验证：
   - `curl -sI https://gaopfediter.github.io/crypto-news/movers.json` → **200**，`content-type` 含 `json`；
   - `curl -s https://gaopfediter.github.io/crypto-news/movers.json | jq .updated_utc8` 与最近一次「更新币安涨跌榜」 workflow 摘要一致。

## Actions 调度说明

- 涨跌榜 workflow：`17 * * * *`（UTC），构建与部署分离；**仅 `deploy` job** 使用 `concurrency: gh-pages-publish`，避免与新闻整段 workflow 互斥排队。
- 新闻 workflow：`3,33 * * * *`（UTC），同样仅在部署阶段串行。
- GitHub 对 `schedule` 仍可能有 **数分钟到数小时** 的延迟；页面文案以 `movers.json` 内 `updated_utc8` 为准。
