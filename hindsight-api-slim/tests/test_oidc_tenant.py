"""Tests for the generic OIDC Tenant Extension."""

from unittest.mock import AsyncMock, MagicMock

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWK

from hindsight_api.extensions.builtin.oidc_tenant import (
    _MAX_IDENTIFIER_LENGTH,
    Identity,
    OidcTenantExtension,
)
from hindsight_api.extensions.tenant import AuthenticationError, SharedSchema, Tenant, TenantContext, TenantExtension
from hindsight_api.models import RequestContext


def _make_extension(**overrides) -> OidcTenantExtension:
    config = {"oidc_issuer": "https://issuer.example.com"}
    config.update(overrides)
    return OidcTenantExtension(config)


class TestInit:
    def test_init_valid(self):
        ext = _make_extension()
        assert ext.issuer == "https://issuer.example.com"
        assert ext.subject_claim == "sub"
        assert ext.schema_prefix == "user"
        assert ext.algorithms == ["RS256", "ES256"]

    def test_strips_trailing_slash(self):
        ext = _make_extension(oidc_issuer="https://issuer.example.com/")
        assert ext.issuer == "https://issuer.example.com"

    def test_requires_issuer_or_jwks(self):
        with pytest.raises(ValueError, match="HINDSIGHT_API_TENANT_OIDC_ISSUER is required"):
            OidcTenantExtension({})

    def test_jwks_uri_alone_is_enough(self):
        ext = OidcTenantExtension({"oidc_jwks_uri": "https://issuer.example.com/jwks"})
        assert ext.jwks_uri == "https://issuer.example.com/jwks"

    def test_custom_algorithms(self):
        ext = _make_extension(algorithms="RS256, HS256")
        assert ext.algorithms == ["RS256", "HS256"]

    def test_rejects_bad_schema_prefix(self):
        with pytest.raises(ValueError, match="Invalid schema_prefix"):
            _make_extension(schema_prefix='"; DROP TABLE')

    def test_is_tenant_extension(self):
        assert isinstance(_make_extension(), TenantExtension)


class TestSchemaDerivation:
    def test_simple_subject(self):
        ext = _make_extension()
        assert ext._schema_for_subject("abc123") == "user_abc123"

    def test_sanitizes_unsafe_chars(self):
        ext = _make_extension()
        # UUID-style hyphens and provider "|" separators become underscores.
        assert ext._schema_for_subject("auth0|a1b2-c3") == "user_auth0_a1b2_c3"

    def test_custom_prefix(self):
        ext = _make_extension(schema_prefix="tenant")
        assert ext._schema_for_subject("xyz") == "tenant_xyz"

    def test_long_subject_is_hashed_within_identifier_limit(self):
        ext = _make_extension()
        schema = ext._schema_for_subject("x" * 200)
        assert len(schema) <= _MAX_IDENTIFIER_LENGTH
        assert schema.startswith("user_")
        # Deterministic.
        assert schema == ext._schema_for_subject("x" * 200)


class TestAuthenticate:
    @pytest.mark.asyncio
    async def test_missing_token_raises_with_www_authenticate(self):
        ext = _make_extension()
        ext._http_client = AsyncMock()
        with pytest.raises(AuthenticationError) as exc:
            await ext.authenticate(RequestContext(api_key=None))
        assert "WWW-Authenticate" in exc.value.headers

    @pytest.mark.asyncio
    async def test_short_token_rejected(self):
        ext = _make_extension()
        ext._http_client = AsyncMock()
        with pytest.raises(AuthenticationError, match="Invalid token format"):
            await ext.authenticate(RequestContext(api_key="short"))

    @pytest.mark.asyncio
    async def test_authenticate_provisions_schema_once(self):
        ext = _make_extension()
        ext._http_client = AsyncMock()
        ext._resolve_identity = AsyncMock(return_value=Identity(subject="user-42"))
        mock_ctx = MagicMock()
        mock_ctx.run_migration = AsyncMock()
        ext.set_context(mock_ctx)

        token = "a" * 40
        result = await ext.authenticate(RequestContext(api_key=token))
        assert isinstance(result, TenantContext)
        assert result.schema_name == "user_user_42"
        mock_ctx.run_migration.assert_awaited_once_with("user_user_42")

        # Second call must not re-run migration.
        await ext.authenticate(RequestContext(api_key=token))
        assert mock_ctx.run_migration.await_count == 1

    @pytest.mark.asyncio
    async def test_jwks_jwt_verification_end_to_end(self):
        """Real RSA-signed JWT is verified locally and yields the subject schema."""
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = pyjwt.encode(
            {"sub": "user-77", "iss": "https://issuer.example.com", "aud": "my-api"},
            private_key,
            algorithm="RS256",
            headers={"kid": "kid-1"},
        )
        jwk = PyJWK.from_json(pyjwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))

        ext = _make_extension(oidc_audience="my-api")
        ext._http_client = AsyncMock()
        ext._jwks_keys = {"kid-1": jwk}
        import time as _t

        ext._jwks_last_fetched = _t.monotonic()
        mock_ctx = MagicMock()
        mock_ctx.run_migration = AsyncMock()
        ext.set_context(mock_ctx)

        result = await ext.authenticate(RequestContext(api_key=token))
        assert result.schema_name == "user_user_77"

    @pytest.mark.asyncio
    async def test_expired_jwt_rejected(self):
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = pyjwt.encode(
            {"sub": "u", "iss": "https://issuer.example.com", "exp": 1},
            private_key,
            algorithm="RS256",
            headers={"kid": "kid-1"},
        )
        jwk = PyJWK.from_json(pyjwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
        ext = _make_extension()
        ext._http_client = AsyncMock()
        ext._jwks_keys = {"kid-1": jwk}
        import time as _t

        ext._jwks_last_fetched = _t.monotonic()
        ext.set_context(MagicMock())
        with pytest.raises(AuthenticationError, match="expired"):
            await ext.authenticate(RequestContext(api_key=token))

    @pytest.mark.asyncio
    async def test_list_tenants_reflects_initialized(self):
        ext = _make_extension()
        ext._http_client = AsyncMock()
        ext._resolve_identity = AsyncMock(return_value=Identity(subject="u1"))
        mock_ctx = MagicMock()
        mock_ctx.run_migration = AsyncMock()
        ext.set_context(mock_ctx)

        await ext.authenticate(RequestContext(api_key="a" * 40))
        tenants = await ext.list_tenants()
        assert tenants == [Tenant(schema="user_u1")]

    @pytest.mark.asyncio
    async def test_no_shared_schemas_by_default(self):
        """Without shared_schema_map, readable/writable schemas are empty."""
        ext = _make_extension()
        ext._http_client = AsyncMock()
        ext._resolve_identity = AsyncMock(return_value=Identity(subject="u1", claims={}))
        mock_ctx = MagicMock()
        mock_ctx.run_migration = AsyncMock()
        ext.set_context(mock_ctx)

        result = await ext.authenticate(RequestContext(api_key="a" * 40))
        assert result.readable_schemas == set()
        assert result.writable_schemas == set()

    @pytest.mark.asyncio
    async def test_tenant_context_readable_writable_populated(self):
        """authenticate() populates readable_schemas and writable_schemas from group claims."""
        ext = _make_extension(shared_schema_map="eng:shared_eng,data:shared_data")
        ext._http_client = AsyncMock()
        ext._resolve_identity = AsyncMock(
            return_value=Identity(subject="u1", claims={"groups": ["eng", "other"]})
        )
        mock_ctx = MagicMock()
        mock_ctx.run_migration = AsyncMock()
        ext.set_context(mock_ctx)

        result = await ext.authenticate(RequestContext(api_key="a" * 40))
        assert result.readable_schemas == {"shared_eng"}
        assert result.writable_schemas == {"shared_eng"}

    @pytest.mark.asyncio
    async def test_list_shared_schemas_after_authenticate(self):
        """list_shared_schemas returns SharedSchema objects matching the cache."""
        ext = _make_extension(shared_schema_map="eng:shared_eng,data:shared_data")
        ext._http_client = AsyncMock()
        ext._resolve_identity = AsyncMock(
            return_value=Identity(subject="u1", claims={"groups": ["eng", "data"]})
        )
        mock_ctx = MagicMock()
        mock_ctx.run_migration = AsyncMock()
        ext.set_context(mock_ctx)

        await ext.authenticate(RequestContext(api_key="a" * 40))
        schemas = await ext.list_shared_schemas(RequestContext(api_key="a" * 40))
        schema_names = {s.schema for s in schemas}
        assert schema_names == {"shared_eng", "shared_data"}
        assert all(isinstance(s, SharedSchema) for s in schemas)
        assert all(s.writable for s in schemas)

    @pytest.mark.asyncio
    async def test_list_shared_schemas_empty_when_no_groups_match(self):
        """list_shared_schemas returns [] when user has no matching groups."""
        ext = _make_extension(shared_schema_map="eng:shared_eng")
        ext._http_client = AsyncMock()
        ext._resolve_identity = AsyncMock(
            return_value=Identity(subject="u1", claims={"groups": ["unrelated"]})
        )
        mock_ctx = MagicMock()
        mock_ctx.run_migration = AsyncMock()
        ext.set_context(mock_ctx)

        await ext.authenticate(RequestContext(api_key="a" * 40))
        schemas = await ext.list_shared_schemas(RequestContext(api_key="a" * 40))
        assert schemas == []


class TestSharedSchemaMapParsing:
    def test_parses_valid_map(self):
        ext = _make_extension(shared_schema_map="eng:shared_eng,data:shared_data")
        assert ext.shared_schema_map == {"eng": "shared_eng", "data": "shared_data"}

    def test_empty_map(self):
        ext = _make_extension()
        assert ext.shared_schema_map == {}

    def test_skips_malformed_no_colon(self):
        ext = _make_extension(shared_schema_map="eng_shared_eng,data:shared_data")
        # "eng_shared_eng" has no colon → skipped
        assert ext.shared_schema_map == {"data": "shared_data"}

    def test_skips_empty_entries(self):
        ext = _make_extension(shared_schema_map=",data:shared_data,")
        assert ext.shared_schema_map == {"data": "shared_data"}

    def test_rejects_invalid_schema_name(self):
        with pytest.raises(ValueError, match="shared_"):
            _make_extension(shared_schema_map="eng:not_a_shared_schema")

    def test_rejects_schema_without_prefix(self):
        with pytest.raises(ValueError):
            _make_extension(shared_schema_map="eng:user_eng")


class TestGroupsFromClaims:
    def test_list_claim(self):
        ext = _make_extension()
        groups = ext._groups_from_claims({"groups": ["eng", "data"]})
        assert groups == {"eng", "data"}

    def test_space_delimited_string(self):
        ext = _make_extension()
        groups = ext._groups_from_claims({"groups": "eng data"})
        assert groups == {"eng", "data"}

    def test_comma_delimited_string(self):
        ext = _make_extension()
        groups = ext._groups_from_claims({"groups": "eng,data"})
        assert groups == {"eng", "data"}

    def test_missing_claim(self):
        ext = _make_extension()
        assert ext._groups_from_claims({}) == set()

    def test_custom_claim_name(self):
        ext = _make_extension(shared_group_claim="roles")
        groups = ext._groups_from_claims({"roles": ["admin", "member"]})
        assert groups == {"admin", "member"}

    def test_none_claim_value(self):
        ext = _make_extension()
        assert ext._groups_from_claims({"groups": None}) == set()
