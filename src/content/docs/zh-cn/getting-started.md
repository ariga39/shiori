---
title: 快速上手
description: 本指南涵盖 Shiori 从锁定的开发环境安装到只读 MCP 服务器的受支持本地生命周期。
---

本指南涵盖 Shiori 从锁定的开发环境安装到只读 MCP 服务器的受支持本地生命周期。

## 安装

克隆仓库并安装锁定的开发环境：

```bash
uv sync --locked --extra dev
```

## 配置

按照[配置参考](../zh-cn/configuration-reference/)设置明确的数据库、数据源与嵌入提供方取值。在摄取归档之前，请阅读[隐私政策](../zh-cn/privacy-policy/)。Shiori 没有隐式的数据源、凭据或提供方路径。

## 迁移

在摄取数据之前应用仅向前的数据库迁移：

```bash
shiori db migrate
```

## 摄取

显式选择数据源。对于已配置的会话目录：

```bash
shiori ingest --source sessions
```

## 查询

从命令行搜索已索引的记忆：

```bash
shiori query 'what did we decide about the release?'
```

## 服务

启动本地只读 MCP stdio 服务器：

```bash
shiori serve
```
