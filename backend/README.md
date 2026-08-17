# AutoScript backend — ordered experiment blocks

The local-first AutoScript API stores an experiment as a named, ordered
collection of one or more independently runnable block ZIPs. PostgreSQL stores
the experiment/block/run metadata and MinIO stores unchanged ZIP and raw-result
JSON bytes.

## Experiment and block API

- `POST /api/v1/staged-blocks` validates and stores an owner-scoped immutable
  Block asset. It requires a UUID `X-Idempotency-Key`; an exact retry returns
  the same asset, while reuse for different bytes returns HTTP 409.
- `POST /api/v1/experiments/publish` atomically creates a non-empty Experiment,
  its ordered live Blocks, and revision 1 from staged assets.
- `POST /api/v1/experiments/{id}/publish` atomically replaces the complete live
  Block order/page layout and advances `current_revision_id`. Its body must
  include the revision seen by the editor as `expected_current_revision_id`;
  stale editors receive HTTP 409.
- `GET /api/v1/experiments/{id}` returns one shared-lab Experiment, including
  the explicit `current_revision_id` and immutable current revision.
- `POST /api/v1/experiments` creates an empty experiment.
- `GET /api/v1/experiments` lists experiments with their ordered `blocks`.
  Existing clients may omit paging parameters and continue receiving the full
  JSON array. New clients send `limit` (1–200), an optional opaque `cursor`
  copied from `X-Next-Cursor`, and an optional literal `search` substring.
  A `cursor` is valid only together with `limit`. Paged responses also include
  `X-Total-Count`. `include_versions=false` omits the unbounded legacy version
  history from list responses without loading it; the `versions` array is empty.
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
The API also records the package's expected prompt count and grid dimensions
on both the live Block and every immutable revision snapshot. Existing rows
migrated from older releases retain `null` metrics because Alembic cannot read
their object-storage ZIPs during a database migration.

Atomic publish bodies carry a UUID `request_id` and ordered references of the
form `{"source":"staged|existing","id":"...","same_page_as_previous":false}`.
The same request can be retried exactly without creating another revision.
The server copies every source to fresh live and revision objects before its
single database commit. A validation, storage, or database failure therefore
leaves the previous runnable Blocks and current revision unchanged; a failed
create leaves no empty Experiment. Staged assets expire after
`AUTOSCRIPT_STAGED_BLOCK_TTL_HOURS` (24 by default) and are cleaned
opportunistically. Legacy CRUD, upload, reorder, and revision routes remain
available for older clients.

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

For a revision-pinned Run, the server validates the Experiment, revision,
server Run, Block index/name/ID, Block count, and expected word count against
the immutable revision. Completion is derived from the number of stored word
records and the revision's expected prompt counts; client completion flags do
not decide final Run status. Revisions migrated without authoritative metrics
retain the previous compatibility calculation.

- `POST /api/v1/experiments/{id}/results` streams one raw Runner JSON file.
  `X-Filename` is optional. The body is validated against
  `schemas/data-contracts/raw-run.schema.json` and stored byte-for-byte.
- `GET /api/v1/experiments/{id}/runs` lists participant sessions. It supports
  the same opt-in `limit`/`cursor` contract plus filters for participant number,
  session text, lifecycle status, completeness, and the presence or absence of
  Raw data, analyzed CSV, and trainable JSON. `include_files=false` returns
  summary counts with empty `results`/`artifacts` arrays so list screens do not
  download every file descriptor. `complete` is true once all expected Blocks
  exist.
- `GET /api/v1/runs/{run_id}` returns one session.
- `GET /api/v1/run-results/{result_id}/download` returns the exact JSON bytes
  with `X-Checksum-SHA256`.
- `POST /api/v1/run-results/resolve` resolves owner-visible immutable results
  by exact SHA-256 and reports both matching result metadata and missing hashes.
- `GET /api/v1/runs/{run_id}/analysis-state` returns the current raw state JSON
  with `ETag`, revision, checksum, source-fingerprint, and `Last-Modified`
  headers. `If-None-Match` supports a bodyless HTTP 304 response.
- `PUT /api/v1/runs/{run_id}/analysis-state` creates an immutable draft. It
  requires a UUID `X-Idempotency-Key` and either the current `If-Match` ETag or
  `If-None-Match: *` for the first state. The newest 20 drafts are retained.
- `POST /api/v1/runs/{run_id}/analysis/finalize` atomically validates and stores
  a ZIP containing only `manifest.json`, `analysis_state.json`, `analysis.csv`,
  and `trainable.json`. The state, CSV, training JSON, and analysis status
  become visible in one database commit. `X-Existing-Analysis-Policy: keep`
  appends an immutable copy; `replace` creates the new copy before atomically
  removing older finalized copies. Finalized revisions are never pruned unless
  explicitly replaced or deleted.
- `GET /api/v1/runs/{run_id}/analysis-copies` returns finalized copies newest
  first, grouping the CSV and trainable JSON created together and identifying
  the copy that currently restores Analyzer editing state.
- `POST /api/v1/runs/{run_id}/analysis-copies/{revision_id}/set-editable`
  restores the matching immutable analysis-state snapshot for future editing.
- `DELETE /api/v1/runs/{run_id}/analysis-copies/{revision_id}` deletes that CSV,
  trainable JSON, and matching state snapshot but never deletes Raw Runner data.
- `POST /api/v1/experiments/{id}/bulk-export` accepts 1–500 unique `run_ids`,
  one or more of `raw_data`, `analysis_csv`, `trainable_json`, and
  `screenshots_zip`, and
  `analysis_policy: latest|all`. The server streams one bounded ZIP containing
  `manifest.json`, all selected immutable objects, their checksums, creation
  times, and analysis revision numbers. Raw data always includes every selected
  Run's Block results; the policy applies to both finalized and legacy analyzed
  copies. The response includes `X-Checksum-SHA256` for the complete ZIP.
- `PATCH /api/v1/runs/{run_id}/analysis` records whether analysis is completed.
- `POST /api/v1/runs/{run_id}/artifacts/{analysis_csv|trainable_json|analysis_state|screenshots_zip}` stores
  immutable Analyzer exports; exact retries are idempotent.
- `GET /api/v1/run-artifacts/{artifact_id}/download` returns exact artifact bytes.

Historical lab data can be staged with `scripts/prepare_historical_import.py`
and audited on the server before any write with:

```bash
python -m app.historical_import /path/to/import.zip --actor-username admin --dry-run
```

After the reported counts and target Experiment are verified, repeat with
`--apply`. Imports use deterministic session IDs and exact checksums: an exact
retry is a no-op, while conflicting bytes for an existing session are rejected.
Legacy Raw JSON bytes are retained unchanged, and screenshots are stored as one
immutable ZIP artifact per participant Run.
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
Migration `20260808_0010` adds authoritative word-count and grid metadata to
live and revision Blocks. Migration `20260808_0011` adds atomic publish
idempotency, expiring staged assets, and the explicit current-revision pointer.
Migration `20260808_0012` adds revisioned Analyzer state, permanent
finalizations, request idempotency records, CAS pointers, and a durable object
deletion queue. The legacy analysis status and artifact routes remain
available for older clients.

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
`/health/live` has no dependency checks. `/health/ready` returns 503 unless the
database is reachable, Alembic is exactly at the application schema revision,
and the configured object-storage bucket is reachable. Both responses disable
caching.

## Multi-user authentication

Set `AUTOSCRIPT_AUTH_MODE=token` to require authentication. Passwords are stored
with salted PBKDF2-SHA256 hashes. Login returns a random opaque bearer token;
only its SHA-256 hash is stored so the token can expire or be revoked.

- `POST /api/v1/auth/login`, `POST /api/v1/auth/logout`, `GET /api/v1/auth/me`
- Admin-only: `GET /api/v1/users`, `POST /api/v1/users`, and
  `PATCH /api/v1/users/{id}`
- Admin-only: `GET /api/v1/security/events` returns recent durable login,
  logout, session-cleanup, and user-administration audit events.

The three supported roles are:

- `admin`: full shared-lab access, including user, security, deployment, and
  application-component maintenance.
- `researcher`: all Experiment, Run, analysis, download, export, and deletion
  capabilities, without user administration or server/interface maintenance.
- `operator`: the same research/result access as a researcher, including Run,
  Test Run, analysis, and exports, but cannot create/edit/delete Experiments or
  delete participant Runs or saved analysis copies. Operators may upload and
  finalize raw results only for Runs they created.

Login failures are rate-limited in shared database state by normalized account
and a one-way hash of the client address, so limits apply across API workers.
The default principal limit is five attempts in five minutes followed by a
15-minute lockout; address limits use a configurable multiplier. Exact settings
are `AUTOSCRIPT_LOGIN_RATE_LIMIT_ATTEMPTS`,
`AUTOSCRIPT_LOGIN_RATE_LIMIT_ADDRESS_MULTIPLIER`,
`AUTOSCRIPT_LOGIN_RATE_LIMIT_WINDOW_SECONDS`, and
`AUTOSCRIPT_LOGIN_RATE_LIMIT_LOCKOUT_SECONDS`. Login/logout and administrator
security writes opportunistically delete expired/revoked access tokens and old
inactive limiter buckets; ordinary authenticated reads remain read-only.

Every HTTP response includes `X-Request-ID` and `X-Correlation-ID`. Safe values
provided by callers are propagated; otherwise a request UUID is generated.
Access and unexpected-error logs are JSON objects containing method, path,
status, duration, and these identifiers. Headers, query strings, request bodies,
passwords, bearer tokens, and exception messages are never logged. Unexpected
errors return a generic correlated HTTP 500 response.

Experiments and their Runs/results form one authenticated shared lab workspace.
Staged Builder uploads and idempotency records remain scoped to their creator,
and operator Run mutation is restricted to the operator who created that Run.
The desktop app prompts after an HTTP 401 and passes the session token only to
its Runner and Analyzer child processes.

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

The development OpenAPI document describes Bearer authentication, pagination
parameters and response headers, the binary Bulk-export response, analysis-copy
lifecycle operations, and the `keep`/`replace` finalization header. The same
schema is available as JSON at <http://127.0.0.1:8000/openapi.json>.

Migration `20260808_0013` adds durable security audit and shared login-rate-limit
tables. It depends on Analyzer migration `20260808_0012`.

## Test

Unit tests do not require Docker:

```powershell
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```
