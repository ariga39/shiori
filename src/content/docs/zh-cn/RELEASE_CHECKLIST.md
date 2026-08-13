---
title: "shiori v0.1.0 私有候选发布检查清单"
description: 这是一份候选发布检查清单，不是发布授权。
slug: zh-cn/RELEASE_CHECKLIST
---

这是一份候选发布检查清单，不是发布授权。在 owner 另行做出可见性决定之前，仓库保持私有。本清单不得作为创建 tag 或 release、发布包、推送镜像、部署、注册外部服务或写入生产环境的一部分。

## 候选身份（Candidate identity）

- [ ] 记录精确候选 commit、parent/base、分支与 Draft PR。
- [ ] 确认候选 worktree 干净，且候选不是基于过期的 main 分支。
- [ ] 记录针对该精确 commit 的 hosted CI run ID。
- [ ] 记录针对该精确 commit 的结对编程 review GO。
- [ ] 在精确 head 门与结对编程 review 全绿后，记录实际受保护 merge SHA。

## 必需工程门（Required engineering gates）

- [ ] `uv sync --locked --extra dev`、`uv lock --check`、Ruff、Pyright 与完整单元套件通过。
- [ ] `npm ci` 安装锁定的 Node 依赖，且公开的 `npm run docs:build -- --outDir <temp>` 将文档站点构建到显式指定的临时目录。
- [ ] 用户可见变更存在 Towncrier changelog 片段且文档化的 checker 与 draft 命令通过；内部或仅测试变更使用经审计的 waiver 路径。
- [ ] Hosted PostgreSQL/pgvector 服务通过 client/server 主版本一致性、vector preload、隔离 marker/identity 检查，以及 `shiori db migrate` 后跟 `shiori db health`。
- [ ] 合成 `schema.sql` 数据库由同一 CLI 命令升级；完整旧式结构在不重放 DDL 的情况下被采纳，部分或漂移结构 fail-closed。
- [ ] Backup 创建摘要清单，restore 仅创建新的 staging 数据库；坏清单、工具失败、路径冲突与身份不匹配均 fail-closed。
- [ ] 已安装 wheel 以合成 sessions、Hermes、Discord 输入、确定性 fake 向量、隐私 retention/export/delete、query 与只读 MCP stdio search 运行 README 生命周期。
- [ ] sdist 与 wheel 包含文档化安装路径所需的运行时 migration/schema/docs/tools。
- [ ] 直接依赖许可证元数据与 `THIRD_PARTY_NOTICES.md` 一致；固定版本 `pip-audit` 报告无未豁免的高严重性漏洞。
- [ ] 可达历史、commit 元数据与构建产物通过离线 secret/private-key/PII/host-path 审计且不暴露匹配项。
- [ ] compose 路径以全新 project-scoped named volume 构建固定版本 Dockerfile 镜像，其运行时 smoke 证明空卷初始化、就绪、vector 扩展写入、重启持久化、非 root 执行、preload/CMD 行为，并在对该同一镜像运行 HIGH/CRITICAL 扫描前仅清理 project 标记资源。

## 隐私与范围门（Privacy and scope gates）

- [ ] 所有源路径、数据库凭据、提供方/密钥/模型/维度设置均显式；不使用主目录或主机凭据回退。
- [ ] 确定性 fake 嵌入要求同时显式 development/test 环境与 opt-in，使用保留的 `shiori-fake-*` 模型命名空间，并披露为本地/无外部调用。
- [ ] Voyage 与 fake 向量按提供方/模型/维度契约隔离；不兼容行被结构性排除或拒绝。
- [ ] 摄取显式，且 clean-machine harness 仅使用合成输入。MCP 暴露只读 search，带受限的 query/page/resource 限制。
- [ ] 产品保持本地单用户、非多租户、仅 stdio，且无认证 HTTP API 或自动爬虫。
- [ ] 不提交或上传真实用户文档、私有路径、凭据、快照或生成的诊断产物。

## 已知限制（Known limitations）

- 私有候选不授权部署、外部注册、生产写入、包发布、镜像发布或可见性变更。
- 真实语义摄取需要生产嵌入服务/密钥；确定性向量仅用于隔离的开发/测试冒烟运行。
- 项目使用结对编程 review 与精确 head 的受保护合并；每次变更没有单独的独立 review 步骤，owner 也不逐个批准合并。但 release、tag、包/镜像发布、部署、外部注册与可见性变更仍需要 owner 另行明确授权；本页不授予该授权。
- `schema.sql` 保留为历史 legacy fixture。它不修复漂移；只有精确的规范旧式结构能被采纳，所有其它既有漂移都是 operator 可见的 fail-closed 条件。
- PostgreSQL/pgvector、容器与全新安装门无法从离线工作站运行推断。若这些能力本地不可用，清单将其记录为未证实，直到 hosted CI 提供证据；跳过测试绝不视为通过。
