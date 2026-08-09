# ADR-0001: 部分嵌入失败时采用原子全量重建

- **状态:** 已接受（Accepted）
- **日期:** 2026-08-03
- **适用范围:** `infra/session-memory-pg/`
- **关联:** `docs/DESIGN.md` §7.1 #5

## 背景（Context）

`store_chunks`（`ingest.py:326-400`）对每个 `session_id` 采用「先删后插」：先
`DELETE FROM session_chunks WHERE session_id = %s`（`:349`），再整批重插有效块
（`:372-389`）。

变更前对嵌入失败的保底逻辑（B11 修复，`:339-345`）：

- 若**全部**嵌入失败 → 直接提交返回，不触发 DELETE，已有数据得以保留。
- 若**部分**失败 → DELETE 已执行，失败块被跳过不插入（`:372`），形成「部分重建」，
  由 `partial=True` 记录在案（`:583`、`:588`）。

问题：这是记忆系统。部分失败丢 chunks 等于丢记忆——DELETE 已销毁旧块，但新块未
完整补回，该 session 的块数量少于应有值，且要等下一轮 mtime/size 变化或 `--force`
才能补全。

## 决策（Decision）

选**方案 1：原子全量重建**——调整执行顺序（write-ahead）：embed 提前到 DELETE 之前，
DELETE+INSERT 仍作为一个事务原子执行：

1. 先对该 batch 的**所有** chunks 计算 embedding，若任何一块嵌入失败则**整体不写**
   （不 DELETE、不 INSERT），保留既有数据，标记 `partial=True` 等待下次重试。
2. 仅当 batch 全部嵌入成功时，才执行 `DELETE` + 全量 `INSERT`，形成**要么全量替换、
   要么完全不变**的原子语义。

配合现有 advisory lock（`ADVISORY_LOCK_ID = 784321`，`:39`、`:501`），不会出现
embed 期间另一实例并发写入的竞态。

## 备选方案（Alternatives）

- **方案 2：repair 标记（真列 + 断点逻辑）。** 在表加标记列、改断点续传逻辑来记录并
  补齐缺失块。可精确修复，但需新增 DB 列、改动 `get_processed_files` / 断点语义，复杂度
  不必要；且仍无法消除「DELETE 与 INSERT 之间的失败窗口」。
- **方案 3：文档化。** 仅在文档里写明该限制。只是把数据丢失写清楚，问题仍在，不采用。

## 后果（Consequences）

**正向：**

- 消除部分重建导致的数据丢失——记忆系统不允许丢块。
- write-ahead 顺序使原子性天然成立，比方案 2 的标记/断点机制简单可靠。
- cron 增量场景每批仅处理少量新文件（当前每小时 1–3 个），chunks 数量有限，
  「先全部 embed」的等待成本可接受，不存在「全新 session 要等很久」的实际场景。

**负向 / 代价：**

- 单批文件数很多时（如首次全量、`--force` 全量重跑），一次性 embed 全部 chunks 的
  等待时间会高于「边 embed 边写」，且需持有内存存完整 batch 的向量。
- 失败即整批不写，重试粒度从「单块」放大到「整批」；若个别坏文本持续嵌入失败，
  会阻塞整个 session 的更新直到该块被排除或修复。

## 实施（Implementation）

代码变更已实施（**2026-08-03**）：`ingest.py` 与 `ingest_discord.py` 的 `store_chunks`
均改为 write-ahead 原子全量重建（embed 提前到 DELETE 之前，DELETE+INSERT 事务原子）。验收点已全部落地：

- `store_chunks` 在 DELETE 前先判定 batch 是否全部嵌入成功（`ingest.py:339-345`）；
- 任一嵌入失败或缺失 → 不 DELETE、不 INSERT，返回 `(0, 0)`，调用方以
  `partial = (stored == 0 and len(chunks) > 0) or len(failed_indices) > 0 or insert_failed > 0`
  记录；
- 全部成功 → DELETE + 全量 INSERT，返回 `(stored, 0)`，调用方 `partial=False`；
- **任一 INSERT 失败 → 整批 `conn.rollback()`（撤销 DELETE+INSERT），返回
  `(0, insert_failed)`，调用方 `partial=True`**（`ingest.py:399-401`）；
- `ingest_discord.py` 同步逻辑（`:310` 的 DELETE）保持一致。
