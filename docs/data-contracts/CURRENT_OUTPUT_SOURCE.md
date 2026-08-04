# Source locations used for the current contracts

The Stage 1 contracts are derived directly from the current serializers in the desktop scripts:

- Builder package JSON: `exp_initializer.py`, method `export_package`, assignment to `config`.
- Runner result JSON: `tablet_experiment.py`, assignment to `self.completed_data` before save.
- Analyzer trainable participant object: `analyzer_refactored.py`, assignment to `p_output`.
- Analyzer trainable word object: `analyzer_refactored.py`, assignment to `word_entry`.
- Analyzer CSV static columns: `analyzer_refactored.py`, assignment to `header`.

`tests/test_data_contract_schemas.py` parses these source assignments with Python's AST and fails when the documented required fields drift from what the scripts emit.
