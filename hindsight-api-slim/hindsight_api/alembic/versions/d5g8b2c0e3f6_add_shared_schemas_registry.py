"""Add public.shared_schemas registry table.

Tracks which shared memory-bank schemas exist. A shared schema is a normal
per-tenant schema (same table set + migrations) that multiple users read from
and write to, as opposed to a private per-user schema.

The registry lives in the global ``public`` schema and must exist exactly once,
so — like the maintenance routines — it is created only during the base-schema
run and skipped for per-tenant schema runs (which would otherwise redundantly
attempt to create the same public table).

Columns:
- ``schema_name``  : PostgreSQL schema name, e.g. ``shared_my_codebase`` (PK).
- ``display_name`` : human-friendly label shown in the UI.
- ``created_by``   : opaque identifier of the admin who created it (tenant_id/subject).
- ``created_at``   : creation timestamp.

Membership (which users may access a shared schema) is NOT stored here — it is
resolved by the tenant extension from its identity provider (GitHub teams / OIDC
groups). This table is only the source of truth for *which* shared schemas exist.

PostgreSQL only (mirrors audit_log / maintenance-routine migrations).

Revision ID: d5g8b2c0e3f6
Revises: c3f7a1b9d2e4
Create Date: 2026-07-01
"""

from collections.abc import Sequence

from alembic import context, op

from hindsight_api.alembic._dialect import run_for_dialect

revision: str = "d5g8b2c0e3f6"
down_revision: str | Sequence[str] | None = "c3f7a1b9d2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_base_schema_run() -> bool:
    """True only for the base-schema migration (no per-tenant target_schema).

    The registry lives in the shared ``public`` schema, so it must be created
    exactly once. Per-tenant runs skip it.
    """
    return not context.config.get_main_option("target_schema")


def _pg_upgrade() -> None:
    if not _is_base_schema_run():
        return
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.shared_schemas (
            schema_name TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            created_by TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def _pg_downgrade() -> None:
    if not _is_base_schema_run():
        return
    op.execute("DROP TABLE IF EXISTS public.shared_schemas")


def upgrade() -> None:
    run_for_dialect(pg=_pg_upgrade)


def downgrade() -> None:
    run_for_dialect(pg=_pg_downgrade)
