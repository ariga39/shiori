# shiyi (拾遗)

> 拾遗 — "to pick up what was left behind."

Searchable long-term memory for AI agents. Ingest conversation history from agent sessions into PostgreSQL + pgvector, then query it with hybrid search (vector + BM25 + exact substring).

## What it does

- **Ingest** agent conversation history (session transcripts) → chunk → embed (Voyage AI) → store in pgvector
- **Search** past sessions with hybrid retrieval: semantic vector + BM25 full-text + exact substring (the exact-substring channel matters for short CJK entity queries like 2-4 char names)
- **Expose** search to agents via a read-only MCP server

## Components

| File | Role |
|------|------|
| `ingest.py` | Session ingestion pipeline (chunk → embed → store) |
| `ingest_hermes.py` | Bridge for Hermes-era sqlite session store |
| `ingest_discord.py` | Discord archive ingestion |
| `query.py` | Hybrid search (vector + BM25 + exact substring) |
| `mcp_server.py` | Read-only MCP stdio server wrapping `query.search` |
| `schema.sql` | PostgreSQL schema |
| `deploy/` | Docker Compose + run script |

## Quick start

Prerequisites: PostgreSQL with pgvector, a Voyage AI API key.

```bash
# 1. Start the database
cd deploy && ./run.sh          # reads db creds, starts postgres+pgvector

# 2. Apply schema
psql -h 127.0.0.1 -p 5433 -U <user> -d <db> -f schema.sql

# 3. Ingest sessions
python3 ingest.py              # OpenClaw-era session dir
python3 ingest_hermes.py       # Hermes-era sqlite store
python3 ingest_discord.py      # Discord archive

# 4. Search
python3 query.py "what did we decide about X?"

# 5. Serve via MCP
python3 mcp_server.py
```

## Configuration

Paths are configurable via environment variables (defaults shown):

| Env var | Default | Purpose |
|---------|---------|---------|
| `SHIYI_SESSIONS_DIR` | `~/.openclaw/agents/main/sessions` | Session transcript dir |
| `SHIYI_VOYAGE_KEY` | `~/.openclaw/credentials/voyage-api-key.txt` | Voyage API key file |
| `SHIYI_PG_CRED` | `~/.openclaw/credentials/session-memory-pg.txt` | PG connection creds file |

## Search behavior

Hybrid retrieval merges three channels:
1. **Vector** (Voyage embeddings, cosine) — semantic similarity
2. **BM25** (PostgreSQL tsvector) — keyword/lexical match
3. **Exact substring** (ILIKE, for queries ≤ 20 chars) — reliable for short entity names where vector and BM25 both dilute

## License

MIT
