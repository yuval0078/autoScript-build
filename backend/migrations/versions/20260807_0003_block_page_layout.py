"""Persist whether an ordered Block shares the previous Block's page."""

from alembic import op
import sqlalchemy as sa


revision = "20260807_0003"
down_revision = "20260807_0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "experiment_blocks",
        sa.Column(
            "same_page_as_previous",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )


def downgrade():
    op.drop_column("experiment_blocks", "same_page_as_previous")
