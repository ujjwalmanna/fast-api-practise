"""The authenticated caller.

`Principal` is the boundary between "how the token was verified" and "what the
application does with it". Routes and services depend on this shape only, so
switching from local JWTs to an external IdP changes no business code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Role(StrEnum):
    """Coarse-grained roles.

    Kept as an enum so a typo in a route decorator is caught by mypy instead of
    silently denying (or granting) access. For finer control, layer scopes or
    per-object permission checks on top - roles answer "what kind of user is
    this", not "may they touch this specific row".
    """

    ADMIN = "admin"
    USER = "user"
    SERVICE = "service"  # machine-to-machine caller
    READ_ONLY = "read_only"


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated caller, whatever verified them.

    Frozen: nothing downstream should be able to escalate its own privileges by
    mutating the principal mid-request.
    """

    subject: str  # stable user id ("sub")
    roles: frozenset[Role] = field(default_factory=frozenset)
    email: str | None = None
    token_id: str | None = None  # "jti", for revocation
    raw_claims: dict[str, object] = field(default_factory=dict)

    def has_role(self, *roles: Role) -> bool:
        """True if the principal holds any of `roles`.

        ADMIN is intentionally *not* granted implicitly here. Implicit
        superuser bypasses hide authorisation bugs; grant ADMIN explicitly
        where it applies - see `require_roles`.
        """
        return bool(self.roles.intersection(roles))

    @property
    def is_admin(self) -> bool:
        return Role.ADMIN in self.roles
