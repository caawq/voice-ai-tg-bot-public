"""Первая миграция: пользователи и записи (события/задачи/цели).

Создаёт две таблицы, два ENUM-типа, шесть CHECK-констрейнтов и пять индексов —
подробности решений см. в docstring db/models.py.

Revision ID: 0001
Revises: 
Create Date: 2026-08-31 23:42:34.168682
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('users',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('telegram_id', sa.BigInteger(), nullable=False),
    sa.Column('timezone', sa.String(length=64), server_default=sa.text("'Europe/Moscow'"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('telegram_id')
    )
    op.create_table('items',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('type', sa.Enum('event', 'task', 'goal', name='item_type'), nullable=False),
    sa.Column('status', sa.Enum('pending', 'done', 'deleted', name='item_status'), server_default=sa.text("'pending'"), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('start_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('due_date', sa.Date(), nullable=True),
    sa.Column('progress_percent', sa.SmallInteger(), nullable=True),
    sa.Column('parent_goal_id', sa.BigInteger(), nullable=True),
    sa.Column('source_transcript', sa.Text(), nullable=True),
    sa.Column('voice_file_id', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("type <> 'event' OR (start_at IS NOT NULL AND due_date IS NULL AND progress_percent IS NULL AND parent_goal_id IS NULL)", name='ck_items_event_shape'),
    sa.CheckConstraint("type <> 'goal' OR (start_at IS NULL AND due_date IS NULL AND parent_goal_id IS NULL AND progress_percent IS NOT NULL)", name='ck_items_goal_shape'),
    sa.CheckConstraint("type <> 'task' OR (start_at IS NULL AND progress_percent IS NULL)", name='ck_items_task_shape'),
    sa.CheckConstraint('length(btrim(title)) > 0', name='ck_items_title_not_blank'),
    sa.CheckConstraint('parent_goal_id IS NULL OR parent_goal_id <> id', name='ck_items_goal_not_self'),
    sa.CheckConstraint('progress_percent IS NULL OR progress_percent BETWEEN 0 AND 100', name='ck_items_progress_range'),
    sa.ForeignKeyConstraint(['parent_goal_id'], ['items.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_items_parent_goal_id', 'items', ['parent_goal_id'], unique=False, postgresql_where=sa.text('parent_goal_id IS NOT NULL'))
    op.create_index('ix_items_user_due_date', 'items', ['user_id', 'due_date'], unique=False, postgresql_where=sa.text("due_date IS NOT NULL AND status <> 'deleted'"))
    op.create_index('ix_items_user_goals', 'items', ['user_id'], unique=False, postgresql_where=sa.text("type = 'goal' AND status <> 'deleted'"))
    op.create_index('ix_items_user_pending_tasks', 'items', ['user_id', 'due_date'], unique=False, postgresql_where=sa.text("type = 'task' AND status = 'pending'"))
    op.create_index('ix_items_user_start_at', 'items', ['user_id', 'start_at'], unique=False, postgresql_where=sa.text("start_at IS NOT NULL AND status <> 'deleted'"))


def downgrade() -> None:
    op.drop_index('ix_items_user_start_at', table_name='items', postgresql_where=sa.text("start_at IS NOT NULL AND status <> 'deleted'"))
    op.drop_index('ix_items_user_pending_tasks', table_name='items', postgresql_where=sa.text("type = 'task' AND status = 'pending'"))
    op.drop_index('ix_items_user_goals', table_name='items', postgresql_where=sa.text("type = 'goal' AND status <> 'deleted'"))
    op.drop_index('ix_items_user_due_date', table_name='items', postgresql_where=sa.text("due_date IS NOT NULL AND status <> 'deleted'"))
    op.drop_index('ix_items_parent_goal_id', table_name='items', postgresql_where=sa.text('parent_goal_id IS NOT NULL'))
    op.drop_table('items')
    op.drop_table('users')
    # ENUM-типы Alembic сам не удаляет: без этих двух строк откат оставил бы
    # item_type и item_status в базе, и повторный upgrade упал бы на
    # "type already exists". Проверено прогоном upgrade -> downgrade -> upgrade.
    sa.Enum(name='item_status').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='item_type').drop(op.get_bind(), checkfirst=True)
