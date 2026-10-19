# AutoScript non-ML release — 2026-10-19

## Scope

Interface 0.4, Builder 0.2, Runner 0.2, Analyzer 0.5.

Includes participant grouping with per-session actions; Updates access for
operators and researchers; background update progress and Interface restart;
Mouse/Tablet Test Run with explicit save/discard, labels and filtering;
Runner compatibility enforcement; revision-pinned downloads; and the server
guard requiring atomic publication for already published experiments.

ML dataset preparation, training, inference, review UI and model artifacts
are excluded. The existing Analyzer annotation/trainable export stays supported.

## Validation

Desktop unit suite: 182 passed, including grouping across pages, original run
selection, operator/researcher Updates access, Test Run and update lifecycle.
Backend unit suite: 94 passed, including the published-experiment guard.
Packaged Windows build results are recorded with the release workflow. The Interface build runs an actual EXE smoke check.

## Operational limits

Participant groups reflect loaded pages and current filters. Loading more adds
sessions to an existing group. Grouping never merges source data on the server.
Existing crash-time persistence and Analyzer ETag conflict behavior are outside
this change; neither is claimed fixed by this release.
