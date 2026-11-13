# API Framework

A production-shaped **FastAPI** base project. Clone it, rename it, and start
writing endpoints — authentication, migrations, error handling, middleware,
logging, tests and CI are already wired together the way they should be.

It runs with **no external services**: the default database is SQLite, so
`alembic upgrade head` and a `uvicorn` start are all it takes.

```bash
git clone <your-fork> my-service && cd my-service
python -m venv .venv && .venv/Scripts/activate      # Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
alembic upgrade head
python -m api_framework --reload
# -> http://127.0.0.1:8000/docs
```

Log in with the bootstrap admin from `.env` (`admin@example.com` /
`ChangeMe123!`), or `POST /api/v1/auth/register` to create your own account.

---

## Contents

- [What you get](#what-you-get)
- [Layout](#layout)
- [Architecture](#architecture)
- [Authentication](#authentication)
- [Error handling](#how-to-add-exception-handling)
- [Middleware](#how-to-add-middleware)
- [Logging](#logging)
- [Database and migrations](#database-and-migrations)
- [Adding a resource](#adding-a-new-resource)
- [Testing](#testing)
- [Configuration reference](#configuration-reference)
- [Deployment](#deployment)
- [Deliberate design decisions](#deliberate-design-decisions)

---

## What you get

| Concern | Implementation |
|---|---|
| **Auth** | Pluggable verifier: self-issued JWT **or** external IdP via JWKS. Argon2id hashing, refresh-token rotation with reuse detection, role-based route guards. |
| **Errors** | One RFC 9457 `application/problem+json` envelope for every failure, correlated to the logs by `request_id`. |
| **Middleware** | Correlation-id (pure ASGI) and access logging (`BaseHTTPMiddleware`) — both styles, documented. |
| **Logging** | Structured JSON in deployment, human-readable locally, `request_id` on every line. |
| **Database** | Async SQLAlchemy 2.0, one transaction per request, repository pattern. |
| **Migrations** | Alembic, async-aware, SQLite batch mode enabled. |
| **Validation** | Pydantic v2 throughout; separate request and response models. |
| **Health** | Separate liveness and readiness probes. |
| **Quality** | ruff, mypy `strict`, pytest with coverage, pre-commit, CI. |
| **Container** | Multi-stage Dockerfile, non-root, healthcheck, compose file. |

---

## Layout

```
src/api_framework/
├── app.py                  # application factory; middleware order lives here
├── __main__.py             # CLI entrypoint
├── core/
│   ├── config.py           # typed settings; refuses to boot if unsafe
│   ├── logging.py          # JSON/human formatters + request-id ContextVar
│   └── lifespan.py         # engine + verifier construction, admin bootstrap
├── api/
│   ├── deps.py             # composition root: services, pagination
│   └── v1/
│       ├── __init__.py     # router aggregation and URL prefixes
│       ├── auth.py         # register / login / refresh / logout / me
│       ├── items.py        # reference CRUD router — copy this one
│       ├── users.py        # admin-only user management
│       └── health.py       # liveness, readiness, info
├── auth/
│   ├── principal.py        # Principal + Role — the identity contract
│   ├── verifiers.py        # LocalJwtVerifier | JwksVerifier (pluggable)
│   ├── dependencies.py     # require_roles(), CurrentPrincipal
│   └── passwords.py        # Argon2id
├── db/
│   ├── base.py             # DeclarativeBase, naming convention, mixins
│   ├── engine.py           # async engine + SQLite pragmas
│   ├── session.py          # request-scoped unit of work
│   └── models.py           # model registry (import every model here)
├── domain/
│   ├── users/              # models, schemas, repository, service
│   └── items/              # models, schemas, repository, service
├── errors/
│   ├── exceptions.py       # AppError hierarchy
│   └── handlers.py         # RFC 9457 handlers
├── middleware/
│   ├── correlation.py      # pure-ASGI middleware (preferred style)
│   └── access_log.py       # BaseHTTPMiddleware (convenient style)
└── migrations/             # Alembic
```

---

## Architecture

Four layers, one direction of dependency:

```
   HTTP        api/v1/*.py        parse, delegate, serialise. No logic.
     |          (FastAPI)
     v
 Service     domain/*/service.py  business rules, authorisation over data.
     |                            Imports no `fastapi`.
     v
Repository  domain/*/repository.py the only place that builds queries.
     |
     v
  Model      domain/*/models.py    SQLAlchemy mappings.
```

Why bother, in a template:

- **Handlers stay thin**, so routing concerns never tangle with rules.
- **Services are unit-testable** without an HTTP client or a database.
- **`fastapi` is absent below the HTTP layer**, so the same logic runs from a
  CLI, a queue consumer, or a test.
- **Query changes are local** to one repository.

The wiring happens in `api/deps.py` — the single composition root. A route
declares `items: ItemServiceDep` and receives a fully assembled service.

---

## Authentication

Two modes, selected by `APP_AUTH_MODE`, behind one interface
(`auth/verifiers.py::TokenVerifier`). Business code depends only on
`Principal`, so switching modes changes no routes and no services.

### `local` (default) — this service issues its own tokens

```
POST /api/v1/auth/register     create an account
POST /api/v1/auth/login        -> { access_token, refresh_token, expires_in }
POST /api/v1/auth/refresh      rotate the refresh token
POST /api/v1/auth/logout       revoke one refresh token
POST /api/v1/auth/logout-all   revoke every session
GET  /api/v1/auth/me           current user
```

- **Access token** — HS256 JWT, 15 min, stateless. Verified with no DB hit.
- **Refresh token** — opaque 48-byte random value, 14 days, stored as a
  SHA-256 hash and revocable.
- **Rotation with reuse detection** — each refresh token is single-use.
  Presenting an already-used one revokes the user's whole token family, on the
  assumption it leaked. This is the OAuth 2.0 Security BCP recommendation.

### `external` — an IdP issues tokens, this service verifies them

Set `APP_AUTH_MODE=external` and point `APP_OIDC_JWKS_URL` at your provider
(Keycloak, Entra ID, Auth0, Okta). Tokens are verified with the provider's
public keys, cached in memory and refreshed on rotation. `/auth/login` is
disabled — this service never sees a credential.

Roles are read from `APP_OIDC_ROLES_CLAIM`, which accepts a dotted path for
nested claims (`realm_access.roles` for Keycloak). `GET /auth/me/principal`
shows exactly what the verifier extracted, which is the fastest way to debug a
claim mapping.

### Authorising a route

```python
from api_framework.auth.dependencies import CurrentPrincipal, require_roles
from api_framework.auth.principal import Role


# any authenticated caller
@router.get("/me")
async def me(principal: CurrentPrincipal): ...


# require a role; 403 otherwise
@router.delete("/{id}", dependencies=[Depends(require_roles(Role.ADMIN))])
async def destroy(id: str): ...


# require a role *and* use the identity
@router.post("")
async def create(principal: Annotated[Principal, Depends(require_roles(Role.USER))]): ...
```

`require_roles` admits `ADMIN` implicitly; pass `allow_admin=False` where
separation of duties matters. For an entire admin surface, attach it to the
router (see `api/v1/users.py`) so a route added later is protected by default.

**Roles vs ownership.** `require_roles` answers "may this kind of user perform
this operation". Whether they may touch *this row* is a service concern,
because only the service can load the row — see
`ItemService._assert_can_access`. Conflating the two is how IDOR bugs happen.

---

## How to add exception handling

### 1. Raise a domain exception

```python
from api_framework.errors.exceptions import NotFoundError

if item is None:
    raise NotFoundError(f"Item {item_id} was not found.")
```

Domain code raises domain exceptions — never `HTTPException`. That is what
keeps services free of `fastapi`.

### 2. Define a new error type when you need one

```python
class QuotaExceededError(AppError):
    status_code = 429
    code = "quota_exceeded"
    title = "Quota exceeded"
```

No registration required: `AppError` is handled generically, so the new class
is mapped correctly the moment it exists. Add a dedicated handler only for
exceptions you do not own (a third-party client's error class).

### 3. The response

Every error — yours, FastAPI's validation errors, Starlette's 404s, and
unhandled crashes — comes back in one shape:

```json
{
  "type": "/problems/not_found",
  "title": "Resource not found",
  "status": 404,
  "detail": "Item 7f3e... was not found.",
  "instance": "/api/v1/items/7f3e...",
  "code": "not_found",
  "request_id": "9c1d4f2ab8e04f7f9a1b"
}
```

`request_id` matches the log line, so a screenshot from a user leads straight
to the traceback.

**Why handlers, not middleware.** The services this project was modelled on
caught exceptions inside `BaseHTTPMiddleware.dispatch`. Handlers registered via
`app.add_exception_handler` are better: they also catch exceptions raised by
Starlette's routing layer (a 404 on an unknown path never reaches your
middleware), they receive the `Request`, and they avoid the task-group wrapper
that interferes with streaming responses and background tasks. See the module
docstring in `errors/handlers.py`.

---

## How to add middleware

Both styles are implemented so you can copy whichever fits.

### Pure ASGI — preferred (`middleware/correlation.py`)

```python
class MyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":  # pass websockets/lifespan through
            await self.app(scope, receive, send)
            return
        # ... before
        await self.app(scope, receive, send)
```

No task-group overhead, safe with streaming responses and background tasks. To
alter the response, wrap `send` — `CorrelationIdMiddleware._send_with_header`
shows how.

### `BaseHTTPMiddleware` — convenient (`middleware/access_log.py`)

```python
class MyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Custom"] = "value"
        return response
```

Friendlier API, at the cost of an anyio task group per request. Fine for
simple, non-streaming concerns.

### Registration order — the part that bites

`add_middleware` **prepends**, so the *last* registered is the *outermost* and
runs **first** inbound. `app.py::_register_middleware` is therefore written in
reverse of execution order:

```
CorrelationIdMiddleware   (added last  -> outermost, runs first)
  AccessLogMiddleware
    CORSMiddleware        (added first -> innermost)
      route handler
```

Correlation must be outermost, or the access log and the exception handlers
emit lines without a `request_id`.

---

## Logging

```python
from api_framework.core.logging import get_logger

logger = get_logger(__name__)

logger.info("Item created", extra={"item_id": item.id, "owner_id": item.owner_id})
```

Anything in `extra` becomes a top-level JSON field. The current `request_id` is
attached automatically.

```
# APP_LOG_JSON=false (local)
14:22:07 INFO     api_framework.domain.items.service  Item created  [rid=9c1d4f2a]

# APP_LOG_JSON=true (deployed)
{"timestamp":"2026-09-16T14:22:07.412+00:00","level":"INFO",
 "logger":"api_framework.domain.items.service","message":"Item created",
 "request_id":"9c1d4f2ab8e04f7f9a1b","item_id":"7f3e...","owner_id":"2a9c..."}
```

Log **events with fields**, not interpolated prose — `logger.info("Item
created", extra={"item_id": x})` is queryable; `logger.info(f"Created item
{x}")` is not. Never log a credential, token, or password hash; note that
`User.__repr__` deliberately omits the email for this reason.

---

## Database and migrations

```bash
alembic upgrade head                              # apply
alembic revision --autogenerate -m "add widgets"  # create from model changes
alembic downgrade -1                              # roll back one
alembic history --verbose                         # what exists
alembic upgrade head --sql > migration.sql        # emit SQL for review
```

**Always read an autogenerated migration before committing it.** Autogenerate
detects most schema changes but not renames (it sees a drop plus an add, which
loses data) and it never generates data backfills.

Switching to PostgreSQL is one variable:

```bash
APP_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/mydb
pip install asyncpg
```

Three SQLite details this project handles that most templates miss:

1. **`PRAGMA foreign_keys=ON`** on every connection. SQLite ignores foreign
   keys by default, so `ON DELETE CASCADE` silently does nothing.
2. **`render_as_batch=True`** in `migrations/env.py`. SQLite cannot
   `DROP COLUMN` or alter a constraint; batch mode rebuilds the table instead.
   Without it, the first migration that drops a column fails.
3. **A metadata naming convention** in `db/base.py`, which batch mode requires
   and which lets Alembic drop constraints it did not create.

---

## Adding a new resource

`domain/items/` plus `api/v1/items.py` is the reference. Copy it:

1. `domain/widgets/models.py` — the SQLAlchemy model.
2. Register it in `db/models.py` — **required**, or Alembic will not see it.
3. `domain/widgets/schemas.py` — `WidgetCreate`, `WidgetUpdate`, `WidgetRead`.
4. `domain/widgets/repository.py` — queries only, no commits.
5. `domain/widgets/service.py` — rules and object-level authorisation.
6. Add a provider to `api/deps.py`.
7. `api/v1/widgets.py` — the router.
8. Mount it in `api/v1/__init__.py`.
9. `alembic revision --autogenerate -m "add widgets"`, then read the output.
10. Tests in `tests/`.

---

## Testing

```bash
pytest                       # with coverage
pytest -k auth               # one area
pytest --cov-report=html     # browsable report
```

Each test gets its own file-backed SQLite database created from the real
models, an app with overridden settings, and an `httpx.AsyncClient` wired
straight to the ASGI app — no network, no running server. See
`tests/conftest.py`.

---

## Configuration reference

Every setting is an `APP_`-prefixed environment variable; see `.env.example`.
`core/config.py` is the contract.

| Variable | Default | Notes |
|---|---|---|
| `APP_ENV` | `local` | `local` / `dev` / `staging` / `production` |
| `APP_DEBUG` | `false` | Must be false outside local |
| `APP_LOG_LEVEL` | `INFO` | |
| `APP_LOG_JSON` | `false` | Set true when deployed |
| `APP_DATABASE_URL` | `sqlite+aiosqlite:///./api_framework.db` | Async driver required |
| `APP_SECRET_KEY` | insecure default | **Must** be overridden outside local |
| `APP_ACCESS_TOKEN_TTL_MINUTES` | `15` | |
| `APP_REFRESH_TOKEN_TTL_DAYS` | `14` | |
| `APP_AUTH_MODE` | `local` | `local` / `external` |
| `APP_OIDC_JWKS_URL` | — | Required when `external` |
| `APP_OIDC_ROLES_CLAIM` | `roles` | Dotted paths supported |
| `APP_CORS_ORIGINS` | `["http://localhost:3000"]` | JSON array; no `"*"` when deployed |
| `APP_FIRST_ADMIN_EMAIL` / `_PASSWORD` | `admin@example.com` / `ChangeMe123!` | Ignored in production |

**Configuration is validated at startup and the process refuses to boot if it
is unsafe for the target environment** — a default signing key, `DEBUG=true`,
or wildcard CORS outside `local` is a hard failure. A service that starts with
a forgeable signing key is worse than one that does not start, because nothing
tells you.

---

## Deployment

```bash
docker compose up --build            # http://localhost:8000/docs
```

The image is multi-stage (build deps discarded), runs as a non-root user, and
declares a healthcheck against `/api/v1/health/ready`. Migrations run in the
container entrypoint before the server starts.

Before going to production:

- [ ] `APP_SECRET_KEY` from a secrets manager — `python -m api_framework --generate-secret`
- [ ] `APP_ENV=production`, `APP_DEBUG=false`, `APP_LOG_JSON=true`
- [ ] `APP_CORS_ORIGINS` set to real origins
- [ ] `APP_FIRST_ADMIN_PASSWORD` unset; provision the first admin out of band
- [ ] PostgreSQL rather than SQLite (see below)
- [ ] TLS terminated upstream; run behind a reverse proxy
- [ ] Rate limiting at the edge or via middleware
- [ ] `/docs` is already disabled automatically when `APP_ENV=production`

**SQLite is the default for zero-setup development, not for production.** It
serialises writes and lives on one filesystem, so it does not survive
horizontal scaling. Switching is one environment variable.

---

## Deliberate design decisions

Things that look like they could be simpler, and why they are not:

| Decision | Reason |
|---|---|
| Separate request/response schemas | A shared model is how a password hash ends up in a response. |
| UUID primary keys | Sequential integers in URLs leak row counts and invite enumeration. |
| `Numeric`, never `Float`, for money | Binary floats cannot represent `0.10`; errors accumulate. |
| Session commits in the dependency, not the service | One transaction per request makes multi-step operations atomic by default. |
| `expire_on_commit=False` | Otherwise attribute access after commit triggers sync IO and raises `MissingGreenlet` under asyncio. |
| Opaque refresh tokens, not JWTs | Revocation needs a DB lookup anyway, so self-describing tokens only add leakage. |
| Hash a password even for an unknown email | Returning early is a timing oracle for user enumeration. |
| 404 (not 403) for someone else's row | Confirming the row exists is itself a leak. |
| `limit` capped at 100 | An uncapped page size is a one-request DoS. |
| Pagination envelope from day one | Adding `total` to a bare array later is a breaking change. |
| Argon2id over passlib/bcrypt | OWASP's first choice; passlib is unmaintained and mis-detects bcrypt 4.x. |
| Algorithms pinned in `jwt.decode` | Trusting the token's own `alg` is the JWT confusion attack. |
| Model registry in `db/models.py` | Alembic autogenerate silently drops tables it cannot see. |

---

## License

MIT. Use it as you like.
