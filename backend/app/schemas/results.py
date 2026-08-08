import uuid
from datetime import datetime

from pydantic import BaseModel
from pydantic import Field


class RunCreate(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    participant_number: int = Field(gt=0)
    participant_age: int = Field(gt=0)
    participant_gender: str = Field(min_length=1, max_length=32)


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
    block_count: int
    source_experiment_name: str
    source_experiment_id: str | None
    analysis_completed: bool | None
    analysis_updated_at: datetime | None
    created_at: datetime
    result_count: int
    completed_word_count: int
    expected_word_count: int
    complete: bool
    results: list[RunResultResponse]
    artifacts: list[RunArtifactResponse]
