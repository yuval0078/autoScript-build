# AutoScript current data contracts

This directory documents the files that the desktop scripts produce **right now**, including the Experiment/Block identity added for the API-backed workflow. Legacy fields and file shapes remain supported where noted.

The schemas are observational contracts for the current files. Proposed database identifiers and future normalized formats are documented separately and must not be confused with the current JSON structure.

## 1. Builder experiment package

`exp_initializer.py` creates a ZIP with this layout:

```text
<experiment>.zip
├── <experiment>.json
└── media/
    ├── <audio file>
    └── ...
```

The JSON currently contains:

```text
app_version
schema_version
package_type
name
block_name
grid
order
sequence
repetitions
active_block_sequence
proceed_condition
beeps
files
groups
```

Important current behavior:

- `sequence` contains the existing word `id` strings.
- `active_block_sequence` is serialized from Python tuples, so every item is a two-element JSON array: `[group_name, repetition_index]`.
- `files[].owner_group` contains a group **name**, not a group ID.
- `groups[].words[].source_file` contains the copied media filename, not a database ID.
- New packages identify themselves with `schema_version: "2.0"` and
  `package_type: "block"`. Stable cloud Experiment and Block IDs live in the
  bundle manifest/API metadata rather than being required in a standalone
  Block ZIP, preserving compatibility with older ZIPs.

Schema: `schemas/data-contracts/experiment-package.schema.json`.

## 2. Runner raw result

`tablet_experiment.py` saves one object per completed Block. All Blocks in the
same Experiment run share a `session_id`; multi-Block runs therefore produce
one JSON file per Block with these top-level fields:

```text
schema_version
app_version
experiment_name
experiment_id
experiment_version
block_name
block_id
block_index
block_count
block_completed
experiment_completed
completed_word_count
expected_word_count
session_experiment_index
session_experiment_count
participant_number
participant_age
participant_gender
session_id
timestamp
calibration
config
words
```

Current details:

- Newly emitted files use `schema_version: "1.2"`. Version 1.0 files without
  Block identity remain schema-valid and loadable by the Analyzer.
- `experiment_id` is the cloud Experiment UUID for cloud runs and falls back
  to the local/legacy experiment identity when no cloud record exists.
- In the manifest compatibility path, `experiment_id` and `experiment_version` can be `null` when the manifest omits them.
- `experiment_version` otherwise falls back to `1`.
- `timestamp` uses local time in `YYYYMMDD_HHMMSS` form, not ISO 8601.
- `session_id` is `<participant>_<timestamp>_<six hex characters>`.
- `block_completed` compares `completed_word_count` with
  `expected_word_count`. `experiment_completed` is true only when every
  expected word in every Block was saved for the run.
- `calibration` is `{"corners": [[x, y], ...]}` with four corner pairs.
- `config` contains the loaded configuration object. In the legacy loading path it may include runtime-only keys such as `__file_path__`.
- Every word contains `word`, `cell`, `group`, timing fields, and `pen_events`.
- Every pen event contains `type`, `x`, `y`, `pressure`, `timestamp`, `absolute_time`, and `speed`.

Schema: `schemas/data-contracts/raw-run.schema.json`.

## 3. Analyzer trainable JSON

`analyzer_refactored.py` currently changes the root type according to participant count:

- one participant: one participant object;
- multiple participants: an array of participant objects.

A participant object contains source identity plus participant metadata:

```text
participant_number
experiment_name
experiment_id
block_name
block_id
block_index
block_count
session_id
participant_age
participant_gender
timestamp
calibration
group
words
```

Each exported word contains only:

```text
written_word
trainability
audio_start_time
audio_end_time
strokes
letters
```

Each stroke contains `stroke_id` and downsampled `events`. Each letter contains `char` and `stroke_ids`.

Important current limitation: the word object does **not** retain the original target word, source group, cell, or an explicit correctness value. Those fields must not be added to the current-format schema until the Analyzer is changed to emit them.

Schema: `schemas/data-contracts/trainable-export.schema.json`.

## 4. Analyzer research CSV

The static columns are:

```text
Exp Step
Participant
Age
Gender
Word
Group
Cell
Correct
Written Word
Reading End
Writing Start
Writing End
Strokes
Avg Interval
```

The Analyzer then appends three columns for every letter position up to the longest target word:

```text
Written Letter N
Letter N Start
Letter N End
```

The final column is:

```text
Screenshot File
```

The number of CSV columns is therefore data-dependent. See `docs/data-contracts/analysis-csv.md`.

## Server metadata and immutable storage

The backend stores existing files unchanged in object storage and keeps
relational metadata in PostgreSQL:

- server experiment record ID;
- server experiment-version record ID;
- server run record ID;
- artifact ID;
- object-storage key;
- SHA-256 digest;
- file size and media type;
- uploader and upload timestamp.

These are **database fields**, not claims about the current JSON output. A later migration can deliberately add stable IDs and normalized envelopes to the desktop file formats.

## Current identity available inside the files

| File | Current usable identity | Limitation |
| --- | --- | --- |
| Experiment ZIP | `name`; word `id` values | No stable experiment-version identity |
| Raw result | `session_id`; `experiment_id`; `experiment_version` | `experiment_id` commonly equals the name |
| Trainable JSON | experiment, Block, session, participant, and timestamp | No server run UUID inside the file |
| CSV | experiment, Block, session, participant, word, and cell | No server run UUID inside the file |

## Server-side versioning rules for the current files

These rules operate around the current files; they do not alter their internal structure.

1. **Exact ZIP bytes define an experiment-version artifact.** On upload, the server calculates SHA-256 and stores the ZIP unchanged.
2. **Logical experiment identity is server metadata.** A user selects whether an uploaded ZIP starts a new experiment or becomes a new version of an existing experiment; the server must not infer this solely from `name`.
3. **Published versions are immutable.** Editing an experiment means uploading a new ZIP and creating a new server-side version record. An existing version record is never repointed to different bytes.
4. **A run is linked to the selected server version before or during upload.** The raw file's `experiment_id` and `experiment_version` remain preserved as source metadata, but the database relationship is authoritative.
5. **Raw results are immutable artifacts.** Repeated upload for the same
   `session_id` and Block index is accepted only when the checksum is identical;
   different bytes are treated as a conflict requiring review.
6. **Analyzer outputs are derived artifacts.** A new CSV or trainable JSON export creates a new artifact record linked to its source raw result; it does not overwrite the source data.

## Current API boundary

Experiment definitions, ordered Blocks, page-sharing layout, Experiment runs,
and immutable raw Block results now flow through the local API. The Runner also
keeps the user-selected local JSON copies. Analyzer CSV/trainable exports are
persisted as immutable per-run artifacts. Each run stores a tri-state analysis
status: not started (`null`), explicitly not completed (`false`), or completed
(`true`).
