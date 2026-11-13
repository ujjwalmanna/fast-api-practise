"""Authentication and authorisation dependencies.

Usage - authorisation is declared on the route, never written inline:

```python
# any authenticated caller
@router.get("/me")
async def me(principal: CurrentPrincipal) -> ...: ...

# specific roles; 403 if the caller lacks them
@router.delete("/items/{item_id}", dependencies=[Depends(require_roles(Role.ADMIN))])
async def delete_item(...): ...

# roles *and* the principal in the body
@router.post("/items")
async def create(principal: Annotated[Principal, Depends(require_roles(Role.USER))]): ...
```

`require_roles` is a dependency *factory* - it returns a dependency, which is
why it is called (`Depends(require_roles(...))`) rather than referenced. This is
the same shape as the `Depends(auth(["ROLE"], "POST"))` pattern used in the
services this project was modelled on, with two differences: roles are an enum
rather than strings (so typos fail type-checking), and the HTTP method is not a
parameter - the method is already part of the route declaration, and repeating
it invites the two drifting apart.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api_framework.auth.principal import Principal, Role
from api_framework.auth.verifiers import TokenVerifier
from api_framework.errors.exceptions import AuthenticationError, PermissionDeniedError

# auto_error=False so a missing header raises our own AuthenticationError
# (RFC 9457 envelope, WWW-Authenticate header) instead of Starlette's bare 403.
_bearer_scheme = HTTPBearer(auto_error=False, description="Bearer <access token>")


def get_verifier(request: Request) -> TokenVerifier:
    """Fetch the verifier built at startup and stored on app state."""
    verifier: TokenVerifier = request.app.state.token_verifier
    return verifier


async def get_current_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    verifier: Annotated[TokenVerifier, Depends(get_verifier)],
) -> Principal:
    """Resolve and verify the bearer token, or raise 401."""
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Missing bearer token.")
    if credentials.scheme.lower() != "bearer":
        raise AuthenticationError("Authorization scheme must be Bearer.")
    return await verifier.verify(credentials.credentials)


async def get_optional_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    verifier: Annotated[TokenVerifier, Depends(get_verifier)],
) -> Principal | None:
    """For endpoints that behave differently when signed in but allow anonymous.

    An *invalid* token still fails: silently treating a bad token as anonymous
    hides client bugs and makes "why am I seeing public data" impossible to debug.
    """
    if credentials is None or not credentials.credentials:
        return None
    return await verifier.verify(credentials.credentials)


CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
OptionalPrincipal = Annotated[Principal | None, Depends(get_optional_principal)]


def require_roles(
    *roles: Role, allow_admin: bool = True
) -> Callable[[Principal], Awaitable[Principal]]:
    """Build a dependency that requires any one of `roles`.

    `allow_admin=True` (the default) also admits ADMIN, which is almost always
    what you want and saves repeating `Role.ADMIN` on every route. Set it to
    False for actions an admin must not perform implicitly - approving their own
    request, for example, where the point is separation of duties.
    """
    permitted = frozenset(roles) | ({Role.ADMIN} if allow_admin else frozenset())

    async def dependency(principal: CurrentPrincipal) -> Principal:
        if not principal.roles.intersection(permitted):
            # 403, not 404: the caller authenticated successfully, so hiding
            # the resource's existence buys nothing and confuses clients.
            raise PermissionDeniedError(
                "This operation requires one of the following roles: "
                + ", ".join(sorted(role.value for role in permitted))
                + ".",
                extra={"required_roles": sorted(role.value for role in permitted)},
            )
        return principal

    return dependency
