"""
Shared foundation for the GitHub single-org team access model.

This module holds the pieces that the three cooperating ``github_org``
extensions all depend on, so there is a single source of truth for:

- :class:`CallerPolicy` — the resolved (user_id, role) for a request.
- :class:`OrgSnapshot` — the whole-org ``bank_ownership`` snapshot with a TTL.
- A process-local cache (:data:`_ORG_OWNERSHIP`) that the HTTP extension primes
  (it has DB access) and the operation-validator extension reads (it does not).
- A shared ``asyncpg`` pool handle so the validator can self-prime on paths the
  HTTP extension never serves (e.g. the worker).
- The ``authz_profile`` fail-fast guard that requires all three extension slots
  to be configured together.

The design is documented in
``hindsight-docs/docs/developer/team-org-model.md``.

Nothing here touches the memory engine or any core file: the extensions
cooperate purely through this module-level state, mirroring the existing
``github_tenant`` → ``github_role_validator`` pattern.

License: MIT
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from hindsight_api.extensions.builtin.github_tenant import (
    ROLE_ADMIN,
    parse_role_from_tenant_id,
)

if TYPE_CHECKING:
    import asyncpg

    from hindsight_api.extensions.context import ExtensionContext

logger = logging.getLogger(__name__)

__all__ = [
    "AUTHZ_PROFILE_GITHUB_ORG",
    "BANK_OWNERSHIP_TABLE",
    "DEFAULT_OWNERSHIP_CACHE_TTL",
    "VISIBILITY_PRIVATE",
    "VISIBILITY_SHARED",
    "CallerPolicy",
    "CallerView",
    "OrgSnapshot",
    "caller_view_from_snapshot",
    "ensure_pool_via_context",
    "get_pool",
    "get_snapshot",
    "parse_user_id_from_tenant_id",
    "set_pool",
    "set_snapshot",
    "snapshot_is_fresh",
    "validate_authz_profile",
]

# Name of the extension-owned per-schema table holding bank ownership/visibility.
BANK_OWNERSHIP_TABLE = "bank_ownership"

VISIBILITY_PRIVATE = "private"
VISIBILITY_SHARED = "shared"

# Default TTL (seconds) for the whole-org ownership snapshot.
DEFAULT_OWNERSHIP_CACHE_TTL = 30.0

# Value of HINDSIGHT_API_AUTHZ_PROFILE that activates this model's fail-fast guard.
AUTHZ_PROFILE_GITHUB_ORG = "github_org"

# Env vars the fail-fast guard inspects. These are the three extension slots that
# must all point at the github_org classes for the profile to be coherent.
_ENV_TENANT_EXTENSION = "HINDSIGHT_API_TENANT_EXTENSION"
_ENV_VALIDATOR_EXTENSION = "HINDSIGHT_API_OPERATION_VALIDATOR_EXTENSION"
_ENV_HTTP_EXTENSION = "HINDSIGHT_API_HTTP_EXTENSION"
_ENV_AUTHZ_PROFILE = "HINDSIGHT_API_AUTHZ_PROFILE"

# Substring that identifies a github_org extension class in a slot value.
_GITHUB_ORG_MARKER = "github_org"


@dataclass
class CallerPolicy:
    """Resolved authorization identity for a request.

    Both fields are recoverable from ``RequestContext.tenant_id`` (encoded as
    ``gh_<user_id>:<role>``), so the validator can operate off ``tenant_id``
    alone if a :class:`CallerPolicy` was not stashed.
    """

    user_id: str
    role: str  # admin | member | viewer


@dataclass
class OrgSnapshot:
    """Whole-org snapshot of the ``bank_ownership`` table.

    ``rows`` maps ``bank_id`` -> ``(owner_user_id, visibility)``.
    """

    rows: dict[str, tuple[str, str]] = field(default_factory=dict)
    fetched_at: float = 0.0


@dataclass
class CallerView:
    """A single caller's derived view over an :class:`OrgSnapshot`."""

    owned: set[str]
    shared: set[str]
    is_admin: bool


# ---------------------------------------------------------------------------
# Process-local shared state (module globals)
# ---------------------------------------------------------------------------

# Whole-org ownership snapshot. Primed by the HTTP extension (and, on worker
# processes, lazily by the validator). ``None`` until first primed.
_ORG_OWNERSHIP: OrgSnapshot | None = None

# Shared asyncpg pool handle. Whichever component first obtains ``memory._get_pool()``
# stores it here so the other can refresh the snapshot without a ``memory`` ref.
_POOL: "asyncpg.Pool | None" = None

# Last-resort pool the validator owns itself, created from the configured
# database URL. This exists because the operation-validator extension's
# ExtensionContext is never wired on the **worker** process (worker/main.py loads
# it without a context) and the worker has no HTTP extension to call
# ``set_pool``. Without this, every worker write to an owned/private bank fails
# closed (no snapshot -> deny), so async retain/consolidation stall. Created once
# under ``_OWN_POOL_LOCK`` and reused. Kept separate from ``_POOL`` so a later
# ``set_pool`` from the engine still takes precedence.
_OWN_POOL: "asyncpg.Pool | None" = None
_OWN_POOL_LOCK: "asyncio.Lock | None" = None

# Env var holding the primary database URL (mirrors hindsight_api.config).
_ENV_DATABASE_URL = "HINDSIGHT_API_DATABASE_URL"


def get_snapshot() -> OrgSnapshot | None:
    """Return the current org ownership snapshot, or ``None`` if never primed."""
    return _ORG_OWNERSHIP


def set_snapshot(snapshot: OrgSnapshot) -> None:
    """Replace the current org ownership snapshot."""
    global _ORG_OWNERSHIP
    _ORG_OWNERSHIP = snapshot


def snapshot_is_fresh(snapshot: OrgSnapshot | None, ttl: float) -> bool:
    """Return True if ``snapshot`` exists and is younger than ``ttl`` seconds."""
    if snapshot is None:
        return False
    return (time.monotonic() - snapshot.fetched_at) < ttl


def get_pool() -> "asyncpg.Pool | None":
    """Return the shared asyncpg pool handle, if one has been registered."""
    return _POOL


def set_pool(pool: "asyncpg.Pool") -> None:
    """Register the shared asyncpg pool handle (idempotent, first writer wins is fine)."""
    global _POOL
    _POOL = pool


async def ensure_pool_via_context(context: "ExtensionContext | None") -> "asyncpg.Pool | None":
    """Obtain the shared asyncpg pool, self-priming it from the memory engine if needed.

    This lets the operation-validator extension refresh the ownership snapshot on
    paths the HTTP extension never serves (e.g. a fresh API process whose first
    request is a core route, or a worker process). Resolution order:

    1. Return :func:`get_pool` if the HTTP extension (or a prior call here)
       already registered a pool.
    2. Otherwise ask the :class:`~hindsight_api.extensions.context.ExtensionContext`
       for the memory engine and call its ``_get_pool`` coroutine. That method is
       only guaranteed on the concrete ``MemoryEngine`` (not the abstract
       ``MemoryEngineInterface``), so it is resolved defensively via ``getattr``.
       ``_get_pool`` triggers ``initialize()`` on first use, which is desirable.
    3. On success the pool is registered via :func:`set_pool` so subsequent calls
       are fast and the HTTP extension and validator share one handle.

    4. As a **last resort** (worker process: no registered pool AND no context),
       create a small pool owned by this module from
       ``HINDSIGHT_API_DATABASE_URL`` via :func:`_ensure_own_pool`. This is what
       keeps async retain/consolidation working on the worker, whose validator
       context is never wired.

    All failures are swallowed and return ``None`` so callers can fail closed.
    ``context`` may be ``None`` (or ``self.context`` may raise ``RuntimeError`` if
    it was never set); both cases fall through to the self-owned pool.
    """
    pool = get_pool()
    if pool is not None:
        return pool
    if context is not None:
        try:
            engine = context.get_memory_engine()
            get_pool_method = getattr(engine, "_get_pool", None)
            if get_pool_method is not None:
                pool = await get_pool_method()
                if pool is not None:
                    set_pool(pool)
                    return pool
        except Exception:  # noqa: BLE001 — fall through to the self-owned pool.
            logger.warning("github_org: failed to obtain pool via extension context", exc_info=True)

    # Last resort: a pool this module owns, built from the configured DB URL.
    return await _ensure_own_pool()


async def _ensure_own_pool() -> "asyncpg.Pool | None":
    """Create (once) and return a small asyncpg pool from ``HINDSIGHT_API_DATABASE_URL``.

    Used only when no engine pool is reachable (the worker path). The pool is
    created under a lock so concurrent first callers share one, and cached in
    :data:`_OWN_POOL`. Failures return ``None`` (fail closed).
    """
    global _OWN_POOL, _OWN_POOL_LOCK
    if _OWN_POOL is not None:
        return _OWN_POOL

    raw_url = (os.getenv(_ENV_DATABASE_URL) or "").strip()
    if not raw_url:
        return None

    if _OWN_POOL_LOCK is None:
        _OWN_POOL_LOCK = asyncio.Lock()

    async with _OWN_POOL_LOCK:
        if _OWN_POOL is not None:  # re-check under the lock
            return _OWN_POOL
        try:
            import asyncpg

            from hindsight_api.db_url import to_libpq_url

            dsn = to_libpq_url(raw_url)
            # Small pool: the validator only runs short SELECT/INSERT ownership
            # queries. Keep it modest so it never competes with the engine pool.
            _OWN_POOL = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
            logger.info("github_org: created self-owned ownership pool (no engine pool/context available)")
            return _OWN_POOL
        except Exception:  # noqa: BLE001 — fail closed; caller denies on None.
            logger.warning("github_org: failed to create self-owned ownership pool", exc_info=True)
            return None


def parse_user_id_from_tenant_id(tenant_id: str | None) -> str | None:
    """Extract the GitHub numeric user id from a ``gh_<user_id>:<role>`` tenant_id.

    Mirrors :func:`github_tenant.parse_role_from_tenant_id` for the id half.
    Returns ``None`` when the tenant_id is missing or not in the expected form.
    """
    if not tenant_id or not tenant_id.startswith("gh_"):
        return None
    body = tenant_id[len("gh_") :]
    user_id = body.split(":", 1)[0]
    return user_id or None


def caller_view_from_snapshot(
    snapshot: OrgSnapshot,
    tenant_id: str | None,
) -> CallerView:
    """Derive a caller's owned/shared/admin view from the whole-org snapshot."""
    user_id = parse_user_id_from_tenant_id(tenant_id)
    role = parse_role_from_tenant_id(tenant_id)
    owned: set[str] = set()
    shared: set[str] = set()
    for bank_id, (owner_user_id, visibility) in snapshot.rows.items():
        if visibility == VISIBILITY_SHARED:
            shared.add(bank_id)
        if user_id is not None and owner_user_id == user_id:
            owned.add(bank_id)
    return CallerView(owned=owned, shared=shared, is_admin=(role == ROLE_ADMIN))


def validate_authz_profile() -> None:
    """Fail fast when the github_org authz profile is only partially configured.

    When ``HINDSIGHT_API_AUTHZ_PROFILE=github_org`` is set, all three extension
    slots (tenant, operation-validator, http) must point at github_org classes.
    This is called from each extension's ``__init__`` so a misconfiguration is
    surfaced regardless of which slot the server happens to load first — and it
    needs no core wiring.

    Raises:
        ValueError: if the profile is requested but a slot is missing/wrong.
    """
    if (os.getenv(_ENV_AUTHZ_PROFILE) or "").strip().lower() != AUTHZ_PROFILE_GITHUB_ORG:
        return

    missing: list[str] = []
    for env_var in (_ENV_TENANT_EXTENSION, _ENV_VALIDATOR_EXTENSION, _ENV_HTTP_EXTENSION):
        value = (os.getenv(env_var) or "").strip()
        if _GITHUB_ORG_MARKER not in value:
            missing.append(env_var)

    if missing:
        raise ValueError(
            f"HINDSIGHT_API_AUTHZ_PROFILE={AUTHZ_PROFILE_GITHUB_ORG} requires all three "
            "github_org extension slots to be configured together. "
            f"The following are missing or not set to a github_org class: {', '.join(missing)}. "
            "Set them to hindsight_api.extensions.builtin.github_org:GitHubOrgTenantExtension, "
            ":GitHubOrgAuthorizationExtension, and :GitHubOrgBanksHttpExtension respectively."
        )
