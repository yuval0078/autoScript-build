import unittest

from app.main import create_app


class OpenApiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = create_app().openapi()

    def test_operation_ids_are_unique(self):
        self.assertEqual(self.document["info"]["version"], "0.8.0")
        bearer = self.document["components"]["securitySchemes"]["HTTPBearer"]
        self.assertEqual(bearer["type"], "http")
        self.assertEqual(bearer["scheme"], "bearer")
        operation_ids = []
        for path_item in self.document["paths"].values():
            for method, operation in path_item.items():
                if method.lower() not in {
                    "get", "put", "post", "delete", "patch", "options", "head"
                }:
                    continue
                operation_id = operation.get("operationId")
                if operation_id:
                    operation_ids.append(operation_id)
        self.assertEqual(len(operation_ids), len(set(operation_ids)))

    def test_protected_operations_use_bearer_security(self):
        operation = self.document["paths"][
            "/api/v1/runs/{run_id}/analysis-copies"
        ]["get"]
        self.assertIn({"HTTPBearer": []}, operation["security"])
        self.assertTrue(
            all(
                parameter["name"].lower() != "authorization"
                for parameter in operation.get("parameters", [])
            )
        )

    def test_analysis_copy_management_is_described(self):
        paths = self.document["paths"]
        listing = paths["/api/v1/runs/{run_id}/analysis-copies"]["get"]
        restoring = paths[
            "/api/v1/runs/{run_id}/analysis-copies/{revision_id}/set-editable"
        ]["post"]
        deleting = paths[
            "/api/v1/runs/{run_id}/analysis-copies/{revision_id}"
        ]["delete"]

        self.assertEqual(listing["summary"], "List saved analyzed copies")
        self.assertIn("trainable JSON", restoring["description"])
        self.assertIn("Raw Runner data is never deleted", deleting["description"])
        for operation in (listing, restoring, deleting):
            self.assertIn("401", operation["responses"])
            self.assertIn("404", operation["responses"])
        copy_schema = self.document["components"]["schemas"][
            "RunAnalysisCopyResponse"
        ]
        for field in (
            "id",
            "run_id",
            "revision",
            "created_at",
            "completed",
            "is_current_editable",
            "analyzed_csv",
            "trainable_json",
        ):
            self.assertTrue(copy_schema["properties"][field].get("description"))

    def test_finalize_documents_duplicate_policy_header(self):
        operation = self.document["paths"][
            "/api/v1/runs/{run_id}/analysis/finalize"
        ]["post"]
        parameter = next(
            item
            for item in operation["parameters"]
            if item["name"] == "X-Existing-Analysis-Policy"
        )
        self.assertIn("replace", parameter["description"])

    def test_list_pagination_filters_and_headers_are_documented(self):
        experiments = self.document["paths"]["/api/v1/experiments"]["get"]
        experiment_parameters = {
            parameter["name"]: parameter for parameter in experiments["parameters"]
        }
        self.assertTrue(
            {"search", "include_versions", "limit", "cursor"}.issubset(
                experiment_parameters
            )
        )
        self.assertIn("Requires limit", experiment_parameters["cursor"]["description"])
        self.assertIn(
            "X-Next-Cursor", experiments["responses"]["200"]["headers"]
        )
        self.assertIn(
            "X-Total-Count", experiments["responses"]["200"]["headers"]
        )

        runs = self.document["paths"][
            "/api/v1/experiments/{experiment_id}/runs"
        ]["get"]
        run_parameters = {
            parameter["name"]: parameter for parameter in runs["parameters"]
        }
        run_parameter_names = set(run_parameters)
        self.assertTrue(
            {
                "participant_number",
                "session_search",
                "status",
                "complete",
                "has_raw_data",
                "has_analyzed_csv",
                "has_trainable_json",
                "include_files",
                "limit",
                "cursor",
            }.issubset(run_parameter_names)
        )
        self.assertIn("X-Next-Cursor", runs["responses"]["200"]["headers"])
        self.assertIn("Requires limit", run_parameters["cursor"]["description"])

    def test_bulk_export_documents_request_and_binary_response(self):
        operation = self.document["paths"][
            "/api/v1/experiments/{experiment_id}/bulk-export"
        ]["post"]
        self.assertEqual(operation["summary"], "Download selected Run data as one ZIP")
        request_schema = operation["requestBody"]["content"]["application/json"][
            "schema"
        ]
        self.assertEqual(
            request_schema["$ref"], "#/components/schemas/BulkExportRequest"
        )
        success = operation["responses"]["200"]
        self.assertIn("application/zip", success["content"])
        self.assertIn("Content-Disposition", success["headers"])
        self.assertIn("X-Checksum-SHA256", success["headers"])
        self.assertEqual(
            success["content"]["application/zip"]["schema"],
            {"type": "string", "format": "binary"},
        )
        for status_code in ("401", "404", "413", "422"):
            self.assertIn(status_code, operation["responses"])


if __name__ == "__main__":
    unittest.main()
