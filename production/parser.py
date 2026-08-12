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
            r"\b(?P<name>[A-Z][A-Za-z'’.-]*(?:\s+[A-Z][A-Za-z'’.-]*){0,2})"
            r"\s+(?i:won|wins)\s+(?:the\s+)?(?i:HOH|head\s+of\s+household)\b"
        ),
        re.compile(
            r"\b(?i:HOH|head\s+of\s+household)\s+(?i:is|goes\s+to)\s+"
            r"(?P<name>[A-Z][A-Za-z'’.-]*(?:\s+[A-Z][A-Za-z'’.-]*){0,2})\b"
        ),
    )

    _POV_WIN_PATTERNS = (
        re.compile(
            r"\b(?P<name>[A-Z][A-Za-z'’.-]*(?:\s+[A-Z][A-Za-z'’.-]*){0,2})"
            r"\s+(?i:won|wins)\s+(?:the\s+)?(?i:POV|power\s+of\s+veto|veto)\b"
        ),
        re.compile(
            r"\b(?i:POV|power\s+of\s+veto|veto)\s+(?i:winner|goes\s+to)\s+"
            r"(?P<name>[A-Z][A-Za-z'’.-]*(?:\s+[A-Z][A-Za-z'’.-]*){0,2})\b"
        ),
    )

    _COMPETITION_WIN_PATTERN = re.compile(
        r"\b(?P<name>[A-Z][A-Za-z'’.-]*(?:\s+[A-Z][A-Za-z'’.-]*){0,2})"
        r"\s+(?i:won|wins)\s+(?:the\s+)?"
        r"(?P<kind>(?i:AI\s+Arena|Battle\s+Back|Luxury))\b"
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

    # Matches discourse markers indicating a mention is reported or
    # hypothetical speech ("Drew brings up if Yash won HOH") rather
    # than a direct announcement of a real result. BB live-feed
    # recaps are full of houseguests referencing past, hypothetical,
    # or rumored outcomes in conversation.
    _REPORTED_SPEECH_MARKERS = re.compile(
        r"\b(?:if|whether|when|since|after|before"
        r"|recalls?|recalled|remembers?|remembered"
        r"|mentions?|mentioned|asks?|asked"
        r"|wonders?|wondered|brings?\s+up|brought\s+up"
        r"|talks?\s+about|talked\s+about|discuss(?:es|ed)?"
        r"|thinks?|thought|said|claims?|claimed"
        r"|reminds?|reminded|notes?|noted"
        r"|points?\s+out|pointed\s+out|references?|referenced)"
        r"\s+(?:\S+\s+){0,4}$",
        re.IGNORECASE,
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

    @classmethod
    def _extract_winner(
        cls,
        text: str,
        patterns: tuple[re.Pattern[str], ...],
    ) -> Optional[str]:
        for pattern in patterns:
            for match in pattern.finditer(text):
                name = re.sub(r"\s+", " ", match.group("name").strip(" ,.-"))
                if not name:
                    continue
                if not cls._is_genuine_announcement(text, match, name):
                    continue
                return name
        return None

    @classmethod
    def _is_genuine_announcement(
        cls,
        text: str,
        match: "re.Match[str]",
        name: str,
    ) -> bool:
        """Rejects matches that look like a name/result but aren't a
        genuine, direct announcement.

        Two independent checks:

        re.IGNORECASE case-folds the entire pattern it's applied to,
        including the [A-Z] meant to require a capitalized name, so
        without this check the pattern would accept any lowercase
        word right before "won HOH" as if it were a name. Verifying
        capitalization here, against the real text, restores what
        the character class looks like it already guarantees but
        doesn't under IGNORECASE.

        Separately, BB live-feed recaps are full of houseguests
        referencing past or hypothetical outcomes in conversation
        ("Drew brings up if Yash won HOH") -- rejecting matches
        immediately preceded by reported-speech markers keeps those
        from being read as fresh results.
        """

        if not name[0].isupper():
            return False

        preceding = text[max(0, match.start() - 80):match.start()]
        if cls._REPORTED_SPEECH_MARKERS.search(preceding):
            return False

        return True

    def _extract_competition_winner(
        self,
        text: str,
    ) -> Optional[tuple[CompetitionType, str]]:
        for match in self._COMPETITION_WIN_PATTERN.finditer(text):
            name = match.group("name").strip(" ,.-")

            if not self._is_genuine_announcement(text, match, name):
                continue

            kind = match.group("kind").lower()

            if kind == "ai arena":
                competition = CompetitionType.AI_ARENA
            elif kind == "battle back":
                competition = CompetitionType.BATTLE_BACK
            else:
                competition = CompetitionType.LUXURY

            return competition, name

        return None

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
