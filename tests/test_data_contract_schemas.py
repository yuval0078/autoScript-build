import ast
import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = REPO_ROOT / "schemas" / "data-contracts"


def parse_source(filename):
    return ast.parse((REPO_ROOT / filename).read_text(encoding="utf-8"))


def string_dict_keys(node):
    if not isinstance(node, ast.Dict):
        return None
    keys = []
    for key in node.keys:
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            return None
        keys.append(key.value)
    return keys


def find_name_dict_keys(tree, variable_name, function_name=None):
    scopes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and (function_name is None or node.name == function_name)
    ]
    if function_name is None:
        scopes.append(tree)

    matches = []
    for scope in scopes:
        for node in ast.walk(scope):
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(target, ast.Name) and target.id == variable_name
                for target in node.targets
            ):
                continue
            keys = string_dict_keys(node.value)
            if keys is not None:
                matches.append(keys)

    if not matches:
        raise AssertionError(f"Could not find dict assignment for {variable_name}")
    return max(matches, key=len)


def find_attribute_dict_keys(tree, attribute_name):
    matches = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Attribute) and target.attr == attribute_name
            for target in node.targets
        ):
            continue
        keys = string_dict_keys(node.value)
        if keys is not None:
            matches.append(keys)

    if not matches:
        raise AssertionError(f"Could not find dict assignment for .{attribute_name}")
    return max(matches, key=len)


def find_csv_header(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.List):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "header"
            for target in node.targets
        ):
            continue
        values = []
        for item in node.value.elts:
            if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
                break
            values.append(item.value)
        if values and values[0] == "Exp Step":
            return values
    raise AssertionError("Could not find Analyzer CSV header")


class CurrentDataContractTests(unittest.TestCase):
    def test_schema_files_are_valid_json(self):
        filenames = [
            "common.schema.json",
            "experiment-package.schema.json",
            "raw-run.schema.json",
            "trainable-export.schema.json",
        ]
        for filename in filenames:
            with self.subTest(filename=filename):
                with (SCHEMA_ROOT / filename).open(encoding="utf-8") as schema_file:
                    json.load(schema_file)

    def test_builder_schema_required_keys_match_emitted_config(self):
        tree = parse_source("exp_initializer.py")
        emitted_keys = find_name_dict_keys(tree, "config", function_name="export_package")
        schema = json.loads(
            (SCHEMA_ROOT / "experiment-package.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(set(schema["required"]), set(emitted_keys))

    def test_runner_schema_required_keys_match_completed_data(self):
        tree = parse_source("tablet_experiment.py")
        emitted_keys = find_attribute_dict_keys(tree, "completed_data")
        schema = json.loads(
            (SCHEMA_ROOT / "raw-run.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(set(schema["required"]), set(emitted_keys))

    def test_trainable_schema_matches_analyzer_participant_and_word_objects(self):
        tree = parse_source("analyzer_refactored.py")
        participant_keys = find_name_dict_keys(tree, "p_output")
        word_keys = find_name_dict_keys(tree, "word_entry")
        schema = json.loads(
            (SCHEMA_ROOT / "trainable-export.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            set(schema["$defs"]["participant"]["required"]),
            set(participant_keys),
        )
        self.assertEqual(
            set(schema["$defs"]["trainableWord"]["required"]),
            set(word_keys),
        )

    def test_analysis_csv_static_header_matches_source(self):
        tree = parse_source("analyzer_refactored.py")
        self.assertEqual(
            find_csv_header(tree),
            [
                "Exp Step",
                "Participant",
                "Age",
                "Gender",
                "Word",
                "Group",
                "Cell",
                "Correct",
                "Written Word",
                "Reading End",
                "Writing Start",
                "Writing End",
                "Strokes",
                "Avg Interval",
            ],
        )


if __name__ == "__main__":
    unittest.main()
