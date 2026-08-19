"""
Julie ChenBot Admin API Auth
=============================

Single shared-secret bearer-token auth for the admin API. The admin
API has exactly one trust boundary to enforce: "is this request from
the dashboard backend." Fine-grained viewer/moderator/admin roles are
the dashboard's own concern (its own user accounts, its own session
auth, its own RBAC) -- Julie only needs to know the caller is the
dashboard, once, at the HTTP layer. See config.ADMIN_API_KEY.

The token is compared with hmac.compare_digest() (constant-time) so
response timing can't be used to guess it character-by-character.

One deliberate, explicit exception: GET /health (admin_api/routes.py)
is a public liveness endpoint for Railway's health check -- it must
be reachable with no Authorization header at all. It returns nothing
beyond a static {"status": "ok"}, so exempting it costs nothing.
Nothing else is exempted: this is an exact-path, exact-method
allowlist, not a prefix match, specifically so it can never
accidentally widen to cover /api/v1/health or anything else.
"""

from __future__ import annotations

import hmac

from aiohttp import web

from config import ADMIN_API_KEY

# Exact (method, path) pairs that bypass authentication entirely.
# GET only, and only this one path -- see the module docstring above.
_UNAUTHENTICATED_PATHS = frozenset({"/health"})


def _extract_token(request: web.Request) -> str | None:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    return header[len("Bearer "):].strip()


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.method == "GET" and request.path in _UNAUTHENTICATED_PATHS:
        return await handler(request)

    token = _extract_token(request)

    if not ADMIN_API_KEY or not token or not hmac.compare_digest(token, ADMIN_API_KEY):
        return web.json_response({"error": "unauthorized"}, status=401)

    return await handler(request)
