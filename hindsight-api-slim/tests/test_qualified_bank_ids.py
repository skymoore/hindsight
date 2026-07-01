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
from hindsight_api.engine.retain import bank_utils
from hindsight_api.extensions import AmbiguousBankIdError, OperationValidationError
from hindsight_api.extensions.builtin.github_tenant import encode_tenant_id
from hindsight_api.models import RequestContext


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
    stub._ensure_shared_schema_ready = types.MethodType(me.MemoryEngine._ensure_shared_schema_ready, stub)
    stub._accessible_schemas = types.MethodType(me.MemoryEngine._accessible_schemas, stub)
    stub._find_bank_in_accessible_schemas = types.MethodType(
        me.MemoryEngine._find_bank_in_accessible_schemas, stub
    )
    stub._shadow_schemas_for = types.MethodType(me.MemoryEngine._shadow_schemas_for, stub)
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


@pytest.fixture
def _existing_banks(monkeypatch):
    """Control which (schema, bank_id) pairs 'exist' for bare-id resolution."""
    present: set[tuple[str, str]] = set()

    async def _exists(_pool, bank_id, schema):
        return (schema, bank_id) in present

    monkeypatch.setattr(bank_utils, "bank_id_exists_in_schema", _exists)
    return present


@pytest.fixture
def _registered_list(monkeypatch):
    """Control the full shared-schema registry (used by admin _accessible_schemas)."""
    names: list[str] = []

    async def _list(_pool):
        return [
            ss.SharedSchemaRecord(schema_name=n, display_name=n, created_by=None, created_at=None)
            for n in names
        ]

    monkeypatch.setattr(ss, "list_shared_schemas", _list)
    return names


class TestResolveBankTargetParsing:
    @pytest.mark.asyncio
    async def test_bare_bank_id_uses_primary(self, _stub_registry, _existing_banks):
        eng = _make_engine(registered=set())
        target = await eng.resolve_bank_target("mybank", write=False)
        assert target.schema is None
        assert target.bank_id == "mybank"

    @pytest.mark.asyncio
    async def test_bare_bank_id_write_no_check(self, _stub_registry, _existing_banks):
        eng = _make_engine(registered=set())
        target = await eng.resolve_bank_target("mybank", write=True)
        assert target.schema is None and target.bank_id == "mybank"


class TestBareIdResolution:
    """Option-A runtime resolver for bare (unqualified) bank ids."""

    @pytest.mark.asyncio
    async def test_zero_matches_read_uses_primary(self, _stub_registry, _existing_banks):
        eng = _make_engine(registered=set())
        target = await eng.resolve_bank_target("docs", write=False)
        assert target.schema is None and target.bank_id == "docs"

    @pytest.mark.asyncio
    async def test_zero_matches_write_uses_primary(self, _stub_registry, _existing_banks):
        eng = _make_engine(registered=set())
        target = await eng.resolve_bank_target("docs", write=True)
        assert target.schema is None and target.bank_id == "docs"

    @pytest.mark.asyncio
    async def test_one_match_in_primary_schema_none(self, _stub_registry, _existing_banks):
        _existing_banks.add(("user_1", "docs"))
        eng = _make_engine(registered=set())
        target = await eng.resolve_bank_target("docs", write=False)
        assert target.schema is None and target.bank_id == "docs"

    @pytest.mark.asyncio
    async def test_one_match_in_shared_member_resolves(self, _stub_registry, _existing_banks):
        _stub_registry["registered"] = {"shared_a"}
        me._readable_schemas.set(frozenset({"shared_a"}))
        _existing_banks.add(("shared_a", "docs"))
        eng = _make_engine(registered={"shared_a"})
        target = await eng.resolve_bank_target("docs", write=False)
        assert target.schema == "shared_a" and target.bank_id == "docs"

    @pytest.mark.asyncio
    async def test_one_match_in_shared_write_bootstraps(self, _stub_registry, _existing_banks):
        _stub_registry["registered"] = {"shared_a"}
        me._writable_schemas.set(frozenset({"shared_a"}))
        _existing_banks.add(("shared_a", "docs"))
        eng = _make_engine(registered={"shared_a"})
        target = await eng.resolve_bank_target("docs", write=True)
        assert target.schema == "shared_a"
        # bare id resolving to a shared schema on write must still bootstrap.
        assert "shared_a" in eng._ext_ctx.migrated
        assert "shared_a" in eng._bootstrapped_shared_schemas

    @pytest.mark.asyncio
    async def test_one_match_in_shared_admin_non_member(
        self, _stub_registry, _existing_banks, _registered_list
    ):
        # Admin who is NOT a member; schema is in the registry → accessible.
        _stub_registry["registered"] = {"shared_admin"}
        _registered_list.append("shared_admin")
        _existing_banks.add(("shared_admin", "docs"))
        eng = _make_engine(registered={"shared_admin"})
        rc = RequestContext(api_key="t", tenant_id=encode_tenant_id("9", "admin"))
        target = await eng.resolve_bank_target("docs", write=False, request_context=rc)
        assert target.schema == "shared_admin" and target.bank_id == "docs"

    @pytest.mark.asyncio
    async def test_two_matches_primary_and_shared_ambiguous(self, _stub_registry, _existing_banks):
        _stub_registry["registered"] = {"shared_a"}
        me._readable_schemas.set(frozenset({"shared_a"}))
        _existing_banks.add(("user_1", "docs"))
        _existing_banks.add(("shared_a", "docs"))
        eng = _make_engine(registered={"shared_a"})
        with pytest.raises(AmbiguousBankIdError) as ei:
            await eng.resolve_bank_target("docs", write=False)
        # Primary first, then the rest sorted.
        assert ei.value.conflicts == ["user_1/docs", "shared_a/docs"]

    @pytest.mark.asyncio
    async def test_two_matches_two_shared_ambiguous(
        self, _stub_registry, _existing_banks
    ):
        _stub_registry["registered"] = {"shared_a", "shared_b"}
        me._readable_schemas.set(frozenset({"shared_b", "shared_a"}))
        _existing_banks.add(("shared_a", "docs"))
        _existing_banks.add(("shared_b", "docs"))
        eng = _make_engine(registered={"shared_a", "shared_b"})
        with pytest.raises(AmbiguousBankIdError) as ei:
            await eng.resolve_bank_target("docs", write=False)
        # primary (user_1) has no match, so only the two shared, sorted.
        assert ei.value.conflicts == ["shared_a/docs", "shared_b/docs"]

    @pytest.mark.asyncio
    async def test_shadow_schemas_for_excludes_primary(self, _stub_registry, _existing_banks):
        _stub_registry["registered"] = {"shared_a"}
        me._readable_schemas.set(frozenset({"shared_a"}))
        _existing_banks.add(("user_1", "docs"))
        _existing_banks.add(("shared_a", "docs"))
        eng = _make_engine(registered={"shared_a"})
        shadow = await eng._shadow_schemas_for("docs", None, None)
        assert shadow == ["shared_a"]


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


class TestAdminBypassMembership:
    """Admins may read/write any registered shared schema regardless of team."""

    @pytest.mark.asyncio
    async def test_admin_write_without_membership(self, _stub_registry):
        from hindsight_api.extensions.builtin.github_tenant import encode_tenant_id
        from hindsight_api.models import RequestContext

        _stub_registry["registered"] = {"shared_other"}
        # No membership at all (readable/writable empty).
        eng = _make_engine(registered={"shared_other"})
        rc = RequestContext(api_key="t", tenant_id=encode_tenant_id("9", "admin"))
        target = await eng.resolve_bank_target("shared_other/bank", write=True, request_context=rc)
        assert target.schema == "shared_other" and target.bank_id == "bank"

    @pytest.mark.asyncio
    async def test_non_admin_still_denied(self, _stub_registry):
        from hindsight_api.extensions.builtin.github_tenant import encode_tenant_id
        from hindsight_api.models import RequestContext

        _stub_registry["registered"] = {"shared_other"}
        eng = _make_engine(registered={"shared_other"})
        rc = RequestContext(api_key="t", tenant_id=encode_tenant_id("9", "member"))
        with pytest.raises(OperationValidationError) as ei:
            await eng.resolve_bank_target("shared_other/bank", write=True, request_context=rc)
        assert ei.value.status_code == 403


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
