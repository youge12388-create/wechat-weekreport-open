"""add login authorization columns

部门级登录授权用**整棵子树**语义：勾选一个部门 = 该部门及全部下级部门的人都能登录。
授权状态只占 departments 一行两位（login_enabled + login_role），
不产生逐人授权行 —— 人数增长不会让授权数据膨胀，组织调整也不用维护名单。

login_enabled 是**可空**三态：NULL=未设置（向上继承）/ true=允许 / false=显式禁止。
存量的部门一律写 NULL（视为未设置），这样升级后「谁能登录」完全由管理员
在设置页勾选决定，不会因为默认值把全公司一次性放开。

Revision ID: f7c2d8a41b93
Revises: b5d3e8a71c24
Create Date: 2026-09-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f7c2d8a41b93"
down_revision = "b5d3e8a71c24"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 三态：NULL（未设置）/ true（允许）/ false（显式禁止）
    op.add_column(
        "departments", sa.Column("login_enabled", sa.Boolean(), nullable=True)
    )
    op.add_column(
        "departments",
        sa.Column(
            "login_role",
            sa.String(length=16),
            nullable=False,
            server_default="member",
        ),
    )
    op.add_column(
        "employees",
        sa.Column(
            "login_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "employees",
        sa.Column(
            "login_role",
            sa.String(length=16),
            nullable=False,
            server_default="member",
        ),
    )


def downgrade() -> None:
    op.drop_column("employees", "login_role")
    op.drop_column("employees", "login_enabled")
    op.drop_column("departments", "login_role")
    op.drop_column("departments", "login_enabled")
