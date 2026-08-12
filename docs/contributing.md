# Contributing

Keep changes narrowly scoped, test them through public behavior, and report
what was and was not verified.

## Development setup

Install the repository's locked development environment:

```bash
uv sync --locked --extra dev
```

## Tests

Run the test suite from the repository root:

```bash
uv run pytest -q
```

Database tests require the isolated settings described in the
[configuration reference](CONFIGURATION.md#test-database-isolation). A skipped
test is an unverified capability, not a passing result. Report skips and
environment-limited failures explicitly.

## Documentation

Build documentation with warnings treated as errors:

```bash
uv run mkdocs build --strict
```

For a local preview only, run:

```bash
uv run mkdocs serve
```

The preview command does not deploy or publish the site. Documentation source
stays as Markdown under `docs/`, with navigation defined in `mkdocs.yml`.

## Pull requests

Keep each pull request focused on one behavior or maintenance concern. Include
the exact commands and outcomes used as evidence, distinguish skipped checks
from passing checks, and call out any operation that was intentionally outside
the task scope. Do not publish packages, images, documentation, or repository
visibility changes without separate authorization.

## Changelog fragments

User-visible changes require at least one changelog fragment. An ordinary
fragment is `changelog.d/<issue>.<type>.md`, where `<issue>` is a positive
integer and `<type>` is one of `feature`, `bugfix`, `doc`, `removal`, or
`misc`.

Internal or test-only pull requests may instead use exactly one non-empty waiver. A waiver is `changelog.d/<issue>.no-changelog.md`.

The waiver must explain why no user-facing changelog entry is needed and must not be mixed with ordinary fragments.
