# RedirX

RedirX proposes URL redirects for website migrations. The current product is a
remote MCP service with a browser companion for sign-in, review and downloads.
The matching engine uses URL retrieval and System One judgments; every proposal
requires an explicit decision before export.

## Current flow

1. Create an owned migration and import old/new URL inventories.
2. Retrieve candidates using path BM25, URL embeddings and confirmed examples.
3. Ask Jev to select candidates, then verify a shortlist in a second pass.
4. Review proposals, confirm or correct destinations, and optionally refine.
5. Export approved mappings through the existing artifact service.

Current runs use URLs only: no page-body crawl, automatic approval or automatic
410 decision. Model confidence is an estimate. Historical sessions and their
review/download paths remain supported.

## Where to work

| Concern | Entry point |
| --- | --- |
| Matching algorithm | `src/redirx/jev/{data,retrieve,examples,pipeline,questions}.py` |
| Algorithm's external interfaces | `src/redirx/jev/ports.py` |
| Pinned model client, validation and accounting | `src/redirx/jev/jev.py` |
| Run admission and review state | `backend/services/jev_pipeline_service.py` |
| Worker execution and durable provider accounting | `backend/services/jev_runner.py`, `jev_store.py` |
| Queue dispatch and leases | `backend/worker.py` |
| Ownership, review and exports | `backend/services/migration_*`, `mapping_decision_service.py` |
| HTTP API | `backend/app.py`, `backend/routes/v2_routes.py` |
| MCP transport and tools | `mcp-server/src/` |
| Resource-bound OAuth issuer | `mcp-auth-server/` |
| Browser companion | `frontend/src/components/PivotCompanionPage.tsx`, `PivotMigrationDetail.tsx` |

Read [CLAUDE.md](CLAUDE.md) for engineering invariants and checks. The
[Jev runtime inventory](docs/architecture/jev-pipeline-keep-manifest.md) and
[companion inventory](docs/architecture/jev-companion-keep-manifest.md) explain
shared and historical dependencies. The [release packet](docs/releases/jev-mvp-implementation.md)
records the original rollout constraints; it is not proof of today's live state.

## Local work

Python dependencies are in `requirements.txt`; install into an isolated virtual
environment. Frontend and MCP packages each have their own npm lockfile.
Use the relevant `.env.example` as a configuration reference, with isolated test
services. Never point an experimental worker at the production job queue.

`python dev.py` starts the legacy frontend/API/worker/mock-site workflow. It does
not start the MCP gateway or OAuth issuer. It stops only its own tracked processes;
on POSIX, each service has an isolated process group for descendant cleanup.
Ports remain fixed, and Vite fails when its port is occupied. Windows descendant
cleanup has not been verified. Start only the needed services individually when
working on the MCP product:

```sh
python -m backend.app
python -m backend.worker
npm run dev --prefix frontend
npm run dev --prefix mcp-server
npm start --prefix mcp-auth-server
```

For an offline core check, after installing Python dependencies:

```sh
python -m unittest backend.tests.test_jev_core -v
node scripts/check_pivot_contract.mjs
```

See [the cleanup audit](docs/architecture/jev-cleanup-20261001.md) for removed code,
verification and the next architectural work. No production deployment is implied
by checking out this repository; `render.yaml` is historically drifted.

## Testing

[CI and local checks](docs/testing.md) cover the application independently of its matching harness.
