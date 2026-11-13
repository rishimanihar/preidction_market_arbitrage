"""Team alias tables and label resolution, scoped by league.

One CSV per league lives in ``pmarb/matching/data``. A ``TeamTable`` indexes each team by its
Kalshi code, Polymarket US code, and every label (display name, city or school, nickname,
aliases). Resolution is case, whitespace and punctuation insensitive. A label that names more
than one team in the league raises ``AmbiguousTeam``; an unknown label returns ``None``.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from functools import cache
from importlib import resources

LEAGUES = ("nfl", "nba", "nhl", "ncaaf", "ncaamb", "ncaawb")
_WOMEN_LEAGUES = frozenset({"ncaawb"})
_WOMEN_MARK = re.compile(r"\(\s*w\s*\)|\bwomen'?s\b", re.IGNORECASE)
_SLASH = re.compile(r"^(?P<prefix>.*\S\s+)(?P<a>\w+)\s*/\s*(?P<b>\w+)$")


class AmbiguousTeam(ValueError):
    """A label matches more than one team in the league."""

    def __init__(self, text: str, league: str, candidates: list[str]) -> None:
        self.text = text
        self.league = league
        self.candidates = sorted(candidates)
        super().__init__(f"ambiguous {league} team label {text!r}: {', '.join(self.candidates)}")


@dataclass(frozen=True, slots=True)
class Team:
    canonical_id: str
    league: str
    display_name: str
    city_or_school: str
    nickname: str
    kalshi_code: str
    polymarket_code: str
    aliases: tuple[str, ...]
    source: str


def _code_key(code: str) -> str:
    return re.sub(r"[^a-z0-9]", "", code.lower())


def _label_key(text: str) -> str:
    """Normalize spelling, preserving Saint at the start and State at the end."""
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    s = _WOMEN_MARK.sub(" ", s)
    s = re.sub(r"['’`]", "", s).replace("&", " and ")
    tokens = re.sub(r"[^a-z0-9]+", " ", s).split()
    tokens = ["university" if token == "univ" else token for token in tokens]
    if len(tokens) > 1 and tokens[0] == "st":
        tokens[0] = "saint"
    if tokens and tokens[-1] == "st":
        tokens[-1] = "state"
    return " ".join(tokens)


def _has_women_mark(text: str) -> bool:
    return bool(_WOMEN_MARK.search(text))


class TeamTable:
    def __init__(self, league: str, teams: list[Team]) -> None:
        self.league = league
        self.teams = tuple(teams)
        self._by_id = {t.canonical_id: t for t in teams}
        if len(self._by_id) != len(teams):
            raise ValueError(f"duplicate canonical_id in {league} table")
        self._kalshi: dict[str, set[str]] = defaultdict(set)
        self._poly: dict[str, set[str]] = defaultdict(set)
        self._labels: dict[str, set[str]] = defaultdict(set)
        for t in teams:
            if t.kalshi_code:
                self._kalshi[_code_key(t.kalshi_code)].add(t.canonical_id)
                self._labels[_label_key(t.kalshi_code)].add(t.canonical_id)
            if t.polymarket_code:
                self._poly[_code_key(t.polymarket_code)].add(t.canonical_id)
                self._labels[_label_key(t.polymarket_code)].add(t.canonical_id)
            for label in (t.display_name, t.city_or_school, t.nickname, *t.aliases):
                if label:
                    self._labels[_label_key(label)].add(t.canonical_id)
        self._labels.pop("", None)

    def __len__(self) -> int:
        return len(self.teams)

    def get(self, canonical_id: str) -> Team | None:
        return self._by_id.get(canonical_id)

    def _pick(self, text: str, ids: set[str]) -> str | None:
        if not ids:
            return None
        if len(ids) > 1:
            raise AmbiguousTeam(text, self.league, list(ids))
        return next(iter(ids))

    def resolve_label(self, text: str) -> str | None:
        """Resolve any team label. None if unknown; AmbiguousTeam if it names several teams."""
        if not text or not text.strip():
            return None
        if _has_women_mark(text) and self.league not in _WOMEN_LEAGUES:
            return None
        slash = _SLASH.match(text.strip())
        if slash:
            prefix = slash["prefix"]
            hits = {
                tid
                for part in (slash["a"], slash["b"])
                if (tid := self._single(prefix + part)) is not None
            }
            if len(hits) > 1:
                raise AmbiguousTeam(text, self.league, list(hits))
            return next(iter(hits)) if hits else None
        return self._single(text)

    def _single(self, text: str) -> str | None:
        return self._pick(text, self._labels.get(_label_key(text), set()))

    def resolve_kalshi(self, code_or_label: str) -> str | None:
        """Kalshi ticker code first (e.g. ``NYG``), then any label (e.g. ``New York G``)."""
        hit = self._pick(code_or_label, self._kalshi.get(_code_key(code_or_label), set()))
        return hit if hit is not None else self.resolve_label(code_or_label)

    def resolve_kalshi_code(self, code: str) -> str | None:
        """Resolve only the Kalshi code namespace, without a label fallback."""
        return self._pick(code, self._kalshi.get(_code_key(code), set()))

    def resolve_polymarket_code(self, code: str) -> str | None:
        """Resolve only the Polymarket code namespace, without a label fallback."""
        return self._pick(code, self._poly.get(_code_key(code), set()))

    def resolve_polymarket(self, code_or_label: str) -> str | None:
        """Polymarket US slug code first (e.g. ``nyg``), then any label."""
        hit = self._pick(code_or_label, self._poly.get(_code_key(code_or_label), set()))
        return hit if hit is not None else self.resolve_label(code_or_label)


@cache
def load_league(league: str) -> TeamTable:
    if league not in LEAGUES:
        raise ValueError(f"unknown league {league!r}; expected one of {', '.join(LEAGUES)}")
    path = resources.files("pmarb.matching").joinpath("data", f"{league}.csv")
    with path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    teams = [
        Team(
            canonical_id=r["canonical_id"],
            league=r["league"],
            display_name=r["display_name"],
            city_or_school=r["city_or_school"],
            nickname=r["nickname"],
            kalshi_code=r["kalshi_code"],
            polymarket_code=r["polymarket_code"],
            aliases=tuple(a for a in r["aliases"].split("|") if a),
            source=r["source"],
        )
        for r in rows
    ]
    return TeamTable(league, teams)
