"""Team alias tables: completeness, resolution, known traps, and local-dump coverage.

Codes below are the Kalshi ticker codes and Polymarket US slug codes seen in the overlap study;
the four NFL teams absent from the dumps (CHI, DEN, PHI, TB) carry inferred codes.
"""

import json
import re
from collections import defaultdict
from importlib import resources
from pathlib import Path

import pytest

from pmarb.matching.teams import LEAGUES, AmbiguousTeam, load_league

# canonical suffix -> (kalshi_code, polymarket_code)
NFL_CODES = {
    "ARI": ("ARI", "ari"),
    "ATL": ("ATL", "atl"),
    "BAL": ("BAL", "bal"),
    "BUF": ("BUF", "buf"),
    "CAR": ("CAR", "car"),
    "CHI": ("CHI", "chi"),
    "CIN": ("CIN", "cin"),
    "CLE": ("CLE", "cle"),
    "DAL": ("DAL", "dal"),
    "DEN": ("DEN", "den"),
    "DET": ("DET", "det"),
    "GB": ("GB", "gb"),
    "HOU": ("HOU", "hou"),
    "IND": ("IND", "ind"),
    "JAX": ("JAC", "jax"),
    "KC": ("KC", "kc"),
    "LAC": ("LAC", "lac"),
    "LAR": ("LAR", "lar"),
    "LV": ("LV", "lv"),
    "MIA": ("MIA", "mia"),
    "MIN": ("MIN", "min"),
    "NE": ("NE", "ne"),
    "NO": ("NO", "no"),
    "NYG": ("NYG", "nyg"),
    "NYJ": ("NYJ", "nyj"),
    "PHI": ("PHI", "phi"),
    "PIT": ("PIT", "pit"),
    "SEA": ("SEA", "sea"),
    "SF": ("SF", "sf"),
    "TB": ("TB", "tb"),
    "TEN": ("TEN", "ten"),
    "WAS": ("WAS", "was"),
}

NBA_CODES = {
    "ATL": ("ATL", "atl"),
    "BKN": ("BKN", "bkn"),
    "BOS": ("BOS", "bos"),
    "CHA": ("CHA", "cha"),
    "CHI": ("CHI", "chi"),
    "CLE": ("CLE", "cle"),
    "DAL": ("DAL", "dal"),
    "DEN": ("DEN", "den"),
    "DET": ("DET", "det"),
    "GSW": ("GSW", "gs"),
    "HOU": ("HOU", "hou"),
    "IND": ("IND", "ind"),
    "LAC": ("LAC", "lac"),
    "LAL": ("LAL", "lal"),
    "MEM": ("MEM", "mem"),
    "MIA": ("MIA", "mia"),
    "MIL": ("MIL", "mil"),
    "MIN": ("MIN", "min"),
    "NOP": ("NOP", "no"),
    "NYK": ("NYK", "ny"),
    "OKC": ("OKC", "okc"),
    "ORL": ("ORL", "orl"),
    "PHI": ("PHI", "phi"),
    "PHX": ("PHX", "pho"),
    "POR": ("POR", "por"),
    "SAC": ("SAC", "sac"),
    "SAS": ("SAS", "sa"),
    "TOR": ("TOR", "tor"),
    "UTA": ("UTA", "uta"),
    "WAS": ("WAS", "was"),
}

NHL_CODES = {
    "ANA": ("ANA", "ana"),
    "BOS": ("BOS", "bos"),
    "BUF": ("BUF", "buf"),
    "CAR": ("CAR", "car"),
    "CBJ": ("CBJ", "cbj"),
    "CGY": ("CGY", "cgy"),
    "CHI": ("CHI", "chi"),
    "COL": ("COL", "col"),
    "DAL": ("DAL", "dal"),
    "DET": ("DET", "det"),
    "EDM": ("EDM", "edm"),
    "FLA": ("FLA", "fla"),
    "LAK": ("LA", "la"),
    "MIN": ("MIN", "min"),
    "MTL": ("MTL", "mon"),
    "NJD": ("NJ", "nj"),
    "NSH": ("NSH", "nas"),
    "NYI": ("NYI", "nyi"),
    "NYR": ("NYR", "nyr"),
    "OTT": ("OTT", "ott"),
    "PHI": ("PHI", "phi"),
    "PIT": ("PIT", "pit"),
    "SEA": ("SEA", "sea"),
    "SJS": ("SJ", "sj"),
    "STL": ("STL", "stl"),
    "TBL": ("TB", "tb"),
    "TOR": ("TOR", "tor"),
    "UTA": ("UTA", "uta"),
    "VAN": ("VAN", "van"),
    "VGK": ("VGK", "veg"),
    "WPG": ("WPG", "wpg"),
    "WSH": ("WSH", "was"),
}

PRO = {"nfl": NFL_CODES, "nba": NBA_CODES, "nhl": NHL_CODES}
EXPECTED_COUNT = {"nfl": 32, "nba": 30, "nhl": 32}

SCRATCH = Path(
    "/private/tmp/claude-501/-Users-avynarra-Documents-preidction-market-arbitrage/"
    "15953eab-eae9-4308-824e-ae5ba92c4b48/scratchpad/research/matching/raw"
)
SERIES = {
    "nfl": "KXNFLGAME",
    "nba": "KXNBAGAME",
    "nhl": "KXNHLGAME",
    "ncaaf": "KXNCAAFGAME",
    "ncaamb": "KXNCAAMBGAME",
    "ncaawb": "KXNCAAWBGAME",
}
POLY_LEAGUE = {
    "nfl": "nfl",
    "nba": "nba",
    "nhl": "nhl",
    "ncaaf": "cfb",
    "ncaamb": "cbb",
    "ncaawb": "wcbb",
}


# --- package data ---------------------------------------------------------------------------


@pytest.mark.parametrize("league", LEAGUES)
def test_data_files_are_package_resources(league):
    res = resources.files("pmarb.matching").joinpath("data", f"{league}.csv")
    assert res.is_file()
    header = res.read_text(encoding="utf-8").splitlines()[0]
    assert header == (
        "canonical_id,league,display_name,city_or_school,nickname,"
        "kalshi_code,polymarket_code,aliases,source"
    )


@pytest.mark.parametrize("league", LEAGUES)
def test_table_integrity(league):
    table = load_league(league)
    assert len(table) > 0
    for team in table.teams:
        assert team.canonical_id.startswith(f"{league}:")
        assert team.league == league
        assert team.source in {"observed", "inferred"}
        assert team.kalshi_code or team.polymarket_code


def test_unknown_league_rejected():
    with pytest.raises(ValueError):
        load_league("mlb")


# --- complete pro leagues -------------------------------------------------------------------


@pytest.mark.parametrize("league", PRO)
def test_pro_league_complete(league):
    table = load_league(league)
    assert len(table) == EXPECTED_COUNT[league]
    assert {t.canonical_id for t in table.teams} == {f"{league}:{c}" for c in PRO[league]}


@pytest.mark.parametrize("league", PRO)
def test_pro_codes_resolve_on_both_venues(league):
    table = load_league(league)
    for canon, (kcode, pcode) in PRO[league].items():
        cid = f"{league}:{canon}"
        assert table.resolve_kalshi(kcode) == cid, (league, kcode)
        assert table.resolve_kalshi(kcode.lower()) == cid
        assert table.resolve_polymarket(pcode) == cid, (league, pcode)
        assert table.resolve_polymarket(pcode.upper()) == cid
        team = table.get(cid)
        assert (team.kalshi_code, team.polymarket_code) == (kcode, pcode)
        assert table.resolve_label(team.display_name) == cid


def test_only_four_nfl_teams_are_inferred():
    inferred = {t.canonical_id for t in load_league("nfl").teams if t.source == "inferred"}
    assert inferred == {"nfl:CHI", "nfl:DEN", "nfl:PHI", "nfl:TB"}
    for league in ("nba", "nhl"):
        assert all(t.source == "observed" for t in load_league(league).teams)


def test_nhl_includes_utah():
    table = load_league("nhl")
    assert table.resolve_kalshi("UTA") == "nhl:UTA"
    assert table.resolve_polymarket("uta") == "nhl:UTA"
    assert table.resolve_label("Utah Mammoth") == "nhl:UTA"


# --- known traps ----------------------------------------------------------------------------


def test_new_york_giants_and_jets_are_distinct():
    nfl = load_league("nfl")
    assert nfl.resolve_kalshi("NYG") == "nfl:NYG"
    assert nfl.resolve_kalshi("NYJ") == "nfl:NYJ"
    assert nfl.resolve_kalshi("New York G") == "nfl:NYG"
    assert nfl.resolve_kalshi("New York J") == "nfl:NYJ"
    assert nfl.resolve_polymarket("nyg") == "nfl:NYG"
    assert nfl.resolve_label("NY Giants") == "nfl:NYG"
    assert nfl.resolve_label("Jets") == "nfl:NYJ"


def test_new_york_g_slash_j_is_ambiguous():
    nfl = load_league("nfl")
    with pytest.raises(AmbiguousTeam) as exc:
        nfl.resolve_label("New York G/J")
    assert exc.value.candidates == ["nfl:NYG", "nfl:NYJ"]


@pytest.mark.parametrize("label", ["New York", "Los Angeles"])
def test_bare_shared_city_is_ambiguous_in_nfl(label):
    with pytest.raises(AmbiguousTeam):
        load_league("nfl").resolve_label(label)


def test_los_angeles_split_labels():
    nfl, nba, nhl = load_league("nfl"), load_league("nba"), load_league("nhl")
    assert nfl.resolve_kalshi("Los Angeles C") == "nfl:LAC"
    assert nfl.resolve_kalshi("Los Angeles R") == "nfl:LAR"
    assert nba.resolve_kalshi("Los Angeles L") == "nba:LAL"
    with pytest.raises(AmbiguousTeam):
        nba.resolve_label("Los Angeles")
    assert nhl.resolve_kalshi("Los Angeles") == "nhl:LAK"  # only one NHL team there


def test_jets_scoped_by_league():
    assert load_league("nhl").resolve_polymarket("Jets") == "nhl:WPG"
    assert load_league("nhl").resolve_label("Winnipeg") == "nhl:WPG"
    assert load_league("nfl").resolve_polymarket("Jets") == "nfl:NYJ"
    assert load_league("nfl").resolve_label("Winnipeg Jets") is None
    assert load_league("nhl").resolve_label("New York Jets") is None


def test_wizards_vs_commanders_by_league():
    assert load_league("nba").resolve_kalshi("WAS") == "nba:WAS"
    assert load_league("nfl").resolve_kalshi("WAS") == "nfl:WAS"
    assert load_league("nhl").resolve_kalshi("WSH") == "nhl:WSH"
    assert load_league("nhl").resolve_polymarket("was") == "nhl:WSH"


def test_label_normalization():
    nhl = load_league("nhl")
    assert nhl.resolve_label("  st.   LOUIS ") == "nhl:STL"
    assert nhl.resolve_label("MONTRÉAL") == "nhl:MTL"
    assert nhl.resolve_label("maple-leafs") == "nhl:TOR"
    assert nhl.resolve_label("") is None
    assert nhl.resolve_label("Nonexistent Team") is None


def test_womens_marker():
    wb = load_league("ncaawb")
    assert wb.resolve_label("Alabama (W)") == wb.resolve_label("Alabama")
    assert wb.resolve_label("Alabama (W)") is not None
    assert wb.resolve_polymarket("Temple (W)") == wb.resolve_label("Temple")
    # a women's label never resolves into a men's table
    assert load_league("ncaamb").resolve_label("Alabama (W)") is None
    assert load_league("ncaamb").resolve_label("Alabama") is not None
    assert load_league("nba").resolve_label("Boston (W)") is None


def test_ncaa_nickname_ambiguity_never_guessed():
    with pytest.raises(AmbiguousTeam):
        load_league("ncaaf").resolve_label("Wildcats")


def test_ncaa_state_abbreviation():
    mb = load_league("ncaamb")
    assert mb.resolve_label("Iowa St.") == mb.resolve_label("Iowa State")
    assert mb.resolve_label("Iowa St.") is not None
    assert mb.resolve_label("Miami (OH)") != mb.resolve_label("Miami")


# --- local-dump coverage --------------------------------------------------------------------


def _kalshi_events(series: str) -> dict[str, dict[str, str]]:
    """event ticker -> {team code: yes label}, for one game series, both 7-day dumps."""
    events: dict[str, dict[str, str]] = defaultdict(dict)
    for fn in ("kalshi_primary_7d.json", "kalshi_close_time_7d.json"):
        data = json.loads((SCRATCH / fn).read_text())
        for markets in data.values():
            if not isinstance(markets, list):
                continue
            for m in markets:
                if m["event_ticker"].startswith(series + "-"):
                    events[m["event_ticker"]][m["ticker"].rsplit("-", 1)[1]] = m["yes_sub_title"]
    return events


@pytest.mark.localdata
@pytest.mark.skipif(not SCRATCH.is_dir(), reason="local overlap-study dumps not present")
@pytest.mark.parametrize("league", LEAGUES)
def test_kalshi_game_coverage(league, record_property):
    table = load_league(league)
    events = _kalshi_events(SERIES[league])
    assert events, "no game events found for series"
    unresolved: list[str] = []
    ok = 0
    for ev, teams in events.items():
        ids = []
        for code, label in teams.items():
            by_code = table.resolve_kalshi(code)
            by_label = table.resolve_kalshi(label)
            ids.append(by_code)
            if by_code is None or by_label != by_code:
                unresolved.append(f"{ev}:{code}:{label}")
        if len(ids) == 2 and None not in ids and ids[0] != ids[1]:
            ok += 1
    pct = 100.0 * ok / len(events)
    record_property("coverage_pct", round(pct, 2))
    print(f"\nKalshi {SERIES[league]} coverage: {ok}/{len(events)} = {pct:.2f}% {unresolved[:5]}")
    if league in PRO:
        assert pct == 100.0, unresolved[:10]
    else:
        assert pct >= 99.0, unresolved[:10]


@pytest.mark.localdata
@pytest.mark.skipif(not SCRATCH.is_dir(), reason="local overlap-study dumps not present")
@pytest.mark.parametrize("league", LEAGUES)
def test_polymarket_game_coverage(league):
    table = load_league(league)
    data = json.loads((SCRATCH / "poly_league_events_window.json").read_text())["events"]
    events = [e for e in data if e["league"] == POLY_LEAGUE[league]]
    assert events
    ok = 0
    bad: list[str] = []
    for e in events:
        sides = [s["team"] for s in e["sides"]]
        ids = [table.resolve_polymarket(t["abbreviation"]) for t in sides]
        if len(ids) == 2 and None not in ids and ids[0] != ids[1]:
            ok += 1
        else:
            bad.append(re.sub(r"-\d{4}-\d{2}-\d{2}$", "", e["event_slug"]))
    pct = 100.0 * ok / len(events)
    print(f"\nPolymarket {POLY_LEAGUE[league]} coverage: {ok}/{len(events)} = {pct:.2f}% {bad[:5]}")
    assert pct == 100.0, bad[:10]
