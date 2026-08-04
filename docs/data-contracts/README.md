# AutoScript current data contracts

This directory documents the files that the desktop scripts produce **right now**. Stage 1 does not change Builder, Runner, or Analyzer output and does not require fields that the scripts do not currently write.

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
name
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
- The package currently has no `schema_version`, stable experiment ID, experiment-version ID, checksums, or creation timestamp.

Schema: `schemas/data-contracts/experiment-package.schema.json`.

## 2. Runner raw result

`tablet_experiment.py` currently saves one object per completed experiment with these top-level fields:

```text
schema_version
app_version
experiment_name
experiment_id
experiment_version
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

- `schema_version` is the string `"1.0"`.
- `experiment_id` falls back to the experiment name when the loaded configuration does not provide one.
- In the manifest compatibility path, `experiment_id` and `experiment_version` can be `null` when the manifest omits them.
- `experiment_version` otherwise falls back to `1`.
- `timestamp` uses local time in `YYYYMMDD_HHMMSS` form, not ISO 8601.
- `session_id` is `<participant>_<timestamp>_<six hex characters>`.
- `calibration` is `{"corners": [[x, y], ...]}` with four corner pairs.
- `config` contains the loaded configuration object. In the legacy loading path it may include runtime-only keys such as `__file_path__`.
- Every word contains `word`, `cell`, `group`, timing fields, and `pen_events`.
- Every pen event contains `type`, `x`, `y`, `pressure`, `timestamp`, `absolute_time`, and `speed`.

Schema: `schemas/data-contracts/raw-run.schema.json`.

## 3. Analyzer trainable JSON

`analyzer_refactored.py` currently changes the root type according to participant count:

- one participant: one participant object;
- multiple participants: an array of participant objects.

A participant object contains:

```text
participant_number
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

## What the server may add without rewriting these files

The first backend can store the existing files unchanged in object storage and keep new relational metadata in PostgreSQL:

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
| Trainable JSON | participant number and timestamp | No source run ID and no original-word identity |
| CSV | participant, experiment step, word, cell | No immutable run/export ID |

## Server-side versioning rules for the current files

These rules operate around the current files; they do not alter their internal structure.

1. **Exact ZIP bytes define an experiment-version artifact.** On upload, the server calculates SHA-256 and stores the ZIP unchanged.
2. **Logical experiment identity is server metadata.** A user selects whether an uploaded ZIP starts a new experiment or becomes a new version of an existing experiment; the server must not infer this solely from `name`.
3. **Published versions are immutable.** Editing an experiment means uploading a new ZIP and creating a new server-side version record. An existing version record is never repointed to different bytes.
4. **A run is linked to the selected server version before or during upload.** The raw file's `experiment_id` and `experiment_version` remain preserved as source metadata, but the database relationship is authoritative.
5. **Raw results are immutable artifacts.** Repeated upload of the same `session_id` is accepted only when the checksum is identical; a different checksum is treated as a conflict requiring review.
6. **Analyzer outputs are derived artifacts.** A new CSV or trainable JSON export creates a new artifact record linked to its source raw result; it does not overwrite the source data.

## Stage 1 boundary

Stage 1 is complete when:

1. the four actual outputs are documented;
2. the three JSON outputs have schemas matching current emitted structure;
3. current omissions and unstable fields are explicit;
4. future database IDs are kept separate from the current file contracts;
5. immutability and versioning are defined around the current artifacts.

No desktop serialization code is changed in this stage.
