# shiori v0.1.0 private release-candidate checklist

This is a release-candidate checklist, not a release authorization. The
repository remains private until the owner makes a separate visibility
decision. Do not create a tag or release, publish a package, push an image,
deploy, register an external service, or write to production as part of this
checklist.

## Candidate identity

- [ ] Record the exact candidate commit, parent/base, branch, and Draft PR.
- [ ] Confirm the candidate worktree is clean and the candidate is not based on
      a stale main branch.
- [ ] Record hosted CI run IDs against that exact commit.
- [ ] Record the independent clean-machine/security peer result against that
      exact commit.
- [ ] Record final owner acceptance and the actual protected merge SHA, if the
      candidate is approved for merge.

## Required engineering gates

- [ ] `uv sync --locked --extra dev`, `uv lock --check`, Ruff, Pyright, and the
      full unit suite pass.
- [ ] Hosted PostgreSQL/pgvector service passes client/server major parity,
      vector preload, isolated marker/identity checks, and `shiori db migrate`
      followed by `shiori db health`.
- [ ] A synthetic `schema.sql` database is upgraded by the same CLI command;
      complete legacy structure is adopted without replaying DDL and partial
      or drifted structure fails closed.
- [ ] Backup creates a digest manifest and restore creates only a new staging
      database; bad manifests, tool failures, path collisions, and identity
      mismatches fail closed.
- [ ] The installed wheel runs the README lifecycle with synthetic sessions,
      Hermes, and Discord inputs, deterministic fake vectors, privacy
      retention/export/delete, query, and read-only MCP stdio search.
- [ ] sdist and wheel contain the runtime migration/schema/docs/tools needed
      by the documented install path.
- [ ] Direct dependency license metadata and `THIRD_PARTY_NOTICES.md` agree;
      pinned `pip-audit` reports no unwaived high-severity vulnerability.
- [ ] Reachable history, commit metadata, and built artifacts pass the
      offline secret/private-key/PII/host-path audit without exposing matches.
- [ ] The compose path builds the pinned Dockerfile image with a fresh
      project-scoped named volume, and its runtime smoke proves empty-volume
      initialization, readiness, vector extension writes, restart persistence,
      non-root execution, preload/CMD behavior, and removal of only the
      project-labeled resources before the HIGH/CRITICAL scan runs on that
      same image.

## Privacy and scope gates

- [ ] All source paths, database credentials, provider/key/model/dimension
      settings are explicit; no home-directory or host-credential fallback is
      used.
- [ ] Deterministic fake embeddings require both explicit development/test
      environment and opt-in, use the reserved `shiori-fake-*` model namespace,
      and are disclosed as local/no-external-call.
- [ ] Voyage and fake vectors are isolated by provider/model/dimension
      contract; incompatible rows are excluded or rejected structurally.
- [ ] Ingest is explicit and the clean-machine harness uses only synthetic
      inputs. MCP exposes read-only search, with bounded query/page/resource
      limits.
- [ ] The product remains local single-user, non-multitenant, stdio-only, and
      without an authenticated HTTP API or automated crawler.
- [ ] No real user documents, private paths, credentials, snapshots, or
      generated diagnostic artifacts are committed or uploaded.

## Known limitations

- The private candidate does not authorize deployment, external registration,
  production writes, package publication, image publication, or visibility
  changes.
- A production embedding service/key is required for real semantic ingest;
  deterministic vectors are only for isolated development/test smoke runs.
- `schema.sql` is retained as a historical legacy fixture. It does not repair
  drift; only an exact canonical legacy structure can be adopted, and all
  other existing drift is an operator-visible fail-closed condition.
- PostgreSQL/pgvector, container, and clean-install gates cannot be inferred
  from an offline workstation run. If those capabilities are unavailable
  locally, the checklist records them as unproven until hosted CI supplies the
  evidence; skipped tests never count as green.
