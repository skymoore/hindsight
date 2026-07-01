# Team Access Model (GitHub, single-org, extension-only)

Status: **design** — target branch `feature/team-org-extensions` (base: PR #2484
`feat/tenant-extensions`, commit `195b8d98d`).

This document specifies an opt-in **team** access model for self-hosted
Hindsight, implemented **entirely in extensions** with **zero changes to the
memory engine** (and, in fact, zero changes to any core file). One deployment
serves **one GitHub organization**; every member of that org shares one tenant
schema, and their banks are partitioned into **private** (owner-only) and
**shared** (whole-org) by an extension-owned table. Roles come from GitHub team
membership. It is the GitHub-native, stateless response to issue #2235 (keep the
core focused on memory operations; put authorization in a wrapper/extension
layer; make it opt-in), and stays architecturally compatible with PR #2262.

> **Scope decision:** multi-org / per-request org switching is **out**. A
> deployment is bound to a single configured org; there is no
> `X-Hindsight-Org-Id` header, no org switcher, and no `/api/me` org selection.
> This keeps the model entirely inside extension slots — no `RequestContext` or
> `get_request_context` changes are needed.

## Goals & non-goals

Goals:

- One Hindsight deployment serves **one** GitHub organization. Every member of
  that org shares a single tenant **schema** (`{prefix}_{org_id}`). Bank ids stay
  bare (single path segment). There is **no cross-schema addressing**.
- **Private and shared banks coexist in the one org schema.** A user's banks are
  private by default; the owner (or an org admin) explicitly shares a bank to the
  team. The bank list shows the caller's private banks **plus** all shared banks
  in one view.
- All authorization (roles, allowed operations, bank ownership/visibility) is
  enforced in an `OperationValidatorExtension`. The engine is untouched.
- Membership and roles are derived **statelessly from GitHub** (team API) at
  request time, cached by a short TTL.

Non-goals (explicitly out of scope for v1):

- **Multi-org / multi-tenant in one deployment.** One deployment = one org.
- Memory-level / row-level ACLs. Visibility is at the **bank** granularity, not
  the individual-memory granularity, so recall never needs per-row filtering.
- Sharing a bank to a *subset* of the org (per-user grants). v1 visibility is
  binary: private-to-owner or shared-to-whole-org.
- Invites, scoped-API-key issuance UI (that is the Supabase profile's territory
  in #2262).
- Any Supabase dependency.

### The one structural addition

Everything ships in **extensions only** — verified against the base commit, with
**no edits** to `hindsight_api/engine/**`, `server.py`, `main.py`,
`operation_validator.py`, or `memory_engine.py`. The feature is delivered as
**three cooperating extension slots**:

| Env var | Class | Responsibility |
|---------|-------|----------------|
| `HINDSIGHT_API_TENANT_EXTENSION`             | `GitHubOrgTenantExtension`        | org → schema selection, role, `CallerPolicy` |
| `HINDSIGHT_API_OPERATION_VALIDATOR_EXTENSION`| `GitHubOrgAuthorizationExtension` | enforce role + per-bank visibility on every core route |
| `HINDSIGHT_API_HTTP_EXTENSION`               | `GitHubOrgBanksHttpExtension`     | own the `bank_ownership` table + share/unshare routes + DB access |

The only new persisted state is an **extension-owned** table, `bank_ownership`,
created and maintained by the HTTP extension in each org schema (via
`memory._get_pool()` + `fq_table()` — the sanctioned extension DB escape hatch).
No changes to the core `banks` table, no Supabase, no cross-schema state.

Why three slots: only the `HttpExtension` slot can register routes **and** reach
the database; only the `OperationValidatorExtension` slot's hooks run on the
existing core routes (they are invoked inside the engine, so they cover recall,
list, get-profile, update, delete, first-write, etc.). The validator has no DB
handle, so the two halves cooperate through a small **process-local cache**
(details under *Enforcement wiring*). This mirrors the existing
`github_tenant` → `github_role_validator` cooperation pattern.

## Why this differs from the previous approach

The earlier attempt (tagged `wip/shared-banks-engine-approach`) introduced a
private-primary + `shared_*` overlay with qualified `schema/bank` addressing and
embedded membership resolution **inside `memory_engine.py`** (contextvars,
`resolve_bank_target`, shadow annotations — ~735 extra engine lines). That
conflicts with the maintainers' stated boundary in #2235 and collides heavily
with PR #2262. This model drops cross-schema addressing entirely and keeps the
engine as-is.

## Architecture

```
                 ┌─────────────────────────── request ───────────────────────────┐
 Authorization: Bearer <gh_token>       (no org header — org is fixed by config)
                 └────────────────────────────────┬───────────────────────────────┘
                                                    │
                        ┌──────────────────────────▼──────────────────────────┐
                        │ GitHubOrgTenantExtension.authenticate(ctx)           │
                        │  - validate token (GET /user) → user_id              │
                        │  - schema_name = f"{prefix}_{org_id}" (org fixed)    │
                        │  - resolve role in the org (team membership)         │
                        │  - encode tenant_id = gh_<uid>:<role>                │
                        │  → TenantContext(schema_name)                        │
                        └──────────────────────────┬──────────────────────────┘
                                                    │  (ctx.tenant_id = gh_<uid>:<role>)
                        ┌──────────────────────────▼──────────────────────────┐
                        │ GitHubOrgAuthorizationExtension (OperationValidator) │
                        │  - reuse CallerPolicy from ctx (no 2nd GH call)      │
                        │  - validate_retain/reflect  → role write gate        │
                        │  - validate_recall          → allow all roles        │
                        │  - validate_bank_read/write → per-bank ownership +   │
                        │                               visibility (reads cache)│
                        │  - filter_bank_list         → hide others' privates  │
                        └───────────────▲──────────────────────┬───────────────┘
                                        │ reads                 │
                     process-local cache│ {(schema,tenant)→      │
                        ┌───────────────┴──────────┐  owned/shared}
                        │ GitHubOrgBanksHttpExt     │            │
                        │  (HINDSIGHT_API_HTTP_...)  │ primes ▲   │
                        │  GET/PUT .../visibility    │─────────┘   │
                        │  owns bank_ownership table │             │
                        │  (memory._get_pool + fq_table)           │
                        └──────────────────────────┐              │
                                                   │              │
                        ┌──────────────────────────▼──────────────▼───────────┐
                        │ MemoryEngine (UNCHANGED)                             │
                        │  operates on TenantContext.schema_name only          │
                        │  (invokes the validator hooks on every core route)   │
                        └──────────────────────────────────────────────────────┘
```

### Schema selection: org = tenant = schema (fixed)

The current `GitHubTenantExtension` derives the schema from the **user** id
(`{prefix}_{github_user_id}`), so every user is isolated in their own schema.
This model derives the schema from the **configured org** id
(`{prefix}_{github_org_id}`) — a **constant** for the deployment, resolved once at
startup from the configured org login. Every member of the org shares that single
schema. Within it, banks are partitioned into **private** (owner-only) and
**shared** (whole-org) by the `bank_ownership` table — see *Bank ownership &
visibility* below. No qualified ids, no overlay, no engine involvement.

A user's **private** banks and the org's **shared** banks therefore live side by
side in the same schema and appear together in one bank list — the "private +
shared in the same deployment" case from issue #2235.

Authentication (in `authenticate`), all inside the tenant extension, reading only
`RequestContext.api_key` (the bearer token) exactly like today's
`GitHubTenantExtension` — **no request header is read**:

1. Validate the token via `GET /user`; capture the immutable numeric `user_id`.
2. `schema_name = f"{prefix}_{org_id}"`, where `org_id` is the fixed configured
   org's numeric id (resolved once at startup from `..._GITHUB_ORG`).
3. Resolve the caller's role from their team membership **in that org** (reuse the
   existing `_resolve_role`/`_fetch_user_team_slugs` logic).
4. If the user is not a member of the org (no eligible team, no default role) →
   `403`, exactly as `GitHubTenantExtension` does today.
5. Encode `RequestContext.tenant_id = gh_<user_id>:<role>` (unchanged
   `encode_tenant_id` scheme) so the validator needs no second GitHub call. The
   `user_id` and `role` are all the downstream cache/validator need.

### Roles (unchanged semantics)

Team-derived roles `admin` / `member` / `viewer`, resolved from the user's team
membership **within the active org**, using the existing
`admin_teams`/`member_teams`/`viewer_teams` config and `_resolve_role` logic.
Capability mapping is unchanged:

- `admin`  — full access; may read/write/share **any** bank in the org
  (private or shared); all bank-config fields writable.
- `member` — read/write their **own** private banks and all **shared** banks;
  may create banks and share/unshare banks they own; limited config fields.
- `viewer` — read-only on shared banks (and their own private banks, if any);
  retain/reflect rejected.

### Authorization extension (`CallerPolicy`)

`GitHubOrgTenantExtension.authenticate` builds a `CallerPolicy` dataclass and
stashes it on `RequestContext` (new optional field, defaults `None` — backward
compatible). `GitHubOrgAuthorizationExtension` reuses it, falling back to parsing
`tenant_id` if absent (mirrors #2262's "resolve once, reuse" pattern).

```python
@dataclass
class CallerPolicy:
    user_id: str
    role: str                    # admin | member | viewer
```

Since the schema is fixed and the org constant, `CallerPolicy` needs only the
caller's `user_id` (for ownership checks) and `role` (for capability gating).
Both are recoverable from `tenant_id` (`gh_<user_id>:<role>`), so the validator
can operate purely off `tenant_id` even if `CallerPolicy` is absent.

Enforcement uses existing `OperationValidatorExtension` hooks already present at
the base — `validate_retain`, `validate_reflect`, `validate_recall`,
`validate_bank_read`, `validate_bank_write`, and the `BankListContext` filter —
so no new engine hook is required.

## Bank ownership & visibility

Private/shared coexistence lives in one table per org schema, owned by the HTTP
extension (created lazily with `CREATE TABLE IF NOT EXISTS` on first use, via
`fq_table("bank_ownership")` so it lands in the active org schema):

```sql
CREATE TABLE IF NOT EXISTS bank_ownership (
    bank_id       text PRIMARY KEY,           -- matches banks.bank_id in this schema
    owner_user_id text NOT NULL,              -- GitHub numeric user id of the creator
    visibility    text NOT NULL DEFAULT 'private',  -- 'private' | 'shared'
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
```

The core `banks` table is **not** modified.

### How a bank is "created" and how ownership is recorded

There is **no `create_bank` endpoint** in Hindsight — banks are created *lazily*
on first write (first `retain`, `update_bank`, etc.). So "create a bank" from the
user's perspective is "first write to a new `bank_id`". Ownership is recorded by
the **first-writer-wins** rule enforced in the operation validator:

- **First write to an unowned `bank_id`** (any write path — `retain`, `reflect`,
  `update_bank`, or the CP "New bank" action): the writing caller becomes the
  **owner** and the bank is created **private**. The validator performs an
  atomic claim before allowing the write:

  ```sql
  INSERT INTO bank_ownership (bank_id, owner_user_id, visibility)
  VALUES ($1, $2, 'private')
  ON CONFLICT (bank_id) DO NOTHING
  ```

  This is race-safe on the `bank_ownership` primary key: if two callers write a
  brand-new `bank_id` concurrently, exactly one wins ownership; the loser is
  then denied write (it is neither owner nor admin of a now-private bank) and
  gets a `403`. This closes the silent-collision hole where two users naming
  their bank the same thing (e.g. an agent defaulting to `bank_id="opencode"`)
  would otherwise share one bank and read/write each other's memories.

  A known caller identity is required (parsed from `tenant_id`); a request
  without one is denied rather than writing an empty owner.

- **Explicit claim / share via the CP**: `PUT /v1/default/banks/{bank_id}/visibility`.
  `{"visibility":"private"}` claims/creates ownership up front (same insert as
  above); `{"visibility":"shared"}` shares an owned bank with the whole org.
  Allowed for the bank's **owner** or an **admin**.
- **Sharing grants read, not write**: a shared bank is readable by the whole org,
  but only its owner (or an admin) may write it.
- **`..._UNOWNED_BANKS` no longer gates writes.** Because the first writer always
  claims the bank private, the policy only affects the defensive **read/list**
  fallback for a bank row that somehow exists with *no* ownership entry (e.g. a
  bank created out-of-band, or before this profile was enabled). `shared` = show
  it to the org; `admin_only` = hide it from non-admins.
- **Delete**: there is no delete hook on the HTTP side, and the priming pass only
  **reads** the `bank_ownership` table — it never mutates it. A deleted bank's
  stale ownership row is harmless (it references a `bank_id` the engine no longer
  serves) and is simply left in place until the id is reclaimed by the next
  first writer.

### Enforcement wiring (how the validator reads ownership without a DB handle)

The `OperationValidatorExtension` hooks run on every core route but receive **no**
database handle. The HTTP extension (which does have DB access) primes a
**process-local cache**. Because there is exactly one schema, the whole-org
`bank_ownership` snapshot is shared; the cache is keyed by `user_id` for the
per-caller `owned` set (derived from the shared snapshot):

```python
# module-global, shared between the HTTP extension and the validator extension
# (same-process cooperation pattern as github_tenant → github_role_validator)

_ORG_OWNERSHIP: OrgSnapshot | None      # whole-org bank_ownership, TTL ~30s
# OrgSnapshot = {rows: dict[bank_id -> (owner_user_id, visibility)], fetched_at: float}
```

The validator computes, for the current caller (`user_id`, `role` parsed from
`tenant_id`):

- `shared`  = `{bank_id | rows[bank_id].visibility == 'shared'}`
- `owned`   = `{bank_id | rows[bank_id].owner_user_id == user_id}`
- `is_admin` = `role == 'admin'`

Priming & freshness:

- The HTTP extension refreshes `_ORG_OWNERSHIP` on any request it handles and via
  the lightweight `GET .../visibility` endpoints the CP calls on load; a stale
  snapshot (older than TTL) triggers a refresh.
- **Cache miss / expired-with-no-refresher ⇒ fail closed** on `validate_bank_read`
  and `validate_bank_write` (reject) so a private bank can never leak before the
  snapshot is warm. To avoid a hard dependency on the CP priming, the validator
  MAY also be given the pool lazily: the *first* validator call in a fresh worker
  process can populate `_ORG_OWNERSHIP` itself using the same
  `memory._get_pool()` handle the HTTP extension uses (the validator has no
  `memory` reference by default — see *Priming without the CP* below).

#### Priming without the CP (worker & direct-API paths)

The CP is not the only client — the worker and direct API callers hit core routes
too. Two safeguards keep them correct without the CP:

1. The HTTP extension's priming runs on **every** `/ext` and root route it serves,
   and the `bank_ownership` table lives in the same schema the engine already
   uses, so any process that has served one visibility request has a warm
   snapshot.
2. For the worker (which serves no HTTP requests), the validator refreshes
   `_ORG_OWNERSHIP` itself. It obtains an `asyncpg` pool through
   `ensure_pool_via_context`, which resolves in this order:
   1. a pool already registered via `set_pool` (e.g. by the HTTP extension in an
      API process);
   2. the memory engine exposed by the validator's `ExtensionContext`, if one was
      wired;
   3. a **self-owned pool** the shared module creates once from
      `HINDSIGHT_API_DATABASE_URL`.

   Step 3 is essential on the worker: the operation-validator extension's
   `ExtensionContext` is **not** wired there (the worker loads it without a
   context) and there is no HTTP extension to call `set_pool`, so without a
   self-owned pool the validator would fail closed and **every async
   retain/consolidation on an owned or private bank would stall** (the job is
   denied `write access` and retried forever). The self-owned pool is small
   (`max_size=2`), created under a lock, and always yields to a real engine pool
   registered later. `fq_table` still resolves the correct schema because the
   worker sets the `_current_schema` contextvar from the task's `_schema` before
   the validator runs. If no pool can be obtained by any route, the validator
   fails closed (rejects) rather than allowing.

   > Async worker tasks preserve the **original** caller's identity: the queued
   > operation stashes `_tenant_id` (`gh_<user_id>:<role>`), and the worker
   > restores it into the `RequestContext` before validation. So the ownership
   > check runs as the user who submitted the retain — the owner of the bank —
   > and passes.

Access decisions (all in the authorization extension):

| Hook                        | Rule |
|-----------------------------|------|
| `validate_bank_read`        | allow if bank in caller's `shared`, in caller's `owned`, or caller `is_admin`; else `reject(404)` (hide existence). |
| `validate_bank_write`       | role must permit writes (member/admin). Then: admin → allow; owner → allow; owned by someone else → reject; **unowned → atomically claim private for the caller (first-writer-wins) and allow** (race loser rejected). |
| `filter_bank_list`          | keep only banks in `owned ∪ shared` (admins keep all). |
| `validate_retain`/`reflect` | role gate (viewer rejected) **and** the write check above (which auto-claims a new bank private for the caller). |
| `validate_recall`           | allow if the caller may read the target bank (viewer allowed). |

Because visibility is at the **bank** level, `recall` never needs per-memory
filtering: if the caller may read the bank, they may recall all of it; otherwise
the whole operation is rejected. This keeps the model out of row-level-ACL
territory (an explicit non-goal).

On a **write**, a bank with no `bank_ownership` row is claimed private for the
writer (first-writer-wins), so it is never left unowned once written. The
`..._UNOWNED_BANKS` policy therefore only affects **reads/lists** of a bank that
somehow has a row in the engine but no ownership entry (created before this
profile, or by another tenant extension): `shared` (default — visible to the
org) or `admin_only`. Enabling the profile never orphans existing data.

## API contract (dataplane, HTTP extension routes)

All routes are mounted at the app root by the HTTP extension and are tenant-scoped
by the active org schema (same auth as every other route).

### `GET /v1/default/banks/{bank_id}/visibility`

Returns the ownership record for one bank (or the caller's view of it).

```jsonc
// 200
{
  "bank_id": "roadmap",
  "owner_user_id": "1024",
  "is_owner": true,
  "visibility": "private",        // "private" | "shared"
  "can_share": true               // caller is owner or admin
}
// 404 if the caller may not see the bank
```

### `PUT /v1/default/banks/{bank_id}/visibility`

Claim/create ownership (first call) or change visibility. Idempotent.

```jsonc
// request
{ "visibility": "shared" }        // "private" | "shared"

// 200
{ "bank_id": "roadmap", "owner_user_id": "1024", "visibility": "shared", "can_share": true }

// 403  { "error": "Only the bank owner or an org admin may change visibility." }
```

### `GET /v1/default/banks-visibility`  *(optional bulk prime)*

> Note: this bulk route uses the hyphenated path `banks-visibility` (a sibling
> of `/v1/default/banks`) rather than `banks/visibility`. The latter would be
> matched by the core `/v1/default/banks/{bank_id}` route (which registers
> PUT/PATCH/DELETE but no GET) and return `405 Method Not Allowed`.

Returns visibility for all banks the caller can see; used by the CP to prime the
list badges and the enforcement cache in one call.

```jsonc
// 200
{ "banks": [ { "bank_id": "roadmap", "visibility": "shared", "is_owner": false, "can_share": false },
             { "bank_id": "scratch", "visibility": "private", "is_owner": true,  "can_share": true } ] }
```

These are **additive** routes; the existing bank endpoints are unchanged. The
existing `GET .../profile` and the bank list responses are **enriched by the CP**
using the bulk endpoint above (the dataplane responses themselves are not
modified, preserving the zero-core-change contract).

## Stateless membership

All org/team/**role** data comes from GitHub at request time — none of it is
persisted:

- `GET /user` — identity (immutable numeric id).
- `GET /user/orgs` — orgs the user belongs to.
- `GET /user/teams` — team slugs (already implemented) → role.

Results are cached per-token by the existing `role_cache_ttl` (default 300s),
extended to also cache the org list. The **only** persisted authz state is the
per-schema `bank_ownership` table (owner + visibility), which is intrinsic data —
it cannot be derived from GitHub. There are no membership/role tables, no invite
tables, and no scoped-API-key tables. Trade-off vs #2262: no self-service invites
or persisted scoped API keys in v1 (acceptable per issue #2235's "keep simple
deployments simple").

## Configuration

```bash
HINDSIGHT_API_TENANT_EXTENSION=hindsight_api.extensions.builtin.github_org:GitHubOrgTenantExtension
HINDSIGHT_API_OPERATION_VALIDATOR_EXTENSION=hindsight_api.extensions.builtin.github_org:GitHubOrgAuthorizationExtension
HINDSIGHT_API_HTTP_EXTENSION=hindsight_api.extensions.builtin.github_org:GitHubOrgBanksHttpExtension
HINDSIGHT_API_AUTHZ_PROFILE=github_org      # fail-fast: all three extensions must be set (see below)

# The single org this deployment serves (login). Resolved to a numeric id at startup.
HINDSIGHT_API_TENANT_GITHUB_ORG=icd-tech

# Team→role mapping (evaluated within the org) — reuses the existing GitHub keys
HINDSIGHT_API_TENANT_GITHUB_ADMIN_TEAMS=sre
HINDSIGHT_API_TENANT_GITHUB_MEMBER_TEAMS=dev
HINDSIGHT_API_TENANT_GITHUB_VIEWER_TEAMS=read-only

HINDSIGHT_API_TENANT_GITHUB_DEFAULT_ROLE=viewer
HINDSIGHT_API_TENANT_SCHEMA_PREFIX=org
HINDSIGHT_API_TENANT_GITHUB_ROLE_CACHE_TTL=300

# How to treat banks with no bank_ownership row (legacy/other-extension data)
HINDSIGHT_API_TENANT_GITHUB_ORG_UNOWNED_BANKS=shared   # shared | admin_only
HINDSIGHT_API_TENANT_GITHUB_OWNERSHIP_CACHE_TTL=30     # seconds, _ORG_OWNERSHIP TTL
```

`GitHubOrgTenantExtension` reuses `GitHubTenantExtension`'s config keys
(`github_org`, `github_*_teams`, `github_default_role`, `github_role_cache_ttl`,
`schema_prefix`) since it subclasses it. The org login is resolved to the numeric
org id once in `on_startup` (`GET /orgs/{login}`), and the schema becomes
`{prefix}_{org_numeric_id}` for all callers.

### Fail-fast profile validation

`HINDSIGHT_API_AUTHZ_PROFILE=github_org` requires **all three** extension slots
(tenant, operation-validator, http) to point at the `github_org` classes; the
server refuses to start otherwise. This prevents partial deployments — e.g.
authenticating orgs but enforcing no visibility (validator missing), or enforcing
visibility with no way to set it (http extension missing). Mirrors #2262's
`authz_profile` guard. The check runs in each extension's `__init__`/`on_startup`
(reading the sibling env vars), so it needs no core wiring.

## Worker / `list_tenants`

The deployment has exactly **one** schema. `list_tenants()` returns the single
`Tenant(schema=f"{prefix}_{org_id}")` for the configured org (id resolved at
startup). This is simpler than per-user isolation: there is one durable schema, so
the worker always knows it. The scan-discovered-schema worker fix (`4b5def22e`,
already on the base) is a belt-and-suspenders backstop but is not required here.

## Control plane

Reuses the request-scoped dataplane client + `resolveDataplaneAuthHeader()`
cascade already on the base branch. Because the org is fixed, the CP needs **no**
org switcher, no `X-Hindsight-Org-Id` forwarding, and no `/api/me`/`select-org`
flow — the existing auth-header passthrough already carries the caller's GitHub
token, which is all the dataplane needs to resolve role + ownership.

The only CP additions are the **bank visibility** surface:

**Create / share / unshare banks** — new BFF routes proxying the HTTP-extension
dataplane endpoints:

| CP BFF route | Method | Proxies to dataplane | Used by |
|--------------|--------|----------------------|---------|
| `/api/banks/[bankId]/visibility` | `GET`  | `GET /v1/default/banks/{bankId}/visibility` | bank page: show private/shared + `can_share` |
| `/api/banks/[bankId]/visibility` | `PUT`  | `PUT /v1/default/banks/{bankId}/visibility` | "New bank" (claim, `private`), "Share"/"Make private" toggle |
| `/api/banks/visibility`          | `GET`  | `GET /v1/default/banks-visibility` (bulk) | bank list: badges + prime enforcement cache |

**UI**

- **New bank** action → `PUT .../visibility {"visibility":"private"}` to claim the
  id (then the user writes to it as normal). This is what "create a bank" means in
  the UI, since the dataplane has no create endpoint.
- **Share / Make private** toggle on the bank page and in the bank list's overflow
  menu — visible only when `can_share` is true (owner or admin).
- **Private / Shared badge** on each bank in the list (from the bulk endpoint).
- Viewer-role users see shared banks read-only; write actions are hidden/disabled.
- New i18n keys across all 10 locales (badge labels, share/unshare actions,
  permission-denied toast).

## Engine contract

The memory engine sees only `TenantContext.schema_name` and never learns about
orgs, roles, or GitHub. This document's central invariant:

> **No change to `hindsight_api/engine/**`.** If a requirement seems to need an
> engine change, it belongs in an extension hook or is out of scope for v1.

## Divergence & convergence with PR #2262

| Aspect              | #2262 (Supabase)                 | This model (GitHub)             |
|---------------------|----------------------------------|---------------------------------|
| Org = tenant = schema | yes                            | yes — one fixed org per deployment |
| Multi-org per deployment | yes                         | no (single org)                 |
| Active-org header   | `X-Hindsight-Org-Id`             | none (org fixed by config)      |
| Authz location      | `OperationValidatorExtension`    | `OperationValidatorExtension`   |
| `authz_profile` guard | yes                            | yes (self-checked in extensions)|
| Identity source     | Supabase Auth                    | GitHub OAuth                    |
| Membership/role state | durable Supabase tables + RLS  | stateless GitHub API + TTL cache|
| Private + shared banks | (not in V1 slice)             | yes — per-bank visibility in the org schema |
| Persisted authz state | orgs/members/invites/keys tables | one `bank_ownership` table |
| Invites / scoped keys | yes                            | no (v1)                         |
| Cross-schema addressing | none                         | none (converges)                |

The shared header name and the org-as-tenant + authz-extension shape are chosen
deliberately so this model does not fight #2262 if both eventually coexist as
selectable profiles.
