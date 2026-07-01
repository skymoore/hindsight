"""
GitHub single-org OperationValidator for Hindsight.

Companion to
:class:`hindsight_api.extensions.builtin.github_org_tenant.GitHubOrgTenantExtension`
and the ``github_org`` HTTP extension. Together they implement the
"single GitHub org, multiple teams" access model documented in
``hindsight-docs/docs/developer/team-org-model.md``.

This extension enforces two orthogonal policies on **every** core route,
purely from process-local state (it holds no database handle):

1. **Role gating** — the caller's GitHub team-derived role is encoded into
   ``RequestContext.tenant_id`` as ``gh_<user_id>:<role>``. Writes/compute
   operations (retain, reflect) require the ``admin`` or ``member`` role;
   the ``viewer`` role (and any unknown role) is read-only.

2. **Per-bank visibility** — a whole-org ``bank_ownership`` snapshot maps each
   bank to its owner and visibility. A private bank is visible only to its
   owner (or an admin); a shared bank is visible to the whole org. Admins see
   and write everything.

The ownership snapshot is primed by the ``github_org`` HTTP extension (which
*does* have DB access). On worker paths the HTTP extension never serves, this
validator lazily self-primes from a shared ``asyncpg`` pool registered in
:mod:`hindsight_api.extensions.builtin.github_org_shared`. All snapshot access
is **fail-closed**: when no fresh snapshot can be obtained, reads and writes are
denied rather than silently allowed.

Everything is read from ``tenant_id`` plus the shared snapshot, so no extra
GitHub API call is made per request. The design mirrors the existing
``github_tenant`` → ``github_role_validator`` pairing.

Enable alongside the GitHub org tenant + HTTP extensions:
    HINDSIGHT_API_OPERATION_VALIDATOR_EXTENSION=hindsight_api.extensions.builtin.github_org_validator:GitHubOrgAuthorizationExtension

License: MIT
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from hindsight_api.extensions.builtin.github_org_shared import (
    BANK_OWNERSHIP_TABLE,
    DEFAULT_OWNERSHIP_CACHE_TTL,
    VISIBILITY_PRIVATE,
    CallerView,
    OrgSnapshot,
    caller_view_from_snapshot,
    ensure_pool_via_context,
    get_snapshot,
    parse_user_id_from_tenant_id,
    set_snapshot,
    snapshot_is_fresh,
    validate_authz_profile,
)
from hindsight_api.extensions.builtin.github_tenant import (
    ROLE_ADMIN,
    ROLE_MEMBER,
    parse_role_from_tenant_id,
)
from hindsight_api.extensions.operation_validator import (
    BankListContext,
    BankListResult,
    BankReadContext,
    BankWriteContext,
    OperationValidatorExtension,
    RecallContext,
    ReflectContext,
    RetainContext,
    ValidationResult,
)

if TYPE_CHECKING:
    from hindsight_api.models import RequestContext

logger = logging.getLogger(__name__)

__all__ = ["GitHubOrgAuthorizationExtension"]

# Roles permitted to perform write/compute operations (retain, reflect, bank writes).
_WRITE_ROLES = {ROLE_ADMIN, ROLE_MEMBER}

# How to treat a bank that is not present in the ownership snapshot.
_UNOWNED_SHARED = "shared"


class GitHubOrgAuthorizationExtension(OperationValidatorExtension):
    """Gate core operations by GitHub role and per-bank org visibility.

    Role gating decides *whether* a caller may write at all; the ownership
    snapshot decides *which banks* the caller may see and write. Both are
    resolved from process-local state so the hooks stay cheap.
    """

    def __init__(self, config: dict[str, str] | None = None) -> None:
        super().__init__(config or {})
        validate_authz_profile()
        if config:
            self._ttl = float(config.get("github_ownership_cache_ttl", DEFAULT_OWNERSHIP_CACHE_TTL))
            # Normalize identically to GitHubOrgBanksHttpExtension so the two
            # slots agree when configured via the same env prefix (e.g. "Shared"
            # or " admin_only " must resolve the same on both sides).
            self._unowned_banks = (config.get("github_org_unowned_banks") or _UNOWNED_SHARED).strip().lower()
        else:
            self._ttl = DEFAULT_OWNERSHIP_CACHE_TTL
            self._unowned_banks = _UNOWNED_SHARED

        # Per-schema guard for the lazy ``bank_ownership`` CREATE TABLE, mirroring
        # GitHubOrgBanksHttpExtension._ensured_schemas. The validator may be the
        # first component to touch the table on worker/cold paths the HTTP
        # extension never serves, so it must be able to create it too.
        self._ensured_schemas: set[str] = set()

    # ------------------------------------------------------------------
    # Role gating helpers (mirror github_role_validator)
    # ------------------------------------------------------------------

    def _can_write(self, tenant_id: str | None) -> bool:
        """Return True when the caller's role permits write/compute operations."""
        return parse_role_from_tenant_id(tenant_id) in _WRITE_ROLES

    # ------------------------------------------------------------------
    # Snapshot access
    # ------------------------------------------------------------------

    def _get_context(self):
        """Return the ExtensionContext, or ``None`` if it was never set.

        ``self.context`` (from :class:`~hindsight_api.extensions.base.Extension`)
        raises ``RuntimeError`` before the loader wires the context, so guard it
        here to keep :meth:`_refresh_snapshot` a single fail-closed path.
        """
        try:
            return self.context
        except RuntimeError:
            return None

    async def _refresh_snapshot(self) -> OrgSnapshot | None:
        """Self-prime the ownership snapshot, obtaining a pool if necessary.

        The pool is resolved via
        :func:`~hindsight_api.extensions.builtin.github_org_shared.ensure_pool_via_context`,
        which prefers an already-registered shared pool (primed by the HTTP
        extension) and otherwise self-primes from the memory engine exposed by
        the ExtensionContext. This is the fix for the hot path: on a fresh API
        process (or any worker) whose first request is a core route, the HTTP
        extension has never run, so ``get_pool()`` alone would return ``None``
        and every read/write would fail closed until a ``/visibility`` route was
        hit. Sourcing the pool from the engine makes the validator independent of
        the HTTP extension.

        Returns the freshly-fetched snapshot, or ``None`` when no pool can be
        obtained or the query fails. All failures are swallowed (fail closed).
        """
        pool = await ensure_pool_via_context(self._get_context())
        if pool is None:
            return None
        try:
            # NOTE: fq_table() resolves the table from the schema contextvar
            # (get_current_schema()). On the validator hook path the engine has
            # already authenticated the tenant, so the schema is set correctly.
            # On a cold self-prime BEFORE any auth (e.g. a worker priming ahead
            # of its first request) this would resolve to the default schema —
            # acceptable for the single-org model, which has effectively one
            # schema.
            from hindsight_api.engine.schema import fq_table

            table = fq_table(BANK_OWNERSHIP_TABLE)
            async with pool.acquire() as conn:
                records = await conn.fetch(f"SELECT bank_id, owner_user_id, visibility FROM {table}")
            rows: dict[str, tuple[str, str]] = {
                record["bank_id"]: (record["owner_user_id"], record["visibility"]) for record in records
            }
            snapshot = OrgSnapshot(rows=rows, fetched_at=time.monotonic())
            set_snapshot(snapshot)
            return snapshot
        except Exception:  # noqa: BLE001 — fail closed on any refresh error.
            logger.warning("github_org: failed to refresh bank ownership snapshot", exc_info=True)
            return None

    async def _get_snapshot(self) -> OrgSnapshot | None:
        """Return a fresh ownership snapshot, refreshing from the pool if stale."""
        snapshot = get_snapshot()
        if not snapshot_is_fresh(snapshot, self._ttl):
            refreshed = await self._refresh_snapshot()
            if refreshed is not None:
                return refreshed
        return snapshot

    async def _get_view(self, request_context: RequestContext) -> CallerView | None:
        """Resolve the caller's owned/shared/admin view, or ``None`` if unavailable."""
        snapshot = await self._get_snapshot()
        if snapshot is None:
            return None
        return caller_view_from_snapshot(snapshot, request_context.tenant_id)

    async def _get_view_and_known(self, request_context: RequestContext) -> tuple[CallerView, set[str]] | None:
        """Return both the caller view and the set of banks known to the snapshot.

        Returns ``None`` (fail closed) when no snapshot is available.
        """
        snapshot = await self._get_snapshot()
        if snapshot is None:
            return None
        view = caller_view_from_snapshot(snapshot, request_context.tenant_id)
        return view, set(snapshot.rows.keys())

    # ------------------------------------------------------------------
    # First-writer ownership claim
    # ------------------------------------------------------------------

    async def _ensure_table(self, conn) -> None:
        """Create the per-schema ``bank_ownership`` table if it does not exist.

        Idempotent and cached per schema per process, mirroring
        :meth:`GitHubOrgBanksHttpExtension._ensure_table`. The validator can be
        the first component to write ownership (e.g. a worker retain before any
        ``/visibility`` route has run), so it must be able to create the table.
        """
        from hindsight_api.engine.memory_engine import get_current_schema
        from hindsight_api.engine.schema import fq_table

        schema = get_current_schema()
        if schema in self._ensured_schemas:
            return
        await conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {fq_table(BANK_OWNERSHIP_TABLE)} (
              bank_id text PRIMARY KEY,
              owner_user_id text NOT NULL,
              visibility text NOT NULL DEFAULT '{VISIBILITY_PRIVATE}',
              created_at timestamptz NOT NULL DEFAULT now(),
              updated_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        self._ensured_schemas.add(schema)

    async def _claim_bank_if_unowned(self, bank_id: str, caller_user_id: str) -> bool:
        """Atomically claim ``bank_id`` as private, owned by ``caller_user_id``.

        This is the first-writer-wins ownership rule: the first write-capable
        caller to touch a bank with no ownership row becomes its owner and the
        bank is created **private**. The claim is race-safe via
        ``INSERT ... ON CONFLICT (bank_id) DO NOTHING`` on the ``bank_ownership``
        primary key, so two callers racing to a brand-new bank produce exactly
        one owner; the loser is subsequently denied by :meth:`_can_write_bank`.

        Returns ``True`` when the caller now owns the bank (either this call
        claimed it, or a concurrent claim by the *same* caller won). Returns
        ``False`` on any failure (fail closed) or when a *different* caller won
        the race.

        The freshly-primed snapshot is refreshed so subsequent checks in the
        same request observe the claim without waiting for the TTL.
        """
        from hindsight_api.engine.schema import fq_table

        pool = await ensure_pool_via_context(self._get_context())
        if pool is None:
            return False
        try:
            table = fq_table(BANK_OWNERSHIP_TABLE)
            async with pool.acquire() as conn:
                await self._ensure_table(conn)
                await conn.execute(
                    f"""
                    INSERT INTO {table} (bank_id, owner_user_id, visibility)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (bank_id) DO NOTHING
                    """,
                    bank_id,
                    caller_user_id,
                    VISIBILITY_PRIVATE,
                )
                # Read back the authoritative owner (handles the race: the row
                # may have been claimed by a concurrent caller).
                owner = await conn.fetchval(
                    f"SELECT owner_user_id FROM {table} WHERE bank_id = $1",
                    bank_id,
                )
        except Exception:  # noqa: BLE001 — fail closed; caller denies on False.
            logger.warning("github_org: failed to claim ownership for bank %r", bank_id, exc_info=True)
            return False

        # Refresh the snapshot so the just-written row is visible to the rest of
        # this request (and to the HTTP extension / other hooks).
        await self._refresh_snapshot()
        return owner == caller_user_id

    # ------------------------------------------------------------------
    # Per-bank access checks
    # ------------------------------------------------------------------

    async def _can_read(self, bank_id: str, request_context: RequestContext) -> bool:
        """Return True when the caller may read ``bank_id`` (fail closed)."""
        resolved = await self._get_view_and_known(request_context)
        if resolved is None:
            return False
        view, known = resolved
        if view.is_admin:
            return True
        if bank_id in view.shared or bank_id in view.owned:
            return True
        if bank_id not in known:
            # Unowned/unknown bank: treat per policy.
            return self._unowned_banks == _UNOWNED_SHARED
        return False

    async def _can_write_bank(self, bank_id: str, request_context: RequestContext) -> bool:
        """Return True when the caller may write ``bank_id`` (fail closed).

        Requires a write-capable role. The per-bank rule is:

        - **Unowned bank (no ownership row)**: the caller becomes the owner via
          first-writer-wins and the bank is created **private** — this applies
          to **admins too**. Privacy-by-default is universal: any bank belongs
          to whoever first writes it and is not shared until its owner (or an
          admin) explicitly shares it via the ``PUT .../visibility`` route. The
          claim is checked *before* the admin/owner short-circuits so an admin
          creating a bank does not leave it unowned (which would surface as
          org-shared).
        - **Owner**: may write banks they own.
        - **Admin**: may write any *already-owned* bank (owned by anyone).
        - **Shared bank owned by someone else**: denied for non-admins. Sharing
          grants *read* to the org, not write; only the owner or an admin writes.
        - **Private bank owned by someone else**: denied for non-admins.

        The ``github_org_unowned_banks`` policy no longer gates writes: the
        first writer always claims the bank private. (It only affects the
        defensive read/list fallback for a bank row that somehow exists with no
        ownership entry.)
        """
        resolved = await self._get_view_and_known(request_context)
        if resolved is None:
            return False
        view, known = resolved
        if not self._can_write(request_context.tenant_id):
            return False

        # First-writer-wins claim runs BEFORE the admin/owner short-circuits so
        # that a brand-new bank is always recorded private for its creator —
        # including when the creator is an admin. Requires a known caller
        # identity so we never write an empty owner.
        if bank_id not in known:
            caller_user_id = parse_user_id_from_tenant_id(request_context.tenant_id)
            if caller_user_id:
                return await self._claim_bank_if_unowned(bank_id, caller_user_id)
            # No parseable identity: an admin may still write (e.g. maintenance),
            # but a non-admin without identity is denied.
            return view.is_admin

        # Bank already has an owner.
        if view.is_admin:
            return True
        if bank_id in view.owned:
            return True
        # Owned by someone else (shared or private) — not writable by a
        # non-owner, non-admin caller.
        return False

    # ------------------------------------------------------------------
    # Core operation hooks (abstract - required)
    # ------------------------------------------------------------------

    async def validate_retain(self, ctx: RetainContext) -> ValidationResult:
        if not self._can_write(ctx.request_context.tenant_id):
            return ValidationResult.reject("Read-only role: retain is not permitted", status_code=403)
        if not await self._can_write_bank(ctx.bank_id, ctx.request_context):
            return ValidationResult.reject("You do not have write access to this bank", status_code=403)
        return ValidationResult.accept()

    async def validate_reflect(self, ctx: ReflectContext) -> ValidationResult:
        if not self._can_write(ctx.request_context.tenant_id):
            return ValidationResult.reject("Read-only role: reflect is not permitted", status_code=403)
        if not await self._can_write_bank(ctx.bank_id, ctx.request_context):
            return ValidationResult.reject("You do not have write access to this bank", status_code=403)
        return ValidationResult.accept()

    async def validate_recall(self, ctx: RecallContext) -> ValidationResult:
        # Viewers may read; visibility is enforced per-bank.
        if not await self._can_read(ctx.bank_id, ctx.request_context):
            return ValidationResult.reject("Bank not found", status_code=404)
        return ValidationResult.accept()

    # ------------------------------------------------------------------
    # Bank management hooks (override - default accept in base)
    # ------------------------------------------------------------------

    async def validate_bank_read(self, ctx: BankReadContext) -> ValidationResult:
        if not await self._can_read(ctx.bank_id, ctx.request_context):
            return ValidationResult.reject("Bank not found", status_code=404)
        return ValidationResult.accept()

    async def validate_bank_write(self, ctx: BankWriteContext) -> ValidationResult:
        if not await self._can_write_bank(ctx.bank_id, ctx.request_context):
            return ValidationResult.reject("You do not have write access to this bank", status_code=403)
        return ValidationResult.accept()

    async def filter_bank_list(self, ctx: BankListContext) -> BankListResult:
        resolved = await self._get_view_and_known(ctx.request_context)
        if resolved is None:
            # Fail closed: hide everything when no snapshot is available.
            return BankListResult(banks=[])
        view, known = resolved
        if view.is_admin:
            return BankListResult(banks=ctx.banks)
        visible = view.owned | view.shared
        filtered: list[dict] = []
        for bank in ctx.banks:
            bank_id = bank["bank_id"]
            if bank_id in visible:
                filtered.append(bank)
            elif bank_id not in known and self._unowned_banks == _UNOWNED_SHARED:
                filtered.append(bank)
        return BankListResult(banks=filtered)
