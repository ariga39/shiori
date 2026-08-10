FROM pgvector/pgvector@sha256:7ae6051efd0e60444282c27c7e141af07f322ce033300e727a49c3dd11075e38

# ⚠️ 历史遗留，未采用：live/代码/schema.sql 均不使用 pg_bigm（仅 vector + pg_trgm）。
# 2026-08-03 起唯一主路径是 deploy/docker-compose.yml + deploy/run.sh（官方
# pinned pgvector/pg17 镜像，无需自定义构建；compose 用 image 而非 build，故本
# Dockerfile 未被部署流程引用）。此文件仅作为可选/未采用的历史镜像定义保留。

# Preload the vector library at startup so the hnsw.ef_search GUC is registered.
# Without this, the extension loads lazily and a SET hnsw.ef_search issued as the
# session's first statement is silently dropped as a custom placeholder (query
# never raises), leaving ef_search stuck at its default 40. Preloading makes the
# GUC registered from the first session, so SET applies immediately and out-of-
# range values raise. Matches the live container's postgresql.conf setting
# (shared_preload_libraries='vector', 2026-08-03). This is mirrored by the
# compose `command` which is the deployed path.
CMD ["postgres", "-c", "shared_preload_libraries=vector"]
