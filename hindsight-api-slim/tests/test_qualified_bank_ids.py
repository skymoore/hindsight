"""Unit tests for shared-schema bank-id routing in the memory engine.

Exercises ``MemoryEngine.resolve_bank_target`` (parse + membership + registry +
bootstrap) and ``_schema_override`` without a live Postgres: the method is
bound to a light stand-in that provides only the attributes the method touches
(``_get_backend``, ``_ext_ctx``, ``_bootstrapped_shared_schemas``). Membership
is driven through the module contextvars, mirroring what ``_authenticate_tenant``
sets in production.
"""

import types

import pytest

from hindsight_api.engine import memory_engine as me
from hindsight_api.engine import shared_schemas as ss
from hindsight_api.extensions import OperationValidationError


class _StubExtCtx:
    def __init__(self):
        self.current_schema = None
        self.migrated: list[str] = []

    async def run_migration(self, schema: str):
        self.migrated.append(schema)


def _make_engine(*, registered: set[str], primary: str = "user_1"):
    """Build a minimal object exposing just what resolve_bank_target needs."""
    stub = types.SimpleNamespace()
    stub._ext_ctx = _StubExtCtx()
    stub._bootstrapped_shared_schemas = set()
    stub._backend = object()

    async def _get_backend():
        return stub._backend

    stub._get_backend = _get_backend

    # Bind the real unbound methods to the stub.
    stub.resolve_bank_target = types.MethodType(me.MemoryEngine.resolve_bank_target, stub)
    stub._schema_override = types.MethodType(me.MemoryEngine._schema_override, stub)
    stub._registered = registered
    return stub


@pytest.fixture(autouse=True)
def _reset_contextvars():
    tok_s = me._current_schema.set("user_1")
    tok_r = me._readable_schemas.set(frozenset())
    tok_w = me._writable_schemas.set(frozenset())
    yield
    me._current_schema.reset(tok_s)
    me._readable_schemas.reset(tok_r)
    me._writable_schemas.reset(tok_w)


@pytest.fixture(autouse=True)
def _stub_registry(monkeypatch):
    # Patched per-test via the engine's _registered set.
    holder = {"registered": set()}

    async def _exists(_backend, schema):
        return schema in holder["registered"]

    monkeypatch.setattr(ss, "shared_schema_exists", _exists)
    return holder


class TestResolveBankTargetParsing:
    @pytest.mark.asyncio
    async def test_bare_bank_id_uses_primary(self, _stub_registry):
        eng = _make_engine(registered=set())
        target = await eng.resolve_bank_target("mybank", write=False)
        assert target.schema is None
        assert target.bank_id == "mybank"

    @pytest.mark.asyncio
    async def test_bare_bank_id_write_no_check(self, _stub_registry):
        eng = _make_engine(registered=set())
        target = await eng.resolve_bank_target("mybank", write=True)
        assert target.schema is None and target.bank_id == "mybank"


class TestResolveBankTargetMembership:
    @pytest.mark.asyncio
    async def test_read_allowed_when_in_readable(self, _stub_registry):
        _stub_registry["registered"] = {"shared_x"}
        me._readable_schemas.set(frozenset({"shared_x"}))
        eng = _make_engine(registered={"shared_x"})
        target = await eng.resolve_bank_target("shared_x/bank", write=False)
        assert target.schema == "shared_x" and target.bank_id == "bank"

    @pytest.mark.asyncio
    async def test_read_denied_when_not_member(self, _stub_registry):
        _stub_registry["registered"] = {"shared_x"}
        # readable set is empty -> not a member
        eng = _make_engine(registered={"shared_x"})
        with pytest.raises(OperationValidationError) as ei:
            await eng.resolve_bank_target("shared_x/bank", write=False)
        assert ei.value.status_code == 403

    @pytest.mark.asyncio
    async def test_write_denied_when_only_readable(self, _stub_registry):
        _stub_registry["registered"] = {"shared_x"}
        me._readable_schemas.set(frozenset({"shared_x"}))
        me._writable_schemas.set(frozenset())  # read-only membership
        eng = _make_engine(registered={"shared_x"})
        with pytest.raises(OperationValidationError) as ei:
            await eng.resolve_bank_target("shared_x/bank", write=True)
        assert ei.value.status_code == 403

    @pytest.mark.asyncio
    async def test_write_allowed_when_writable(self, _stub_registry):
        _stub_registry["registered"] = {"shared_x"}
        me._readable_schemas.set(frozenset({"shared_x"}))
        me._writable_schemas.set(frozenset({"shared_x"}))
        eng = _make_engine(registered={"shared_x"})
        target = await eng.resolve_bank_target("shared_x/bank", write=True)
        assert target.schema == "shared_x"
        # first write bootstraps a migration
        assert "shared_x" in eng._ext_ctx.migrated
        assert "shared_x" in eng._bootstrapped_shared_schemas

    @pytest.mark.asyncio
    async def test_bootstrap_only_once(self, _stub_registry):
        _stub_registry["registered"] = {"shared_x"}
        me._writable_schemas.set(frozenset({"shared_x"}))
        eng = _make_engine(registered={"shared_x"})
        await eng.resolve_bank_target("shared_x/bank", write=True)
        await eng.resolve_bank_target("shared_x/bank2", write=True)
        assert eng._ext_ctx.migrated == ["shared_x"]  # migrated exactly once


class TestResolveBankTargetGuards:
    @pytest.mark.asyncio
    async def test_bad_prefix_rejected_403(self, _stub_registry):
        me._readable_schemas.set(frozenset({"notshared_x"}))
        eng = _make_engine(registered=set())
        with pytest.raises(OperationValidationError) as ei:
            await eng.resolve_bank_target("notshared_x/bank", write=False)
        assert ei.value.status_code == 403

    @pytest.mark.asyncio
    async def test_unregistered_schema_404(self, _stub_registry):
        # Member, but the shared schema isn't registered in public.shared_schemas.
        me._readable_schemas.set(frozenset({"shared_ghost"}))
        _stub_registry["registered"] = set()
        eng = _make_engine(registered=set())
        with pytest.raises(OperationValidationError) as ei:
            await eng.resolve_bank_target("shared_ghost/bank", write=False)
        assert ei.value.status_code == 404

    @pytest.mark.asyncio
    async def test_primary_schema_qualified_always_allowed(self, _stub_registry):
        # Addressing the caller's own primary schema via qualified form is allowed
        # even without registry/membership, but primary isn't a shared_ schema so
        # it would fail the prefix check — verify a shared-named primary works.
        me._current_schema.set("shared_self")
        _stub_registry["registered"] = {"shared_self"}
        eng = _make_engine(registered={"shared_self"})
        target = await eng.resolve_bank_target("shared_self/bank", write=True)
        assert target.schema == "shared_self" and target.bank_id == "bank"


class TestSchemaOverride:
    @pytest.mark.asyncio
    async def test_override_sets_and_resets(self, _stub_registry):
        eng = _make_engine(registered={"shared_x"})
        assert me.get_current_schema() == "user_1"
        async with eng._schema_override("shared_x"):
            assert me.get_current_schema() == "shared_x"
            assert eng._ext_ctx.current_schema == "shared_x"
        assert me.get_current_schema() == "user_1"

    @pytest.mark.asyncio
    async def test_override_none_is_noop(self, _stub_registry):
        eng = _make_engine(registered=set())
        async with eng._schema_override(None):
            assert me.get_current_schema() == "user_1"
        assert me.get_current_schema() == "user_1"

    @pytest.mark.asyncio
    async def test_override_resets_on_exception(self, _stub_registry):
        eng = _make_engine(registered={"shared_x"})
        with pytest.raises(RuntimeError):
            async with eng._schema_override("shared_x"):
                raise RuntimeError("boom")
        assert me.get_current_schema() == "user_1"
