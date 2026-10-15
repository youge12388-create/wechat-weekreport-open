"""add translation settings and period summary

Revision ID: e42a1b7c9d30
Revises: c12f0f9d6a21
Create Date: 2026-09-11
"""

from typing import Sequence, Union
import re

from alembic import op
import sqlalchemy as sa


revision: str = "e42a1b7c9d30"
down_revision: Union[str, None] = "c12f0f9d6a21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LATIN = re.compile(r"[A-Za-z]")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _language(text: str | None) -> str:
    value = (text or "").strip()
    latin = len(_LATIN.findall(value))
    cjk = len(_CJK.findall(value))
    letters = latin + cjk
    if letters < 3:
        return "unknown"
    if latin / letters >= 0.85 and cjk / letters <= 0.1:
        return "english"
    if cjk / letters >= 0.6:
        return "chinese"
    return "other"


def upgrade() -> None:
    op.create_table(
        "system_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value_encrypted", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_system_settings")),
        sa.UniqueConstraint("key", name=op.f("uq_system_settings_key")),
    )
    op.create_table(
        "period_summary_queue",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("report_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_period_summary_queue")),
        sa.UniqueConstraint("start_date", "end_date", name="uq_period_summary_queue_period"),
    )
    op.create_table(
        "report_translations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("source_language", sa.String(length=16), nullable=True),
        sa.Column("target_language", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("translated_content", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(
            ["report_id"], ["weekly_reports.id"], ondelete="CASCADE",
            name=op.f("fk_report_translations_report_id_weekly_reports"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_translations")),
        sa.UniqueConstraint("report_id", "target_language", name="uq_report_translation_target"),
    )
    with op.batch_alter_table("weekly_reports", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "content_language",
                sa.String(length=16),
                nullable=False,
                server_default="unknown",
            )
        )

    # 回填只是读取本地正文进行确定性分类，不调用任何外部语言服务。
    bind = op.get_bind()
    reports = sa.table(
        "weekly_reports",
        sa.column("id", sa.Integer),
        sa.column("content_raw", sa.Text),
        sa.column("content", sa.Text),
        sa.column("content_language", sa.String),
    )
    for report_id, content_raw, content in bind.execute(
        sa.select(reports.c.id, reports.c.content_raw, reports.c.content)
    ):
        bind.execute(
            reports.update()
            .where(reports.c.id == report_id)
            .values(content_language=_language(content_raw or content))
        )


def downgrade() -> None:
    with op.batch_alter_table("weekly_reports", schema=None) as batch_op:
        batch_op.drop_column("content_language")
    op.drop_table("report_translations")
    op.drop_table("period_summary_queue")
    op.drop_table("system_settings")
