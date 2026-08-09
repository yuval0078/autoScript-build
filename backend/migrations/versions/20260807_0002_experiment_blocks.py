"""Add ordered experiment blocks and backfill the latest legacy package."""

import uuid

from alembic import op
import sqlalchemy as sa


revision = "20260807_0002"
down_revision = "20260807_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "experiment_blocks",
        sa.Column("experiment_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=True),
        sa.Column("app_version", sa.String(length=32), nullable=True),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("position >= 0", name="ck_block_position_nonnegative"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_block_size_nonnegative"),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key"),
        sa.UniqueConstraint(
            "experiment_id",
            "position",
            name="uq_experiment_blocks_position",
        ),
    )
    op.create_index(
        "ix_experiment_blocks_experiment_id",
        "experiment_blocks",
        ["experiment_id"],
    )
    op.create_index(
        "ix_experiment_blocks_sha256",
        "experiment_blocks",
        ["sha256"],
    )

    # Keep the immutable version history, and expose only its newest package as
    # the initial block. The old API required package.name == experiment.name,
    # so the experiment name is the correct legacy block name without opening
    # object-storage files during a database migration.
    connection = op.get_bind()
    experiments = sa.table(
        "experiments",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
    )
    versions = sa.table(
        "experiment_versions",
        sa.column("id", sa.Uuid()),
        sa.column("experiment_id", sa.Uuid()),
        sa.column("version_number", sa.Integer()),
        sa.column("schema_version", sa.String()),
        sa.column("app_version", sa.String()),
        sa.column("storage_key", sa.String()),
        sa.column("original_filename", sa.String()),
        sa.column("sha256", sa.String()),
        sa.column("size_bytes", sa.BigInteger()),
        sa.column("created_by", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    blocks = sa.table(
        "experiment_blocks",
        sa.column("id", sa.Uuid()),
        sa.column("experiment_id", sa.Uuid()),
        sa.column("position", sa.Integer()),
        sa.column("name", sa.String()),
        sa.column("schema_version", sa.String()),
        sa.column("app_version", sa.String()),
        sa.column("storage_key", sa.String()),
        sa.column("original_filename", sa.String()),
        sa.column("sha256", sa.String()),
        sa.column("size_bytes", sa.BigInteger()),
        sa.column("created_by", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    rows = connection.execute(
        sa.select(
            experiments.c.id.label("experiment_id"),
            experiments.c.name.label("experiment_name"),
            versions.c.version_number,
            versions.c.schema_version,
            versions.c.app_version,
            versions.c.storage_key,
            versions.c.original_filename,
            versions.c.sha256,
            versions.c.size_bytes,
            versions.c.created_by,
            versions.c.created_at,
        )
        .join(versions, versions.c.experiment_id == experiments.c.id)
        .order_by(experiments.c.id, versions.c.version_number.desc())
    ).mappings()
    seen_experiments = set()
    for row in rows:
        experiment_id = row["experiment_id"]
        if experiment_id in seen_experiments:
            continue
        seen_experiments.add(experiment_id)
        connection.execute(
            blocks.insert().values(
                id=uuid.uuid4(),
                experiment_id=experiment_id,
                position=0,
                name=row["experiment_name"],
                schema_version=row["schema_version"],
                app_version=row["app_version"],
                storage_key=row["storage_key"],
                original_filename=row["original_filename"],
                sha256=row["sha256"],
                size_bytes=row["size_bytes"],
                created_by=row["created_by"],
                created_at=row["created_at"],
            )
        )


def downgrade():
    op.drop_index("ix_experiment_blocks_sha256", table_name="experiment_blocks")
    op.drop_index(
        "ix_experiment_blocks_experiment_id",
        table_name="experiment_blocks",
    )
    op.drop_table("experiment_blocks")
