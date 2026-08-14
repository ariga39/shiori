---
title: 参与贡献
description: 保持变更范围精简，通过公开行为进行测试，并报告哪些内容已验证、哪些尚未验证。
---

保持变更范围精简，通过公开行为进行测试，并报告哪些内容已验证、哪些尚未验证。

## 开发环境搭建

安装仓库的锁定开发环境：

```bash
uv sync --locked --extra dev
```

## 测试

从仓库根目录运行测试套件：

```bash
uv run pytest -q
```

数据库测试需要[配置参考](../CONFIGURATION/#测试数据库隔离)中描述的隔离设置。跳过的测试是未验证的能力，而不是通过的结果。请显式报告跳过与环境受限的失败。

## 文档

使用公开的 Starlight 脚本在本地构建文档站点：

```bash
npm run docs:build
```

这仅在本地构建站点，不会部署或发布。文档源保持为 `src/content/docs/` 下的 Markdown，英文为根语言环境，简体中文位于 `zh-cn/`。

## 拉取请求

保持每个拉取请求只聚焦一个行为或维护关注点。提供用于佐证的确切命令与结果，区分跳过检查与通过检查，并明确指出任何有意超出任务范围的操作。未经单独授权，不要发布包、镜像、文档或更改仓库可见性。

## Changelog 片段

用户可见的变更至少需要一个 changelog 片段。普通片段为 `changelog.d/<issue>.<type>.md`，其中 `<issue>` 是正整数，`<type>` 是 `feature`、`bugfix`、`doc`、`removal` 或 `misc` 之一。

内部或仅测试的拉取请求可改用恰好一个非空 waiver。waiver 为 `changelog.d/<issue>.no-changelog.md`。

waiver 必须说明为何不需要面向用户的 changelog 条目，且不得与普通片段混合。
