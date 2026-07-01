"""Tests for the GitHub role-based OperationValidator."""

from unittest.mock import MagicMock

import pytest

from hindsight_api.extensions.builtin.github_role_validator import GitHubRoleOperationValidator, is_admin
from hindsight_api.extensions.builtin.github_tenant import (
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_VIEWER,
    encode_tenant_id,
)
from hindsight_api.models import RequestContext


def _ctx(role: str | None):
    tenant_id = encode_tenant_id("1", role) if role else None
    c = MagicMock()
    c.request_context = RequestContext(api_key="x", tenant_id=tenant_id)
    return c


@pytest.fixture
def validator():
    return GitHubRoleOperationValidator({})


class TestRetain:
    @pytest.mark.asyncio
    async def test_admin_allowed(self, validator):
        assert (await validator.validate_retain(_ctx(ROLE_ADMIN))).allowed

    @pytest.mark.asyncio
    async def test_member_allowed(self, validator):
        assert (await validator.validate_retain(_ctx(ROLE_MEMBER))).allowed

    @pytest.mark.asyncio
    async def test_viewer_rejected(self, validator):
        result = await validator.validate_retain(_ctx(ROLE_VIEWER))
        assert not result.allowed

    @pytest.mark.asyncio
    async def test_unknown_rejected(self, validator):
        assert not (await validator.validate_retain(_ctx(None))).allowed


class TestReflect:
    @pytest.mark.asyncio
    async def test_viewer_rejected(self, validator):
        assert not (await validator.validate_reflect(_ctx(ROLE_VIEWER))).allowed

    @pytest.mark.asyncio
    async def test_member_allowed(self, validator):
        assert (await validator.validate_reflect(_ctx(ROLE_MEMBER))).allowed


class TestRecall:
    @pytest.mark.asyncio
    async def test_viewer_allowed(self, validator):
        assert (await validator.validate_recall(_ctx(ROLE_VIEWER))).allowed

    @pytest.mark.asyncio
    async def test_unknown_allowed_to_read(self, validator):
        # Recall is permitted for everyone, including unmapped tenants.
        assert (await validator.validate_recall(_ctx(None))).allowed


class TestIsAdminHelper:
    def test_admin_returns_true(self):
        assert is_admin(encode_tenant_id("1", ROLE_ADMIN)) is True

    def test_member_returns_false(self):
        assert is_admin(encode_tenant_id("1", ROLE_MEMBER)) is False

    def test_viewer_returns_false(self):
        assert is_admin(encode_tenant_id("1", ROLE_VIEWER)) is False

    def test_none_returns_false(self):
        assert is_admin(None) is False

    def test_malformed_returns_false(self):
        assert is_admin("gh_123") is False


class TestValidateCreateSharedSchema:
    @pytest.mark.asyncio
    async def test_admin_allowed(self, validator):
        ctx = RequestContext(api_key="x", tenant_id=encode_tenant_id("1", ROLE_ADMIN))
        result = await validator.validate_create_shared_schema(ctx)
        assert result.allowed

    @pytest.mark.asyncio
    async def test_member_rejected(self, validator):
        ctx = RequestContext(api_key="x", tenant_id=encode_tenant_id("1", ROLE_MEMBER))
        result = await validator.validate_create_shared_schema(ctx)
        assert not result.allowed
        assert result.status_code == 403

    @pytest.mark.asyncio
    async def test_viewer_rejected(self, validator):
        ctx = RequestContext(api_key="x", tenant_id=encode_tenant_id("1", ROLE_VIEWER))
        result = await validator.validate_create_shared_schema(ctx)
        assert not result.allowed
        assert result.status_code == 403

    @pytest.mark.asyncio
    async def test_unknown_rejected(self, validator):
        ctx = RequestContext(api_key="x", tenant_id=None)
        result = await validator.validate_create_shared_schema(ctx)
        assert not result.allowed
        assert result.status_code == 403
