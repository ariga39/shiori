# session-memory-pg 设计文档

**版本:** 1.0
**日期:** 2026-08-03
**适用范围:** `infra/session-memory-pg/` 仓库

> 本设计文档完全基于仓库内实际代码编写（`ingest.py`、`ingest_discord.py`、`query.py`、`Dockerfile`），不描述代码中不存在的行为。代码引用以 `文件名:行号` 标注，可对照核验。

---

## 1. 项目概述与目标

> 当前安装与运行合同以根目录 `README.md`、`pyproject.toml`、
> `shiori.config.Settings` 和 `docs/CONFIGURATION.md` 为准。本文保留早期
> 运行时设计中的路径/部署记录；任何 OpenClaw/Hermes 路径在当前代码中
> 只通过显式 `--legacy-openclaw` 迁移开关启用，不是默认配置。

`session-memory-pg` 是一个把「对话会话历史」加工成语义可检索记忆的摄取与查询管线。它解决的核心问题是：OpenClaw / Discord 的会话记录是海量、非结构化、不可语义检索的原始文本，无法快速回答「我之前什么时候讨论过 X？」这类问题。

项目目标：

1. **把会话文本变成可语义检索的记忆** —— 用 token 级切块 + Voyage 向量嵌入，把原始 JSONL 转化为 `session_chunks` 表中的稠密向量片段。
2. **混合检索提升召回与精度** —— 向量相似度（语义）与关键词 tsvector（精确词）双路召回，用 RRF 融合排序。
3. **时间敏感排序** —— 带半衰期的时间衰减，让近期记忆更有权重。
4. **去重与保真** —— MMR 式的相似度去重，避免返回大量冗余片段。
5. **安全、幂等、可断点续传** —— advisory lock 防并发、按 session 先删后插、`ingestion_state` 表记录已处理文件以便增量。

覆盖两路数据源：

- `ingest.py` —— OpenClaw 主 agent 的 session JSONL（`ingest.py:26`）。
- `ingest_discord.py` —— Discord 频道归档 JSONL（`ingest_discord.py:27`）。

查询端为 `query.py`，CLI 用法：`python3 query.py "搜索词" [--limit N]`（`main()` 区 `query.py:248`）。

---

## 2. 系统架构

文本架构图（摄取与查询两路独立）：

```
                         ┌─────────────────────────────────────────────┐
                         │              PostgreSQL 17 (pgvector)       │
                         │                                             │
   OpenClaw sessions     │  ┌───────────────────────────────────────┐  │
   ~/.openclaw/agents/   │  │  session_chunks                       │  │
   main/sessions/*.jsonl │  │   id · content · embedding(1024)      │  │
        │                │  │   embedding_model · tsvector(ts)      │  │
        ▼                │  │   timestamp_start/end · source_type   │  │
   ┌──────────────┐      │  │   session_id · turn_index · channel   │  │
   │  ingest.py   │──────▶ │  └───────────────────────────────────────┘  │
   └──────────────┘ 读取/  │  ┌───────────────────────────────────────┐  │
                    切块/  │  │  ingestion_state                      │  │
   Discord archives │嵌入/  │  │   file_path · mtime · size · offset  │  │
   ~/.openclaw/.../ │ 写入  │  │   source_type · chunks_created       │  │
   discord-archive/ │      │  └───────────────────────────────────────┘  │
        │           │      └─────────────────────────────────────────────┘
        ▼           │
   ┌──────────────┐ │      ┌─────────────────────────────┐
   │ingest_discord│ │      │        query.py              │
   └──────────────┘ │      │  1. 查询文本 → Voyage 向量   │
                    │      │  2. pgvector 余弦 + tsvector │
                    │      │  3. RRF 融合 + 时间衰减 + MMR│
                    │      │  4. 返回 top-K 片段          │
                    │      └─────────────┬───────────────┘
                     │                    │
                     └── compose + 本地 pinned build ─┘ (pgvector/pg17)
                       (project-scoped named volume，主路径)

   嵌入服务（外部 API）: Voyage AI — voyage-4-large, 1024 维
   （摄取用 input_type="document"，查询用 input_type="query"）
```

**模块职责划分：**

| 文件 | 职责 | 关键入口 |
|------|------|---------|
| `ingest.py` | OpenClaw 会话摄取：解析 → 切块 → 嵌入 → 存储 | `main()`（`ingest.py:487`） |
| `ingest_discord.py` | Discord 归档摄取：解析 → 切块 → 嵌入 → 存储 | `main()`（`ingest_discord.py:393`） |
| `query.py` | 混合检索 + 时间衰减 + MMR 去重 | `search()`（`query.py:104`） |
| `deploy/docker-compose.yml` + `deploy/run.sh` | **数据库部署主路径**：构建仓库 Dockerfile 的 pinned `pgvector/pg17` 镜像 + preload（见 §6.1） | `run.sh up -d --build` |
| `Dockerfile` | **数据库镜像定义**：固定 pgvector 基础 digest，以非 root postgres 用户运行并通过 CMD preload vector | compose build |

---

## 3. 数据模型

> ⚠️ **关于 `session_facts` 表：** `session_facts` **存在于 live 数据库**（10 列，见 §3.3，由 `schema.sql` 固化对齐），但仓库当前**没有任何源码引用它**（`git grep` 无结果；两个 ingest 脚本与 query.py 仅读写 `session_chunks` 与 `ingestion_state`）。「源码无引用」≠「表不存在」：本文档 §3 按代码实际读写记录两张表（§3.1/§3.2），另在 §3.3 单独记录 live 中存在的 `session_facts` 结构以保证与 `schema.sql` 一致。

### 3.1 `session_chunks` —— 记忆片段主表

由 `ingest.py:367-383` 与 `ingest_discord.py:328-346` 的 INSERT 语句归纳字段。缺少数值类型时以推断标注。

| 字段 | 类型（推断） | 说明 | 来源 |
|------|------------|------|------|
| `id` | uuid (PK) | 主键 | `query.py:133` SELECT 首列 |
| `session_id` | text | 来源会话标识（OpenClaw UUID，Discord 为 `discord-<channel>`） | `ingest.py:226`、`ingest_discord.py:183` |
| `source_type` | text | `main_user` / `subagent` / `discord` 等 | `ingest.py:227`、`ingest_discord.py:209` |
| `content` | text | 切块后的文本内容 | `ingest.py:228` |
| `embedding` | vector(1024) | Voyage-4-large 向量 | `ingest.py:31`、`:377` |
| `embedding_model` | text | 固定为 `voyage-4-large` | `ingest.py:378` |
| `timestamp_start` | timestamptz | 块内最早消息时间 | `ingest.py:229` |
| `timestamp_end` | timestamptz | 块内最晚消息时间 | `ingest.py:230` |
| `turn_index_start` / `turn_index_end` | int | 块覆盖的起始/结束消息序号 | `ingest.py:231-232` |
| `metadata` | jsonb | 附加元数据（默认值填充，INSERT 不写） | 数据库 schema 实测 |
| `created_at` | timestamptz | 行创建时间（默认值填充，INSERT 不写） | 数据库 schema 实测 |
| `content_tsvector` | tsvector | `to_tsvector('simple', content)` 生成的全文索引 | `ingest.py:372` |
| `channel` | text | 仅 Discord 有值，会话来源为 NULL | `ingest_discord.py:215`、`:343` |

**要点：**

- `content_tsvector` 使用 **`'simple'` 配置**生成（`ingest.py:372`），对中英文都按字/词切分，供关键词检索。
- `embedding` 写入时用 `%s::vector` 强转字符串形式的向量列表（`ingest.py:377`）。
- `source_type` 用于标记来源与分类过滤。

### 3.2 `ingestion_state` —— 摄取断点表（幂等基础）

由 `ingest.py:409-427` 的 `get_processed_files` / `mark_file_processed` 归纳：

| 字段 | 类型（推断） | 说明 | 来源 |
|------|------------|------|------|
| `file_path` | text (PK) | 已处理文件的绝对路径，冲突更新键 | `ingest.py:424` |
| `file_mtime` | timestamptz | 处理时的文件 mtime | `ingest.py:417` |
| `file_size` | bigint | 处理时的文件大小；**partial 失败时记为 0 以触发下次重试** | `ingest.py:422` |
| `processed_offset` | bigint | 进度偏移，**恒等于文件 size** | `ingest.py:422` |
| `source_type` | text | `main_user` / `subagent` / `cron` / `empty` / channel 名等 | `ingest.py:422` |
| `chunks_created` | int | 本次生成的块数 | `ingest.py:422` |
| `processed_at` | timestamptz | `now()` 自动更新时间 | `ingest.py:421` |

### 3.3 `session_facts` —— live 中存在、源码未引用的表

> 本表存在于 **live 数据库**（10 列），但仓库当前**没有任何源码引用它**（`git grep` 无结果）。为与 `schema.sql` / live 保持一致，此处记录其真实结构（依据 live `information_schema` + `pg_dump --schema-only`，2026-08-03）。字段类型 / 可空性 / DEFAULT 逐列来自 live。规划中的「事实抽取」见 §7.2 #1。

| 字段 | 类型 | 说明 | 来源 |
|------|------|------|------|
| `id` | uuid (PK) | 主键 | live schema |
| `session_id` | text (NOT NULL) | 关联会话 | live schema |
| `category` | text (NOT NULL) | 事实分类 | live schema |
| `content` | text (NOT NULL) | 事实文本 | live schema |
| `embedding` | vector(1024) (nullable) | 事实向量 | live schema |
| `embedding_model` | text (NOT NULL) | 默认 `qwen3-embedding-0.6b` | live schema |
| `"timestamp"` | timestamptz (NOT NULL) | 事实时间戳 | live schema |
| `task_summary` | text (nullable) | 任务摘要 | live schema |
| `metadata` | jsonb (nullable, DEFAULT `'{}'`) | 附加元数据 | live schema |
| `created_at` | timestamptz (nullable, DEFAULT `now()`) | 行创建时间 | live schema |

对应索引：`idx_facts_category`（btree category）、`idx_facts_embedding`（hnsw）、`idx_facts_time`（btree `"timestamp"`）、`idx_facts_trgm`（gin content）。

---

## 4. 摄取管线设计

两个摄取脚本（`ingest.py` 与 `ingest_discord.py`）逻辑高度一致，差异仅在源解析部分。下面按阶段描述共同管线。

### 4.1 源文件发现（OpenClaw）

`find_session_files()`（`ingest.py:433`）在 `SESSIONS_DIR`（`~/.openclaw/agents/main/sessions`）下 glob 匹配 `*.jsonl` 与 `*.jsonl.deleted.*`，然后排除：

- `.trajectory.jsonl`、`.checkpoint.`、`.bak`、`.trajectory-path.json`（`ingest.py:437-444`）。

接着按 UUID 分组（文件名首段 `.` 前为 UUID，`ingest.py:448`），同一 UUID 若有多个文件：

- 全部活跃文件都保留；
- 已删除文件只取 **体积最大** 的一个（`ingest.py:466`），避免重复摄取。

**session_id 派生**（`derive_session_id`，`ingest.py:478`）：UUID；若文件名含 `.deleted.` 则加 `:deleted` 后缀，避免活跃/已删除会话在主键 `session_id` 上冲突（对应 REVIEW 记录 B6）。

### 4.2 消息解析与过滤（OpenClaw）

`parse_session_file`（`ingest.py:95`）逐行 JSON 解析，只保留 `type == "message"` 的对象。

`extract_text_from_message`（`ingest.py:117`）过滤规则：

- 只保留 `role in ("user", "assistant")` 的消息；跳过 tool/toolResult/system/image 等（`ingest.py:123`）。
- `content` 为列表时只拼接 `type == "text"` 的段（`ingest.py:131-137`）。
- 跳过纯 tool call JSON 的 assistant 消息（`ingest.py:143`）。
- 去空白后长度 < 5 的丢弃（`ingest.py:147`）。
- 最终文本格式化为 `"[role] text"`（`ingest.py:150`）。

**会话分类**（`classify_session`，`ingest.py:85`）读前 20 行各前 200 字符：

- 含 `Subagent` / `SubagentTask` / `[Subagent` → `subagent`；
- 含 `[cron:` / `cron job` → `cron`（**直接跳过不摄取**，`ingest.py:557-561`）；
- 否则 → `main_user`。

### 4.3 消息解析与过滤（Discord）

`ingest_discord.py`：

- 目录 `~/.openclaw/workspace/data/discord-archive`（`ingest_discord.py:27`），按 `*.jsonl` 处理，channel 名取文件名 stem（`:430`）。
- 只保留 `type ∈ {0, 19}`（普通 / 回复）消息（`ALLOWED_TYPES`，`ingest_discord.py:42`、`:91`）。
- 按时间戳升序排序（`ingest_discord.py:138`）。
- 文本格式化为 `"[YYYY-MM-DD HH:MM] 作者: 内容"`，附件/内嵌追加 `[attachment: ...]` / `[embed: ...]` 标记（`ingest_discord.py:105-115`）。
- `session_id` 固定为 `discord-<channel>`（`ingest_discord.py:183`）。

### 4.4 Token 级切块（chunking）

两脚本共用相同算法，参数一致（`ingest.py:33-34` / `ingest_discord.py:34-35`）：

```
CHUNK_TOKENS  = 400    # 目标块大小
CHUNK_OVERLAP = 80     # 相邻块重叠
```

- 用 `tiktoken` 的 **`cl100k_base`**（GPT-4 tokenizer）做近似切分（`ingest.py:42`）。
- 把所有符合条件消息文本用换行拼接成一个 token 流（`ingest.py:176`），再按固定窗口滑动切块：`tok_start` 每次前进 `CHUNK_TOKENS - CHUNK_OVERLAP` = 320 token（`ingest.py:237`）。
- 通过 token 偏移反向映射回字符区间，找到每个块覆盖的消息项，据此填 `timestamp_start/end` 与 `turn_index_start/end`（`ingest.py:205-212`）。
- 每块独立记录 `session_id`、`source_type`（`ingest.py:225-233`）。

**实现差异：** `ingest.py` 用字符偏移映射（`:184-212`），`ingest_discord.py` 用 token 边界映射（`:171-194`），结果一致。

### 4.5 Embedding（Voyage AI）

`embed_texts_with_retry`（`ingest.py:243` / `ingest_discord.py:226`）：

- 模型 `voyage-4-large`，维度 `EMBED_DIM = 1024`（`ingest.py:29-31`）。
- 批量调用，`VOYAGE_BATCH_SIZE = 128`（Voyage 单次上限）。
- 单条文本截断到 32000 字符（Voyage 输入上限，`ingest.py:251`）。
- 请求体 `input_type = "document"`（摄取）。
- 退避重试 `MAX_RETRIES = 3`，指数退避 `(2**attempt)*2` 秒（`:286`）。
- 429 时按 `Retry-After` 头等待（`:269-272`）。
- 请求间限速 `VOYAGE_RPS_LIMIT = 8`（每次调用后 sleep 1/8 秒，`:297-298`），控制在全局限速之下。
- 返回 `(embeddings, failed_indices)`，失败的批次记录索引供上层跳过。

### 4.6 幂等 / 去重 / 增量处理

**去重策略（先删后插）：** 每个 `session_id` 在写新块前先 `DELETE FROM session_chunks WHERE session_id = %s`（`ingest.py:349`、`ingest_discord.py:310`），然后整批重插。因此**重跑同一文件会整体替换**该 session 的块，天然幂等（对应 REVIEW B1、B11）。

**嵌入失败的保底（ADR-0001 原子全量重建）：** `store_chunks` 采用 write-ahead 语义——先检查整批嵌入是否全部有效；只要有任何一块嵌入失败或缺失，则**不 DELETE、不 INSERT**，`cur.close()` 后返回 `(0, 0)` 保留既有数据（`ingest.py:338-345`）；仅当整批嵌入全部有效时才执行 DELETE + 全量 INSERT，形成「要么全量替换、要么完全不变」的原子语义。由 `partial=True` 记录在案（`ingest.py:584`、`:589`）。

**每条插入用 SAVEPOINT 保护：** 单块插入失败回滚到 savepoint、记录 `insert_failed` 而不中断整批（`ingest.py:371-396`）；循环结束后任一 INSERT 失败即整批 `conn.rollback()`（撤销 DELETE+INSERT），返回 `(0, insert_failed)` 供调用方标记 `partial=True`（`ingest.py:399-401`）。

**增量处理（断点续传）：** `get_processed_files`（`ingest.py:409`）读取 `ingestion_state`；主循环对每个文件比较 **mtime 与 size**，两者均未变则跳过（`ingest.py:518-520`）。文件内容变化（mtime/size 变）即重新处理。`--force` 参数强制全量重跑（`ingest.py:508`）。

**分类跳过也记断点：** 空文件记为 `source_type="empty"`（`:542`），cron 会话记为 `"cron"`（`:558`），避免下次重复扫描。

### 4.7 并发控制（advisory lock）

- `ingest.py` 用 `pg_try_advisory_lock(784321)`（`ADVISORY_LOCK_ID`，`:39`、`:501`）——**尝试获取，拿不到即退出**（另一实例在跑）。
- `ingest_discord.py` 用 `ADVISORY_LOCK_ID = 784322`（与 ingest 不同，`ingest_discord.py:40`、`:404`），因此两脚本可并行，同脚本不并行。
- 结束时仅当确实持有锁才 `pg_advisory_unlock`（`ingest.py:636-641`，对应 REVIEW NB-unlock 修复）。

### 4.8 容错与错误恢复

`ingest.py` 主循环：

- 单文件异常捕获后 `conn.rollback()`；rollback 失败（连接不可用）会关闭并重连数据库，重连后**重新 `pg_try_advisory_lock`**（NB-C5-01 修复）——新连接不继承 advisory lock，拿不到锁则中止退出（`ingest.py:593-626`）。
- 连续错误数 > 20 则中止整个任务（`ingest.py:624-626`）。
- 每 50 个文件打一次进度日志（`:521-522`）。

---

## 5. 查询设计

`query.py` 的 `search(query, limit, offset)` 与 `search_page(query, limit, offset)`
执行完整混合检索流程。查询文本、页大小、offset、候选集都有硬上限；结果
按稳定的 score/id 顺序返回，`search_page` 用一个 look-ahead 行返回
`has_more`/`next_offset`，不执行无界 count 查询。

### 5.1 流程总览

```
查询文本
  │
  ├─ ① 向量检索（pgvector 余弦）
  │      query_embedding = Voyage(query, input_type="query")   ── 查询端嵌入
  │      SELECT ... WHERE model/dimension compatible
  │             ORDER BY embedding <=> q LIMIT pool              （余弦距离）
  │
  ├─ ② 关键词检索（tsvector BM25 风格）
  │      tsq = "词1 & 词2 & ..."（按空格 & 连接）
  │      ts_rank_cd(content_tsvector, to_tsquery('simple', tsq))
  │      ORDER BY tscore DESC LIMIT pool
  │        └─ 若空/异常 → 回退 pg_trgm similarity(content, query)
  │
  ├─ ③ RRF 融合（Reciprocal Rank Fusion）
  │      score(id) += 1/(k + rank)，k = 60
  │
  ├─ ④ 时间衰减  score *= 2^(-days_old / HALF_LIFE_DAYS)，半衰期 30 天
  │
  ├─ ⑤ 排序 → MMR 去重（余弦 > 0.85 则跳过）
  │
  └─ 返回 top-K（content, score, ts, session_id, source_type,
                  embedding_model, embedding_dimension）
```

### 5.2 ① 向量检索

- 查询文本先经 Voyage 嵌入，**`input_type = "query"`**（区别于摄取的 `"document"`），输入超过 8000 字符直接结构化拒绝，超时 30s。
- provider 返回的向量必须是有限数值、恰好 1024 维；若响应声明了不同 model
  或数据库行的 `embedding_model`/dimension 与当前配置不一致，结果 fail closed，
  不把不同模型/维度混入同一页。
- `embedding <=>` 为 pgvector 余弦距离，`1 - distance` 得相似度 `vscore`（`query.py:133-134`）。
- 候选池按请求页前缀计算为 `min(max((limit+offset)*5, 30), 1000)`，即取足够多的候选供后续融合去重，但不允许资源随请求无界增长。
- 向量查询前会 `SET hnsw.ef_search = clamp(pool, 200, 1000)`（`query.py:124`），clamp 下限 200、上限 1000。HNSW 默认 `ef_search=40` 在表增长到数万行时召回率不足，会静默漏掉相关块——此项最多取回 `ef_search=1000` 个候选（pgvector 参数上限，**NB-C6-01**：`pool>1000`（即 `limit>200`）时召回有上限，候选池不随 pool 无上限增长）。**2026-08-03 起容器已配置 `shared_preload_libraries='vector'`**（见 §6.1），GUC 在启动时注册：合法值直接生效，超范围（`> 1000`，即 `pool > 1000` / `limit > 200`）SET 会报 `InvalidParameterValue`。故查询端将值 **clamp 到 1000** 保证 SET 合法，召回不再回退打折；`except` 分支（B-C5-01 修复）仅作防御兜底。延迟不随 pool 无上限升高。

### 5.3 ② 关键词检索（tsvector / pg_trgm 回退）

- `_build_tsquery`（`query.py:86`）把查询按空白拆词，清洗 `' & | ! ( )` 后以 `&`（AND）连接，得到 `tsquery`。对中文因 `'simple'` 配置可逐字成词。
- 用 `ts_rank_cd` 对 `content_tsvector` 排名（`query.py:148`）——这是 **tsvector 全文检索排名**，代码注释称其为 BM25，严格说是类 BM25 的关键词排序（仓库内无真正的 BM25 排名函数）。
- **回退机制：** 若 tsvector 查询为空或抛异常（例如迁移期列不存在），回退到 **pg_trgm 的 `similarity()`**（`query.py:166-175`），需要数据库启用 `pg_trgm` 扩展。tsvector 异常后查询端会 `conn.rollback()`（NB-C5-04 修复），保证回退查询在可用连接上执行。

### 5.4 ③ RRF 融合

两路结果各自按 rank 打分，`k = 60`（RRF 常量，`query.py:181`）：

```
score(id) += 1.0 / (k + rank)     # 向量路 + 关键词路各加一次
```

纯基于排名而非原始分数融合，避免两种分数量纲不一致问题（`query.py:185-194`）。同时用 `meta` 字典缓存每 id 的 `(content, ts, session_id, source_type, embedding)`（`query.py:183`、`:188`、`:194`）。

### 5.5 ④ 时间衰减

```
HALF_LIFE_DAYS = 30
decay = 2 ** (-days_old / HALF_LIFE_DAYS)
score *= decay
```

（`query.py:24`、`:193-204`）以 `timestamp_start` 计算距今天数，半衰期 30 天，即记忆每 30 天权重折半。

**NULL-ts 兜底语义（2026-08-03 更新，对应 NB-C4-04 / NB-C5-05 / NB-C5-06）：** 时间戳解析失败时，`ingest.py` / `ingest_discord.py` 的 `store_chunks` **主路径写入文件 mtime** 作为 `timestamp_start` 兜底（`fallback_ts` 参数，`ingest.py:365`）——这比 INSERT 的 `created_at` 更接近消息时间，且重摄取同一文件（mtime 不变）不会把旧记忆「变新」、同批 NULL-ts 块衰减仍有区分度。**仅当 `fallback_ts=None`（`store_chunks` 旧签名 / API 默认 / 老调用）时才保留 `timestamp_start=NULL`**（`test_bad_timestamp_stores_null` 覆盖）；因此「不可解析时间戳存 NULL」的表述已更正——主路径不再存 NULL，NULL 仅是 `fallback_ts=None` 时的残留。

> ⚠️ **mtime 兜底的局限（NB-C5-06）：** mtime 是**文件修改时间，不是消息时间**。会话 JSONL 以追加方式写入，追加后整个文件的 mtime≈最近写入时刻，会把该文件内**历史坏 ts 的块整体抬成近期记忆**（它们共享新 mtime），造成时间衰减曲线失真。当前库里 null-ts 行数为 0，属**潜伏债**——一旦出现 fallback_ts=None 的旧调用或坏 ts 块，就会暴露。缓解方向：优先用文件内其它消息的 ts 作为 fallback_ts（而非 mtime），或拆分按消息时间归档；改动面大，暂只文档化。

查询端对**极少数双 NULL**（既无 `timestamp_start` 也无 `created_at`）的残留行应用固定低 prior `NULL_TS_PRIOR`（`query.py:27`、`:204`），避免其按恒 1.0 排成全新——该分支由 `test_double_null_uses_null_ts_prior` 覆盖（手工 INSERT 双 NULL 行，断言其被 0.25 prior 压到近期行之下）。管道 INSERT 均写 `created_at`（DEFAULT now()），故双 NULL 仅能由库外 INSERT 产生，属防御网。

### 5.6 ⑤ MMR 去重

按衰减后分数降序遍历候选，维护已选集合：

- 若新候选的嵌入与**任一已选嵌入的余弦相似度 > `MMR_SIM_THRESHOLD = 0.85`**，则跳过（`query.py:29`、`:225-230`）。
- 否则加入已选集并保留（`:236`）。
- 嵌入从 `embedding::text` 字符串解析回浮点列表在 Python 端做比较（`query.py:216-244`，见 §6 限制）。

这是**贪心最大边缘相关（maximal marginal relevance）式去重**：保留与既有结果足够不同的片段，避免同一话题重复刷屏。

### 5.7 输出

`main()`（`query.py:248`）打印每条的 score、时间、source_type，正文预览截断到 500 字符（`:261-262`）。

---

## 6. 运维

### 6.1 数据库与 Docker

- **容器重建剧本（`deploy/docker-compose.yml` + `deploy/run.sh`）：** compose 从仓库 Dockerfile 构建 pinned `pgvector/pg17` 镜像，使用按 Compose project 命名空间隔离的 named volume，端口 `127.0.0.1:5433:5432`，`restart: unless-stopped`。`POSTGRES_DB/USER/PASSWORD` 不硬编码明文，由 `deploy/run.sh` 从显式 `SHIORI_PG_CRED` 文件或环境变量注入；可选的 `SHIORI_COMPOSE_PROJECT` 只用于选择本地 project 命名空间。首次启动命令：`SHIORI_PG_CRED=/secure/shiori/postgres.env SHIORI_COMPOSE_PROJECT=shiori-local ./deploy/run.sh up -d --build`。
- **`shared_preload_libraries='vector'` 必须配置：** Dockerfile 的 CMD 在服务启动时预加载 vector，使 `hnsw.ef_search` GUC 在首个会话前注册；compose 不覆盖该 CMD。CI 的 runtime smoke 会检查 CMD、`SHOW shared_preload_libraries`、扩展写入、非 root uid 与重启后的数据。
- **回滚与导出：** 同一 project 的 named volume 可跨容器重建保留数据；只有显式 `docker compose down --volumes` 才删除它。跨 project 或升级前使用 `shiori db backup <path>` 与 `shiori db restore <src> --target <newdb>`，不要复制或绑定宿主数据目录。该路径不使用外部或预命名 volume，也不回滚到缺少 vector preload 的旧容器。
- 需在数据库中启用扩展：`pgvector`（`vector` 类型）、`pg_trgm`（query 回退用）。
- 表结构的历史快照保留在仓库根 `schema.sql`（`session_chunks`、`ingestion_state`、扩展、索引），但运行时换库/重建统一执行 `shiori db migrate`。完整、未登记的 legacy 结构会先做结构校验并登记初始 migration；部分或漂移结构拒绝升级。其中 `timestamp_start` / `timestamp_end` 为 **nullable**；主路径下时间戳解析失败会写入文件 mtime 兜底（`fallback_ts`），仅当 `fallback_ts=None` 时才存 `NULL`（见 §5.5）。
- 连接信息由 `deploy/run.sh` 从显式 `SHIORI_PG_CRED` 文件或 `POSTGRES_*` 环境变量注入，格式为 `key=value`，含 `dbname` / `user` / `password`；不会读取 home-directory fallback。
- Voyage API key 位于 `~/.openclaw/credentials/voyage-api-key.txt`（`ingest.py:30`）。

### 6.2 凭据管理

- 凭据均置于 OpenClaw 的 `credentials/` 目录，**不在仓库内**；`.gitignore` 明确忽略 `*.key`、`*credentials*`、`.env`、日志（`.gitignore:5,8-9,11-12`）。
- 代码运行前提：对应凭据文件存在且有读取权限。

### 6.3 调度（cron）

脚本为一次性 CLI，适合 cron 定时执行：

- 摄取（会话）：`python3 ingest.py`，可加 `--force` 全量重跑。
- 摄取（Discord）：`python3 ingest_discord.py`，可加 `--file <path>` 只处理单文件。
- 查询：`python3 query.py "关键词" --limit 10`，供其他进程/agent 调用读取。

仓库当前**未包含** cron 配置或 systemd unit；实际调度方式需由部署环境决定（见 §7）。

### 6.4 日志

- `ingest.py`：`logging.FileHandler("/tmp/session-memory-ingest.log")` + stdout（`ingest.py:44-50`）。
- `ingest_discord.py`：`/tmp/discord-ingest.log`（UTF-8）+ stdout（`ingest_discord.py:47-54`）。
- `query.py`：无文件日志，直接 print 结果。
- `.gitignore` 忽略 `*.log`，日志不入库。

### 6.5 干跑模式

两脚本均支持 `--dry-run`：只打印将要产生的块数与预览，**不连接数据库、不写入**（`ingest.py:487`、`:576-581`；`ingest_discord.py:393`、`:456-464`），用于上线前验证。

---

## 7. 已知限制与未来改进

以下均基于当前代码的**实际行为**整理，标注为现状与建议。

### 7.1 已知限制

1. **`session_facts` 表无源码引用。** 该表**存在于 live 数据库**（见 §3.3），但仓库无任何代码读写它。当前记忆粒度仅为「切块」，没有结构化的「事实/实体」层。
2. **MMR 在 Python 端逐条计算余弦，性能差。** `query.py:216-244` 把 `embedding::text` 字符串解析成 float 列表再与已选集逐对比较，候选多时开销大；且 `SELECT embedding::text` 全量取回向量的方式浪费带宽。
3. **tsvector 非真正的 BM25。** `ts_rank_cd`（`query.py:148`）是 tsvector 关键词排名，代码注释称「BM25」，实际并非标准 BM25 算法。
4. **`source_type` 分类基于前 20 行启发式。** `classify_session`（`ingest.py:85`）按关键词判断，可能误分类。
5. **部分嵌入失败时的「部分重建」：已关闭（ADR-0001 原子全量重建，2026-08-03）。** write-ahead 原子全量重建——任一嵌入失败整批不写（不 DELETE、不 INSERT），仅当全部嵌入成功才 DELETE+INSERT；若 INSERT 阶段仍有失败则整批回滚，无部分重建窗口，形成「要么全量替换、要么完全不变」的原子语义（见 `docs/adr/0001-atomic-rebuild-on-partial-embed-failure.md`）。
6. **~~无独立 schema 定义文件~~（已修复，2026-08-03）。** 表结构已固化在仓库根 `schema.sql`（含 `timestamp_start`/`end` nullable、扩展、索引），换库或重建直接执行该文件即可。
7. **`get_db()` 每次新建连接，无连接池。** 高频查询场景下开销偏高。
8. **只处理文本；图片、附件、tool 调用内容被丢弃。** 附件仅记录文件名占位（Discord）或完全跳过（OpenClaw）。

### 7.2 未来改进建议

1. **接入 `session_facts` 表**，从切块中抽取结构化事实/实体，支持「事实问答」，与 `session_chunks` 关联（表已存在于 live 库，见 §3.3，尚无源码引用）。
2. **服务化 / MCP server**：把 `query.py` 封装为 HTTP 服务或 MCP tool，供 agent 运行时内嵌调用，避免每次起进程、连库、调 Voyage。**已实现（2026-08-09）**：`mcp_server.py` 仅暴露只读 `search` 工具（query + bounded limit/offset，上限 20），返回 `has_more`/`next_offset` 与稳定 provenance，通过 stdio transport 运行；错误码不回显 DSN、凭据或后端异常文本。
3. **提供 forward-only migration** 并纳入 CI，另保留 `schema.sql` 作为 legacy 结构对照与受控升级输入，锁定表结构、索引（HNSW/IVFFlat 向量索引、GIN tsvector 索引）与扩展启用。
4. **MMR 向量化**：把候选嵌入一次性读入（或交给 pgvector 内置运算），避免逐条解析字符串。
5. **数据库连接池**（如 `psycopg2.pool` / PgBouncer）降低连接开销。
6. **备份与恢复**：对 `session_chunks` / `ingestion_state` 做定期 pg_dump / pgvector 感知备份；制定恢复演练。
7. **监控与告警**：跟踪摄取失败批次、Voyage 429/超时率、查询耗时、向量索引膨胀；对接 metrics。
8. **embedding 缓存与幂等去重**：按文本 hash 复用已嵌入向量，减少 API 调用成本。
9. **配置外置化**：把路径、参数、凭据挪到环境变量 / 配置文件，减少硬编码路径依赖。

---

## 附录：关键常量速查

| 常量 | 值 | 位置 |
|------|-----|------|
| `CHUNK_TOKENS` / `CHUNK_OVERLAP` | 400 / 80 | `ingest.py:33-34` |
| `VOYAGE_BATCH_SIZE` / `VOYAGE_RPS_LIMIT` | 128 / 8 | `ingest.py:35-36` |
| `EMBED_TIMEOUT` / `MAX_RETRIES` | 60s / 3 | `ingest.py:37-38` |
| `ADVISORY_LOCK_ID`（会话 / Discord） | 784321 / 784322 | `ingest.py:39`、`ingest_discord.py:40` |
| `EMBED_DIM` | 1024 | `ingest.py:31` |
| 单条嵌入截断 | 32000 字符 | `ingest.py:251` |
| 查询嵌入截断 / 超时 | 8000 字符 / 30s | `query.py:70,73` |
| `HALF_LIFE_DAYS` | 30 | `query.py:24` |
| `MMR_SIM_THRESHOLD` | 0.85 | `query.py:29` |
| RRF `k` | 60 | `query.py:181` |
| 候选池 `pool` | `max(limit*5, 30)` | `query.py:112` |
| 时间戳解析失败 | 写入文件 mtime 兜底（`fallback_ts`）；双 NULL 时查询端用 `NULL_TS_PRIOR` | `ingest.py:365`、`query.py:27` |
