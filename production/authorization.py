"""
Julie ChenBot Shared Moderator Authorization
================================================

One small, read-only check, reusing the exact policy already
established by commands/teach.py's `_is_trusted_moderator()`: a full
Discord administrator always qualifies; otherwise the user must hold
the single role configured via config.TRUSTED_MODERATOR_ROLE_ID. Not a
new authentication system -- same env var, same Discord-native
permission bits, decides nothing teach.py's own check doesn't already
decide.

Extracted here (rather than left duplicated a third time) only because
this feature needs the identical check from two call sites that never
see a discord.Interaction -- commands/chat.py's `/chat` handler and
services/discord.py's @mention/DM `on_message` handler -- both of
which only have a discord.Member (in a guild) or discord.User (in a
DM) to check against. teach.py's own local check is left exactly as
it is; nothing here changes /teach's behavior.

Duck-typed deliberately (no discord.py import): works uniformly
against anything with optional `guild_permissions`/`roles` attributes,
treating their absence (a DM's discord.User) as "not a moderator"
rather than raising.
"""

from __future__ import annotations

from typing import Any

from config import TRUSTED_MODERATOR_ROLE_ID


def is_trusted_moderator(user: Any) -> bool:
    """True if `user` is a full server administrator, or holds the
    configured TRUSTED_MODERATOR_ROLE_ID role. False for anything
    else, including a DM user (no guild_permissions/roles at all) or
    an unset TRUSTED_MODERATOR_ROLE_ID."""

    permissions = getattr(user, "guild_permissions", None)
    if permissions is not None and getattr(permissions, "administrator", False):
        return True

    if not TRUSTED_MODERATOR_ROLE_ID:
        return False

    roles = getattr(user, "roles", None) or []
    return any(getattr(role, "id", None) == TRUSTED_MODERATOR_ROLE_ID for role in roles)
