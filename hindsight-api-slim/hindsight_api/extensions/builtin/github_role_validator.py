"""
GitHub role-based OperationValidator for Hindsight.

Companion to
:class:`hindsight_api.extensions.builtin.github_tenant.GitHubTenantExtension`.

The tenant extension resolves the user's GitHub team-based role and encodes it
into ``RequestContext.tenant_id`` as ``gh_<id>:<role>``. This validator reads
that role and gates the core memory operations accordingly:

    - ``admin``  : all operations allowed.
    - ``member`` : all operations allowed (read/write).
    - ``viewer`` : recall allowed; retain and reflect rejected (read-only).
    - unknown    : fail closed — retain/reflect rejected.

The role is read directly from ``tenant_id`` (no additional GitHub API call),
so this validator stays cheap and stateless.

Enable alongside the GitHub tenant extension:
    HINDSIGHT_API_OPERATION_VALIDATOR_EXTENSION=hindsight_api.extensions.builtin.github_role_validator:GitHubRoleOperationValidator

License: MIT
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from hindsight_api.extensions.builtin.github_tenant import (
    ROLE_ADMIN,
    ROLE_MEMBER,
    parse_role_from_tenant_id,
)
from hindsight_api.extensions.operation_validator import (
    OperationValidatorExtension,
    RecallContext,
    ReflectContext,
    RetainContext,
    ValidationResult,
)

if TYPE_CHECKING:
    from hindsight_api.models import RequestContext

logger = logging.getLogger(__name__)

__all__ = ["GitHubRoleOperationValidator", "is_admin"]

# Roles permitted to perform write/compute operations (retain, reflect).
_WRITE_ROLES = {ROLE_ADMIN, ROLE_MEMBER}


def is_admin(tenant_id: str | None) -> bool:
    """Return True if the tenant_id encodes an admin role.

    This is a reusable helper that the API layer can import to gate
    admin-only operations (e.g. shared-schema creation) without needing
    a full OperationValidator hook.

    Design note: There is no existing OperationValidator hook for schema
    creation (it is a tenant-level management operation, not a per-bank
    memory operation). Rather than adding a new abstract hook to the base
    class (which would require all existing validators to implement it),
    we expose this standalone helper. The API endpoint for creating a
    shared schema should call ``is_admin(request_context.tenant_id)``
    and return 403 if it returns False.

    Args:
        tenant_id: The ``RequestContext.tenant_id`` value, encoded by
            :func:`~hindsight_api.extensions.builtin.github_tenant.encode_tenant_id`
            as ``gh_<id>:<role>``.

    Returns:
        True if the role is ``admin``, False otherwise.
    """
    return parse_role_from_tenant_id(tenant_id) == ROLE_ADMIN


class GitHubRoleOperationValidator(OperationValidatorExtension):
    """Gate retain/recall/reflect based on the GitHub team-derived role."""

    def __init__(self, config: dict[str, str] | None = None) -> None:
        super().__init__(config or {})

    def _can_write(self, tenant_id: str | None) -> bool:
        return parse_role_from_tenant_id(tenant_id) in _WRITE_ROLES

    async def validate_retain(self, ctx: RetainContext) -> ValidationResult:
        if self._can_write(ctx.request_context.tenant_id):
            return ValidationResult.accept()
        return ValidationResult.reject("Read-only role: retain is not permitted", status_code=403)

    async def validate_reflect(self, ctx: ReflectContext) -> ValidationResult:
        if self._can_write(ctx.request_context.tenant_id):
            return ValidationResult.accept()
        return ValidationResult.reject("Read-only role: reflect is not permitted", status_code=403)

    async def validate_recall(self, ctx: RecallContext) -> ValidationResult:
        # All roles (including viewer) may read.
        return ValidationResult.accept()

    async def validate_create_shared_schema(self, ctx: "RequestContext") -> ValidationResult:
        """Gate shared-schema creation to admin role only.

        Design note: There is no existing OperationValidator hook for schema
        creation (it is a tenant-level management operation). This method is
        provided as a convenience for API layers that want to call it directly.
        The standalone :func:`is_admin` helper is the preferred lightweight
        alternative for simple endpoint guards.

        Args:
            ctx: The authenticated RequestContext (tenant_id must be set).

        Returns:
            ValidationResult.accept() for admins, reject(403) for all others.
        """
        if is_admin(ctx.tenant_id):
            return ValidationResult.accept()
        return ValidationResult.reject("Only admins may create shared schemas", status_code=403)
