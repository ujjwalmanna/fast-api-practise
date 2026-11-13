"""Correlation-id middleware, written as pure ASGI.

## How to add middleware

There are two ways, and the choice matters:

### 1. Pure ASGI middleware (this file) - preferred

A class implementing `__call__(scope, receive, send)`. It is the fastest
option, adds no task group, and does not interfere with streaming responses or
background tasks.

```python
class MyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":          # always pass through websockets/lifespan
            await self.app(scope, receive, send)
            return
        ...                                   # before the request
        await self.app(scope, receive, send)
        ...                                   # after (see send-wrapper below)
```

To modify the *response*, wrap `send` - as `_send_with_header` does below.

### 2. `BaseHTTPMiddleware` - convenient, with caveats

Starlette's `BaseHTTPMiddleware` gives you the friendlier
`dispatch(request, call_next)` signature. It costs an anyio task group per
request and historically breaks streaming responses and `BackgroundTasks`.
Fine for simple, non-streaming concerns; avoid it on hot paths.
See `middleware/access_log.py` for a documented comparison.

### Registration order - the part that catches people out

`app.add_middleware()` builds an onion from the outside in, and the **last**
one added is the **outermost** layer. So registering A then B yields
`B -> A -> route`. This middleware must be registered last so that the id is
set before anything else - including the access log - runs.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from api_framework.core.logging import request_id_ctx

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"


class CorrelationIdMiddleware:
    """Assign every request an id, expose it to logs, echo it to the client.

    An inbound `X-Request-ID` is honoured so a trace can be followed across
    service hops; otherwise a UUID4 is generated. The value is bounded and
    sanitised because it is reflected in a response header - an attacker-supplied
    newline there would be a header-injection primitive.
    """

    MAX_LENGTH = 128

    def __init__(self, app: ASGIApp, header_name: str = REQUEST_ID_HEADER) -> None:
        self.app = app
        self.header_name = header_name
        self._header_bytes = header_name.encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = self._resolve_request_id(scope)

        # Store it in the ASGI scope as well as the ContextVar.
        #
        # Both are needed. The ContextVar is what lets arbitrary logging calls
        # pick the id up with no plumbing. But Starlette's ServerErrorMiddleware
        # - which invokes the handler registered for `Exception` - sits OUTSIDE
        # this middleware, so by the time a 500 is rendered the `finally` below
        # has already reset the ContextVar. The scope copy survives, and
        # `errors/handlers.py` reads it via `request.state.request_id`.
        scope.setdefault("state", {})["request_id"] = request_id

        token = request_id_ctx.set(request_id)
        try:
            await self.app(scope, receive, self._send_with_header(send, request_id))
        finally:
            # Always reset: ContextVars leak across tasks reusing the context.
            request_id_ctx.reset(token)

    def _resolve_request_id(self, scope: Scope) -> str:
        for raw_name, raw_value in scope.get("headers", []):
            if raw_name.lower() == self._header_bytes.lower():
                candidate = raw_value.decode("latin-1", errors="replace").strip()
                # Keep only characters that are safe in a header value.
                cleaned = "".join(
                    ch for ch in candidate[: self.MAX_LENGTH] if ch.isprintable() and ch != ","
                )
                if cleaned:
                    return cleaned
        return uuid.uuid4().hex

    def _send_with_header(self, send: Send, request_id: str) -> Send:
        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.append((self._header_bytes, request_id.encode("latin-1")))
            await send(message)

        return wrapped
