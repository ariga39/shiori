---
title: Deploy to Cloudflare Workers
description: Connect the repository in Cloudflare Workers Builds, then use the pinned repository commands below.
---

The documentation site is a static Starlight build emitted to `dist/`. It is
deployed as Cloudflare Workers Static Assets, with no Worker script or
bindings required.

Connect the repository in Cloudflare Workers Builds, then use the pinned repository commands below.

## Local preparation

Install the pinned Node dependencies and verify the static build and the
Workers dry-run locally before relying on Cloudflare:

```bash
npm ci
npm run docs:workers:dry-run -- --outdir /tmp/shiori-worker-bundle
```

The dry-run builds `dist/` and packages it without uploading anything. No
authentication, account, or network upload happens during the dry-run.

## Cloudflare Workers Builds configuration

An owner links this GitHub repository in the Cloudflare dashboard, selects the
production branch, and the dashboard stores the deployment authorization on
the Cloudflare side.

The repository provides only the project-level configuration:

- **Build command:** `npm run docs:build`
- **Deploy command:** `npm exec -- wrangler deploy --config wrangler.jsonc`
- **Build output directory:** `dist/` (the site root is `/`)

`wrangler.jsonc` only pins the static assets directory. Do not commit Cloudflare account IDs, Worker identifiers, routes, domains, or API tokens to this repository.

A real deploy occurs only after the owner completes the dashboard
configuration and Cloudflare executes the pinned commands. Until then the
local dry-run is the only verified deployment path.
