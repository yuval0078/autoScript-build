# AutoScript data contracts

This directory defines the durable data boundary between Builder, Runner, Analyzer, the future API, PostgreSQL, and object storage.

The contracts are intentionally separate from the application release version. Application code may change without changing a data schema, and a data schema may evolve while older application releases remain readable.

## Current-format inventory

The contracts are based on the formats currently produced by the desktop applications:

- Builder exports a ZIP containing one experiment JSON file and a `media/` directory.
- Runner writes a result object containing experiment metadata, participant metadata, calibration data, and word-level pen events.
- Analyzer exports a CSV research table and a trainable JSON file.
- Existing trainable JSON is ambiguous because one participant is exported as an object while multiple participants are exported as an array. The canonical contract always uses an object with a `participants` array.

Legacy files remain importable. The schemas in this directory describe the canonical format that new server-facing code will produce.

## Contract families

| Contract | Schema name | Version | Primary owner |
| --- | --- | --- | --- |
| Experiment package manifest | `autoscript.experiment-package` | `1.0.0` | Builder |
| Raw run data | `autoscript.raw-run` | `1.0.0` | Runner |
| Trainable export | `autoscript.trainable-export` | `1.0.0` | Analyzer |
| Analysis export | `autoscript.analysis-export` | `1.0.0` | Analyzer |

The corresponding JSON Schemas are in `schemas/data-contracts/`.

## Common envelope

Every canonical JSON artifact has these fields:

```json
{
  "schema_name": "autoscript.raw-run",
  "schema_version": "1.0.0",
  "created_at": "2026-08-04T11:30:00Z",
  "app_version": "1.0.3"
}
```

Rules:

1. `schema_name` identifies the contract family.
2. `schema_version` uses Semantic Versioning and describes the data structure, not the desktop release.
3. Timestamps use RFC 3339 UTC with a trailing `Z`.
4. Identifiers are lowercase UUID strings generated independently of filenames, names, participant codes, and database row numbers.
5. Readers must ignore unknown fields so compatible additions do not break older clients.
6. Writers must not silently change the meaning or type of an existing field.

## Identifier policy

### `experiment_id`

Stable identity of the logical experiment. Editing and publishing a new version preserves this value.

### `experiment_version_id`

Identity of one immutable experiment version. A new value is created for every published version. Runs reference this value rather than only the experiment name or version number.

### `version_number`

A human-readable positive integer scoped to one `experiment_id`. It starts at `1` and increases for every published version. It is not a database key.

### `run_id`

Identity of one execution of one experiment version for one participant code. Retries and resumed uploads preserve the same `run_id`; a genuinely new execution receives a new value.

### `prompt_id`, `group_id`, and `file_id`

Stable identities inside an experiment lineage. They permit labels, filenames, and ordering to change without losing referential integrity.

### `export_id`

Identity of one generated analysis or training artifact. Re-exporting creates a new value and records its source run IDs.

Participant codes are opaque strings, not identities for database relations. They must not contain names, email addresses, phone numbers, or other direct personal identifiers.

## Immutability rules

- An experiment version becomes immutable when it is published or referenced by a run.
- Correcting a published experiment creates a new `experiment_version_id` and increments `version_number`.
- Raw run artifacts are append-only after completion. Upload retries may replace an incomplete transfer only when the SHA-256 digest is identical.
- Analysis and trainable exports are derived artifacts. They can be regenerated, but each generated artifact receives a new `export_id` and records its source runs and analysis version.
- Git/application versions and schema versions are never used as substitutes for experiment version IDs.

## Compatibility rules

Schema versions follow `MAJOR.MINOR.PATCH`:

- **PATCH**: documentation or validation correction that does not alter accepted data.
- **MINOR**: backward-compatible optional fields or enum additions.
- **MAJOR**: required-field changes, removals, type changes, or semantic changes.

A reader supporting `1.x` must:

- accept unknown optional fields;
- reject a different schema family;
- reject unsupported major versions;
- preserve unrecognized fields when performing a read-modify-write operation where practical.

## Storage boundary

PostgreSQL stores relational metadata and searchable fields. Object storage stores ZIP, audio, raw JSON, CSV, screenshots, and trainable JSON.

Every stored object will have database metadata containing at least:

- artifact type;
- object-storage key;
- original filename;
- media type;
- byte size;
- SHA-256 digest;
- creator and creation timestamp;
- source experiment version or run.

The desktop applications never connect directly to PostgreSQL or object storage. They communicate only with the authenticated HTTPS API.

## Contract relationships

```text
experiment_id
  └── experiment_version_id
        ├── package manifest + media artifacts
        └── run_id
              ├── raw-run artifact
              ├── analysis export(s)
              └── trainable export(s)
```

## Legacy migration

Legacy files are adapted at the boundary rather than rewritten in place.

### Legacy experiment ZIP

- Generate `experiment_id` and `experiment_version_id` during first import.
- Set `version_number` to `1` unless explicit version metadata exists.
- Generate stable `file_id`, `group_id`, and `prompt_id` values.
- Compute SHA-256 for every media file.
- Preserve the original ZIP as an artifact.

### Legacy raw result

- Preserve an existing `session_id` as a display/reference field.
- Generate `run_id` when absent.
- Resolve `experiment_version_id` from the imported package; otherwise mark the run as requiring reconciliation.
- Convert flat participant fields into the canonical `participant` object.

### Legacy trainable JSON

- Wrap a single object or multiple-object array in the canonical export envelope.
- Add source run IDs when they can be resolved.
- Populate `original_word`, group, cell, correctness, and prompt identity during Analyzer migration.

## Implementation order

1. Keep these schemas and constants as the source of truth.
2. Add adapters that read legacy data into canonical Python objects.
3. Make Builder emit the canonical experiment package manifest while retaining legacy aliases during transition.
4. Make Runner produce canonical raw-run data and save locally before upload.
5. Make Analyzer consume canonical raw runs and emit deterministic export envelopes.
6. Validate API requests and stored artifacts against the same schema versions.
