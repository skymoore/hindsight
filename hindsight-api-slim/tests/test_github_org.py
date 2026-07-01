"""Unit tests for the three cooperating github_org extensions.

Exercises the shared helpers, the fail-fast authz-profile guard, the
OperationValidator half, and the DB-free parts of the HTTP half without
touching a database. The shared module keeps its snapshot/pool in module-level
globals, so an autouse fixture resets them around every test to prevent
cross-test leakage.
"""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import APIRouter
from pydantic import ValidationError

import hindsight_api.extensions.builtin.github_org_shared as shared
import hindsight_api.extensions.builtin.github_org_validator as validator_module
from hindsight_api.extensions.builtin.github_org_http import (
    GitHubOrgBanksHttpExtension,
    VisibilityUpdate,
)
from hindsight_api.extensions.builtin.github_org_shared import (
    VISIBILITY_PRIVATE,
    VISIBILITY_SHARED,
    OrgSnapshot,
    caller_view_from_snapshot,
    encode_org_tenant_id,
    ensure_pool_via_context,
    get_pool,
    parse_login_from_tenant_id,
    parse_role_from_org_tenant_id,
    parse_user_id_from_tenant_id,
    set_pool,
    set_snapshot,
    snapshot_is_fresh,
    validate_authz_profile,
)
from hindsight_api.extensions.builtin.github_org_validator import GitHubOrgAuthorizationExtension
from hindsight_api.extensions.builtin.github_tenant import (
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_VIEWER,
    encode_tenant_id,
)
from hindsight_api.extensions.operation_validator import (
    BankListContext,
    BankReadContext,
    BankReadOperation,
    BankWriteContext,
    BankWriteOperation,
    RecallContext,
    RetainContext,
)
from hindsight_api.models import RequestContext


@pytest.fixture(autouse=True)
def _reset_shared_state():
    """Reset the module-level snapshot/pool globals before and after each test."""
    shared._ORG_OWNERSHIP = None
    shared._POOL = None
    shared._OWN_POOL = None
    shared._OWN_POOL_LOCK = None
    yield
    shared._ORG_OWNERSHIP = None
    shared._POOL = None
    shared._OWN_POOL = None
    shared._OWN_POOL_LOCK = None


def _fresh_snapshot(rows: dict[str, tuple]) -> OrgSnapshot:
    """Build a snapshot that reads as fresh under the default TTL.

    Accepts 2-tuples ``(owner_user_id, visibility)`` or 3-tuples
    ``(owner_user_id, visibility, owner_login)`` and normalizes to the 3-tuple
    shape the OrgSnapshot now stores.
    """
    normalized = {
        bank_id: (row[0], row[1], row[2] if len(row) > 2 else None) for bank_id, row in rows.items()
    }
    return OrgSnapshot(rows=normalized, fetched_at=time.monotonic())


def _ctx(user_id: str, role: str, login: str | None = None) -> RequestContext:
    return RequestContext(api_key="x", tenant_id=encode_org_tenant_id(user_id, role, login))


# ---------------------------------------------------------------------------
# 1. Shared helpers
# ---------------------------------------------------------------------------


class TestTenantIdCodec:
    """The login-aware ``gh_<id>:<role>[:<login>]`` codec."""

    def test_encode_with_login(self):
        assert encode_org_tenant_id("1024", "admin", "skymoore") == "gh_1024:admin:skymoore"

    def test_encode_without_login(self):
        assert encode_org_tenant_id("1024", "admin", None) == "gh_1024:admin"
        assert encode_org_tenant_id("1024", "admin", "") == "gh_1024:admin"

    def test_roundtrip_login_aware(self):
        tid = encode_org_tenant_id("55", "member", "octocat")
        assert parse_user_id_from_tenant_id(tid) == "55"
        assert parse_role_from_org_tenant_id(tid) == "member"
        assert parse_login_from_tenant_id(tid) == "octocat"

    def test_role_not_polluted_by_login(self):
        # The 3rd segment must not fold into the role (the base parser bug we avoid).
        assert parse_role_from_org_tenant_id("gh_55:member:octocat") == "member"

    def test_legacy_two_segment(self):
        assert parse_user_id_from_tenant_id("gh_55:member") == "55"
        assert parse_role_from_org_tenant_id("gh_55:member") == "member"
        assert parse_login_from_tenant_id("gh_55:member") is None

    def test_malformed(self):
        for bad in (None, "garbage", "gh_:admin", ""):
            assert parse_login_from_tenant_id(bad) is None
        assert parse_role_from_org_tenant_id("gh_1024") is None


class TestTenantAllowedConfigFields:
    """get_allowed_config_fields must derive the role with the login-aware parser.

    Regression for the template-import / bank-config failure: the inherited
    github_tenant implementation parses the role with the base
    parse_role_from_tenant_id, which folds the login into the role for a
    gh_<id>:<role>:<login> tenant_id and denies *all* config writes (even for
    admins).
    """

    @staticmethod
    def _tenant():
        # The method reads only context.tenant_id, so bypass __init__ (which
        # requires GitHub org config / network wiring).
        from hindsight_api.extensions.builtin.github_org_tenant import GitHubOrgTenantExtension

        return GitHubOrgTenantExtension.__new__(GitHubOrgTenantExtension)

    @pytest.mark.asyncio
    async def test_admin_with_login_allows_all(self):
        t = self._tenant()
        # gh_<id>:admin:<login> must resolve to admin -> None (allow all).
        assert await t.get_allowed_config_fields(_ctx("1024", "admin", "skymoore"), "portal-dev") is None

    @pytest.mark.asyncio
    async def test_admin_legacy_two_segment_allows_all(self):
        t = self._tenant()
        assert await t.get_allowed_config_fields(_ctx("1024", "admin"), "portal-dev") is None

    @pytest.mark.asyncio
    async def test_member_unowned_bank_allows_all(self):
        # No snapshot primed -> unowned as far as this member is concerned ->
        # full config (first-writer-wins: the member is about to own it).
        t = self._tenant()
        assert shared.get_snapshot() is None
        assert await t.get_allowed_config_fields(_ctx("55", "member", "octocat"), "new-bank") is None

    @pytest.mark.asyncio
    async def test_member_own_bank_allows_all(self):
        # A member owns "mine" -> full config on their own bank.
        set_snapshot(_fresh_snapshot({"mine": ("55", VISIBILITY_PRIVATE, "octocat")}))
        t = self._tenant()
        assert await t.get_allowed_config_fields(_ctx("55", "member", "octocat"), "mine") is None

    @pytest.mark.asyncio
    async def test_member_other_owner_bank_restricted_to_subset(self):
        from hindsight_api.extensions.builtin.github_tenant import _MEMBER_ALLOWED_FIELDS

        # Bank owned by user 999 -> member 55 gets only the curated subset.
        set_snapshot(_fresh_snapshot({"theirs": ("999", VISIBILITY_SHARED, "someone")}))
        t = self._tenant()
        allowed = await t.get_allowed_config_fields(_ctx("55", "member", "octocat"), "theirs")
        assert allowed == set(_MEMBER_ALLOWED_FIELDS)

    @pytest.mark.asyncio
    async def test_viewer_and_unknown_deny_all(self):
        t = self._tenant()
        assert await t.get_allowed_config_fields(_ctx("7", "viewer", "v"), "b") == set()
        # Unparseable role -> fail closed (empty set), never None.
        bad = RequestContext(api_key="x", tenant_id="garbage")
        assert await t.get_allowed_config_fields(bad, "b") == set()


class TestSharedHelpers:
    def test_parse_user_id_variants(self):
        assert parse_user_id_from_tenant_id("gh_1024:admin") == "1024"
        assert parse_user_id_from_tenant_id("gh_55:member") == "55"
        assert parse_user_id_from_tenant_id(None) is None
        assert parse_user_id_from_tenant_id("garbage") is None
        # "gh_:viewer" -> body ":viewer" -> split -> "" -> returned as None.
        assert parse_user_id_from_tenant_id("gh_:viewer") is None

    def test_caller_view_member(self):
        snapshot = _fresh_snapshot(
            {
                "a": ("1024", VISIBILITY_PRIVATE),
                "b": ("999", VISIBILITY_SHARED),
                "c": ("1024", VISIBILITY_SHARED),
            }
        )
        view = caller_view_from_snapshot(snapshot, encode_tenant_id("1024", ROLE_MEMBER))
        assert view.owned == {"a", "c"}
        assert view.shared == {"b", "c"}
        assert view.is_admin is False

    def test_caller_view_admin(self):
        snapshot = _fresh_snapshot(
            {
                "a": ("1024", VISIBILITY_PRIVATE),
                "b": ("999", VISIBILITY_SHARED),
                "c": ("1024", VISIBILITY_SHARED),
            }
        )
        view = caller_view_from_snapshot(snapshot, encode_tenant_id("1024", ROLE_ADMIN))
        assert view.is_admin is True
        assert view.owned == {"a", "c"}

    def test_caller_view_other_user(self):
        snapshot = _fresh_snapshot(
            {
                "a": ("1024", VISIBILITY_PRIVATE),
                "b": ("999", VISIBILITY_SHARED),
                "c": ("1024", VISIBILITY_SHARED),
            }
        )
        view = caller_view_from_snapshot(snapshot, encode_tenant_id("777", ROLE_VIEWER))
        assert view.owned == set()
        assert view.shared == {"b", "c"}
        assert view.is_admin is False

    def test_snapshot_is_fresh(self):
        fresh = OrgSnapshot(rows={}, fetched_at=time.monotonic())
        stale = OrgSnapshot(rows={}, fetched_at=time.monotonic() - 100)
        assert snapshot_is_fresh(fresh, ttl=30) is True
        assert snapshot_is_fresh(stale, ttl=30) is False
        assert snapshot_is_fresh(None, ttl=30) is False


class TestEnsurePoolViaContext:
    """Cover the shared ``ensure_pool_via_context`` helper (DB-free)."""

    @pytest.mark.asyncio
    async def test_no_context_no_pool_returns_none(self, monkeypatch):
        # No registered pool, no context, and no DB URL -> nothing to resolve.
        monkeypatch.delenv("HINDSIGHT_API_DATABASE_URL", raising=False)
        assert get_pool() is None
        assert await ensure_pool_via_context(None) is None

    @pytest.mark.asyncio
    async def test_returns_already_registered_pool_without_context(self):
        # An already-registered pool short-circuits before any context lookup.
        fake_pool = MagicMock()
        set_pool(fake_pool)
        assert await ensure_pool_via_context(None) is fake_pool

    @pytest.mark.asyncio
    async def test_self_primes_from_context_and_registers_pool(self):
        # A fresh process: no pool yet, but a context can supply one.
        assert get_pool() is None
        fake_pool = MagicMock()
        context = MagicMock()
        context.get_memory_engine.return_value._get_pool = AsyncMock(return_value=fake_pool)

        resolved = await ensure_pool_via_context(context)

        assert resolved is fake_pool
        # The resolved pool is registered so subsequent calls are fast.
        assert get_pool() is fake_pool

    @pytest.mark.asyncio
    async def test_engine_lookup_failure_returns_none(self, monkeypatch):
        # If the context cannot yield an engine AND there is no DB URL for the
        # self-owned fallback, fail closed (None).
        monkeypatch.delenv("HINDSIGHT_API_DATABASE_URL", raising=False)
        context = MagicMock()
        context.get_memory_engine.side_effect = RuntimeError("no engine yet")

        assert await ensure_pool_via_context(context) is None
        assert get_pool() is None

    @pytest.mark.asyncio
    async def test_self_owned_pool_fallback_created_without_context(self, monkeypatch):
        # Worker path: no registered pool, no context, but a DB URL is set ->
        # the module creates and caches its own pool via asyncpg.create_pool.
        monkeypatch.setenv("HINDSIGHT_API_DATABASE_URL", "postgresql://u:p@h:5432/db")
        fake_pool = MagicMock()
        create_pool = AsyncMock(return_value=fake_pool)
        import asyncpg

        monkeypatch.setattr(asyncpg, "create_pool", create_pool)

        resolved = await ensure_pool_via_context(None)
        assert resolved is fake_pool
        # Cached: a second call reuses it without creating another pool.
        assert await ensure_pool_via_context(None) is fake_pool
        assert create_pool.await_count == 1

    @pytest.mark.asyncio
    async def test_registered_pool_preferred_over_self_owned(self, monkeypatch):
        # A registered engine pool always wins over the self-owned fallback.
        monkeypatch.setenv("HINDSIGHT_API_DATABASE_URL", "postgresql://u:p@h:5432/db")
        engine_pool = MagicMock()
        set_pool(engine_pool)
        import asyncpg

        create_pool = AsyncMock()
        monkeypatch.setattr(asyncpg, "create_pool", create_pool)

        assert await ensure_pool_via_context(None) is engine_pool
        create_pool.assert_not_awaited()


# ---------------------------------------------------------------------------
# 2. Authz-profile fail-fast guard
# ---------------------------------------------------------------------------


_GITHUB_ORG_CLASS = "hindsight_api.extensions.builtin.github_org:GitHubOrgTenantExtension"


class TestAuthzProfileFailFast:
    def test_profile_unset_no_raise(self, monkeypatch):
        monkeypatch.delenv("HINDSIGHT_API_AUTHZ_PROFILE", raising=False)
        # Should not raise regardless of the slot values.
        validate_authz_profile()

    def test_profile_set_missing_slot_raises(self, monkeypatch):
        monkeypatch.setenv("HINDSIGHT_API_AUTHZ_PROFILE", "github_org")
        monkeypatch.setenv("HINDSIGHT_API_TENANT_EXTENSION", _GITHUB_ORG_CLASS)
        monkeypatch.setenv("HINDSIGHT_API_OPERATION_VALIDATOR_EXTENSION", _GITHUB_ORG_CLASS)
        # HTTP slot missing entirely.
        monkeypatch.delenv("HINDSIGHT_API_HTTP_EXTENSION", raising=False)
        with pytest.raises(ValueError):
            validate_authz_profile()

    def test_profile_set_all_slots_no_raise(self, monkeypatch):
        monkeypatch.setenv("HINDSIGHT_API_AUTHZ_PROFILE", "github_org")
        monkeypatch.setenv("HINDSIGHT_API_TENANT_EXTENSION", _GITHUB_ORG_CLASS)
        monkeypatch.setenv("HINDSIGHT_API_OPERATION_VALIDATOR_EXTENSION", _GITHUB_ORG_CLASS)
        monkeypatch.setenv("HINDSIGHT_API_HTTP_EXTENSION", _GITHUB_ORG_CLASS)
        validate_authz_profile()


# ---------------------------------------------------------------------------
# 3. OperationValidator half
# ---------------------------------------------------------------------------


_ROWS = {
    "mine": ("1024", VISIBILITY_PRIVATE),
    "team": ("999", VISIBILITY_SHARED),
    "secret": ("999", VISIBILITY_PRIVATE),
}


def _read_ctx(bank_id: str, request_context: RequestContext) -> BankReadContext:
    return BankReadContext(
        bank_id=bank_id,
        operation=BankReadOperation.GET_BANK_PROFILE,
        request_context=request_context,
    )


def _write_ctx(bank_id: str, request_context: RequestContext) -> BankWriteContext:
    return BankWriteContext(
        bank_id=bank_id,
        operation=BankWriteOperation.UPDATE_BANK,
        request_context=request_context,
    )


def _recall_ctx(bank_id: str, request_context: RequestContext) -> RecallContext:
    return RecallContext(bank_id=bank_id, query="q", request_context=request_context)


def _retain_ctx(bank_id: str, request_context: RequestContext) -> RetainContext:
    return RetainContext(bank_id=bank_id, contents=[], request_context=request_context)


@pytest.fixture
def validator():
    return GitHubOrgAuthorizationExtension({})


class TestValidatorFailClosed:
    @pytest.mark.asyncio
    async def test_bank_read_denied_without_snapshot(self, validator):
        ctx = _read_ctx("mine", _ctx("1024", ROLE_MEMBER))
        result = await validator.validate_bank_read(ctx)
        assert not result.allowed
        assert result.status_code == 404

    @pytest.mark.asyncio
    async def test_bank_write_denied_without_snapshot(self, validator):
        result = await validator.validate_bank_write(_write_ctx("mine", _ctx("1024", ROLE_MEMBER)))
        assert not result.allowed

    @pytest.mark.asyncio
    async def test_bank_list_empty_without_snapshot(self, validator):
        ctx = BankListContext(banks=[{"bank_id": "mine"}], request_context=_ctx("1024", ROLE_MEMBER))
        result = await validator.filter_bank_list(ctx)
        assert result.banks == []

    @pytest.mark.asyncio
    async def test_recall_denied_without_snapshot(self, validator):
        result = await validator.validate_recall(_recall_ctx("team", _ctx("1024", ROLE_MEMBER)))
        assert not result.allowed
        assert result.status_code == 404


class TestValidatorSelfPrime:
    """Cover the validator's snapshot self-prime wiring via the shared helper.

    The validator resolves a pool through the module-level ``ensure_pool_via_context``
    it imported. We monkeypatch that name so the tests stay deterministic and never
    touch asyncpg or a real database.
    """

    @pytest.mark.asyncio
    async def test_no_pool_no_context_denies(self, validator, monkeypatch):
        # snapshot None, pool None, no context set on the extension.
        assert shared.get_snapshot() is None
        assert validator._get_context() is None
        # Self-prime cannot obtain a pool -> refresh returns None -> deny.
        monkeypatch.setattr(validator_module, "ensure_pool_via_context", AsyncMock(return_value=None))

        assert await validator._get_view(_ctx("1024", ROLE_MEMBER)) is None
        result = await validator.validate_recall(_recall_ctx("team", _ctx("1024", ROLE_MEMBER)))
        assert not result.allowed
        assert result.status_code == 404

    @pytest.mark.asyncio
    async def test_refresh_none_pool_read_denied(self, validator, monkeypatch):
        # Even with the fail-closed helper returning None, bank reads are denied.
        monkeypatch.setattr(validator_module, "ensure_pool_via_context", AsyncMock(return_value=None))
        result = await validator.validate_bank_read(_read_ctx("mine", _ctx("1024", ROLE_MEMBER)))
        assert not result.allowed
        assert result.status_code == 404

    @pytest.mark.asyncio
    async def test_self_primed_snapshot_allows_reads(self, validator, monkeypatch):
        # Prove the positive branch of the self-prime wiring: stub the validator's
        # refresh so it sets a fresh snapshot, then confirm reads are allowed.
        async def _fake_refresh():
            set_snapshot(_fresh_snapshot(_ROWS))
            return shared.get_snapshot()

        monkeypatch.setattr(validator, "_refresh_snapshot", _fake_refresh)

        member = _ctx("1024", ROLE_MEMBER)
        assert (await validator.validate_bank_read(_read_ctx("mine", member))).allowed
        assert (await validator.validate_bank_read(_read_ctx("team", member))).allowed
        # Other user's private bank is still hidden.
        assert not (await validator.validate_bank_read(_read_ctx("secret", member))).allowed


class TestValidatorRefreshSnapshotQuery:
    """Exercise the real ``_refresh_snapshot`` DB path against a fake pool.

    Regression cover for two bugs: (1) the SELECT must include ``owner_login``
    and build 3-tuples matching ``OrgSnapshot.rows`` (else callers unpacking
    ``(owner_user_id, visibility, owner_login)`` raise "expected 3, got 2");
    (2) ``_ensure_table`` must run before the SELECT so a fresh/reset DB (no
    ``bank_ownership`` table yet) does not fail the whole snapshot closed.
    """

    @staticmethod
    def _fake_pool(records, executed):
        conn = MagicMock()
        conn.execute = AsyncMock(side_effect=lambda sql, *a: executed.append(sql))
        conn.fetch = AsyncMock(return_value=records)

        acquire_cm = MagicMock()
        acquire_cm.__aenter__ = AsyncMock(return_value=conn)
        acquire_cm.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=acquire_cm)
        return pool, conn

    @pytest.mark.asyncio
    async def test_refresh_selects_owner_login_and_builds_three_tuples(self, validator, monkeypatch):
        executed: list[str] = []
        records = [
            {"bank_id": "mine", "owner_user_id": "1024", "owner_login": "alice", "visibility": VISIBILITY_PRIVATE},
            {"bank_id": "team", "owner_user_id": "2048", "owner_login": None, "visibility": VISIBILITY_SHARED},
        ]
        pool, conn = self._fake_pool(records, executed)
        monkeypatch.setattr(validator_module, "ensure_pool_via_context", AsyncMock(return_value=pool))

        snapshot = await validator._refresh_snapshot()

        assert snapshot is not None
        # Rows are 3-tuples: (owner_user_id, visibility, owner_login).
        assert snapshot.rows["mine"] == ("1024", VISIBILITY_PRIVATE, "alice")
        assert snapshot.rows["team"] == ("2048", VISIBILITY_SHARED, None)
        # The SELECT column list must include owner_login.
        assert "owner_login" in conn.fetch.await_args.args[0]

    @pytest.mark.asyncio
    async def test_refresh_ensures_table_before_select(self, validator, monkeypatch):
        # On a fresh DB the table must be created (CREATE TABLE IF NOT EXISTS)
        # before the SELECT; _ensure_table issues the CREATE via conn.execute.
        executed: list[str] = []
        pool, _conn = self._fake_pool([], executed)
        monkeypatch.setattr(validator_module, "ensure_pool_via_context", AsyncMock(return_value=pool))

        snapshot = await validator._refresh_snapshot()

        assert snapshot is not None
        assert any("CREATE TABLE IF NOT EXISTS" in sql for sql in executed), executed


class TestValidatorPerBankAccess:
    @pytest.mark.asyncio
    async def test_member_reads_owned_and_shared(self, validator):
        set_snapshot(_fresh_snapshot(_ROWS))
        member = _ctx("1024", ROLE_MEMBER)
        assert (await validator.validate_bank_read(_read_ctx("mine", member))).allowed
        assert (await validator.validate_bank_read(_read_ctx("team", member))).allowed

    @pytest.mark.asyncio
    async def test_member_cannot_read_others_private(self, validator):
        set_snapshot(_fresh_snapshot(_ROWS))
        result = await validator.validate_bank_read(_read_ctx("secret", _ctx("1024", ROLE_MEMBER)))
        assert not result.allowed
        assert result.status_code == 404

    @pytest.mark.asyncio
    async def test_admin_reads_secret(self, validator):
        set_snapshot(_fresh_snapshot(_ROWS))
        assert (await validator.validate_bank_read(_read_ctx("secret", _ctx("1", ROLE_ADMIN)))).allowed

    @pytest.mark.asyncio
    async def test_viewer_retain_rejected(self, validator):
        set_snapshot(_fresh_snapshot(_ROWS))
        result = await validator.validate_retain(_retain_ctx("team", _ctx("1024", ROLE_VIEWER)))
        assert not result.allowed

    @pytest.mark.asyncio
    async def test_viewer_recall_allowed_on_shared(self, validator):
        set_snapshot(_fresh_snapshot(_ROWS))
        assert (await validator.validate_recall(_recall_ctx("team", _ctx("1024", ROLE_VIEWER)))).allowed

    @pytest.mark.asyncio
    async def test_member_writes_owned_but_not_shared(self, validator):
        set_snapshot(_fresh_snapshot(_ROWS))
        member = _ctx("1024", ROLE_MEMBER)
        assert (await validator.validate_bank_write(_write_ctx("mine", member))).allowed
        # "team" is shared but owned by 999 -> member is neither owner nor admin.
        # Sharing grants read to the org, not write; the claim path is NOT taken
        # because the bank is already owned.
        assert not (await validator.validate_bank_write(_write_ctx("team", member))).allowed

    @pytest.mark.asyncio
    async def test_member_cannot_write_others_private(self, validator):
        set_snapshot(_fresh_snapshot(_ROWS))
        member = _ctx("1024", ROLE_MEMBER)
        # "secret" is private, owned by 999 -> denied, no claim attempted.
        assert not (await validator.validate_bank_write(_write_ctx("secret", member))).allowed

    @pytest.mark.asyncio
    async def test_admin_writes_shared(self, validator):
        set_snapshot(_fresh_snapshot(_ROWS))
        assert (await validator.validate_bank_write(_write_ctx("team", _ctx("1", ROLE_ADMIN)))).allowed

    @pytest.mark.asyncio
    async def test_filter_bank_list_member(self, validator):
        set_snapshot(_fresh_snapshot(_ROWS))
        banks = [{"bank_id": "mine"}, {"bank_id": "team"}, {"bank_id": "secret"}]
        ctx = BankListContext(banks=banks, request_context=_ctx("1024", ROLE_MEMBER))
        result = await validator.filter_bank_list(ctx)
        assert [b["bank_id"] for b in result.banks] == ["mine", "team"]

    @pytest.mark.asyncio
    async def test_filter_bank_list_admin(self, validator):
        set_snapshot(_fresh_snapshot(_ROWS))
        banks = [{"bank_id": "mine"}, {"bank_id": "team"}, {"bank_id": "secret"}]
        ctx = BankListContext(banks=banks, request_context=_ctx("1", ROLE_ADMIN))
        result = await validator.filter_bank_list(ctx)
        assert [b["bank_id"] for b in result.banks] == ["mine", "team", "secret"]


class TestValidatorUnownedBanks:
    @pytest.mark.asyncio
    async def test_default_shared_member_reads_legacy(self, validator):
        # "legacy" is not present in the snapshot rows.
        set_snapshot(_fresh_snapshot(_ROWS))
        assert (await validator.validate_bank_read(_read_ctx("legacy", _ctx("1024", ROLE_MEMBER)))).allowed

    @pytest.mark.asyncio
    async def test_admin_only_member_denied_admin_allowed(self):
        validator = GitHubOrgAuthorizationExtension({"github_org_unowned_banks": "admin_only"})
        set_snapshot(_fresh_snapshot(_ROWS))
        member = _ctx("1024", ROLE_MEMBER)
        assert not (await validator.validate_bank_read(_read_ctx("legacy", member))).allowed
        assert (await validator.validate_bank_read(_read_ctx("legacy", _ctx("1", ROLE_ADMIN)))).allowed


class TestFirstWriterOwnership:
    """First write to an unowned bank claims it private for the caller.

    The DB-bound claim (:meth:`_claim_bank_if_unowned`) is stubbed so these stay
    DB-free; a separate integration path exercises the real INSERT.
    """

    @pytest.mark.asyncio
    async def test_member_write_unowned_bank_claims_and_allows(self, validator, monkeypatch):
        set_snapshot(_fresh_snapshot(_ROWS))
        claimed: list[tuple[str, str, str | None]] = []

        async def _fake_claim(bank_id, caller_user_id, caller_login=None):
            claimed.append((bank_id, caller_user_id, caller_login))
            return True

        monkeypatch.setattr(validator, "_claim_bank_if_unowned", _fake_claim)
        # login-aware tenant_id: the claim must receive the username too.
        member = _ctx("1024", ROLE_MEMBER, "skymoore")
        # "fresh" is not in the snapshot -> unowned -> claim -> allowed.
        assert (await validator.validate_bank_write(_write_ctx("fresh", member))).allowed
        assert claimed == [("fresh", "1024", "skymoore")]

    @pytest.mark.asyncio
    async def test_retain_unowned_bank_claims_private(self, validator, monkeypatch):
        set_snapshot(_fresh_snapshot(_ROWS))
        calls: list[tuple[str, str]] = []

        async def _fake_claim(bank_id, caller_user_id, caller_login=None):
            calls.append((bank_id, caller_user_id))
            return True

        monkeypatch.setattr(validator, "_claim_bank_if_unowned", _fake_claim)
        member = _ctx("1024", ROLE_MEMBER)
        assert (await validator.validate_retain(_retain_ctx("fresh", member))).allowed
        assert calls == [("fresh", "1024")]

    @pytest.mark.asyncio
    async def test_race_loser_denied(self, validator, monkeypatch):
        # A concurrent caller won the claim: our claim returns False -> deny.
        set_snapshot(_fresh_snapshot(_ROWS))

        async def _fake_claim(bank_id, caller_user_id, caller_login=None):
            return False

        monkeypatch.setattr(validator, "_claim_bank_if_unowned", _fake_claim)
        member = _ctx("1024", ROLE_MEMBER)
        assert not (await validator.validate_bank_write(_write_ctx("fresh", member))).allowed

    @pytest.mark.asyncio
    async def test_no_identity_no_claim(self, validator, monkeypatch):
        set_snapshot(_fresh_snapshot(_ROWS))
        called = False

        async def _fake_claim(bank_id, caller_user_id, caller_login=None):
            nonlocal called
            called = True
            return True

        monkeypatch.setattr(validator, "_claim_bank_if_unowned", _fake_claim)
        # tenant_id without a parseable user id -> no claim, denied.
        ctx = RequestContext(api_key="x", tenant_id="not-a-gh-tenant")
        assert not (await validator.validate_bank_write(_write_ctx("fresh", ctx))).allowed
        assert called is False

    @pytest.mark.asyncio
    async def test_admin_write_unowned_bank_claims_private(self, validator, monkeypatch):
        # Privacy-by-default is universal: an admin creating a NEW (unowned) bank
        # also claims it private, rather than leaving it unowned/org-shared.
        set_snapshot(_fresh_snapshot(_ROWS))
        claimed: list[tuple[str, str]] = []

        async def _fake_claim(bank_id, caller_user_id, caller_login=None):
            claimed.append((bank_id, caller_user_id))
            return True

        monkeypatch.setattr(validator, "_claim_bank_if_unowned", _fake_claim)
        assert (await validator.validate_bank_write(_write_ctx("fresh", _ctx("1", ROLE_ADMIN)))).allowed
        assert claimed == [("fresh", "1")]

    @pytest.mark.asyncio
    async def test_admin_writes_others_owned_bank_without_claim(self, validator, monkeypatch):
        # Admins may write banks ALREADY owned by someone else (no claim; the
        # existing owner is preserved).
        set_snapshot(_fresh_snapshot(_ROWS))
        called = False

        async def _fake_claim(bank_id, caller_user_id, caller_login=None):
            nonlocal called
            called = True
            return True

        monkeypatch.setattr(validator, "_claim_bank_if_unowned", _fake_claim)
        # "secret" is owned by 999 (private); admin may still write it.
        assert (await validator.validate_bank_write(_write_ctx("secret", _ctx("1", ROLE_ADMIN)))).allowed
        assert called is False

    @pytest.mark.asyncio
    async def test_viewer_unowned_write_rejected_without_claim(self, validator, monkeypatch):
        set_snapshot(_fresh_snapshot(_ROWS))
        called = False

        async def _fake_claim(bank_id, caller_user_id, caller_login=None):
            nonlocal called
            called = True
            return True

        monkeypatch.setattr(validator, "_claim_bank_if_unowned", _fake_claim)
        # Read-only role is rejected by the role gate before any claim.
        result = await validator.validate_retain(_retain_ctx("fresh", _ctx("1024", ROLE_VIEWER)))
        assert not result.allowed
        assert called is False


# ---------------------------------------------------------------------------
# 4. HTTP half — DB-free unit tests
# ---------------------------------------------------------------------------


class TestHttpExtensionUnit:
    def test_default_unowned_banks(self):
        ext = GitHubOrgBanksHttpExtension({})
        assert ext._unowned_banks == "shared"

    def test_admin_only_unowned_banks(self):
        ext = GitHubOrgBanksHttpExtension({"github_org_unowned_banks": "admin_only"})
        assert ext._unowned_banks == "admin_only"

    def test_invalid_unowned_banks_raises(self):
        with pytest.raises(ValueError):
            GitHubOrgBanksHttpExtension({"github_org_unowned_banks": "bogus"})

    def test_request_context_bearer(self):
        ctx = GitHubOrgBanksHttpExtension._request_context("Bearer abc123")
        assert ctx.api_key == "abc123"

    def test_request_context_raw(self):
        ctx = GitHubOrgBanksHttpExtension._request_context("rawtoken")
        assert ctx.api_key == "rawtoken"

    def test_request_context_none(self):
        ctx = GitHubOrgBanksHttpExtension._request_context(None)
        assert ctx.api_key is None

    def test_get_router_is_router(self):
        ext = GitHubOrgBanksHttpExtension({})
        router = ext.get_router(MagicMock())
        assert isinstance(router, APIRouter)

    def test_get_root_router_has_routes(self):
        ext = GitHubOrgBanksHttpExtension({})
        router = ext.get_root_router(MagicMock())
        assert isinstance(router, APIRouter)
        assert len(router.routes) >= 3
        paths = {getattr(r, "path", None) for r in router.routes}
        # The bulk list route must use the hyphenated sibling path; the
        # ``banks/visibility`` form collides with the core
        # ``/v1/default/banks/{bank_id}`` route and yields 405 on GET.
        assert "/v1/default/banks-visibility" in paths
        assert "/v1/default/banks/visibility" not in paths
        assert "/v1/default/banks/{bank_id}/visibility" in paths

    def test_visibility_update_accepts_private(self):
        assert VisibilityUpdate(visibility="private").visibility == "private"

    def test_visibility_update_accepts_shared(self):
        assert VisibilityUpdate(visibility="shared").visibility == "shared"

    def test_visibility_update_rejects_public(self):
        with pytest.raises(ValidationError):
            VisibilityUpdate(visibility="public")

    def test_visibility_update_rejects_missing(self):
        with pytest.raises(ValidationError):
            VisibilityUpdate()

    # NOTE: The full PUT .../visibility behavior — the "is_owner"/"can_share"
    # fields in the response and the 401 when caller identity is missing — is
    # covered by integration tests that require a real database, since the route
    # handler primes the snapshot and writes the bank_ownership table via a pool.
    # These unit tests intentionally only exercise the DB-free request model.
