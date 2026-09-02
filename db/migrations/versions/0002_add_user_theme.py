"""Добавляет users.theme для Промпта 5 (картинка недели: тема light/dark).

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-02 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('theme', sa.String(length=5), server_default=sa.text("'light'"), nullable=False),
    )
    op.create_check_constraint('ck_users_theme_valid', 'users', "theme IN ('light', 'dark')")


def downgrade() -> None:
    op.drop_constraint('ck_users_theme_valid', 'users', type_='check')
    op.drop_column('users', 'theme')
