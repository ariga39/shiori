# Third-party notices

shiori is MIT-licensed under `LICENSE`. That license does not replace or
relicense the terms of dependencies. The direct dependencies locked by
`uv.lock` are listed below; their own distributions remain authoritative for
full notices and bundled-code terms.

| Package | Locked version | Declared license metadata |
| --- | ---: | --- |
| mcp | 1.29.0 | MIT |
| numpy | 2.4.6 / 2.5.1 (Python markers) | BSD-3-Clause, 0BSD, MIT, Zlib, CC0-1.0 |
| pyright (development) | 1.1.411 | MIT |
| psycopg2-binary | 2.9.12 | LGPL with exceptions |
| requests | 2.34.2 | Apache-2.0 |
| tiktoken | 0.13.0 | MIT |
| pytest (development) | 8.4.2 | MIT |
| ruff (development) | 0.16.2 | MIT |
| pip-audit (development/security gate) | 2.9.0 | Apache Software License |

The pinned metadata is checked without network access:

```bash
uv run python tools/check_licenses.py
```

The check fails closed when a direct dependency is missing or its installed
metadata no longer matches the documented allowlist. Transitive dependencies
are resolved by the lockfile and must retain their upstream notices when
redistributed in an application image.
