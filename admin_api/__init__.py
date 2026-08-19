"""
Julie ChenBot Admin API
========================

A narrow, authenticated HTTP surface consumed only by the separate
Julie ChenBot Admin Dashboard (bobbyb-debug/julie-chenbot-admin-
dashboard). Off by default (config.ENABLE_ADMIN_API); when enabled it
runs as a background task inside the same process, event loop, and
ProductionEngine instance as the Discord bot itself -- there is no
second copy of Julie's state anywhere.

Every route in admin_api/routes.py mirrors an operation that already
exists for a Discord moderator (see commands/teach.py, commands/
status.py): list/read knowledge, teach/forget knowledge, preview and
apply a batch or manual state update, and read-only health/game-state/
source/diagnostic/routing views. Nothing here adds a new way to change
Julie's game state or knowledge -- it exposes the existing ways over
HTTP, behind a single shared-secret bearer token (see admin_api/auth.py).
"""
