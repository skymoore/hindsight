"""
GitHub single-org, multi-team Tenant Extension for Hindsight.

A specialization of :class:`GitHubTenantExtension` that implements the
**"one GitHub organization, many teams"** tenancy model. Where the base
:class:`GitHubTenantExtension` gives every GitHub user their *own* isolated
schema (``{prefix}_{github_user_id}``), this variant instead places **every
member of one configured org** into a **single shared schema**
(``{prefix}_{org_numeric_id}``). Isolation and access control between users of
that shared org happen at the row/authorization layer (the companion
``github_org`` authorization + HTTP extensions), not at the schema layer.

Key properties of this model:

- **Single org.** Exactly one org is configured (via
  ``HINDSIGHT_API_TENANT_GITHUB_ORG``). There is no multi-org support: a token
  that does not belong to the configured org resolves no role and is denied.
- **Shared schema keyed by org id.** All members share
  ``{schema_prefix}_{org_numeric_id}``. The *immutable numeric* org id is used
  (never the login, which can change). Org numeric ids are short, so the schema
  name comfortably fits within Postgres' 63-character identifier limit.
- **Roles from team membership.** Role resolution is inherited verbatim from
  :class:`GitHubTenantExtension`: a user's team membership in the configured org
  is mapped to ``admin`` / ``member`` / ``viewer`` and encoded into
  ``RequestContext.tenant_id`` as ``gh_<user_id>:<role>`` for the companion
  operation validator.
- **No request headers.** This extension reads *only* the bearer token from
  ``RequestContext.api_key`` (exactly like :class:`GitHubTenantExtension`). It
  never inspects any custom request header to select an org or a tenant — the
  org is fixed by configuration.

Because the org login must be resolved to its numeric id and that endpoint may
require authentication for private orgs, the numeric id is resolved **lazily**
on the first successful ``authenticate`` (using the caller's token) and cached
process-locally for the lifetime of the extension.

Configuration mirrors :class:`GitHubTenantExtension`; point the tenant slot at
this class instead::

    HINDSIGHT_API_TENANT_EXTENSION=hindsight_api.extensions.builtin.github_org_tenant:GitHubOrgTenantExtension
    HINDSIGHT_API_TENANT_GITHUB_ORG=my-org
    HINDSIGHT_API_TENANT_GITHUB_ADMIN_TEAMS=platform-admins
    HINDSIGHT_API_TENANT_GITHUB_MEMBER_TEAMS=engineering,data
    HINDSIGHT_API_TENANT_GITHUB_VIEWER_TEAMS=analysts

License: MIT
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from hindsight_api.extensions.builtin.github_org_shared import validate_authz_profile
from hindsight_api.extensions.builtin.github_tenant import GitHubTenantExtension
from hindsight_api.extensions.builtin.oidc_tenant import Identity
from hindsight_api.extensions.tenant import AuthenticationError, Tenant
from hindsight_api.models import RequestContext

logger = logging.getLogger(__name__)

__all__ = ["GitHubOrgTenantExtension"]


class GitHubOrgTenantExtension(GitHubTenantExtension):
    """GitHub tenant extension mapping one org (all teams) to one shared schema.

    Identity resolution and team/role logic are inherited unchanged from
    :class:`GitHubTenantExtension`. The only behavioral differences are:

    - the schema is keyed by the configured org's numeric id (shared by all
      members) rather than by the per-user numeric id, and
    - the org numeric id is resolved lazily from the org login using the first
      authenticating caller's token, then cached.
    """

    def __init__(self, config: dict[str, str]) -> None:
        super().__init__(config)

        # Fail fast if the github_org authz profile is only partially wired.
        validate_authz_profile()

        # Numeric org id, resolved lazily on first authenticate (see
        # _ensure_org_id) and cached for the extension's lifetime.
        self._org_id: str | None = None

        # Serializes the first-request org id resolution so concurrent callers
        # do not each issue a redundant ``GET /orgs/{login}`` (double-checked
        # locking below). Safe to construct without a running loop on >=3.11.
        self._org_id_lock = asyncio.Lock()

        # Accepted for configuration symmetry with the companion extensions;
        # this tenant extension does not itself maintain an ownership cache.
        self._ownership_cache_ttl_raw: str | None = config.get("github_ownership_cache_ttl")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_startup(self) -> None:
        # Base creates the shared httpx client and logs the org/prefix.
        await super().on_startup()
        logger.info(
            "GitHub org tenant extension active (org=%s); org id resolved lazily on first request",
            self.github_org,
        )

    # ------------------------------------------------------------------
    # Org id resolution (login -> immutable numeric id)
    # ------------------------------------------------------------------

    async def _ensure_org_id(self, token: str) -> str:
        """Resolve and cache the configured org's numeric id.

        Uses the authenticating caller's token because ``GET /orgs/{org}`` needs
        authentication for private orgs. Once resolved, the cached value is
        returned without further GitHub calls.

        Raises:
            AuthenticationError: if the org cannot be resolved.
        """
        # Fast path: once resolved the cached value is returned without paying
        # the lock cost.
        if self._org_id is not None:
            return self._org_id

        async with self._org_id_lock:
            # Re-check under the lock: a concurrent first request may have
            # resolved the id while we waited to acquire it.
            if self._org_id is not None:
                return self._org_id

            if self._http_client is None:
                raise AuthenticationError("Extension not initialized")

            try:
                response = await self._http_client.get(
                    f"{self.github_api_url}/orgs/{self.github_org}",
                    headers=self._headers(token),
                )
            except httpx.TimeoutException:
                raise AuthenticationError("GitHub authentication timeout - please retry")
            except httpx.RequestError as e:
                raise AuthenticationError(f"GitHub connection error: {e!s}")

            if response.status_code != 200:
                raise AuthenticationError(f"Failed to resolve GitHub org '{self.github_org}': {response.status_code}")

            org = response.json()
            org_id = org.get("id")
            if org_id is None:
                raise AuthenticationError(f"GitHub org '{self.github_org}' returned no id")

            self._org_id = str(org_id)
            logger.info("Resolved GitHub org '%s' to id %s", self.github_org, self._org_id)
            return self._org_id

    # ------------------------------------------------------------------
    # Role loading + shared-schema derivation
    # ------------------------------------------------------------------

    async def _load_roles(self, token: str, identity: Identity, context: RequestContext) -> None:
        """Resolve the role (via the base) and ensure the org id is cached.

        ``authenticate`` invokes this *before* :meth:`_schema_for_subject`, so
        resolving the org id here (while the caller's token is available)
        guarantees the shared schema name can be derived without a token.
        """
        await super()._load_roles(token, identity, context)
        await self._ensure_org_id(token)

    def _schema_for_subject(self, subject: str) -> str:
        """Return the single shared org schema, ignoring the per-user subject.

        The org id must already be resolved by :meth:`_load_roles`.

        Raises:
            AuthenticationError: if the org id has not been resolved yet.
        """
        if self._org_id is None:
            raise AuthenticationError("GitHub org id not resolved")
        return f"{self.schema_prefix}_{self._org_id}"

    # ------------------------------------------------------------------
    # Worker discovery
    # ------------------------------------------------------------------

    async def list_tenants(self) -> list[Tenant]:
        """Return the single shared org tenant, once the org id is known.

        Before any request has authenticated the org id is unresolved, so an
        empty list is returned; the scan-discovered-schema worker path covers
        that startup window.
        """
        if self._org_id is None:
            return []
        return [Tenant(schema=f"{self.schema_prefix}_{self._org_id}")]
