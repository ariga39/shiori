---
title: CLI 与 MCP 参考
description: 两者使用同一个已配置的搜索服务，但分页接口有意采用不同形式。
---

Shiori 提供已安装的命令行界面与本地只读 MCP stdio 服务器。两者使用同一个已配置的搜索服务，但分页接口有意采用不同形式。

## CLI 命令

摄取总是显式指定一个已配置的数据源：

```bash
shiori ingest --source sessions
```

搜索已索引的记忆或启动 MCP 服务器：

```bash
shiori query 'what did we decide?'
shiori serve
```

数据库与隐私生命周期命令位于 `shiori db` 与 `shiori privacy` 之下。

## 查询选项

已安装的 `shiori query` 命令接受 `--limit`（也可用 `-n`）、可重复的 source/session 过滤器、含端点/不含端点的 RFC3339 时间范围，以及 opt-in 的 `--explain` 诊断。已安装的 CLI 返回第一个有界页；它不暴露 offset 标志。

Explain 诊断输出到 stderr，因此正常结果文本仍可管道传输。报告的 RRF 分数与渠道匹配描述检索排序与佐证，而非正确率概率。

## MCP search

MCP 服务器暴露名为 `search` 的单个工具。其输入包含 query、`limit`、裸 `offset`、相同的结构化过滤器与可选的 `explain`。响应包含 `results`、`count`、`limit`、`offset`、`has_more` 与 `next_offset`。启用解释时还包含附加的分数语义、检索通道证据、交叉印证与来源字段。

MCP 表面是只读的：它不能摄取、迁移、删除、导出或修改源数据。

## 限制与错误

MCP 分页最多接受 20 条结果。offset 限制在 0 到 255 之间，分页报告 `has_more` 与稳定的 `next_offset`，而不是无界计数查询。

无效输入、配置失败、提供方失败与数据库错误返回稳定错误码。响应不暴露后端异常文本、凭据或连接细节。
