# Extensions

Extensions allow you to customize and extend Hindsight behavior without modifying core code. They enable multi-tenancy, custom authentication, additional HTTP endpoints, and operation hooks.

---

## Available Extensions

### TenantExtension

Handles multi-tenancy and API key authentication. Validates incoming requests and determines which PostgreSQL schema to use for database operations, enabling tenant isolation at the database level.

**Built-in: ApiKeyTenantExtension**

A simple implementation that validates API keys against an environment variable and uses the `public` schema for all authenticated requests.

```bash
HINDSIGHT_API_TENANT_EXTENSION=hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension
HINDSIGHT_API_TENANT_API_KEY=your-secret-key
```

**Built-in: SupabaseTenantExtension**

Validates [Supabase](https://supabase.com) JWTs and provides multi-tenant memory isolation. Each authenticated user gets their own PostgreSQL schema (`{prefix}_{user_id}`), ensuring complete data separation. Performs local JWT verification using JWKS for optimal performance (no network call per request).

```bash
HINDSIGHT_API_TENANT_EXTENSION=hindsight_api.extensions.builtin.supabase_tenant:SupabaseTenantExtension
HINDSIGHT_API_TENANT_SUPABASE_URL=https://your-project.supabase.co
# Optional - only needed for legacy HS256 projects or health check
HINDSIGHT_API_TENANT_SUPABASE_SERVICE_KEY=your-service-role-key
```

See the [source code](https://github.com/vectorize-io/hindsight/blob/main/hindsight-api-slim/hindsight_api/extensions/builtin/supabase_tenant.py) for complete configuration options and implementation details.

**Built-in: OidcTenantExtension**

Generic OpenID Connect / OAuth multi-tenancy for any standard OIDC provider (Auth0, Okta, Keycloak, Microsoft Entra ID, Google, ...). Each authenticated user gets their own PostgreSQL schema (`{prefix}_{subject}`), ensuring complete data separation. Tokens are verified locally using JWKS discovered from the provider's `/.well-known/openid-configuration` document (no network call per request).

```bash
HINDSIGHT_API_TENANT_EXTENSION=hindsight_api.extensions.builtin.oidc_tenant:OidcTenantExtension
HINDSIGHT_API_TENANT_OIDC_ISSUER=https://your-tenant.auth0.com/

# Optional
HINDSIGHT_API_TENANT_OIDC_AUDIENCE=your-api-audience        # expected `aud` claim
HINDSIGHT_API_TENANT_OIDC_JWKS_URI=https://.../jwks.json    # skip discovery
HINDSIGHT_API_TENANT_SUBJECT_CLAIM=sub                      # claim used to derive the schema
HINDSIGHT_API_TENANT_SCHEMA_PREFIX=user                     # schema prefix (default "user")
HINDSIGHT_API_TENANT_ALGORITHMS=RS256,ES256                 # allowed signing algorithms
```

The subject claim is sanitized into a valid Postgres identifier; long/opaque subjects are hashed to stay within the identifier length limit. See the [source code](https://github.com/vectorize-io/hindsight/blob/main/hindsight-api-slim/hindsight_api/extensions/builtin/oidc_tenant.py) for details.

**Built-in: GitHubTenantExtension**

A purpose-built configuration of `OidcTenantExtension` for **GitHub OAuth** that adds **team-based role assignment**. GitHub OAuth user access tokens are opaque (not JWTs), so this variant validates them against the GitHub REST API instead of JWKS.

Like the other tenant extensions, **each GitHub user gets their own isolated schema** — `{prefix}_{github_user_id}`, keyed on the immutable numeric user id. In addition, the user's GitHub **team membership** in a configured org is mapped to a Hindsight role that **gates capabilities** (the schema stays per-user):

| Role | Memory operations | Bank config |
| --- | --- | --- |
| `admin` | retain / recall / reflect | all configurable fields |
| `member` | retain / recall / reflect | a curated subset of fields |
| `viewer` | recall only (read-only) | none (read-only) |

```bash
HINDSIGHT_API_TENANT_EXTENSION=hindsight_api.extensions.builtin.github_tenant:GitHubTenantExtension
HINDSIGHT_API_TENANT_GITHUB_ORG=my-org

# Team slugs (comma-separated) mapped to each role
HINDSIGHT_API_TENANT_GITHUB_ADMIN_TEAMS=platform-admins
HINDSIGHT_API_TENANT_GITHUB_MEMBER_TEAMS=engineering,data
HINDSIGHT_API_TENANT_GITHUB_VIEWER_TEAMS=analysts

# Optional
HINDSIGHT_API_TENANT_GITHUB_DEFAULT_ROLE=viewer             # role for org members in no mapped team; empty = deny
HINDSIGHT_API_TENANT_GITHUB_API_URL=https://api.github.com  # set for GitHub Enterprise Server
HINDSIGHT_API_TENANT_GITHUB_ROLE_CACHE_TTL=300              # seconds to cache resolved roles
HINDSIGHT_API_TENANT_SCHEMA_PREFIX=user

# Companion validator that enforces read/write gating on retain/recall/reflect
HINDSIGHT_API_OPERATION_VALIDATOR_EXTENSION=hindsight_api.extensions.builtin.github_role_validator:GitHubRoleOperationValidator
```

Role enforcement is split across two hooks: bank-config write permissions are enforced by the tenant extension's `get_allowed_config_fields`, while retain/recall/reflect gating is enforced by the companion `GitHubRoleOperationValidator`. The resolved role is propagated to the validator through `RequestContext.tenant_id` (encoded as `gh_<id>:<role>`), so no extra GitHub API call is made per operation. See the [source code](https://github.com/vectorize-io/hindsight/blob/main/hindsight-api-slim/hindsight_api/extensions/builtin/github_tenant.py) for details.

For other multi-tenant setups with separate schemas per tenant (e.g., custom JWT-based auth), implement a custom `TenantExtension`.

---

### HttpExtension

Adds custom HTTP endpoints under the `/ext/` path prefix. Useful for adding domain-specific APIs that integrate with Hindsight's memory engine.

Provides two router methods:
- `get_router(memory)` — returns a FastAPI router mounted at `/ext/`
- `get_root_router(memory)` — returns a FastAPI router mounted at the application root (for well-known endpoints or other paths that must be at specific locations). Returns `None` by default.

**No built-in implementation** - implement your own to add custom endpoints.

```bash
HINDSIGHT_API_HTTP_EXTENSION=mypackage.ext:MyHttpExtension
```

---

### OperationValidatorExtension

Hooks into retain/recall/reflect operations for validation and monitoring. Use cases include:
- Rate limiting and quota enforcement
- Permission checks and content filtering
- Audit logging and usage tracking
- Custom metrics collection

**No built-in implementation** - implement your own based on your requirements.

```bash
HINDSIGHT_API_OPERATION_VALIDATOR_EXTENSION=mypackage.validators:MyValidator
```

---

### MCPExtension

Registers additional MCP (Model Context Protocol) tools on the Hindsight MCP server. Enables external packages to add custom tools without modifying core code.

**No built-in implementation** - implement your own to add custom MCP tools.

```bash
HINDSIGHT_API_MCP_EXTENSION=mypackage.mcp:MyMCPExtension
```

---

## Writing Custom Extensions

### Extension Basics

Extensions are Python classes loaded via environment variables:

```bash
HINDSIGHT_API_<TYPE>_EXTENSION=mypackage.module:MyExtensionClass
```

Configuration is passed via prefixed environment variables:

```bash
HINDSIGHT_API_<TYPE>_SOME_CONFIG=value
# Extension receives: {"some_config": "value"}
```

All extensions support lifecycle hooks:
- `on_startup()` - Called when the application starts
- `on_shutdown()` - Called when the application shuts down

Extensions have access to an `ExtensionContext` that provides:
- `run_migration(schema)` - Run database migrations for a schema
- `get_memory_engine()` - Get the MemoryEngine interface

### Example: Custom TenantExtension with JWT

```python
import jwt
from hindsight_api.extensions import TenantExtension, TenantContext, AuthenticationError

class JwtTenantExtension(TenantExtension):
    def __init__(self, config: dict[str, str]):
        super().__init__(config)
        self.jwt_secret = config.get("jwt_secret")
        if not self.jwt_secret:
            raise ValueError("HINDSIGHT_API_TENANT_JWT_SECRET is required")

    async def authenticate(self, context: RequestContext) -> TenantContext:
        token = context.api_key
        if not token:
            # Optional headers dict is forwarded in HTTP/MCP error responses
            raise AuthenticationError("Bearer token required")

        try:
            payload = jwt.decode(token, self.jwt_secret, algorithms=["HS256"])
            tenant_id = payload.get("tenant_id")
            if not tenant_id:
                raise AuthenticationError("Missing tenant_id in token")
            return TenantContext(schema_name=f"tenant_{tenant_id}")
        except jwt.InvalidTokenError as e:
            raise AuthenticationError(str(e))
```

`AuthenticationError` accepts an optional `headers` dict that is forwarded in both HTTP and MCP error responses. This is useful for returning custom headers like `WWW-Authenticate`:

```python
raise AuthenticationError(
    "Authorization required",
    headers={"WWW-Authenticate": 'Bearer realm="example"'},
)
```

### Example: Custom HttpExtension

```python
from fastapi import APIRouter
from hindsight_api.extensions import HttpExtension

class MyHttpExtension(HttpExtension):
    def get_router(self, memory: MemoryEngine) -> APIRouter:
        router = APIRouter()

        @router.get("/hello")
        async def hello():
            return {"message": "Hello from extension!"}

        @router.post("/custom/{bank_id}/action")
        async def custom_action(bank_id: str):
            # Access memory engine for database operations
            pool = await memory._get_pool()
            # ... custom logic
            return {"status": "ok"}

        return router

    def get_root_router(self, memory: MemoryEngine) -> APIRouter | None:
        """Optional: mount routes at the application root (not under /ext/)."""
        router = APIRouter()

        @router.get("/.well-known/my-metadata")
        async def metadata():
            return {"version": "1.0"}

        return router
```

Routes from `get_router` are available at `/ext/hello`, `/ext/custom/{bank_id}/action`, etc.
Routes from `get_root_router` are mounted at the app root (e.g., `/.well-known/my-metadata`).

### Example: Custom OperationValidatorExtension

```python
from hindsight_api.extensions import (
    OperationValidatorExtension,
    ValidationResult,
    PrecheckContext,
    RetainContext,
    RecallContext,
    ReflectContext,
    RetainResult,
)

class MyValidator(OperationValidatorExtension):
    # Pre-body validation (optional)
    async def precheck(self, ctx: PrecheckContext) -> ValidationResult:
        if ctx.content_length is not None and ctx.content_length > 10_000_000:
            return ValidationResult.reject("Payload is too large")
        return ValidationResult.accept()

    # Pre-operation validation (required)
    async def validate_retain(self, ctx: RetainContext) -> ValidationResult:
        # Implement your validation logic
        return ValidationResult.accept()
        # Or reject: return ValidationResult.reject("Reason")

    async def validate_recall(self, ctx: RecallContext) -> ValidationResult:
        return ValidationResult.accept()

    async def validate_reflect(self, ctx: ReflectContext) -> ValidationResult:
        return ValidationResult.accept()

    # Post-operation hooks (optional)
    async def on_retain_complete(self, result: RetainResult) -> None:
        # Log usage, update metrics, send notifications, etc.
        pass
```

`precheck` runs before the request body is read or deserialized. Its
`PrecheckContext.content_length` is the parsed `Content-Length` header as an
integer, or `None` when the header is missing or cannot be parsed (for example,
chunked transfer encoding). Use it for cheap size-aware quota or cost guards;
the full `validate_*` hooks still run after parsing and should enforce precise
per-operation limits.

#### Deferring an operation

In addition to `accept` and `reject`, a `validate_*` hook can ask the
worker to **requeue** the operation for a future time by raising
`DeferOperation`. Use this for backpressure (rate-limited upstream,
quota window not yet open, dependency warming up) — unlike a retry, it
does not increment `retry_count` or write `error_message`. The worker
sets `next_retry_at` to your `exec_date` and the task is invisible to
claim queries until that time.

```python
from datetime import datetime, timedelta, timezone

from hindsight_api.extensions import (
    DeferOperation,
    OperationValidatorExtension,
    RetainContext,
    ValidationResult,
)


class QuotaAwareValidator(OperationValidatorExtension):
    async def validate_retain(self, ctx: RetainContext) -> ValidationResult:
        if not await self._quota_available(ctx.bank_id):
            raise DeferOperation(
                exec_date=datetime.now(timezone.utc) + timedelta(minutes=5),
                reason="bank quota window exhausted",
            )
        return ValidationResult.accept()
```

`DeferOperation` is **worker-only**: do not raise it from
`validate_recall` or `validate_reflect` in synchronous HTTP request
paths — there is no queue to defer to and it will surface as a 500.

### Example: Custom MCPExtension

```python
from mcp.server.fastmcp import FastMCP
from hindsight_api.extensions import MCPExtension
from hindsight_api.engine import MemoryEngine

class MyMCPExtension(MCPExtension):
    async def register_tools(self, mcp: FastMCP, memory: MemoryEngine) -> None:
        @mcp.tool()
        async def custom_search(query: str) -> str:
            """Custom MCP tool for specialized search."""
            # Access memory engine for operations
            pool = await memory._get_pool()
            # ... custom logic
            return f"Results for: {query}"
```

---

## Deploying Custom Extensions

### With Docker

Mount your extension package as a volume and set the environment variable:

```yaml
# docker-compose.yml
services:
  hindsight-api:
    image: vectorize/hindsight-api:latest
    volumes:
      - ./my_extensions:/app/my_extensions
    environment:
      - HINDSIGHT_API_TENANT_EXTENSION=my_extensions.auth:JwtTenantExtension
      - HINDSIGHT_API_TENANT_JWT_SECRET=${JWT_SECRET}
      - PYTHONPATH=/app
```

Or build a custom image with your extensions:

```dockerfile
FROM vectorize/hindsight-api:latest
COPY my_extensions /app/my_extensions
ENV PYTHONPATH=/app
```

### Bare Metal

Install your extension package in the same Python environment as Hindsight:

```bash
# Install Hindsight
pip install hindsight-api

# Install your extension package
pip install ./my-extensions
# or
pip install my-extensions-package

# Configure
export HINDSIGHT_API_TENANT_EXTENSION=my_extensions.auth:JwtTenantExtension
export HINDSIGHT_API_TENANT_JWT_SECRET=your-secret

# Run
hindsight-api
```

---

## Shared memory banks

Shared memory banks let multiple users read from and write to the same set of banks — for example, a shared codebase bank that every engineer on a team can query and contribute to. This is an opt-in feature layered on top of the existing per-user schema isolation model.

### Concept

Hindsight isolates each tenant into its own PostgreSQL schema. Historically, each user got exactly one private schema (e.g. `user_<subject>`). Shared schemas are **additional** schemas that multiple users access simultaneously (read-many / write-many). A user now sees:

- Their **private banks** — addressed by bare `bank_id` (e.g. `api-v2`)
- The **banks in any shared schema they're a member of** — addressed by qualified `schema/bank_id` (e.g. `shared_frontend/api-v2`)

Shared schemas are structurally identical to private schemas: same table set, same migrations, same worker processing. The only difference is that membership is shared across users rather than being one-to-one.

### Registry

Shared schemas are tracked in a global `public.shared_schemas` table:

| Column | Type | Description |
| --- | --- | --- |
| `schema_name` | TEXT (PK) | PostgreSQL schema name, e.g. `shared_my_codebase` |
| `display_name` | TEXT | Human-friendly label |
| `created_by` | TEXT | Opaque identifier of the admin who created it |
| `created_at` | TIMESTAMPTZ | Creation timestamp |

**Naming rule:** shared schema names must start with `shared_` and contain only lowercase letters, digits, and underscores (e.g. `shared_frontend`, `shared_platform_infra`). This is enforced at creation time and validated at extension startup.

**Membership is not stored here.** The registry only tracks which shared schemas exist. Who may access each schema is resolved at request time by the tenant extension from the identity provider (GitHub team membership / OIDC group claims). This keeps the registry simple and avoids a second source of truth for group membership.

### Qualified bank IDs

| Format | Meaning |
| --- | --- |
| `api-v2` | Bank `api-v2` in the caller's own private schema |
| `shared_frontend/api-v2` | Bank `api-v2` in the `shared_frontend` schema |

When using qualified IDs in REST paths, URL-encode the `/` separator:

```
GET /v1/default/banks/shared_frontend%2Fapi-v2/memories/recall
```

`GET /v1/default/banks` returns private banks with bare IDs and shared banks with `schema/`-qualified IDs, so callers can discover all accessible banks from a single call.

### Per-extension configuration

#### OidcTenantExtension

The OIDC extension resolves shared schema membership from a **JWT group claim**. Two new config keys control this:

| Config key | Env var | Default | Description |
| --- | --- | --- | --- |
| `shared_group_claim` | `HINDSIGHT_API_TENANT_SHARED_GROUP_CLAIM` | `groups` | JWT claim that carries the user's group memberships |
| `shared_schema_map` | `HINDSIGHT_API_TENANT_SHARED_SCHEMA_MAP` | _(none)_ | Comma-separated `group:shared_schema` pairs |

```bash
HINDSIGHT_API_TENANT_EXTENSION=hindsight_api.extensions.builtin.oidc_tenant:OidcTenantExtension
HINDSIGHT_API_TENANT_OIDC_ISSUER=https://your-tenant.auth0.com/

# Map OIDC groups to shared schemas
HINDSIGHT_API_TENANT_SHARED_GROUP_CLAIM=groups          # claim name in the JWT (default: groups)
HINDSIGHT_API_TENANT_SHARED_SCHEMA_MAP=frontend-team:shared_frontend,platform:shared_platform_infra
```

The `groups` claim may be a JSON array of strings or a space/comma-delimited string. A user whose token contains `"groups": ["frontend-team", "platform"]` would gain access to both `shared_frontend` and `shared_platform_infra` in addition to their private schema.

#### GitHubTenantExtension

The GitHub extension resolves shared schema membership from **GitHub team slugs** (within the configured org). The same `shared_schema_map` env var is used, but the keys are team slugs instead of group names:

| Config key | Env var | Default | Description |
| --- | --- | --- | --- |
| `shared_schema_map` | `HINDSIGHT_API_TENANT_SHARED_SCHEMA_MAP` | _(none)_ | Comma-separated `team-slug:shared_schema` pairs |

```bash
HINDSIGHT_API_TENANT_EXTENSION=hindsight_api.extensions.builtin.github_tenant:GitHubTenantExtension
HINDSIGHT_API_TENANT_GITHUB_ORG=my-org

# Existing role mapping (unchanged)
HINDSIGHT_API_TENANT_GITHUB_ADMIN_TEAMS=platform-admins
HINDSIGHT_API_TENANT_GITHUB_MEMBER_TEAMS=engineering,data
HINDSIGHT_API_TENANT_GITHUB_VIEWER_TEAMS=analysts

# Map GitHub team slugs to shared schemas
HINDSIGHT_API_TENANT_SHARED_SCHEMA_MAP=frontend:shared_frontend,platform-infra:shared_platform_infra
```

Team slugs are matched case-insensitively. A user who is a member of the `frontend` team in `my-org` gains access to `shared_frontend`. Team membership is fetched from the GitHub API alongside role resolution and cached for the same TTL (`HINDSIGHT_API_TENANT_GITHUB_ROLE_CACHE_TTL`).

> **Note:** The `shared_schema_map` config key is the same for both extensions. The env var prefix rule applies: `HINDSIGHT_API_TENANT_SHARED_SCHEMA_MAP` → config key `shared_schema_map` (prefix stripped, lowercased).

### Access model

| Actor | Capability |
| --- | --- |
| Any member of a shared schema | Read (recall/reflect) and write (retain) to all banks in that schema |
| `admin` role (GitHub) | May create, deregister, and destructively drop (delete all data of) shared schemas via the API |
| Non-admin / `member` / `viewer` | Cannot create, deregister, or drop shared schemas |

Shared schemas follow the same read-many/write-many model: there is no read-only membership tier at the schema level. If you need read-only access to a shared schema, gate it at the `OperationValidatorExtension` layer.

Creating, deregistering, or destructively dropping a shared schema is an admin-only operation. For `GitHubTenantExtension`, "admin" means the user's resolved role is `admin` (i.e. they are in one of the `HINDSIGHT_API_TENANT_GITHUB_ADMIN_TEAMS`). For `OidcTenantExtension`, admin gating is enforced by the API layer based on the caller's resolved role.

### API endpoints

These endpoints manage the shared schema registry. They are available when a tenant extension that supports shared schemas is configured.

#### `GET /v1/shared/schemas`

Returns the shared schemas visible to the caller — their private schema plus any shared schemas they are a member of. Also indicates whether the caller may create new shared schemas.

**Response:**
```json
{
  "schemas": [
    {
      "schema_name": "shared_frontend",
      "display_name": "Frontend Codebase",
      "created_by": "gh_12345678:admin",
      "created_at": "2026-07-01T10:00:00Z"
    }
  ],
  "can_create": true
}
```

`can_create` is `true` when the caller has the `admin` role (or equivalent). Non-admin callers see `false`.

#### `POST /v1/shared/schemas` _(admin only)_

Registers and provisions a new shared schema. The `schema_name` must start with `shared_`.

**Request:**
```json
{
  "schema_name": "shared_frontend",
  "display_name": "Frontend Codebase"
}
```

**Response:** the created registry record (same shape as a single item in the `GET` response).

Returns `403 Forbidden` for non-admin callers. Returns `400 Bad Request` if the name does not start with `shared_` or is otherwise invalid.

#### `DELETE /v1/shared/schemas/{schema}` _(admin only)_

Deregisters a shared schema — removes the `public.shared_schemas` row. **This does not drop the underlying PostgreSQL schema or its data.** The schema becomes invisible and inaccessible via qualified bank IDs, but its tables and rows are preserved (e.g. so it can be re-registered later). To also erase the data, use the destructive endpoint below.

Returns `404 Not Found` if the schema is not registered. Returns `403 Forbidden` for non-admin callers.

#### `DELETE /v1/shared/schemas/{schema}/data` _(admin only)_

**Destructive and irreversible.** Deregisters the shared schema **and** drops the underlying PostgreSQL schema with `DROP SCHEMA ... CASCADE`, permanently deleting every bank, memory, and document stored in it. Deregistration and the schema drop run in a single transaction.

**Response:**
```json
{ "schema_name": "shared_frontend", "dropped": true }
```

Guardrails: the target name is re-validated against the `shared_` prefix + identifier rules before any drop, so this can never affect `public` or a private `user_*` schema. Returns `404 Not Found` if the schema is not registered, `403 Forbidden` for non-admin callers, and `400 Bad Request` if the name is not a valid shared schema.

In the Control Plane UI this is the second step of a deliberate two-step flow: operators first **Deregister**, and the destructive **Delete data** action requires typing the exact schema name to confirm.

### Worker behavior

No configuration is needed. The worker discovers schemas with pending work by scanning `pg_namespace` for schemas that have pending tasks, so shared schemas are serviced automatically alongside private schemas. There is no distinction in worker processing between a private schema and a shared schema.

### Backward compatibility

Shared schemas are entirely opt-in:

- Extensions that only set `TenantContext.schema_name` (e.g. `ApiKeyTenantExtension`, `SupabaseTenantExtension`, single-tenant custom extensions) are **unaffected**. `readable_schemas` and `writable_schemas` default to empty sets, so no shared-schema logic is triggered.
- For `OidcTenantExtension` and `GitHubTenantExtension`, if `HINDSIGHT_API_TENANT_SHARED_SCHEMA_MAP` is not set, no shared schemas are resolved and behavior is identical to before.
- Existing bank IDs (bare, unqualified) continue to refer to the caller's private schema with no change.

### How it fits together

```
User authenticates
  └─ TenantExtension.authenticate()
       ├─ Resolves private schema (e.g. user_12345)
       ├─ Resolves shared schemas from identity provider
       │    OIDC: reads JWT groups claim → maps via shared_schema_map
       │    GitHub: fetches team slugs → maps via shared_schema_map
       └─ Returns TenantContext(
              schema_name="user_12345",
              readable_schemas={"shared_frontend"},
              writable_schemas={"shared_frontend"},
          )

Caller requests bank "shared_frontend/api-v2"
  └─ API layer parses schema/bank_id → schema="shared_frontend", bank_id="api-v2"
  └─ Checks schema is in TenantContext.readable_schemas (or writable_schemas for writes)
  └─ Executes query against shared_frontend schema

Worker polls for work
  └─ Scans pg_namespace for schemas with pending tasks
  └─ Processes shared_frontend tasks alongside user_* tasks — no special config needed
```

---

## Contributing Extensions

Custom extensions that solve common use cases are welcome contributions to the Hindsight project. If you've built an extension for:

- Authentication providers (OAuth, SAML, API gateways)
- Rate limiting or quota management
- Audit logging integrations
- Metrics exporters (Datadog, New Relic, etc.)
- Custom HTTP endpoints for specific platforms

Consider contributing it to the `hindsight_api.extensions.builtin` package. Open an issue or pull request on [GitHub](https://github.com/vectorize-io/hindsight) to discuss your extension.
