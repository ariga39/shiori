# Phase 3 v0.1 private release-candidate handoff

This is a private release-candidate handoff, not a release authorization. The
repository must remain private. No tag, GitHub release, package publication,
image push, deployment, external registration, or production write is part of
this task.

## Candidate identity

- Base/current main at task start: `c0e22f499602ca4331444a85e70e69faba9890af`.
- Historical implementation checkpoint before the append-only hosted-driven
  successors (not an acceptance object):
  `3b6224298b4cb1d7c1bc502045278315f259f46c`.
- The current candidate is the append-only tip after this handoff and the
  narrow hosted-driven pgvector guard successors; its complete SHA, branch,
  Draft PR, and hosted run IDs are supplied by the publication message because
  a commit cannot safely self-reference its own object ID. Earlier tips are
  historical parents, not acceptance objects, and their head-specific CI or
  peer evidence must not be reused.
- Branch: `phase3/v01-release-shizuka`.
- Worktree: `/home/raft/shiori-phase3-release-shizuka`.
- The candidate must be checked for a clean worktree and exact ancestry before
  publication. A changed candidate invalidates all head-specific evidence.

## Owned delivery

The candidate covers package/install metadata, explicit configuration and the
development-only deterministic embedding provider, forward migration/health
and legacy adoption, isolated backup/restore lifecycle, synthetic source
smoke, privacy lifecycle, bounded query/read-only MCP smoke, README/config
contracts, CI gates, license/dependency/container/history/artifact audits, and
the release checklist. Existing migration files and prior search/privacy
semantics were not rewritten.

The deterministic provider requires all of: `provider=fake`, explicit
`SHIORI_ALLOW_FAKE_EMBEDDINGS=true`, `SHIORI_ENVIRONMENT=development|test`, an
explicit reserved `shiori-fake-*` model, and dimension 1024. It uses no network
or credentials and is disclosed as local-only. Query and ingest both bind
provider/model/dimension, so fake and production vectors cannot mix; production
rejects the fake namespace. Provider/privacy output is redacted and never
returns a key, DSN, or path.

The explicit `SHIORI_PG_CRED` key/value file path is validated at the typed
configuration boundary and returned as safe `psycopg2.connect(**credentials)`
parameters, while a direct DSN remains `{"dsn": ...}`. The installed CLI and
privacy/query callers therefore do not index a raw mapping as if it contained
a `dsn` key. Unknown, duplicate, missing, malformed-port, symlink, and
non-private-file inputs fail with structured, non-secret errors. The clean
wheel smoke now creates a synthetic mode-0600 key/value file and runs the
installed CLI lifecycle through that path.

Restore verifies the staging database name, live database OID, generated
marker, and creation-time OID before returning success. A one-row guard that
was replaced by restore input therefore fails closed; cleanup also compares
against the locally captured creation-time OID rather than trusting a mutable
replacement guard row.

Legacy adoption is fail-closed: only a complete canonical `schema.sql` shape
can be registered as migration 0001 without replaying DDL. Partial, drifted,
or ambiguous structures receive a structured error, write no migration ledger
row, and do not mutate the schema. Forward migrations then run normally.

The pgvector preload gate receives the GitHub Actions job-service container ID,
requires exactly one matching container, reads its immutable image ID, and
checks that image object's `RepoDigests` for the pinned digest before restart.
Missing, empty, ambiguous, malformed, or mismatched identity data fails closed;
the verifier is packaged and covered by script-level counterexamples.

The retained container delivery now has one runtime path: `deploy/docker-compose.yml`
builds the pinned `Dockerfile` and runs its local `shiori-pgvector:local` image;
there is no optional-only image path. The compose service uses a fresh
project-scoped named volume, never a host bind path or external/fixed volume.
`tools/container_runtime_smoke.sh` uses a fresh project name to verify
empty-volume initialization, readiness, vector extension/write, restart
persistence, non-root execution, inherited entrypoint/CMD and preload, and
removal of only the project-labeled resources. The container scan consumes the
same compose-built image tag after its immutable image ID is recorded; the image
is never pushed. Portable data movement is through `shiori db backup` and
`shiori db restore`, not host-directory copying.

## Evidence ledger

The following statuses are deliberately separate; a local skip is not a green
release gate.

| Gate | Local candidate evidence | Hosted requirement |
| --- | --- | --- |
| locked install, Ruff, Pyright, unit tests | `163 passed, 103 skipped`; skips are explicit no-local-PostgreSQL classes; Ruff/Pyright/lock/compileall/diff-check clean | terminal green |
| PostgreSQL/pgvector, client/server major parity, vector preload, isolated DB marker/identity | not available on the author workstation | real service, no required skips |
| fresh `shiori db migrate`/`db health` | command is wired into CI and clean-machine smoke | terminal green |
| legacy schema adoption and partial/drift rejection | synthetic tests and `tools/legacy_schema_upgrade_smoke.sh` | terminal green |
| wheel/sdist clean install and README lifecycle | harness is packaged and invoked with the installed wheel, then sdist | terminal green |
| MCP stdio tool-list/search | harness is packaged; local execution requires the isolated DB | terminal green |
| license and locked dependency audit | `check_licenses.py`, locked export, and pinned `pip-audit` are wired | terminal green with no unwaived high/critical finding |
| reachable history/commit metadata/artifact audit | offline `release_audit.py` is wired and emits counts/object prefixes only | terminal green on a non-shallow checkout |
| compose-built container runtime smoke and HIGH/CRITICAL scan | Docker unavailable locally | terminal green against the same built image |

At the latest offline verification, the local full suite was `163 passed, 103
skipped`; the skips are explicit no-local-PostgreSQL classes. Ruff,
Pyright, lock, compileall, and diff-check were clean. The exact successor's
release-audit aggregate is recomputed after its append-only commit and reported
with the publication message; these local results do not substitute for the
hosted PostgreSQL, clean-install, MCP, runtime-container, and scan gates.

At the last offline verification, the workstation had no PostgreSQL/pgvector
service and no Docker. Those capabilities therefore remain unproven locally;
the expected `skipped` test classes must be reported by hosted CI and cannot be
silently counted as passed.

## Scanner and pin contract

The workflow uses immutable full-SHA action references with their reviewed
release mapping:

- `actions/checkout` v5.0.0:
  `fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09`.
- `actions/setup-python` v6.3.0:
  `ece7cb06caefa5fff74198d8649806c4678c61a1`.
- `astral-sh/setup-uv` v6.6.1:
  `557e51de59eb14aaaba2ed9621916900a91d50c6`.
- `actions/upload-artifact` v4.6.2:
  `ea165f8d65b6e75b540449e92b4886f43607fa02`.
- `aquasecurity/trivy-action` v0.36.0:
  `ed142fd0673e97e23eac54620cfb913e5ce36c25`.

The pgvector service, retained Dockerfile, and local compose image all pin
the immutable image digest
`sha256:7ae6051efd0e60444282c27c7e141af07f322ce033300e727a49c3dd11075e38`.
The container gate builds the image first and scans that built image, rather
than scanning only the Dockerfile. The hosted runner is authoritative for the
action-runtime/Node compatibility check; no floating action tag is accepted.

`pip-audit==2.9.0` is locked and runs with `--strict`; the container scan fails
on HIGH/CRITICAL vulnerabilities and ignores only unfixed upstream findings.
There is no broad package/CVE allowlist. The history audit scans all reachable
blobs and commit metadata, verifies the checkout is not shallow, checks the
ignore policy, and scans built artifacts. It reports only rule/count/source and
object-hash prefixes; it never prints matched text, private paths, or samples.

## Failure evidence and cleanup

CI creates an isolated random database and marker, runs the required gates,
and drops the database only after the cleanup guard verifies the job-owned name
and marker. Failure evidence is generated only after a failure, then audited
by construction as a fixed JSON summary with `raw_logs_uploaded=false`,
`secret_matches=counts_only`, and one-day retention; successful runs upload no
diagnostic artifact. No host `HOME`, ambient PostgreSQL password, user
credential, private source path, or real document is used by the clean-machine
harness. The container smoke likewise uses only synthetic credentials and a
fresh Compose project/name-scoped volume; it refuses to reuse an existing
project and fails if compose leaves project-labeled containers, networks, or
volumes behind.

## Peer and final gates

After the exact candidate is published as a private Draft PR, hosted CI must be
terminal green on that exact SHA. Only then should @Mirai perform the
independent clean-machine/security peer review. Any author correction must be
an append-only successor with fresh CI and fresh peer evidence. @momoko gives
the final acceptance and records the protected merge SHA if the candidate is
approved; release/publication/visibility decisions remain separate and are not
implied by merge.
