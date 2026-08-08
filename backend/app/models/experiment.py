import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class Experiment(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "experiments"
    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_experiments_owner_name"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    owner = relationship("User")
    versions: Mapped[list["ExperimentVersion"]] = relationship(
        back_populates="experiment",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    blocks: Mapped[list["ExperimentBlock"]] = relationship(
        back_populates="experiment",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ExperimentBlock.position",
    )
    runs: Mapped[list["ExperimentRun"]] = relationship(
        back_populates="experiment",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ExperimentRun.created_at.desc()",
    )


class ExperimentBlock(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """An ordered, independently downloadable runnable unit in an experiment."""

    __tablename__ = "experiment_blocks"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "position",
            name="uq_experiment_blocks_position",
        ),
        CheckConstraint("position >= 0", name="ck_block_position_nonnegative"),
        CheckConstraint("size_bytes >= 0", name="ck_block_size_nonnegative"),
    )

    experiment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("experiments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    same_page_as_previous: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    schema_version: Mapped[Optional[str]] = mapped_column(String(32))
    app_version: Mapped[Optional[str]] = mapped_column(String(32))
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    experiment: Mapped[Experiment] = relationship(back_populates="blocks")
    creator = relationship("User")
    run_results: Mapped[list["RunResult"]] = relationship(
        back_populates="block",
        passive_deletes=True,
    )


class ExperimentRun(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One participant session for an Experiment, containing Block results."""

    __tablename__ = "experiment_runs"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "session_id",
            name="uq_experiment_runs_session",
        ),
        CheckConstraint(
            "participant_number > 0",
            name="ck_run_participant_number_positive",
        ),
        CheckConstraint("participant_age > 0", name="ck_run_participant_age_positive"),
        CheckConstraint("block_count > 0", name="ck_run_block_count_positive"),
    )

    experiment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("experiments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    participant_number: Mapped[int] = mapped_column(Integer, nullable=False)
    participant_age: Mapped[int] = mapped_column(Integer, nullable=False)
    participant_gender: Mapped[str] = mapped_column(String(32), nullable=False)
    block_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_experiment_name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_experiment_id: Mapped[Optional[str]] = mapped_column(String(255))
    analysis_completed: Mapped[Optional[bool]] = mapped_column(Boolean)
    analysis_updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    experiment: Mapped[Experiment] = relationship(back_populates="runs")
    creator = relationship("User")
    results: Mapped[list["RunResult"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="RunResult.block_index",
    )
    artifacts: Mapped[list["RunArtifact"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="RunArtifact.created_at.desc()",
    )


class RunResult(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Immutable exact JSON artifact produced by one completed Block."""

    __tablename__ = "run_results"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "block_index",
            name="uq_run_results_block_index",
        ),
        CheckConstraint("block_index > 0", name="ck_result_block_index_positive"),
        CheckConstraint("block_count > 0", name="ck_result_block_count_positive"),
        CheckConstraint("size_bytes >= 0", name="ck_result_size_nonnegative"),
        CheckConstraint(
            "completed_word_count >= 0",
            name="ck_result_completed_words_nonnegative",
        ),
        CheckConstraint(
            "expected_word_count >= 0",
            name="ck_result_expected_words_nonnegative",
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("experiment_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    block_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("experiment_blocks.id", ondelete="SET NULL"),
        index=True,
    )
    block_index: Mapped[int] = mapped_column(Integer, nullable=False)
    block_count: Mapped[int] = mapped_column(Integer, nullable=False)
    block_name: Mapped[str] = mapped_column(String(200), nullable=False)
    block_completed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    experiment_completed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    completed_word_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    expected_word_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    app_version: Mapped[str] = mapped_column(String(32), nullable=False)
    result_timestamp: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    run: Mapped[ExperimentRun] = relationship(back_populates="results")
    block: Mapped[Optional[ExperimentBlock]] = relationship(back_populates="run_results")
    creator = relationship("User")


class RunArtifact(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """An immutable Analyzer export associated with one participant run."""

    __tablename__ = "run_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "kind",
            "sha256",
            name="uq_run_artifacts_exact_content",
        ),
        CheckConstraint(
            "kind IN ('analysis_csv', 'trainable_json', 'analysis_state')",
            name="ck_run_artifact_kind",
        ),
        CheckConstraint(
            "size_bytes >= 0",
            name="ck_run_artifact_size_nonnegative",
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("experiment_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    run: Mapped[ExperimentRun] = relationship(back_populates="artifacts")
    creator = relationship("User")


class ExperimentVersion(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "experiment_versions"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "version_number",
            name="uq_experiment_versions_number",
        ),
        CheckConstraint("version_number > 0", name="ck_version_number_positive"),
        CheckConstraint("size_bytes >= 0", name="ck_version_size_nonnegative"),
    )

    experiment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("experiments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[Optional[str]] = mapped_column(String(32))
    app_version: Mapped[Optional[str]] = mapped_column(String(32))
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    experiment: Mapped[Experiment] = relationship(back_populates="versions")
    creator = relationship("User")
