---
title: 部署到 Cloudflare Workers
description: 在 Cloudflare Workers Builds 中连接此仓库，然后使用下列仓库内固定版本命令。
---

文档站点是输出到 `dist/` 的静态 Starlight 构建。它以 Cloudflare Workers Static Assets 部署，无需 Worker 脚本或 binding。

在 Cloudflare Workers Builds 中连接此仓库，然后使用下列仓库内固定版本命令。

## 本地准备

在依赖 Cloudflare 之前，安装锁定的 Node 依赖并在本地验证静态构建与 Workers dry-run：

```bash
npm ci
npm run docs:workers:dry-run -- --outdir /tmp/shiori-worker-bundle
```

dry-run 构建 `dist/` 并打包，不上传任何内容。dry-run 期间不进行认证、不访问账户、不发起网络上传。

## Cloudflare Workers Builds 配置

owner 在 Cloudflare dashboard 中关联此 GitHub 仓库，选择生产分支，部署授权保存在 Cloudflare 侧。

仓库只提供项目级配置：

- **Build command：** `npm run docs:build`
- **Deploy command：** `npm exec -- wrangler deploy --config wrangler.jsonc`
- **Build output directory：** `dist/`（站点根路径为 `/`）

`wrangler.jsonc` 只固定静态资源目录。不要将 Cloudflare 账户 ID、Worker 标识符、路由、域名或 API 令牌提交到此仓库。

只有在 owner 完成 dashboard 配置且 Cloudflare 执行固定命令后才会发生真实部署。在此之前，本地 dry-run 是唯一已验证的部署路径。
