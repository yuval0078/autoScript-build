import uuid
from datetime import datetime

from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator


class RunCreate(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    participant_number: int = Field(gt=0)
    participant_age: int = Field(gt=0)
    participant_gender: str = Field(min_length=1, max_length=32)
    is_test: bool = False


class RunArtifactResponse(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    kind: str
    original_filename: str
    sha256: str
    size_bytes: int
    created_at: datetime
    download_url: str


class RunAnalysisUpdate(BaseModel):
    completed: bool


class RunAnalysisRevisionResponse(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    revision: int
    etag: str
    source_fingerprint: str
    finalized: bool
    completed: bool | None
    created_at: datetime
    finalized_at: datetime | None
    artifacts: list[RunArtifactResponse]


class RunAnalysisCopyResponse(BaseModel):
    id: uuid.UUID = Field(
        description="Immutable analysis-revision identifier used by copy-management endpoints."
    )
    run_id: uuid.UUID = Field(description="Participant Run that owns this copy.")
    revision: int = Field(
        gt=0,
        description="Monotonically increasing analysis revision number within the Run.",
    )
    created_at: datetime = Field(
        description="UTC timestamp at which this analyzed copy was created."
    )
    completed: bool = Field(
        description="Whether the Analyzer marked this exported revision complete."
    )
    is_current_editable: bool = Field(
        description=(
            "True when this copy's matching analysis-state snapshot is the state "
            "that the Analyzer will restore next."
        )
    )
    analyzed_csv: RunArtifactResponse = Field(
        description="Immutable analyzed CSV artifact belonging to this revision."
    )
    trainable_json: RunArtifactResponse = Field(
        description="Immutable trainable JSON artifact belonging to this revision."
    )


class RunResultResponse(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    block_id: uuid.UUID | None
    block_index: int
    block_count: int
    block_name: str
    block_completed: bool
    experiment_completed: bool
    completed_word_count: int
    expected_word_count: int
    schema_version: str
    app_version: str
    result_timestamp: str
    original_filename: str
    sha256: str
    size_bytes: int
    created_at: datetime
    download_url: str


class RunResultResolveRequest(BaseModel):
    sha256: list[str] = Field(min_length=1, max_length=1000)

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, values):
        normalized = []
        for value in values:
            value = str(value).lower()
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError("Every sha256 value must be a 64-character hexadecimal digest.")
            if value not in normalized:
                normalized.append(value)
        return normalized


class RunResultResolveResponse(BaseModel):
    results: list[RunResultResponse]
    missing_sha256: list[str]


class ExperimentRunResponse(BaseModel):
    id: uuid.UUID
    experiment_id: uuid.UUID
    revision_id: uuid.UUID | None
    status: str
    started_at: datetime | None
    finalized_at: datetime | None
    session_id: str
    participant_number: int
    participant_age: int
    participant_gender: str
    is_test: bool
    block_count: int
    source_experiment_name: str
    source_experiment_id: str | None
    analysis_completed: bool | None
    analysis_updated_at: datetime | None
    created_at: datetime
    result_count: int
    raw_data_count: int
    analyzed_csv_count: int
    trainable_json_count: int
    screenshots_count: int
    completed_word_count: int
    expected_word_count: int
    complete: bool
    results: list[RunResultResponse]
    artifacts: list[RunArtifactResponse]
