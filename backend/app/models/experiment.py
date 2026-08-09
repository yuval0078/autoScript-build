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
    current_revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "experiment_revisions.id",
            ondelete="SET NULL",
            name="fk_experiments_current_revision_id",
            use_alter=True,
        ),
        index=True,
    )

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
    revisions: Mapped[list["ExperimentRevision"]] = relationship(
        back_populates="experiment",
        foreign_keys="ExperimentRevision.experiment_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ExperimentRevision.revision_number",
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
        CheckConstraint(
            "expected_word_count IS NULL OR expected_word_count >= 0",
            name="ck_block_expected_words_nonnegative",
        ),
        CheckConstraint(
            "grid_rows IS NULL OR grid_rows > 0",
            name="ck_block_grid_rows_positive",
        ),
        CheckConstraint(
            "grid_cols IS NULL OR grid_cols > 0",
            name="ck_block_grid_cols_positive",
        ),
    )

    experiment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    same_page_as_previous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    schema_version: Mapped[Optional[str]] = mapped_column(String(32))
    app_version: Mapped[Optional[str]] = mapped_column(String(32))
    expected_word_count: Mapped[Optional[int]] = mapped_column(Integer)
    grid_rows: Mapped[Optional[int]] = mapped_column(Integer)
    grid_cols: Mapped[Optional[int]] = mapped_column(Integer)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    experiment: Mapped[Experiment] = relationship(back_populates="blocks")
    creator = relationship("User")
    run_results: Mapped[list["RunResult"]] = relationship(back_populates="block", passive_deletes=True)


class ExperimentRevision(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Immutable snapshot of an Experiment's ordered runnable Blocks."""

    __tablename__ = "experiment_revisions"
    __table_args__ = (
        UniqueConstraint("experiment_id", "revision_number", name="uq_experiment_revision_number"),
        CheckConstraint("revision_number > 0", name="ck_experiment_revision_positive"),
    )

    experiment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    experiment: Mapped[Experiment] = relationship(
        back_populates="revisions", foreign_keys=[experiment_id]
    )
    creator = relationship("User")
    blocks: Mapped[list["ExperimentRevisionBlock"]] = relationship(
        back_populates="revision", cascade="all, delete-orphan", passive_deletes=True,
        order_by="ExperimentRevisionBlock.position",
    )
    runs: Mapped[list["ExperimentRun"]] = relationship(back_populates="revision")


class ExperimentRevisionBlock(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "experiment_revision_blocks"
    __table_args__ = (
        UniqueConstraint("revision_id", "position", name="uq_revision_blocks_position"),
        CheckConstraint("position >= 0", name="ck_revision_block_position_nonnegative"),
        CheckConstraint("size_bytes >= 0", name="ck_revision_block_size_nonnegative"),
        CheckConstraint(
            "expected_word_count IS NULL OR expected_word_count >= 0",
            name="ck_revision_block_expected_words_nonnegative",
        ),
        CheckConstraint(
            "grid_rows IS NULL OR grid_rows > 0",
            name="ck_revision_block_grid_rows_positive",
        ),
        CheckConstraint(
            "grid_cols IS NULL OR grid_cols > 0",
            name="ck_revision_block_grid_cols_positive",
        ),
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("experiment_revisions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_block_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid(as_uuid=True))
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    same_page_as_previous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    schema_version: Mapped[Optional[str]] = mapped_column(String(32))
    app_version: Mapped[Optional[str]] = mapped_column(String(32))
    expected_word_count: Mapped[Optional[int]] = mapped_column(Integer)
    grid_rows: Mapped[Optional[int]] = mapped_column(Integer)
    grid_cols: Mapped[Optional[int]] = mapped_column(Integer)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revision: Mapped[ExperimentRevision] = relationship(back_populates="blocks")


class StagedBlockAsset(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Owner-scoped immutable Block upload awaiting an atomic publish."""

    __tablename__ = "staged_block_assets"
    __table_args__ = (
        UniqueConstraint("owner_id", "request_id", name="uq_staged_block_owner_request"),
        CheckConstraint("size_bytes >= 0", name="ck_staged_block_size_nonnegative"),
        CheckConstraint(
            "expected_word_count >= 0", name="ck_staged_block_expected_words_nonnegative"
        ),
        CheckConstraint("grid_rows > 0", name="ck_staged_block_grid_rows_positive"),
        CheckConstraint("grid_cols > 0", name="ck_staged_block_grid_cols_positive"),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    schema_version: Mapped[Optional[str]] = mapped_column(String(32))
    app_version: Mapped[Optional[str]] = mapped_column(String(32))
    expected_word_count: Mapped[int] = mapped_column(Integer, nullable=False)
    grid_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    grid_cols: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    owner = relationship("User")


class ExperimentPublishOperation(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Committed idempotency record for an atomic Experiment publish."""

    __tablename__ = "experiment_publish_operations"
    __table_args__ = (
        UniqueConstraint("owner_id", "request_id", name="uq_publish_operation_owner_request"),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("experiment_revisions.id", ondelete="CASCADE"), nullable=False
    )
    owner = relationship("User")


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
        CheckConstraint(
            "status IN ('created', 'running', 'completed', 'incomplete', 'failed', 'cancelled')",
            name="ck_experiment_run_status",
        ),
    )

    experiment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("experiments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("experiment_revisions.id", ondelete="RESTRICT"), index=True
    )
    session_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    participant_number: Mapped[int] = mapped_column(Integer, nullable=False)
    participant_age: Mapped[int] = mapped_column(Integer, nullable=False)
    participant_gender: Mapped[str] = mapped_column(String(32), nullable=False)
    block_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_experiment_name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_experiment_id: Mapped[Optional[str]] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="created", server_default="created"
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finalized_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    analysis_completed: Mapped[Optional[bool]] = mapped_column(Boolean)
    analysis_updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    current_analysis_revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "run_analysis_revisions.id",
            ondelete="SET NULL",
            name="fk_experiment_runs_current_analysis_revision_id",
            use_alter=True,
        ),
        index=True,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    experiment: Mapped[Experiment] = relationship(back_populates="runs")
    revision: Mapped[Optional[ExperimentRevision]] = relationship(back_populates="runs")
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
    analysis_revisions: Mapped[list["RunAnalysisRevision"]] = relationship(
        back_populates="run",
        foreign_keys="RunAnalysisRevision.run_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="RunAnalysisRevision.revision_number",
    )
    current_analysis_revision: Mapped[Optional["RunAnalysisRevision"]] = relationship(
        foreign_keys=[current_analysis_revision_id],
        post_update=True,
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
            "analysis_revision_id",
            "kind",
            name="uq_run_artifacts_analysis_revision_kind",
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
    analysis_revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("run_analysis_revisions.id", ondelete="CASCADE"),
        index=True,
    )
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
    analysis_revision: Mapped[Optional["RunAnalysisRevision"]] = relationship(
        back_populates="artifacts"
    )
    creator = relationship("User")


class RunAnalysisRevision(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One immutable Analyzer state revision, optionally with final exports."""

    __tablename__ = "run_analysis_revisions"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "revision_number",
            name="uq_run_analysis_revision_number",
        ),
        CheckConstraint(
            "revision_number > 0",
            name="ck_run_analysis_revision_positive",
        ),
        CheckConstraint(
            "(finalized = false AND completed IS NULL AND finalized_at IS NULL) OR "
            "(finalized = true AND completed IS NOT NULL AND finalized_at IS NOT NULL)",
            name="ck_run_analysis_finalization_fields",
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("experiment_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    finalized: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    completed: Mapped[Optional[bool]] = mapped_column(Boolean)
    finalized_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    run: Mapped[ExperimentRun] = relationship(
        back_populates="analysis_revisions",
        foreign_keys=[run_id],
    )
    artifacts: Mapped[list[RunArtifact]] = relationship(
        back_populates="analysis_revision",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    creator = relationship("User")


class RunAnalysisOperation(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Permanent idempotency record for a state save or finalization request."""

    __tablename__ = "run_analysis_operations"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "request_id",
            name="uq_run_analysis_operation_request",
        ),
        CheckConstraint(
            "operation_kind IN ('state', 'finalize')",
            name="ck_run_analysis_operation_kind",
        ),
        CheckConstraint(
            "revision_number > 0",
            name="ck_run_analysis_operation_revision_positive",
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("experiment_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("run_analysis_revisions.id", ondelete="SET NULL"),
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)

    run = relationship("ExperimentRun", foreign_keys=[run_id])
    revision: Mapped[Optional[RunAnalysisRevision]] = relationship()


class ObjectDeletionTask(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Durable best-effort cleanup task for an unreferenced object."""

    __tablename__ = "object_deletion_tasks"
    __table_args__ = (
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_object_deletion_attempt_nonnegative",
        ),
    )

    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text)


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
