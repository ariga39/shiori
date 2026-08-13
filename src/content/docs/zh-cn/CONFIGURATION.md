---
title: 配置契约
description: 这是当前的运行时契约。旧的 docs/DESIGN.md 是历史实现记录，不是默认路径的来源。
slug: zh-cn/CONFIGURATION
---

这是当前的运行时契约。旧的 `docs/DESIGN.md` 是历史实现记录，不是默认路径的来源。

## 解析与校验

`shiori.config.load_config()` 按以下顺序解析取值：

1. 显式的 Python/API 覆盖；
2. `SHIORI_*` 环境变量；
3. 显式选择的 JSON/TOML 文件；
4. 安全的非机密数字默认值。

以下项在操作者提供之前保持有意未设置：

- 会话、Hermes SQLite 与 Discord 归档路径；
- PostgreSQL DSN 或显式的键/值凭据文件；
- 嵌入提供方、密钥/密钥文件、模型与向量维度。

已安装的 CLI 在打开 PostgreSQL 或调用嵌入服务之前校验这些要求。失败使用稳定的 `error[code]: ...` 输出。`Settings.redacted()` 与 `config_summary()` 会在渲染诊断信息前替换 API 密钥与 DSN 密码。

`--legacy-openclaw` 是显式迁移模式。它仅在对应的 `SHIORI_*` 值尚未设置时提供旧式数据源/凭据路径；普通调用从不检查这些位置。

## 数据源选择

每个摄取命令指定一个数据源：

```text
shiori ingest --source sessions
shiori ingest --source hermes
shiori ingest --source discord --file /path/to/archive.jsonl
```

数据源路径通过 `SHIORI_SESSIONS_DIR`、`SHIORI_HERMES_DB` 或 `SHIORI_DISCORD_ARCHIVE_DIR` 传递。`--file` 是显式的单文件 Discord 导入，不会启用目录发现。

## PostgreSQL 凭据

使用以下任一方式：

- `SHIORI_DATABASE_DSN` / `SHIORI_DATABASE_URL`；或
- `SHIORI_PG_CRED`，指向包含 `host`、`port`、`dbname`、`user` 与 `password`（或 `dsn` 条目）的 mode-0600 `key=value` 文件。

不存在主目录凭据回退。Docker 助手通过 `SHIORI_PG_CRED` 接受同样的显式文件，或接受调用方提供的合成 `POSTGRES_DB`、`POSTGRES_USER` 与 `POSTGRES_PASSWORD` 变量。

## 嵌入

生产环境要求 `SHIORI_EMBEDDING_PROVIDER=voyage`、`SHIORI_VOYAGE_API_KEY` / `SHIORI_VOYAGE_KEY_FILE` 之一、`SHIORI_VOYAGE_MODEL` 与 `SHIORI_EMBED_DIM`。端点可用 `SHIORI_VOYAGE_API_URL` 覆盖；旧式开关为迁移显式提供历史 Voyage 端点/模型/维度。

当前 PostgreSQL schema 为 `vector(1024)`，因此在 schema 迁移新增其它维度之前，`SHIORI_EMBED_DIM` 必须为 `1024`。

测试使用确定性的内存向量，绝不使用生产密钥。不存在隐式的 fake 提供方。隔离的本地/CI 冒烟运行可显式选择以下全部设置；该提供方从不发起网络请求，且除非 opt-in 标志为 true，否则会被拒绝：

```text
SHIORI_EMBEDDING_PROVIDER=fake
SHIORI_ALLOW_FAKE_EMBEDDINGS=true
SHIORI_ENVIRONMENT=development
SHIORI_VOYAGE_MODEL=shiori-fake-v1
SHIORI_EMBED_DIM=1024
```

`fake`、缺失提供方、缺失密钥、缺失模型与缺失维度都会被生产预检拒绝。fake 提供方不是生产嵌入替代品；其模型必须使用保留的 `shiori-fake-*` 命名空间，生产环境同样会拒绝该命名空间。普通配置从不启用它。

## 测试数据库隔离

数据库测试仅在以下全部条件存在时激活：

```text
SHIORI_TEST_DATABASE_DSN
SHIORI_TEST_DATABASE_NAME
SHIORI_TEST_DATABASE_MARKER
```

fixture 在使用连接前校验 `current_database()` 与标记行。它只删除自身 `test-<run-id>` 命名空间下的行。CI 运行会创建随机临时数据库与标记，通过 `shiori db migrate` 应用已检入的仅向前迁移，然后在 `always()` 清理步骤中删除。另一个隔离的 CI fixture 应用历史 `schema.sql` 一次，并验证同一 CLI 命令将完整旧式结构纳入迁移台账；部分或漂移的旧式 schema 会被拒绝。全新数据库 fixture 不把 `schema.sql` 当作捷径。
