"""Label saved Test Runs.

Revision ID: 20260910_0015
Revises: 20260817_0014
"""

import sqlalchemy as sa
from alembic import op


revision = "20260910_0015"
down_revision = "20260817_0014"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "experiment_runs",
        sa.Column(
            "is_test",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index(
        "ix_experiment_runs_is_test",
        "experiment_runs",
        ["is_test"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_experiment_runs_is_test", table_name="experiment_runs")
    op.drop_column("experiment_runs", "is_test")
