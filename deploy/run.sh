#!/usr/bin/env bash
# session-memory-pg 容器重建脚本（Cycle 6 B-C6-01 修复的固化启动路径）。
#
# 作用：从凭据文件读取 POSTGRES_* 环境变量（不硬编码明文），然后基于
# deploy/docker-compose.yml 重建容器。官方镜像 + 显式
# `-c shared_preload_libraries=vector`，保证 pgvector 的 hnsw.ef_search GUC
# 启动即注册（B-C6-01）。
#
# 用法：
#   ./deploy/run.sh up    # 启动/重建并前台跟随（等同 docker compose up）
#   ./deploy/run.sh down  # 停止并移除容器（数据卷 session-memory-pgdata 保留）
#   ./deploy/run.sh logs  # 查看日志
#
# 回滚（2026-08-03，B-C7-01）：旧容器 session-memory-pg-old 已删除（其 Cmd 无
# preload，按字面回滚会撤销 preload 修复）。当前回滚 = 用本剧本重建（compose
# command 自带 preload，数据卷 session-memory-pgdata 未动）：./deploy/run.sh up。
# 若确需用旧容器回滚，须先确认其带 -c shared_preload_libraries=vector。

set -euo pipefail
cd "$(dirname "$0")/.."

CRED="${OPENCLAW_CRED_DIR:-$HOME/.openclaw}/credentials/session-memory-pg.txt"
if [[ ! -f "$CRED" ]]; then
  echo "error: credentials not found at $CRED" >&2
  exit 1
fi

# 从凭据文件读取 key=value，映射到 compose 需要的 POSTGRES_* 变量。
get() { sed -n "s/^$1=//p" "$CRED" | tr -d '\r'; }
export POSTGRES_DB="$(get dbname)"
export POSTGRES_USER="$(get user)"
export POSTGRES_PASSWORD="$(get password)"

if [[ -z "$POSTGRES_DB" || -z "$POSTGRES_USER" || -z "$POSTGRES_PASSWORD" ]]; then
  echo "error: credentials file missing dbname/user/password keys" >&2
  exit 1
fi

exec docker compose -f deploy/docker-compose.yml "$@"
