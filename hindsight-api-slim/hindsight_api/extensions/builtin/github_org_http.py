"""
HTTP extension for the GitHub single-org, multiple-teams access model.

This is the HTTP half of the three cooperating ``github_org`` extensions
described in ``hindsight-docs/docs/developer/team-org-model.md``. It has three
jobs:

1. **Owns the per-schema ``bank_ownership`` table.** The table records, for each
   bank, its ``owner_user_id`` and its ``visibility`` (``private`` | ``shared``).
   It is created lazily (``CREATE TABLE IF NOT EXISTS``) the first time a route
   touches the schema, so no core migration wiring is required.

2. **Exposes visibility routes at the application root** (not under ``/ext``), so
   they live alongside the built-in bank routes as
   ``/v1/default/banks/{bank_id}/visibility`` and
   ``/v1/default/banks/visibility``. This is why the routes are returned from
   :meth:`get_root_router`; :meth:`get_router` returns an empty router because
   the :class:`HttpExtension` base requires it.

3. **Primes the process-local ownership snapshot** that the companion
   operation-validator extension reads. The validator has no DB handle, so this
   extension refreshes :class:`~hindsight_api.extensions.builtin.github_org_shared.OrgSnapshot`
   at the start of every route handler and registers the shared pool via
   :func:`~hindsight_api.extensions.builtin.github_org_shared.set_pool`, keeping
   the snapshot warm for the validator.

Authorization identity is recovered entirely from ``RequestContext.tenant_id``
(encoded as ``gh_<user_id>:<role>`` by the tenant extension): the caller's
user id via :func:`parse_user_id_from_tenant_id` and their admin status via
:func:`parse_role_from_tenant_id`. Private banks owned by other users are hidden
(HTTP 404) from non-admin callers so their existence is not leaked.

License: MIT
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from hindsight_api.engine.schema import fq_table
from hindsight_api.extensions.builtin.github_org_shared import (
    BANK_OWNERSHIP_TABLE,
    DEFAULT_OWNERSHIP_CACHE_TTL,
    VISIBILITY_PRIVATE,
    VISIBILITY_SHARED,
    OrgSnapshot,
    caller_view_from_snapshot,
    parse_user_id_from_tenant_id,
    set_pool,
    set_snapshot,
    validate_authz_profile,
)
from hindsight_api.extensions.builtin.github_tenant import (
    ROLE_ADMIN,
    parse_role_from_tenant_id,
)
from hindsight_api.extensions.http import HttpExtension
from hindsight_api.models import RequestContext

if TYPE_CHECKING:
    import asyncpg

    from hindsight_api import MemoryEngine

__all__ = ["GitHubOrgBanksHttpExtension"]

# Allowed values for the "how to treat banks with no ownership row" policy.
_UNOWNED_SHARED = "shared"
_UNOWNED_ADMIN_ONLY = "admin_only"
_UNOWNED_CHOICES = {_UNOWNED_SHARED, _UNOWNED_ADMIN_ONLY}


class VisibilityUpdate(BaseModel):
    """Request body for setting a bank's visibility."""

    visibility: Literal["private", "shared"]


class GitHubOrgBanksHttpExtension(HttpExtension):
    """Root-mounted visibility routes + ownership-snapshot priming for github_org.

    Config keys (prefix ``HINDSIGHT_API_HTTP_``):
        - ``github_ownership_cache_ttl`` : float seconds for snapshot freshness
          (default :data:`DEFAULT_OWNERSHIP_CACHE_TTL`).
        - ``github_org_unowned_banks`` : ``shared`` | ``admin_only`` — how to
          treat a bank that has no ``bank_ownership`` row yet (default
          ``shared``).
    """

    def __init__(self, config: dict[str, str] | None = None) -> None:
        super().__init__(config or {})

        # Fail fast if the github_org authz profile is only partially configured.
        validate_authz_profile()

        raw_ttl = self.config.get("github_ownership_cache_ttl")
        if raw_ttl is None or str(raw_ttl).strip() == "":
            self._cache_ttl = DEFAULT_OWNERSHIP_CACHE_TTL
        else:
            try:
                self._cache_ttl = float(raw_ttl)
            except (TypeError, ValueError):
                raise ValueError("HINDSIGHT_API_HTTP_GITHUB_OWNERSHIP_CACHE_TTL must be a number (seconds)")

        unowned = (self.config.get("github_org_unowned_banks") or _UNOWNED_SHARED).strip().lower()
        if unowned not in _UNOWNED_CHOICES:
            raise ValueError(
                f"Invalid github_org_unowned_banks '{unowned}'. Must be one of: {', '.join(sorted(_UNOWNED_CHOICES))}."
            )
        self._unowned_banks = unowned

        # Per-schema guard: ``CREATE TABLE IF NOT EXISTS`` need only run once per
        # schema per process. Mirrors OidcTenantExtension._initialized_schemas.
        self._ensured_schemas: set[str] = set()

    # ------------------------------------------------------------------
    # Request context / identity helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _request_context(authorization: str | None) -> RequestContext:
        """Build a :class:`RequestContext` from an ``Authorization`` header.

        Mirrors ``get_request_context`` in ``api/http.py``: strips a
        case-insensitive ``Bearer `` prefix, otherwise uses the raw header.
        """
        token: str | None = None
        if authorization:
            if authorization.lower().startswith("bearer "):
                token = authorization[len("bearer ") :].strip()
            else:
                token = authorization.strip()
        return RequestContext(api_key=token)

    # ------------------------------------------------------------------
    # Table + snapshot management
    # ------------------------------------------------------------------

    async def _ensure_table(self, conn: "asyncpg.Connection") -> None:
        """Create the per-schema ``bank_ownership`` table if it does not exist.

        Runs the ``CREATE TABLE IF NOT EXISTS`` at most once per schema per
        process: callers reach here with the schema contextvar already set (via
        ``_authenticate_tenant``), so ``get_current_schema()`` is valid and lets
        us skip the DDL on every subsequent request for that schema.
        """
        from hindsight_api.engine.memory_engine import get_current_schema

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

    async def _refresh_snapshot(
        self,
        memory: "MemoryEngine",
        request_context: RequestContext,
    ) -> OrgSnapshot:
        """Authenticate, load the whole-org ownership table, and prime the snapshot.

        Called at the start of each route handler so the process-local snapshot
        the validator reads stays warm.
        """
        await memory._authenticate_tenant(request_context)

        pool = await memory._get_pool()
        set_pool(pool)

        async with pool.acquire() as conn:
            await self._ensure_table(conn)
            rows = await conn.fetch(f"SELECT bank_id, owner_user_id, visibility FROM {fq_table(BANK_OWNERSHIP_TABLE)}")

        snapshot = OrgSnapshot(
            rows={r["bank_id"]: (r["owner_user_id"], r["visibility"]) for r in rows},
            fetched_at=time.monotonic(),
        )
        set_snapshot(snapshot)
        return snapshot

    # ------------------------------------------------------------------
    # Routers
    # ------------------------------------------------------------------

    def get_router(self, memory: "MemoryEngine") -> APIRouter:
        """No ``/ext`` routes; all visibility routes live at the app root."""
        return APIRouter()

    def get_root_router(self, memory: "MemoryEngine") -> APIRouter | None:
        """Root-mounted visibility routes for the github_org model."""
        router = APIRouter(tags=["GitHub Org Banks"])

        @router.get("/v1/default/banks/{bank_id}/visibility")
        async def get_visibility(
            bank_id: str,
            authorization: str | None = Header(default=None),
        ) -> dict[str, object]:
            request_context = self._request_context(authorization)
            snapshot = await self._refresh_snapshot(memory, request_context)

            caller_user_id = parse_user_id_from_tenant_id(request_context.tenant_id)
            caller_is_admin = parse_role_from_tenant_id(request_context.tenant_id) == ROLE_ADMIN

            row = snapshot.rows.get(bank_id)
            if row is None:
                # No ownership row yet: policy decides how to present it.
                if self._unowned_banks == _UNOWNED_ADMIN_ONLY and not caller_is_admin:
                    raise HTTPException(status_code=404, detail="Bank not found")
                return {
                    "bank_id": bank_id,
                    "owner_user_id": "",
                    "is_owner": False,
                    "visibility": VISIBILITY_SHARED,
                    "can_share": caller_is_admin,
                }

            owner_user_id, visibility = row
            is_owner = caller_user_id is not None and owner_user_id == caller_user_id
            # Hide the existence of private banks the caller can neither own nor admin.
            if visibility == VISIBILITY_PRIVATE and not is_owner and not caller_is_admin:
                raise HTTPException(status_code=404, detail="Bank not found")
            return {
                "bank_id": bank_id,
                "owner_user_id": owner_user_id,
                "is_owner": is_owner,
                "visibility": visibility,
                "can_share": is_owner or caller_is_admin,
            }

        @router.put("/v1/default/banks/{bank_id}/visibility")
        async def set_visibility(
            bank_id: str,
            body: VisibilityUpdate,
            authorization: str | None = Header(default=None),
        ) -> dict[str, object]:
            request_context = self._request_context(authorization)
            await self._refresh_snapshot(memory, request_context)

            caller_user_id = parse_user_id_from_tenant_id(request_context.tenant_id)
            caller_is_admin = parse_role_from_tenant_id(request_context.tenant_id) == ROLE_ADMIN

            # Changing (or claiming) ownership requires a known caller identity;
            # never write a bank owned by the empty string.
            if not caller_user_id:
                raise HTTPException(status_code=401, detail="Unable to determine caller identity")

            pool = await memory._get_pool()
            set_pool(pool)

            table = fq_table(BANK_OWNERSHIP_TABLE)
            async with pool.acquire() as conn:
                await self._ensure_table(conn)
                existing = await conn.fetchrow(
                    f"SELECT owner_user_id, visibility FROM {table} WHERE bank_id = $1",
                    bank_id,
                )

                if existing is None:
                    # First writer claims ownership and creates the row. The
                    # caller identity is guaranteed non-empty (checked above).
                    owner_user_id = caller_user_id
                    await conn.execute(
                        f"INSERT INTO {table} (bank_id, owner_user_id, visibility) VALUES ($1, $2, $3)",
                        bank_id,
                        owner_user_id,
                        body.visibility,
                    )
                else:
                    owner_user_id = existing["owner_user_id"]
                    is_owner = caller_user_id is not None and owner_user_id == caller_user_id
                    if not is_owner and not caller_is_admin:
                        raise HTTPException(
                            status_code=403,
                            detail="Only the bank owner or an org admin may change visibility.",
                        )
                    await conn.execute(
                        f"UPDATE {table} SET visibility = $1, updated_at = now() WHERE bank_id = $2",
                        body.visibility,
                        bank_id,
                    )

            # Re-prime the snapshot so the validator observes the write immediately.
            await self._refresh_snapshot(memory, request_context)

            # can_share mirrors the GET route: owner OR admin. After the write
            # the owner is the caller (create branch) or the existing owner
            # (update branch), both captured in owner_user_id above.
            is_owner = caller_user_id is not None and owner_user_id == caller_user_id
            return {
                "bank_id": bank_id,
                "owner_user_id": owner_user_id,
                "is_owner": is_owner,
                "visibility": body.visibility,
                "can_share": is_owner or caller_is_admin,
            }

        @router.get("/v1/default/banks/visibility")
        async def list_visibility(
            authorization: str | None = Header(default=None),
        ) -> dict[str, object]:
            request_context = self._request_context(authorization)
            snapshot = await self._refresh_snapshot(memory, request_context)

            view = caller_view_from_snapshot(snapshot, request_context.tenant_id)

            banks: list[dict[str, object]] = []
            for bank_id, (_owner_user_id, visibility) in snapshot.rows.items():
                is_owner = bank_id in view.owned
                visible = view.is_admin or is_owner or visibility == VISIBILITY_SHARED
                if not visible:
                    # Private bank owned by someone else: omit unless admin.
                    continue
                banks.append(
                    {
                        "bank_id": bank_id,
                        "visibility": visibility,
                        "is_owner": is_owner,
                        "can_share": is_owner or view.is_admin,
                    }
                )

            return {"banks": banks}

        return router
