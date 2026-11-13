"""v1 router aggregation.

Routers are mounted here, not in the app factory, so the app factory stays
agnostic of individual resources and adding a v2 is a matter of adding one
sibling package.

Prefixes live here too rather than inside each router module, which keeps a
router reusable at a different path and puts the entire URL surface in one
readable list.
"""

from fastapi import APIRouter

from api_framework.api.v1 import auth, health, items, users

api_router = APIRouter()

api_router.include_router(health.router, prefix="/health")
api_router.include_router(auth.router, prefix="/auth")
api_router.include_router(items.router, prefix="/items")
api_router.include_router(users.router, prefix="/users")

__all__ = ["api_router"]
