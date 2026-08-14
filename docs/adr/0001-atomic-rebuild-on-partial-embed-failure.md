---
title: "ADR-0001: Atomic full rebuild on partial embed failure"
description: "Choose option 1: atomic full rebuild. Embeddings are prepared before deletion, and delete plus insert remains transactional."
---

- **Status:** Accepted
- **Date:** 2026-08-03

## Context

Storage for each session id uses a delete-then-insert strategy: existing chunks
are removed and the batch is reinserted. Before this decision, the fallback
behavior for embedding failures was:

- if **all** embeddings failed, the run committed and returned without
  deleting, preserving existing data;
- if only **some** failed, the delete had already executed and the failed
  chunks were skipped, producing a partial rebuild recorded with
  `partial=True`.

The problem: this is a memory system, and dropping chunks loses memory. After a
partial failure the old chunks were destroyed but the new chunks were not fully
written back, so the session had fewer chunks than it should, and completion
had to wait for a later mtime/size change or a forced full rerun.

## Decision

Choose option 1: atomic full rebuild. Embeddings are prepared before deletion,
and delete plus insert remains transactional.

1. First compute embeddings for **all** chunks in the batch. If any embedding
   fails, the batch is **not written at all** (no delete, no insert); existing
   data is preserved and the batch is marked `partial=True` to be retried.
2. Only when the whole batch embedded successfully does the code execute
   `DELETE` plus the full `INSERT`, giving "replace entirely or change
   nothing" atomic semantics.

The existing advisory lock on the protected ingest path prevents a concurrent
instance from writing during the embedding phase.

## Alternatives

- **Option 2: repair markers.** Add a marker column and extend the checkpoint
  logic to record and backfill missing chunks. This can repair precisely, but
  requires a new database column and changes to checkpoint semantics, and it
  still cannot eliminate the failure window between delete and insert.
- **Option 3: document only.** Describe the limitation in the documentation.
  This only documents the data loss; the problem remains. Not adopted.

## Consequences

Positive:

- Eliminates the data loss caused by partial rebuild; a memory system must not
  drop chunks.
- The write-ahead ordering makes atomicity hold naturally, simpler and more
  reliable than the marker/checkpoint mechanism of option 2.

Negative / cost:

- For very large single batches (for example an initial full ingest or a
  forced full rerun), embedding the entire batch up front takes longer than
  writing as embeddings are produced, and the full batch of vectors must be
  held in memory.
- Failure aborts the whole batch, so the retry granularity moves from one
  chunk to the whole batch; if a single bad text keeps failing to embed, it
  blocks the session's update until that chunk is excluded or repaired.

## Current implementation

The write-ahead atomic full rebuild is implemented in `store_chunks` in both
the sessions and Discord ingest paths, and the Hermes ingest path reuses the
sessions chunking/embedding/storage path so all three sources implement the
same contract:

- the batch's embeddings are validated before any delete;
- any missing or failed embedding means no delete and no insert, and the
  caller records `partial=True`;
- a successful whole-batch embed performs the delete plus full insert with the
  caller recording `partial=False`;
- any insert failure rolls back the whole batch (undoing delete plus insert)
  and the caller records `partial=True`.

The `partial=True` outcome is written into the checkpoint as a zero size so the
file is retried on the next run rather than being treated as fully processed.
The advisory lock serializes concurrent runs of the same protected ingest
path.
