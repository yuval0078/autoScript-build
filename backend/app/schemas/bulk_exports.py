import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


BulkExportKind = Literal["raw_data", "analysis_csv", "trainable_json"]
BulkAnalysisPolicy = Literal["latest", "all"]


class BulkExportRequest(BaseModel):
    """Select immutable Run data to package into one server-generated ZIP."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "run_ids": [
                        "9be576b8-9381-4c0a-8d65-b7d11c0848b8",
                        "a2bb1f76-34a5-4cbb-b1f5-b6ea078c9bee",
                    ],
                    "include": [
                        "raw_data",
                        "analysis_csv",
                        "trainable_json",
                    ],
                    "analysis_policy": "latest",
                }
            ]
        }
    )

    run_ids: list[uuid.UUID] = Field(
        min_length=1,
        max_length=500,
        examples=[["9be576b8-9381-4c0a-8d65-b7d11c0848b8"]],
        description=(
            "Unique participant Run IDs. Every Run must belong to the Experiment "
            "identified in the URL and to the authenticated owner."
        ),
    )
    include: list[BulkExportKind] = Field(
        min_length=1,
        max_length=3,
        examples=[["raw_data", "analysis_csv", "trainable_json"]],
        description=(
            "Artifact categories to include. raw_data includes every immutable Block "
            "result for each selected Run."
        ),
    )
    analysis_policy: BulkAnalysisPolicy = Field(
        default="latest",
        examples=["latest"],
        description=(
            "For analyzed CSV and trainable JSON, include the newest saved copy "
            "per Run or every saved copy. Versioned finalized revisions and legacy "
            "unversioned artifacts participate in the same selection."
        ),
    )

    @field_validator("run_ids")
    @classmethod
    def run_ids_must_be_unique(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("run_ids must not contain duplicates.")
        return values

    @field_validator("include")
    @classmethod
    def include_must_be_unique(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("include must not contain duplicates.")
        return values
