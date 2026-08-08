"""
Julie ChenBot Production Parser
================================

Converts JokersUpdates RSS entries into structured production state.

The parser owns interpretation of RSS text. It does not download RSS,
communicate with Discord, or emit ProductionEvents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html import unescape
from typing import Optional

from production.competition import CompetitionState, CompetitionType
from production.house_status import HouseStatus
from production.rss import FeedUpdate
from services.logger import ProductionLogger

logger = ProductionLogger.get("Parser")


@dataclass(slots=True)
class ParsedProductionData:
    """Structured production state produced from one RSS item."""

    house_status: HouseStatus
    competition: CompetitionState
    recognized: bool = False
    fields: tuple[str, ...] = ()


class ProductionParser:
    """Parses JokersUpdates RSS text into cumulative production state."""

    _HOH_WIN_PATTERNS = (
        re.compile(
            r"\b(?P<name>[A-Z][A-Za-z'’.-]*(?:\s+[A-Z][A-Za-z'’.-]*)*)"
            r"\s+(?:won|wins)\s+(?:the\s+)?(?:HOH|head\s+of\s+household)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:HOH|head\s+of\s+household)\s+(?:is|goes\s+to)\s+"
            r"(?P<name>[A-Z][A-Za-z'’.-]*(?:\s+[A-Z][A-Za-z'’.-]*)*)\b",
            re.IGNORECASE,
        ),
    )

    _POV_WIN_PATTERNS = (
        re.compile(
            r"\b(?P<name>[A-Z][A-Za-z'’.-]*(?:\s+[A-Z][A-Za-z'’.-]*)*)"
            r"\s+(?:won|wins)\s+(?:the\s+)?(?:POV|power\s+of\s+veto|veto)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:POV|power\s+of\s+veto|veto)\s+(?:winner|goes\s+to)\s+"
            r"(?P<name>[A-Z][A-Za-z'’.-]*(?:\s+[A-Z][A-Za-z'’.-]*)*)\b",
            re.IGNORECASE,
        ),
    )

    _COMPETITION_WIN_PATTERN = re.compile(
        r"\b(?P<name>[A-Z][A-Za-z'’.-]*(?:\s+[A-Z][A-Za-z'’.-]*)*)"
        r"\s+(?:won|wins)\s+(?:the\s+)?"
        r"(?P<kind>AI\s+Arena|Battle\s+Back|Luxury)\b",
        re.IGNORECASE,
    )

    _NOMINATION_PATTERNS = (
        re.compile(
            r"\b(?:nominees?|nominations?)\s+(?:are|is)\s+(?P<names>[^.!?]+)",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?P<names>[^.!?]+?)\s+(?:were|are)\s+nominated"
            r"(?:\s+for\s+eviction)?\b",
            re.IGNORECASE,
        ),
    )

    _HAVE_NOT_PATTERN = re.compile(
        r"\b(?:have[- ]?nots?)\s*(?:are|is|:)?\s*(?P<names>[^.!?]+)",
        re.IGNORECASE,
    )

    _FEEDS_DOWN = (
        "feeds are down",
        "feeds down",
        "feeds cut",
        "feeds have cut",
        "live feeds are down",
        "live feeds down",
    )

    _FEEDS_UP = (
        "feeds are back",
        "feeds are up",
        "feeds returned",
        "feeds back",
        "live feeds are back",
        "live feeds returned",
    )

    _VETO_USED = (
        "veto was used",
        "veto has been used",
        "used the veto",
        "uses the veto",
        "veto used",
    )

    _VETO_NOT_USED = (
        "veto was not used",
        "did not use the veto",
        "didn't use the veto",
    )

    def __init__(self) -> None:
        self.house_status = HouseStatus()
        self.competition = CompetitionState()
        logger.info("Production parser initialized.")

    def parse(self, update: FeedUpdate) -> ParsedProductionData:
        """Parse one RSS update and return cumulative production state."""

        text = self._clean_text(f"{update.title}. {update.description}")
        fields: list[str] = []

        hoh = self._extract_winner(text, self._HOH_WIN_PATTERNS)
        if hoh:
            self.house_status = self._replace_house_status(hoh=hoh)
            self.competition = CompetitionState(
                competition=CompetitionType.HOH,
                active=False,
                winner=hoh,
                ended_at=datetime.now(UTC),
            )
            fields.extend(("hoh", "competition"))

        pov = self._extract_winner(text, self._POV_WIN_PATTERNS)
        if pov:
            self.house_status = self._replace_house_status(veto_holder=pov)
            self.competition = CompetitionState(
                competition=CompetitionType.POV,
                active=False,
                winner=pov,
                ended_at=datetime.now(UTC),
            )
            fields.extend(("veto_holder", "competition"))

        other_competition = self._extract_competition_winner(text)
        if other_competition is not None:
            competition_type, winner = other_competition
            self.competition = CompetitionState(
                competition=competition_type,
                active=False,
                winner=winner,
                ended_at=datetime.now(UTC),
            )
            fields.append("competition")

        nominees = self._extract_names(text, self._NOMINATION_PATTERNS)
        if nominees:
            self.house_status = self._replace_house_status(nominees=nominees)
            fields.append("nominees")

        have_nots = self._extract_names(text, (self._HAVE_NOT_PATTERN,))
        if have_nots:
            self.house_status = self._replace_house_status(have_nots=have_nots)
            fields.append("have_nots")

        feed_state = self._extract_feed_state(text)
        if feed_state is not None:
            self.house_status = self._replace_house_status(feeds=feed_state)
            fields.append("feeds")

        veto_used = self._extract_veto_used(text)
        if veto_used is not None:
            self.house_status = self._replace_house_status(veto_used=veto_used)
            fields.append("veto_used")

        unique_fields = tuple(dict.fromkeys(fields))
        recognized = bool(unique_fields)

        if recognized:
            logger.info("Parsed RSS production state: %s", ", ".join(unique_fields))
        else:
            logger.debug("RSS item contained no recognized production state: %s", update.title)

        return ParsedProductionData(
            house_status=self.house_status,
            competition=self.competition,
            recognized=recognized,
            fields=unique_fields,
        )

    def _replace_house_status(
        self,
        *,
        hoh: Optional[str] = None,
        nominees: Optional[tuple[str, ...]] = None,
        veto_holder: Optional[str] = None,
        veto_used: Optional[bool] = None,
        have_nots: Optional[tuple[str, ...]] = None,
        feeds: Optional[str] = None,
    ) -> HouseStatus:
        current = self.house_status
        return HouseStatus(
            hoh=current.hoh if hoh is None else hoh,
            nominees=current.nominees if nominees is None else nominees,
            veto_holder=current.veto_holder if veto_holder is None else veto_holder,
            veto_used=current.veto_used if veto_used is None else veto_used,
            have_nots=current.have_nots if have_nots is None else have_nots,
            feeds=current.feeds if feeds is None else feeds,
        )

    @staticmethod
    def _clean_text(value: str) -> str:
        value = unescape(value or "")
        value = re.sub(r"<[^>]+>", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    @staticmethod
    def _extract_winner(
        text: str,
        patterns: tuple[re.Pattern[str], ...],
    ) -> Optional[str]:
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                name = re.sub(r"\s+", " ", match.group("name").strip(" ,.-"))
                if name:
                    return name
        return None

    def _extract_competition_winner(
        self,
        text: str,
    ) -> Optional[tuple[CompetitionType, str]]:
        match = self._COMPETITION_WIN_PATTERN.search(text)
        if match is None:
            return None

        name = match.group("name").strip(" ,.-")
        kind = match.group("kind").lower()

        if kind == "ai arena":
            competition = CompetitionType.AI_ARENA
        elif kind == "battle back":
            competition = CompetitionType.BATTLE_BACK
        else:
            competition = CompetitionType.LUXURY

        return competition, name

    def _extract_names(
        self,
        text: str,
        patterns: tuple[re.Pattern[str], ...],
    ) -> Optional[tuple[str, ...]]:
        for pattern in patterns:
            match = pattern.search(text)
            if match is None:
                continue

            raw = match.group("names")
            raw = re.sub(r"\s*\((?:NT|[^)]*)\)\s*$", "", raw, flags=re.IGNORECASE)
            raw = raw.strip(" .,:;-—–")
            raw = re.split(
                r"\s+(?:for|and then|because|but|as|while)\s+",
                raw,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0]

            parts = re.split(r"\s*,\s*|\s+and\s+|\s*&\s*", raw)
            names = [part.strip(" .,:;-—–") for part in parts if part.strip(" .,:;-—–")]
            names = [name for name in names if len(name.split()) <= 4]

            if names:
                return tuple(dict.fromkeys(names))

        return None

    @classmethod
    def _extract_feed_state(cls, text: str) -> Optional[str]:
        lowered = text.lower()
        if any(phrase in lowered for phrase in cls._FEEDS_DOWN):
            return "down"
        if any(phrase in lowered for phrase in cls._FEEDS_UP):
            return "up"
        return None

    @classmethod
    def _extract_veto_used(cls, text: str) -> Optional[bool]:
        lowered = text.lower()
        if any(phrase in lowered for phrase in cls._VETO_NOT_USED):
            return False
        if any(phrase in lowered for phrase in cls._VETO_USED):
            return True
        return None
