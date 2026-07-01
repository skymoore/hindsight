"""
Registry helpers for shared memory-bank schemas.

A *shared schema* is a normal per-tenant PostgreSQL schema (same table set,
same migrations) that multiple users read from and write to — as opposed to a
private per-user schema. The set of shared schemas that exist is tracked in a
single global registry table, ``public.shared_schemas``.

This module is the single source of truth for reading/writing that registry.
Unlike per-tenant tables (which are addressed via ``fq_table`` and the current
schema contextvar), the registry always lives in ``public`` and is addressed
explicitly here.

Naming: shared schema names must start with ``shared_`` (enforced at creation)
so they are visually distinct from private ``user_*`` schemas and cannot
collide with them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .db_utils import acquire_with_retry

__all__ = [
    "SHARED_SCHEMA_PREFIX",
    "SharedSchemaRecord",
    "SharedSchemaValidationError",
    "is_valid_shared_schema_name",
    "validate_shared_schema_name",
    "create_shared_schema",
    "delete_shared_schema",
    "drop_shared_schema",
    "get_shared_schema",
    "list_shared_schemas",
    "shared_schema_exists",
]

# Registry table lives in the global ``public`` schema.
_REGISTRY_TABLE = "public.shared_schemas"

# Required prefix for shared schema names (decision: enforce ``shared_``).
SHARED_SCHEMA_PREFIX = "shared_"

# Postgres identifier maximum length (bytes).
_MAX_IDENTIFIER_LENGTH = 63

# A valid shared schema name: shared_ + [a-z0-9_], starting after the prefix
# with a letter/digit/underscore, all lowercase to keep them unquoted-safe.
_SHARED_SCHEMA_RE = re.compile(r"^shared_[a-z0-9_]+$")


class SharedSchemaValidationError(ValueError):
    """Raised when a proposed shared schema name is invalid."""


@dataclass
class SharedSchemaRecord:
    """A row in ``public.shared_schemas``."""

    schema_name: str
    display_name: str
    created_by: str | None
    created_at: str | None

    def to_dict(self) -> dict:
        return {
            "schema_name": self.schema_name,
            "display_name": self.display_name,
            "created_by": self.created_by,
            "created_at": self.created_at,
        }


def is_valid_shared_schema_name(name: str) -> bool:
    """Return True if ``name`` is a syntactically valid shared schema name."""
    if not name or len(name) > _MAX_IDENTIFIER_LENGTH:
        return False
    return bool(_SHARED_SCHEMA_RE.match(name))


def validate_shared_schema_name(name: str) -> str:
    """Validate and normalize a shared schema name, or raise.

    Returns the (unchanged) name if valid so callers can inline the check.
    """
    if not name:
        raise SharedSchemaValidationError("Shared schema name is required")
    if not name.startswith(SHARED_SCHEMA_PREFIX):
        raise SharedSchemaValidationError(
            f"Shared schema name must start with '{SHARED_SCHEMA_PREFIX}' (got '{name}')"
        )
    if not is_valid_shared_schema_name(name):
        raise SharedSchemaValidationError(
            f"Invalid shared schema name '{name}'. Use lowercase letters, digits and "
            f"underscores only, e.g. 'shared_my_codebase' (max {_MAX_IDENTIFIER_LENGTH} chars)."
        )
    return name


async def create_shared_schema(
    pool,
    schema_name: str,
    display_name: str | None = None,
    created_by: str | None = None,
) -> SharedSchemaRecord:
    """Insert (or upsert) a shared schema registry row.

    Validates the name (must be ``shared_*``). Does NOT run migrations — callers
    are responsible for provisioning the schema itself via
    ``ExtensionContext.run_migration``.
    """
    validate_shared_schema_name(schema_name)
    display = display_name or schema_name
    async with acquire_with_retry(pool) as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO {_REGISTRY_TABLE} (schema_name, display_name, created_by)
            VALUES ($1, $2, $3)
            ON CONFLICT (schema_name) DO UPDATE
                SET display_name = EXCLUDED.display_name
            RETURNING schema_name, display_name, created_by, created_at
            """,
            schema_name,
            display,
            created_by,
        )
    return _row_to_record(row)


async def delete_shared_schema(pool, schema_name: str) -> bool:
    """Remove a shared schema registry row (deregister only).

    Returns True if a row was removed. This does NOT drop the underlying
    PostgreSQL schema or its data — destructive removal is a separate,
    explicit operation.
    """
    async with acquire_with_retry(pool) as conn:
        result = await conn.execute(
            f"DELETE FROM {_REGISTRY_TABLE} WHERE schema_name = $1",
            schema_name,
        )
    # asyncpg returns e.g. "DELETE 1"
    try:
        return int(result.split()[-1]) > 0
    except (ValueError, IndexError):
        return False


async def drop_shared_schema(pool, schema_name: str) -> bool:
    """Destructively drop a shared schema: deregister AND drop all its data.

    This is the irreversible companion to :func:`delete_shared_schema`. It:

    1. Removes the registry row from ``public.shared_schemas`` (deregister), and
    2. Executes ``DROP SCHEMA "<schema>" CASCADE`` — permanently deleting every
       table, index and row inside the shared schema.

    Both steps run in a single transaction, so either both apply or neither does.

    Safety: the name is validated against the ``shared_`` prefix + identifier
    rules first, so this can NEVER drop ``public`` or a private ``user_*`` schema.

    Returns:
        True if a registry row existed and was removed (whether or not the
        physical schema existed — ``DROP SCHEMA IF EXISTS`` is idempotent).
        False if there was no registry row for ``schema_name`` (nothing dropped).

    Raises:
        SharedSchemaValidationError: If ``schema_name`` is not a valid shared
            schema name (defends against dropping arbitrary schemas).
    """
    # Hard guardrail: refuse to drop anything that isn't a valid shared_ schema.
    validate_shared_schema_name(schema_name)

    async with acquire_with_retry(pool) as conn:
        async with conn.transaction():
            deleted = await conn.execute(
                f"DELETE FROM {_REGISTRY_TABLE} WHERE schema_name = $1",
                schema_name,
            )
            try:
                removed = int(deleted.split()[-1]) > 0
            except (ValueError, IndexError):
                removed = False

            if not removed:
                # Not registered — do not touch any physical schema.
                return False

            # Name is validated above; safe to interpolate as a quoted identifier.
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')

    return True


async def get_shared_schema(pool, schema_name: str) -> SharedSchemaRecord | None:
    """Return a single registry record, or None if not registered."""
    async with acquire_with_retry(pool) as conn:
        row = await conn.fetchrow(
            f"""
            SELECT schema_name, display_name, created_by, created_at
            FROM {_REGISTRY_TABLE}
            WHERE schema_name = $1
            """,
            schema_name,
        )
    return _row_to_record(row) if row else None


async def shared_schema_exists(pool, schema_name: str) -> bool:
    """Return True if ``schema_name`` is a registered shared schema."""
    async with acquire_with_retry(pool) as conn:
        row = await conn.fetchrow(
            f"SELECT 1 FROM {_REGISTRY_TABLE} WHERE schema_name = $1",
            schema_name,
        )
    return row is not None


async def list_shared_schemas(pool) -> list[SharedSchemaRecord]:
    """Return all registered shared schemas ordered by name."""
    async with acquire_with_retry(pool) as conn:
        rows = await conn.fetch(
            f"""
            SELECT schema_name, display_name, created_by, created_at
            FROM {_REGISTRY_TABLE}
            ORDER BY schema_name
            """
        )
    return [_row_to_record(row) for row in rows]


def _row_to_record(row) -> SharedSchemaRecord:
    created_at = row["created_at"]
    return SharedSchemaRecord(
        schema_name=row["schema_name"],
        display_name=row["display_name"],
        created_by=row["created_by"],
        created_at=created_at.isoformat() if created_at is not None and hasattr(created_at, "isoformat") else created_at,
    )
