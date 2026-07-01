"""Unit tests for the shared-schema admin engine methods (create/delete/drop).

Binds the real unbound ``MemoryEngine`` methods to a light stand-in so the
admin-gating, error-code, and destructive-drop wiring can be exercised without a
live Postgres. The GitHub role validator's ``is_admin`` is driven through the
encoded ``RequestContext.tenant_id`` (``gh_<id>:<role>``).
"""

import types

import pytest

from hindsight_api.engine import memory_engine as me
from hindsight_api.engine import shared_schemas as ss
from hindsight_api.extensions import OperationValidationError
from hindsight_api.extensions.builtin.github_tenant import encode_tenant_id
from hindsight_api.models import RequestContext


class _StubExtCtx:
    def __init__(self):
        self.current_schema = None
        self.migrated: list[str] = []

    async def run_migration(self, schema: str):
        self.migrated.append(schema)


def _make_engine():
    stub = types.SimpleNamespace()
    stub._ext_ctx = _StubExtCtx()
    stub._bootstrapped_shared_schemas = set()
    stub._backend = object()

    async def _get_backend():
        return stub._backend

    async def _authenticate_tenant(_ctx):
        return "user_1"

    stub._get_backend = _get_backend
    stub._authenticate_tenant = _authenticate_tenant

    stub.drop_shared_schema = types.MethodType(me.MemoryEngine.drop_shared_schema, stub)
    stub.delete_shared_schema = types.MethodType(me.MemoryEngine.delete_shared_schema, stub)
    stub.create_shared_schema = types.MethodType(me.MemoryEngine.create_shared_schema, stub)
    return stub


def _admin_ctx():
    return RequestContext(api_key="t", tenant_id=encode_tenant_id("1", "admin"))


def _member_ctx():
    return RequestContext(api_key="t", tenant_id=encode_tenant_id("2", "member"))


class TestDropSharedSchemaEngine:
    @pytest.mark.asyncio
    async def test_member_rejected_403(self, monkeypatch):
        eng = _make_engine()
        called = {"drop": False}

        async def _drop(_b, _s):
            called["drop"] = True
            return True

        monkeypatch.setattr(ss, "drop_shared_schema", _drop)

        with pytest.raises(OperationValidationError) as ei:
            await eng.drop_shared_schema(schema_name="shared_x", request_context=_member_ctx())
        assert ei.value.status_code == 403
        assert called["drop"] is False  # never reaches the destructive call

    @pytest.mark.asyncio
    async def test_admin_drop_succeeds(self, monkeypatch):
        eng = _make_engine()
        eng._bootstrapped_shared_schemas.add("shared_x")

        async def _drop(_b, schema):
            assert schema == "shared_x"
            return True

        monkeypatch.setattr(ss, "drop_shared_schema", _drop)

        result = await eng.drop_shared_schema(schema_name="shared_x", request_context=_admin_ctx())
        assert result == {"schema_name": "shared_x", "dropped": True}
        # bootstrap cache cleared so a re-created schema will re-migrate
        assert "shared_x" not in eng._bootstrapped_shared_schemas

    @pytest.mark.asyncio
    async def test_admin_drop_not_found_404(self, monkeypatch):
        eng = _make_engine()

        async def _drop(_b, _s):
            return False

        monkeypatch.setattr(ss, "drop_shared_schema", _drop)

        with pytest.raises(OperationValidationError) as ei:
            await eng.drop_shared_schema(schema_name="shared_ghost", request_context=_admin_ctx())
        assert ei.value.status_code == 404

    @pytest.mark.asyncio
    async def test_invalid_name_maps_to_400(self, monkeypatch):
        eng = _make_engine()

        async def _drop(_b, _s):
            raise ss.SharedSchemaValidationError("bad name")

        monkeypatch.setattr(ss, "drop_shared_schema", _drop)

        with pytest.raises(OperationValidationError) as ei:
            await eng.drop_shared_schema(schema_name="public", request_context=_admin_ctx())
        assert ei.value.status_code == 400
