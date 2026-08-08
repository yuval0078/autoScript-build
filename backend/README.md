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

- `POST /api/v1/experiments/{id}/results` streams one raw Runner JSON file.
  `X-Filename` is optional. The body is validated against
  `schemas/data-contracts/raw-run.schema.json` and stored byte-for-byte.
- `GET /api/v1/experiments/{id}/runs` lists participant sessions and their
  ordered Block results. `complete` is true once all expected Blocks exist.
- `GET /api/v1/runs/{run_id}` returns one session.
- `GET /api/v1/run-results/{result_id}/download` returns the exact JSON bytes
  with `X-Checksum-SHA256`.
- `PATCH /api/v1/runs/{run_id}/analysis` records whether analysis is completed.
- `POST /api/v1/runs/{run_id}/artifacts/{analysis_csv|trainable_json}` stores
  immutable Analyzer exports; exact retries are idempotent.
- `GET /api/v1/run-artifacts/{artifact_id}/download` returns exact artifact bytes.
- `DELETE /api/v1/runs/{run_id}` removes a participant run and all raw/derived
  objects after explicit UI confirmation.

The first result for a `session_id` creates its Experiment run. A retry for the
same session and Block index is idempotent only when its SHA-256 is identical;
different bytes return HTTP 409. Raw results are never overwritten. Legacy 1.0
result JSON without Block identity is accepted as a one-Block-compatible file.
Migration `20260807_0004` adds `experiment_runs` and `run_results`; migration
`20260807_0005` adds word/Block completeness, tri-state analysis status, and
immutable Analyzer artifacts.

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
bootstraps a local actor before Uvicorn starts. Authentication remains deferred;
the development Compose stack is bound to localhost.

## Test

Unit tests do not require Docker:

```powershell
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```
