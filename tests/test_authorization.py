"""Tests for production/authorization.py -- the shared, read-only
is_trusted_moderator() check used by the KNOWLEDGE_SUMMARY feature's
moderator-briefing mode (see production/knowledge_summary.py and
services/ai_service.py format_knowledge_summary_guidance()).

Reuses the exact policy already established by commands/teach.py's own
_is_trusted_moderator(): full Discord administrator always qualifies;
otherwise the user must hold the single role configured via
config.TRUSTED_MODERATOR_ROLE_ID. Not a new authentication system --
these tests exist to prove this extraction didn't change that policy.
"""

from __future__ import annotations

from types import SimpleNamespace

import production.authorization as authorization_module
from production.authorization import is_trusted_moderator


class _Permissions:
    def __init__(self, administrator: bool) -> None:
        self.administrator = administrator


class _Role:
    def __init__(self, role_id: int) -> None:
        self.id = role_id


def _member(*, administrator: bool = False, role_ids: tuple[int, ...] = ()) -> SimpleNamespace:
    return SimpleNamespace(
        guild_permissions=_Permissions(administrator),
        roles=[_Role(role_id) for role_id in role_ids],
    )


def test_full_administrator_is_a_trusted_moderator(monkeypatch):
    monkeypatch.setattr(authorization_module, "TRUSTED_MODERATOR_ROLE_ID", 999)

    assert is_trusted_moderator(_member(administrator=True)) is True


def test_user_holding_the_configured_role_is_a_trusted_moderator(monkeypatch):
    monkeypatch.setattr(authorization_module, "TRUSTED_MODERATOR_ROLE_ID", 555)

    assert is_trusted_moderator(_member(role_ids=(111, 555))) is True


def test_ordinary_member_is_not_a_trusted_moderator(monkeypatch):
    monkeypatch.setattr(authorization_module, "TRUSTED_MODERATOR_ROLE_ID", 555)

    assert is_trusted_moderator(_member(role_ids=(111, 222))) is False


def test_member_with_no_roles_is_not_a_trusted_moderator(monkeypatch):
    monkeypatch.setattr(authorization_module, "TRUSTED_MODERATOR_ROLE_ID", 555)

    assert is_trusted_moderator(_member()) is False


def test_unset_role_id_means_only_administrators_qualify(monkeypatch):
    monkeypatch.setattr(authorization_module, "TRUSTED_MODERATOR_ROLE_ID", None)

    assert is_trusted_moderator(_member(administrator=True)) is True
    assert is_trusted_moderator(_member(role_ids=(111,))) is False


def test_dm_user_with_no_guild_attributes_is_not_a_trusted_moderator():
    """A discord.User in a DM has no guild_permissions/roles at all --
    must be treated as "not a moderator", never raise."""

    dm_user = SimpleNamespace()

    assert is_trusted_moderator(dm_user) is False


def test_none_user_is_not_a_trusted_moderator():
    assert is_trusted_moderator(None) is False
