"""Health and readiness probes.

Liveness and readiness are different questions and must be different endpoints:

* **Liveness** - "is the process wedged?" Answers without touching any
  dependency. If it fails, the orchestrator restarts the pod.
* **Readiness** - "can I serve traffic?" Checks dependencies. If it fails, the
  pod is removed from the load-balancer pool but *not* restarted.

Conflating them causes a classic cascading outage: the database blips, liveness
fails because it checks the DB, every pod is killed simultaneously, and the
restart stampede keeps the database down.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

from api_framework.api.deps import SettingsDep
from api_framework.core.logging import get_logger
from api_framework.db.engine import verify_connectivity

logger = get_logger(__name__)

router = APIRouter(tags=["Health"])


class LivenessResponse(BaseModel):
    status: Literal["ok"]


class ReadinessResponse(BaseModel):
    status: Literal["ready", "degraded"]
    checks: dict[str, str]


@router.get("/live", response_model=LivenessResponse, summary="Liveness probe")
async def liveness() -> LivenessResponse:
    """Return 200 whenever the event loop is responsive. No dependencies."""
    return LivenessResponse(status="ok")


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    responses={503: {"description": "One or more dependencies are unavailable"}},
)
async def readiness(request: Request, response: Response) -> ReadinessResponse:
    """Verify every dependency required to serve requests.

    Returns 503 when any check fails, so the orchestrator stops routing traffic
    here. Add a check per hard dependency (cache, queue, upstream API); do not
    add checks for optional ones, or an optional outage takes you offline.
    """
    checks: dict[str, str] = {}
    healthy = True

    try:
        await verify_connectivity(request.app.state.engine)
        checks["database"] = "ok"
    except Exception as exc:
        logger.error("Readiness check failed: database", exc_info=exc)
        checks["database"] = "unavailable"
        healthy = False

    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(status="ready" if healthy else "degraded", checks=checks)


class InfoResponse(BaseModel):
    name: str
    environment: str
    auth_mode: str


@router.get("/info", response_model=InfoResponse, summary="Non-sensitive build info")
async def info(settings: SettingsDep) -> InfoResponse:
    """Expose only values that are safe unauthenticated.

    Deliberately excludes the database URL, secret key and CORS config. A
    "debug info" endpoint that echoes settings is a recurring source of
    credential disclosure.
    """
    return InfoResponse(
        name=settings.project_name,
        environment=settings.env.value,
        auth_mode=settings.auth_mode.value,
    )
