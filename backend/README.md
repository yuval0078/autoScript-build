# AutoScript backend — ordered experiment blocks

The local-first AutoScript API stores an experiment as a named, ordered
collection of one or more independently runnable block ZIPs. PostgreSQL stores
the experiment/block/run metadata and MinIO stores unchanged ZIP and raw-result
JSON bytes.

## Experiment and block API

- `POST /api/v1/experiments` creates an empty experiment.
- `GET /api/v1/experiments` lists experiments with their ordered `blocks`.
- `PATCH /api/v1/experiments/{id}` renames or updates the description.
- `DELETE /api/v1/experiments/{id}` deletes metadata and unreferenced objects.
- `POST /api/v1/experiments/{id}/duplicate` copies an experiment and its block
  objects. Names use `name copy`, `name copy 2`, and so on when needed.
- `POST /api/v1/experiments/{id}/blocks` streams a ZIP upload. Send the ZIP
  filename in `X-Filename`, its package/config name in `X-Block-Name`, and an
  optional zero-based insertion index in `X-Position`.
- `PUT` or `PATCH /api/v1/experiments/{id}/blocks/order` accepts
  `{"block_ids": [...]}` containing every block ID in the desired order.
- `DELETE /api/v1/blocks/{block_id}` removes a block and closes the position gap.
- `GET /api/v1/blocks/{block_id}/download` returns the exact stored ZIP bytes.
- `GET /api/v1/experiments/{id}/download` returns the exact ZIP for a one-block
  experiment. For multiple blocks it returns a ZIP containing `experiment.json`
  and unchanged nested ZIPs under `blocks/`.

Uploaded block ZIPs are validated against
`schemas/data-contracts/experiment-package.schema.json`. The `name` inside the
package is the block name and must match `X-Block-Name`.

The legacy immutable-version routes remain available. A publish to
`POST /api/v1/experiments/{id}/versions` creates a block when empty and replaces
the runnable contents of a sole block while preserving version history. It only
appends when called on an existing multi-block experiment. Meanwhile,
`GET /api/v1/experiment-versions/{id}/download` still returns the exact legacy
package. Migration `20260807_0002` keeps the version history and creates one
block per existing experiment from only its latest version.

## Run results API

Cloud Runs have an explicit lifecycle:

- `POST /api/v1/experiment-revisions/{revision_id}/runs` creates an idempotent
  participant Run before the Runner starts.
- `POST /api/v1/runs/{run_id}/start` marks it running and can resume an
  `incomplete` or `failed` Run.
- `POST /api/v1/runs/{run_id}/results` stores one immutable Block result.
- `POST /api/v1/runs/{run_id}/finalize` derives `completed` or `incomplete`.
- `POST /api/v1/runs/{run_id}/cancel` records an intentional cancellation.
- `POST /api/v1/runs/{run_id}/fail` records an execution failure.

The desktop Runner copies pending uploads into its durable `api_upload_queue`.
Result uploads and finalization are retried in FIFO order after a connection
failure.

- `POST /api/v1/experiments/{id}/results` streams one raw Runner JSON file.
  `X-Filename` is optional. The body is validated against
  `schemas/data-contracts/raw-run.schema.json` and stored byte-for-byte.
- `GET /api/v1/experiments/{id}/runs` lists participant sessions and their
  ordered Block results. `complete` is true once all expected Blocks exist.
- `GET /api/v1/runs/{run_id}` returns one session.
- `GET /api/v1/run-results/{result_id}/download` returns the exact JSON bytes
  with `X-Checksum-SHA256`.
- `PATCH /api/v1/runs/{run_id}/analysis` records whether analysis is completed.
- `POST /api/v1/runs/{run_id}/artifacts/{analysis_csv|trainable_json|analysis_state}` stores
  immutable Analyzer exports; exact retries are idempotent.
- `GET /api/v1/run-artifacts/{artifact_id}/download` returns exact artifact bytes.
- `DELETE /api/v1/runs/{run_id}` removes a participant run and all raw/derived
  objects after explicit UI confirmation.

The legacy experiment-scoped route still creates a Run from the first result
for older clients. A retry for the
same session and Block index is idempotent only when its SHA-256 is identical;
different bytes return HTTP 409. Raw results are never overwritten. Legacy 1.0
result JSON without Block identity is accepted as a one-Block-compatible file.
Migration `20260807_0004` adds `experiment_runs` and `run_results`; migration
`20260807_0005` adds word/Block completeness, tri-state analysis status, and
immutable Analyzer artifacts. Migrations `20260808_0008` and `0009` add access
tokens, explicit Run lifecycle timestamps/status, and historical Run backfill.

## Run with Docker

From this directory:

```powershell
docker compose up -d --build
```

- API docs: <http://127.0.0.1:8000/docs>
- liveness: <http://127.0.0.1:8000/health/live>
- readiness: <http://127.0.0.1:8000/health/ready>
- MinIO console: <http://127.0.0.1:9001>

The API container applies migrations, creates the object-storage bucket, and
bootstraps a local actor before Uvicorn starts. The development stack uses
`AUTOSCRIPT_AUTH_MODE=local` and remains bound to localhost.

## Multi-user authentication

Set `AUTOSCRIPT_AUTH_MODE=token` to require authentication. Passwords are stored
with salted PBKDF2-SHA256 hashes. Login returns a random opaque bearer token;
only its SHA-256 hash is stored so the token can expire or be revoked.

- `POST /api/v1/auth/login`, `POST /api/v1/auth/logout`, `GET /api/v1/auth/me`
- Admin-only: `GET /api/v1/users`, `POST /api/v1/users`, and
  `PATCH /api/v1/users/{id}`

Experiments and all nested revisions, Runs, results, and artifacts are filtered
through their owner's user ID. The desktop app prompts after an HTTP 401 and
passes the session token only to its Runner and Analyzer child processes.

## Production deployment

Copy `.env.example` to `.env`, replace every `change-me` value, set a long
`AUTOSCRIPT_BOOTSTRAP_ADMIN_PASSWORD`, and run:

```powershell
docker compose -f compose.yml -f compose.production.yml up -d --build
```

The override enables token auth, disables API documentation, hides the MinIO
console, and binds the API to loopback for a TLS reverse proxy. Put Caddy,
nginx, or the hosting platform's HTTPS proxy in front of port 8000. Do not
expose PostgreSQL or MinIO publicly. Desktop clients set
`AUTOSCRIPT_API_URL=https://your-api-host` and authenticate in the app.

## Test

Unit tests do not require Docker:

```powershell
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```
