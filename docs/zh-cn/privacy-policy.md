---
title: 隐私政策
description: 本文说明摄取与隐私边界所执行的本地数据最小化与生命周期契约。
---

Shiori 为 AI 智能体存储可搜索的长期记忆。本文说明摄取与隐私边界所执行的本地数据最小化与生命周期契约。

## 数据最小化（fail-closed）

- 进入存储的每条消息文本都会在摄取边界经过 `shiori.privacy.minimize`（sessions 与 Hermes 使用 `ingest.extract_text_from_message`，Discord 使用 `ingest_discord.format_message`）。
- 已识别的敏感形状会在存储前被脱敏：
  - 提供方实时 API 令牌（`sk_`/`pk_`/`rk_` + `_live_`），
  - GitHub 风格令牌（`ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_`、`github_pat_`），
  - bearer 授权头，
  - 电子邮件地址，
  - 绝对文件系统路径与 Windows 绝对路径。
- 脱敏强制开启：CLI `ingest --redact` 标志默认启用且无法关闭，因此错误配置不可能静默存储 PII。
- 无法安全处理的输入类型（非字符串）会以结构化 `PrivacyError` 拒绝。这并非声称每个未识别值都安全。

## 留存

每个数据源声明一个正向留存窗口：

| source   | kind    | retention_days |
| -------- | ------- | -------------- |
| sessions | jsonl   | 90             |
| hermes   | sqlite  | 90             |
| discord  | jsonl   | 30             |

`shiori privacy retention-check --scope <s>` 使用存储的 aware-UTC `processed_at`/`created_at` 报告该 scope 行的受管数据年龄，绝不使用外部源文件 mtime。它不执行任何删除。

## Scope 与受管存储

隐私生命周期操作**仅**作用于 shiori 自身的受管行（`session_chunks`、`session_facts`、`ingestion_state`）。外部源文件（`sessions_dir`、`hermes_db`、`discord_archive_dir`）是只读来源：export 或 delete 从不 unlink、重命名或重写它们。

Scope 解析复用现有 provenance 规则，当 scope 无法唯一归属时以 `scope_evidence_unavailable` fail-closed。它适用于真实形状——绝对源路径、纯 discord stem（`general.jsonl` → `discord-general`）与任意 hermes 会话 id——且不依赖任何调用方提供的前缀：

- sessions：使用真实 adapter 选择规则在配置的 sessions 根目录下发现文件，会话 id 由 basename 派生，
- discord：配置的 discord 根目录下每个 `*.jsonl` 映射为 `discord-{stem}`，
- hermes：会话 id 通过 `hermes://<session_id>` `ingestion_state` 绑定。

`scope=all` 原子解析 sessions、discord 与 hermes；若任何 scope 无法唯一归属，整个操作以零副作用 fail-closed。符号链接或越出根目录的 provenance 会被拒绝。

## Export

- `shiori privacy export --scope <s> --dest <p>` 返回 dry-run（行数与目标）而不写入。
- 使用 `--yes` 时，export 原子写入（同目录临时文件 + fsync + chmod 0600 + 原子替换）。目标内容已相同时报告 `already_exported`；内容不同则 fail-closed，绝不覆盖。
- 产物是包含可读内容、时间戳与 provenance 哈希的单一确定性 JSON 文档。它绝不包含 embeddings、tsvector、密钥、DSN 或绝对源路径。

## Delete

- `shiori privacy delete --scope <s>` 返回 dry-run 计数而不触碰任何东西。
- 使用 `--yes` 时，删除是单个事务，只移除绑定到所选 scope 解析文件路径的受管行与 checkpoint；任何失败都会回滚所有行。重复删除报告零（幂等）。`--older-than N` 将删除集合收窄为 `processed_at` 早于 N 天的受管行。
- 外部源文件在删除前后保持逐字节不变。

## 提供方披露

- `shiori privacy providers` 列出每个数据源的提供方端点、数据流、留存窗口与仅本地状态，并针对每个数据源与嵌入提供方报告 `configured` 或 `not_configured`。
- 嵌入提供方在已配置时以其真实端点与模型报告；未配置时报告为 `not_configured`，而不是被静默假定。
