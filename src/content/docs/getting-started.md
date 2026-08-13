---
title: Getting started
description: This guide follows Shiori's supported local lifecycle from a locked development install through its read-only MCP server.
---

This guide follows Shiori's supported local lifecycle from a locked development
install through its read-only MCP server.

## Install

Clone the repository and install the locked development environment:

```bash
uv sync --locked --extra dev
```

## Configure

Set the explicit database, source, and embedding-provider values described in
the [configuration reference](../configuration-reference/). Review the
[privacy policy](../privacy-policy/) before ingesting an archive. Shiori has no
implicit source, credential, or provider paths.

## Migrate

Apply the forward-only database migrations before ingesting data:

```bash
shiori db migrate
```

## Ingest

Select a source explicitly. For a configured sessions directory:

```bash
shiori ingest --source sessions
```

## Query

Search the indexed memory from the CLI:

```bash
shiori query 'what did we decide about the release?'
```

## Serve

Start the local read-only MCP stdio server:

```bash
shiori serve
```
