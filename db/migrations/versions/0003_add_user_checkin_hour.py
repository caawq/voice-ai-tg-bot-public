"""Час вечернего чек-ина как настройка пользователя (Промпт 6, /settings).

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-02 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default=20 — ровно то значение, которое до этой миграции было
    # захардкожено в services/checkin.py, поэтому у существующих пользователей
    # поведение не меняется вообще.
    op.add_column(
        'users',
        sa.Column('checkin_hour', sa.SmallInteger(), server_default=sa.text('20'), nullable=False),
    )
    op.create_check_constraint('ck_users_checkin_hour_range', 'users', 'checkin_hour BETWEEN 0 AND 23')


def downgrade() -> None:
    op.drop_constraint('ck_users_checkin_hour_range', 'users', type_='check')
    op.drop_column('users', 'checkin_hour')
