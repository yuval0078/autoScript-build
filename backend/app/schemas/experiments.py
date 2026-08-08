import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator


class ExperimentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Experiment name cannot be blank.")
        return value


class ExperimentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value):
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("Experiment name cannot be blank.")
        return value

    @model_validator(mode="after")
    def require_change(self):
        if not self.model_fields_set:
            raise ValueError("At least one field must be supplied.")
        return self


class ExperimentBlockResponse(BaseModel):
    id: uuid.UUID
    experiment_id: uuid.UUID
    position: int
    same_page_as_previous: bool
    name: str
    schema_version: str | None
    app_version: str | None
    original_filename: str
    sha256: str
    size_bytes: int
    created_at: datetime
    download_url: str


class BlockReorder(BaseModel):
    block_ids: list[uuid.UUID] = Field(min_length=1)
    same_page_block_ids: list[uuid.UUID] = Field(default_factory=list)

    @field_validator("block_ids")
    @classmethod
    def block_ids_are_unique(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Block IDs must be unique.")
        return value

    @field_validator("same_page_block_ids")
    @classmethod
    def same_page_block_ids_are_unique(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("same_page_block_ids must be unique.")
        return value

    @model_validator(mode="after")
    def same_page_blocks_belong_to_order(self):
        ordered = set(self.block_ids)
        joined = set(self.same_page_block_ids)
        if not joined.issubset(ordered):
            raise ValueError("same_page_block_ids must be a subset of block_ids.")
        if self.block_ids and self.block_ids[0] in joined:
            raise ValueError("The first Block cannot share a page with a previous Block.")
        return self


class ExperimentVersionResponse(BaseModel):
    id: uuid.UUID
    experiment_id: uuid.UUID
    version_number: int
    schema_version: str | None
    app_version: str | None
    original_filename: str
    sha256: str
    size_bytes: int
    created_at: datetime
    download_url: str


class ExperimentRevisionBlockResponse(BaseModel):
    id: uuid.UUID
    source_block_id: uuid.UUID | None
    position: int
    same_page_as_previous: bool
    name: str
    sha256: str
    size_bytes: int


class ExperimentRevisionResponse(BaseModel):
    id: uuid.UUID
    experiment_id: uuid.UUID
    revision_number: int
    name: str
    created_at: datetime
    blocks: list[ExperimentRevisionBlockResponse]
    download_url: str


class ExperimentResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    owner_id: uuid.UUID
    created_at: datetime
    blocks: list[ExperimentBlockResponse]
    versions: list[ExperimentVersionResponse]
    current_revision: ExperimentRevisionResponse | None
    download_url: str
