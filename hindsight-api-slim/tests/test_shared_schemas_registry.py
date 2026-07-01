"""Unit tests for the shared-schema registry helpers and name validation.

These cover the pure, DB-independent logic (name validation / prefix rules).
DB-backed CRUD is exercised by integration tests where a Postgres fixture is
available; here we focus on the invariants that guard the ``shared_`` naming
contract.
"""

import pytest

from hindsight_api.engine import shared_schemas as ss


class TestSharedSchemaNameValidation:
    def test_valid_names(self):
        for name in ["shared_a", "shared_my_codebase", "shared_frontend_v2", "shared_x1"]:
            assert ss.is_valid_shared_schema_name(name) is True
            # validate_* returns the name unchanged when valid
            assert ss.validate_shared_schema_name(name) == name

    def test_missing_prefix_rejected(self):
        for name in ["frontend", "user_123", "public", "myschema"]:
            assert ss.is_valid_shared_schema_name(name) is False
            with pytest.raises(ss.SharedSchemaValidationError):
                ss.validate_shared_schema_name(name)

    def test_empty_rejected(self):
        assert ss.is_valid_shared_schema_name("") is False
        with pytest.raises(ss.SharedSchemaValidationError):
            ss.validate_shared_schema_name("")

    def test_uppercase_rejected(self):
        # Names must be lowercase to stay unquoted-safe as PG identifiers.
        assert ss.is_valid_shared_schema_name("shared_Frontend") is False
        with pytest.raises(ss.SharedSchemaValidationError):
            ss.validate_shared_schema_name("shared_Frontend")

    def test_invalid_characters_rejected(self):
        for name in ["shared_a-b", "shared_a b", "shared_a.b", "shared_a;drop", "shared_"]:
            assert ss.is_valid_shared_schema_name(name) is False
            with pytest.raises(ss.SharedSchemaValidationError):
                ss.validate_shared_schema_name(name)

    def test_too_long_rejected(self):
        # Postgres identifiers max at 63 bytes.
        long_name = "shared_" + ("a" * 60)
        assert len(long_name) > 63
        assert ss.is_valid_shared_schema_name(long_name) is False
        with pytest.raises(ss.SharedSchemaValidationError):
            ss.validate_shared_schema_name(long_name)

    def test_prefix_constant(self):
        assert ss.SHARED_SCHEMA_PREFIX == "shared_"


class _FakeTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    """Minimal asyncpg-connection stand-in for registry CRUD unit tests."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.dropped_schemas: list[str] = []

    def transaction(self):
        return _FakeTx()

    async def fetchrow(self, query: str, *args):
        q = query.strip().upper()
        if q.startswith("INSERT"):
            schema_name, display_name, created_by = args
            self.rows[schema_name] = {
                "schema_name": schema_name,
                "display_name": display_name,
                "created_by": created_by,
                "created_at": None,
            }
            return self.rows[schema_name]
        if q.startswith("SELECT 1"):
            name = args[0]
            return {"?column?": 1} if name in self.rows else None
        if q.startswith("SELECT"):
            name = args[0]
            return self.rows.get(name)
        return None

    async def fetch(self, query: str, *args):
        return list(self.rows.values())

    async def execute(self, query: str, *args):
        q = query.strip().upper()
        if q.startswith("DROP SCHEMA"):
            # DROP SCHEMA IF EXISTS "name" CASCADE — record the drop.
            import re as _re

            m = _re.search(r'DROP SCHEMA IF EXISTS "([^"]+)"', query)
            if m:
                self.dropped_schemas.append(m.group(1))
            return "DROP SCHEMA"
        name = args[0]
        existed = name in self.rows
        self.rows.pop(name, None)
        return f"DELETE {1 if existed else 0}"


class _FakePool:
    """Pool whose ``acquire_with_retry`` yields a shared fake connection."""

    def __init__(self, conn):
        self._conn = conn


@pytest.fixture
def fake_pool(monkeypatch):
    conn = _FakeConn()

    class _CtxMgr:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *exc):
            return False

    def _acquire(_pool):
        return _CtxMgr()

    monkeypatch.setattr(ss, "acquire_with_retry", _acquire)
    return _FakePool(conn)


class TestRegistryCrudLogic:
    @pytest.mark.asyncio
    async def test_create_validates_prefix(self, fake_pool):
        with pytest.raises(ss.SharedSchemaValidationError):
            await ss.create_shared_schema(fake_pool, "not_shared", "X")

    @pytest.mark.asyncio
    async def test_create_and_get_and_exists(self, fake_pool):
        rec = await ss.create_shared_schema(fake_pool, "shared_demo", "Demo", created_by="gh_1:admin")
        assert rec.schema_name == "shared_demo"
        assert rec.display_name == "Demo"
        assert await ss.shared_schema_exists(fake_pool, "shared_demo") is True
        got = await ss.get_shared_schema(fake_pool, "shared_demo")
        assert got is not None and got.schema_name == "shared_demo"

    @pytest.mark.asyncio
    async def test_default_display_name_is_schema_name(self, fake_pool):
        rec = await ss.create_shared_schema(fake_pool, "shared_nodisplay")
        assert rec.display_name == "shared_nodisplay"

    @pytest.mark.asyncio
    async def test_list(self, fake_pool):
        await ss.create_shared_schema(fake_pool, "shared_a", "A")
        await ss.create_shared_schema(fake_pool, "shared_b", "B")
        rows = await ss.list_shared_schemas(fake_pool)
        names = {r.schema_name for r in rows}
        assert names == {"shared_a", "shared_b"}

    @pytest.mark.asyncio
    async def test_delete_returns_true_when_removed_false_otherwise(self, fake_pool):
        await ss.create_shared_schema(fake_pool, "shared_del", "D")
        assert await ss.delete_shared_schema(fake_pool, "shared_del") is True
        assert await ss.delete_shared_schema(fake_pool, "shared_del") is False
        assert await ss.shared_schema_exists(fake_pool, "shared_del") is False


class TestDropSharedSchema:
    @pytest.mark.asyncio
    async def test_drop_deregisters_and_drops_schema(self, fake_pool):
        await ss.create_shared_schema(fake_pool, "shared_drop", "Drop")
        dropped = await ss.drop_shared_schema(fake_pool, "shared_drop")
        assert dropped is True
        # registry row gone
        assert await ss.shared_schema_exists(fake_pool, "shared_drop") is False
        # physical schema drop was issued
        assert "shared_drop" in fake_pool._conn.dropped_schemas

    @pytest.mark.asyncio
    async def test_drop_unregistered_returns_false_and_no_schema_drop(self, fake_pool):
        dropped = await ss.drop_shared_schema(fake_pool, "shared_absent")
        assert dropped is False
        # must NOT issue a DROP SCHEMA when the schema was never registered
        assert fake_pool._conn.dropped_schemas == []

    @pytest.mark.asyncio
    async def test_drop_rejects_non_shared_name(self, fake_pool):
        # Guardrail: never allow dropping public / user_* / arbitrary schemas.
        for bad in ["public", "user_123", "not_shared", ""]:
            with pytest.raises(ss.SharedSchemaValidationError):
                await ss.drop_shared_schema(fake_pool, bad)
        assert fake_pool._conn.dropped_schemas == []
