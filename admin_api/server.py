"""
Julie ChenBot Admin API Server
================================

Builds and runs the aiohttp.web application backing the admin API
(see admin_api/routes.py). Started as a background asyncio task
alongside the existing production scheduler (see services/discord.py
on_ready()) -- same event loop, same long-lived ProductionEngine
instance, no second copy of any state.

Entirely optional: only runs when config.ENABLE_ADMIN_API is true, and
refuses to start without config.ADMIN_API_KEY set, so there is no way
to accidentally expose an unauthenticated admin surface. If this
process is unreachable or crashes, the Discord bot and its production
scheduler are entirely unaffected -- this server owns no state and is
not on the tick()/announce() path.
"""

from __future__ import annotations

import asyncio

from aiohttp import web

from admin_api.auth import auth_middleware
from admin_api.routes import routes
from config import ADMIN_API_KEY, ADMIN_API_PORT
from production.engine import ProductionEngine
from services.logger import ProductionLogger

logger = ProductionLogger.get("AdminAPI")


def build_app(engine: ProductionEngine) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app["engine"] = engine
    app.add_routes(routes)
    return app


async def run_admin_api(engine: ProductionEngine, *, port: int | None = None) -> None:
    """Runs the admin API server until this task is cancelled.

    Intended to be wrapped in asyncio.create_task() by the caller (see
    services/discord.py on_ready()), the same way the production
    scheduler is started -- cancelling that task is this server's
    shutdown path.
    """

    if not ADMIN_API_KEY:
        logger.error(
            "ENABLE_ADMIN_API is true but ADMIN_API_KEY is unset; "
            "refusing to start the admin API."
        )
        return

    app = build_app(engine)
    runner = web.AppRunner(app)
    await runner.setup()

    bound_port = port or ADMIN_API_PORT
    site = web.TCPSite(runner, host="0.0.0.0", port=bound_port)

    try:
        await site.start()
        logger.info("Admin API listening on port %d.", bound_port)

        # AppRunner/TCPSite run their own internal server loop; this
        # just needs to stay alive (and cancellable) to keep the site
        # running and give shutdown a single place to clean up from.
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        logger.info("Admin API stopped.")
