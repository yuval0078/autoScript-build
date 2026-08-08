import uuid
from datetime import datetime

from pydantic import BaseModel


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
