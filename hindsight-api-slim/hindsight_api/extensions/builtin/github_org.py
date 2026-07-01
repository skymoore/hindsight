"""
GitHub single-org team access model — public entry point.

This module re-exports the three cooperating extension classes so they can be
referenced from environment variables with a single module path:

    HINDSIGHT_API_TENANT_EXTENSION=hindsight_api.extensions.builtin.github_org:GitHubOrgTenantExtension
    HINDSIGHT_API_OPERATION_VALIDATOR_EXTENSION=hindsight_api.extensions.builtin.github_org:GitHubOrgAuthorizationExtension
    HINDSIGHT_API_HTTP_EXTENSION=hindsight_api.extensions.builtin.github_org:GitHubOrgBanksHttpExtension

The implementations live in sibling modules:

- ``github_org_shared``    — shared cache, CallerPolicy, fail-fast guard.
- ``github_org_tenant``    — :class:`GitHubOrgTenantExtension`.
- ``github_org_http``      — :class:`GitHubOrgBanksHttpExtension`.
- ``github_org_validator`` — :class:`GitHubOrgAuthorizationExtension`.

See ``hindsight-docs/docs/developer/team-org-model.md`` for the full design.

License: MIT
"""

from __future__ import annotations

from hindsight_api.extensions.builtin.github_org_http import GitHubOrgBanksHttpExtension
from hindsight_api.extensions.builtin.github_org_tenant import GitHubOrgTenantExtension
from hindsight_api.extensions.builtin.github_org_validator import (
    GitHubOrgAuthorizationExtension,
)

__all__ = [
    "GitHubOrgAuthorizationExtension",
    "GitHubOrgBanksHttpExtension",
    "GitHubOrgTenantExtension",
]
