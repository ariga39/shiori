---
title: 设计
description: Shiori 是一个将对话会话历史加工成语义可检索记忆的摄取与查询管线。
---

Shiori 是一个将对话会话历史加工成语义可检索记忆的摄取与查询管线。本文档描述当前架构。早期 `session-memory-pg` 实现的历史材料（旧脚本名、`python3 ingest.py`/`query.py` 调用与 OpenClaw 默认路径）仅在下方作为明确标注的 legacy 语境保留，不是当前主路径。安装、配置与运行时契约以根目录 `README.md`、`pyproject.toml`、`shiori.config.Settings` 与 `CONFIGURATION` 为准。

## 1. 入口与读写边界

已安装的 `shiori` 命令是当前主路径：

- `shiori ingest --source <sessions|hermes|discord> [--file <path>] [--dry-run]` 写入受管行。
- `shiori query [--limit/-n] [--explain]` 读取可搜索记忆。
- `shiori serve` 启动本地只读 MCP stdio 服务器，暴露单个 `search` 工具。
- `shiori db migrate|health|backup|restore` 管理 schema 与可移植快照。
- `shiori privacy retention-check|export|delete|providers` 实现生命周期契约。

MCP 表面严格只读：它不能摄取、迁移、删除、导出或修改源数据。原有的根级脚本（`ingest.py`、`ingest_discord.py`、`ingest_hermes.py`、`query.py`、`mcp_server.py`）保留为接受相同 `--config` 与 `--legacy-openclaw` 开关的兼容包装；新部署使用已安装的 `shiori` 命令。

## 2. 数据模型

### 2.1 `session_chunks` —— 记忆片段主表

检索使用的受管行集合。关键字段包括 `id`（uuid 主键）、`session_id`、`source_type`、`content`、`embedding`（`vector(1024)`）、`embedding_model`、`timestamp_start`/`timestamp_end`、`turn_index_start`/`turn_index_end`、`metadata`、`created_at`、`content_tsvector`（使用 `'simple'` 文本配置生成）与 `channel`（仅 Discord）。`embedding_model` 与向量维度参与查询时的兼容性过滤。

### 2.2 `ingestion_state` —— 断点表

记录每个已处理文件（`file_path`、`file_mtime`、`file_size`、`processed_offset`、`source_type`、`chunks_created`、`processed_at`），使重处理增量且幂等。部分失败的文件记录 size 为 0，以在下次运行时强制重试。

### 2.3 `session_facts` —— legacy 状态

部分 live 数据库存在 `session_facts` 表（含 HNSW 嵌入索引与 category/time/trigram 索引）。摄取与检索管线不使用它，但隐私生命周期操作仍会统计、导出和删除 legacy 行。这里将其记录为 legacy 结构事实，不是活跃的摄取或检索能力。

## 3. 摄取管线

### 3.1 数据源发现

每次摄取显式指定一个数据源；不会静默发现任何东西。

- sessions：使用真实 adapter 选择规则在配置的 sessions 根目录下发现文件，会话 id 由 basename 派生。
- hermes：来自已配置 Hermes SQLite 数据库的会话数据。
- discord：配置的 discord 根目录下每个 `*.jsonl` 映射为 `discord-{stem}`；`--file` 恰好导入一个文件。

### 3.2 解析与过滤

每个 adapter 解析其归档并过滤为受支持的消息形状：sessions 与 Hermes 保留 `user`/`assistant` 文本并丢弃 tool/附件/空片段；Discord 保留普通/回复消息并格式化为 `[timestamp] 作者: 内容`，带附件/内嵌标记。敏感形状在存储前由 `shiori.privacy.minimize` 脱敏（见下方隐私生命周期）。

### 3.3 Token 切块

文本使用分词器切分为固定大小的块（`CHUNK_TOKENS` 与 `CHUNK_OVERLAP`），将 token 偏移反向映射回字符区间，使每块记录其覆盖的 `timestamp_start/end` 与 `turn_index_start/end`。

### 3.4 嵌入与校验

块通过已配置提供方嵌入（生产使用 Voyage 与 `EMBED_DIM = 1024`），批量调用带有限重试与限速。响应必须是配置维度的有限向量；提供方/模型不匹配时 fail-closed，而不是混入不兼容向量。确定性的 `fake` 提供方仅可显式选择，绝不隐式选择，也绝不用于生产数据。

### 3.5 原子存储、断点与锁

- 存储按会话整体要么全写、要么不写：仅当整批嵌入全部成功才写入新批次，形成「要么整体替换、要么完全不变」的语义。
- 每次插入受 savepoint 保护；任何失败都会回滚整批。
- `ingestion_state` 记录文件 mtime/size，使未变化文件被跳过、已变化文件被重处理。
- PostgreSQL advisory lock 串行化同一命令的并发运行。

## 4. 检索

### 4.1 候选渠道

混合检索从三个渠道构建候选：

- dense：基于查询嵌入的 pgvector 余弦相似度；
- lexical：PostgreSQL 全文排名（对 `content_tsvector` 的 `ts_rank_cd`，退化输入使用 trigram 回退）；
- exact：短查询的子串匹配。

不同嵌入模型或向量维度的行会被排除，而不是静默混入一页。候选池有界，因此资源不会无界增长。

### 4.2 RRF 融合

渠道排名通过倒数排名融合合并（`score += 1/(k + rank)`），这是排序信号，不是正确率概率。

### 4.3 Intent-gated 时间衰减

衰减仅在显式时间意图下应用（结构化时间范围或受限的 recency grammar）；普通事实/历史查询不衰减。衰减公式与半衰期与冻结契约保持一致。

### 4.4 Provenance-preserving 去重

相似度超过固定阈值的嵌入会被抑制，同时保留携带 provenance 的独特片段。

### 4.5 有界分页

分页限制结果数量并约束 offset。分页使用单行 look-ahead 报告 `has_more` 与稳定的 `next_offset`，而不是无界计数查询。

### 4.6 Opt-in 解释

`shiori query --explain`（或 MCP `search` 的 `explain:true`）报告每条结果的 `score_kind`、`adjustments`、`channels`（含 `matched` 与 `candidate_rank`）、`matched_channel_count` 与 `multi_channel`，以及页面级 `explain_summary`。解释字段描述检索排序与佐证；它们都不是概率、置信分或硬阈值。CLI 诊断输出到 stderr，使 stdout 保持管道干净。

## 5. 数据库、容器、凭据与隐私生命周期

- Schema 由记录在 `shiori_schema_migrations` 中的仅向前迁移管理，由 `shiori db migrate` 在由 advisory lock 串行化的隔离事务中应用。`schema.sql` 是 legacy 一次性引导参考；旧式结构会被校验并记录而不是重放。
- 容器镜像是固定版本 `pgvector/pg17` 构建，由 compose 使用 project 命名空间 named volume、非 root `postgres` 用户与 `vector` preload 运行。`shiori db backup`/`restore` 提供可移植快照；绝不绑定宿主机数据目录。
- 凭据显式提供：DSN 或 mode-0600 `key=value` 文件。不存在主目录凭据回退。
- 隐私生命周期仅作用于受管行，并保持外部源文件逐字节不变：`retention-check` 报告年龄，`export` 写入不含 embeddings/tsvector/密钥的确定性原子产物，`delete` 是单个幂等事务，`providers` 披露端点与 `configured`/`not_configured` 状态。

## 6. 已知限制与未来方向

- `session_facts` 没有活跃源码引用；当前记忆粒度是片段而非结构化事实。
- 关键词排名使用 PostgreSQL 全文排名，而非真正的 BM25 实现。
- 去重的嵌入比较在候选窗口的 Python 端完成，当前规模下可用，但可下沉到 pgvector。
- 连接每次命令新建；高频率查询下连接池可降低开销。
- 只索引文本；图片、附件与 tool 调用正文被丢弃（Discord 附件可能保留占位符）。

未来方向包括结构化事实抽取、MMR 向量化、连接池、按内容哈希的嵌入缓存，以及对摄取失败、提供方延迟、查询延迟与索引增长的监控/告警。

## 7. 关键常量

| 常量 | 值 | 说明 |
| --- | --- | --- |
| `CHUNK_TOKENS` / `CHUNK_OVERLAP` | 400 / 80 | token 切块窗口 |
| `EMBED_DIM` | 1024 | `vector(1024)` schema |
| `HALF_LIFE_DAYS` | 30 | 时间衰减半衰期 |
| RRF `k` | 60 | 融合常量 |
| MMR 相似度阈值 | 0.85 | 近重复抑制 |
| MCP `limit` 上限 | 20 | 每页最大结果数 |
| MCP `offset` 范围 | 0..255 | 有界分页 |
| fake 提供方命名空间 | `shiori-fake-*` | 生产环境拒绝 |

所有技术 literal（命令、环境变量、常量、错误码）以当前源码与根文档为准；本文档仅汇总，不发明新契约。
