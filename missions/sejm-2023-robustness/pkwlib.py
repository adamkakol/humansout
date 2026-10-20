"""Shared definitions for the Sejm 2023 robustness mission: the frozen PKW data, D'Hondt, thresholds,
inconsistency volumes and witnesses. Used by the mission's check and free for everyone to use.

Standard library only, no network: everything is computed from the three official PKW files frozen with
the mission (pkw_obwody.zip, pkw_okregi.zip, pkw_okregi_wyniki.zip).
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from functools import lru_cache
from pathlib import Path

WORK = Path("/work")
DATA = {"stations": "pkw_obwody.zip", "districts": "pkw_okregi.zip", "official": "pkw_okregi_wyniki.zip"}
# PKW's announcement of 17 October 2023: seats per committee (matched by name, all others 0)
OFFICIAL_SEATS_BY_NAME = {"PRAWO I SPRAWIEDLIWOŚĆ": 194, "KOALICJA OBYWATELSKA": 157, "TRZECIA DROGA": 65,
                          "NOWA LEWICA": 26, "KONFEDERACJA WOLNOŚĆ I NIEPODLEGŁOŚĆ": 18}
SEATS_TOTAL = 460

# protocol fields used here (columns of the station file)
FIELDS = {"A": "Komisja otrzymała kart do głosowania",
          "C": "Nie wykorzystano kart do głosowania",
          "D": "Liczba wyborców, którym wydano karty do głosowania",
          "N": "Liczba kopert na kartę do głosowania wrzuconych do urny",
          "O": "Liczba kart wyjętych z urny",
          "P": "W tym liczba kart wyjętych z kopert na kartę do głosowania",
          "T": "Liczba głosów ważnych oddanych łącznie na wszystkie listy kandydatów"}


def _rows(name: str, folder: Path) -> list:
    with zipfile.ZipFile(folder / name) as z:
        text = z.read(z.namelist()[0]).decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text), delimiter=";"))


def _int(value) -> int:
    value = str(value).strip()
    return int(value) if value else 0   # a blank cell: the list was not registered there, or the field is unused


@lru_cache(maxsize=None)
def data(folder: str = str(WORK)) -> dict:
    folder = Path(folder)
    stations, districts, official = (_rows(DATA[k], folder) for k in ("stations", "districts", "official"))
    names = [c for c in stations[0] if c.startswith(("KOMITET", "KOALICYJNY"))]
    lists = {f"L{i + 1:02d}": name for i, name in enumerate(names)}
    by_name = {name: lid for lid, name in lists.items()}
    out = {"lists": lists,
           "coalitions": {lid for lid, name in lists.items() if name.startswith("KOALICYJNY")},
           "minorities": {lid for lid, name in lists.items() if "MNIEJSZOŚĆ" in name},
           "seats": {int(r["Numer okręgu"]): int(r["Liczba mandatów"]) for r in districts},
           "official_seats": {lid: next((n for key, n in OFFICIAL_SEATS_BY_NAME.items() if key in name), 0)
                              for lid, name in lists.items()},
           "votes": {}, "stations": []}
    for r in official:
        out["votes"][int(r["Nr okręgu"])] = {by_name[n]: _int(r[n]) for n in names}
        out.setdefault("official_valid", {})[int(r["Nr okręgu"])] = _int(r[FIELDS["T"]])
    for r in stations:
        s = {k: _int(r[col]) for k, col in FIELDS.items()}
        s["id"] = f"{r['TERYT Gminy'].strip()}/{r['Nr komisji'].strip()}"   # TERYT is empty abroad and on ships
        s["district"] = int(r["Nr okręgu"])
        s["lists"] = {by_name[n]: _int(r[n]) for n in names}
        out["stations"].append(s)
    return out


# ---- the electoral rules ----------------------------------------------------------------------------
def eligible(national: dict, coalitions: set, minorities: set) -> set:
    """5% of all valid votes, 8% for coalitions, national minorities exempt; exact integer comparison."""
    total = sum(national.values())
    return {lid for lid, v in national.items()
            if lid in minorities or (total and v * 100 >= (8 if lid in coalitions else 5) * total)}


def allocate(seats: int, votes: dict, eligible_lists: set) -> dict:
    """D'Hondt in one district. A tie for the last seat(s) goes to the list with more votes in the district,
    then to the lower list number (the code's final tiebreak, the number of polling stations, is not in the data)."""
    result = {lid: 0 for lid in votes}
    taking = [lid for lid in sorted(votes) if lid in eligible_lists and votes[lid] > 0]
    for _ in range(seats if taking else 0):
        best = taking[0]
        for lid in taking[1:]:
            a, b = votes[lid] * (result[best] + 1), votes[best] * (result[lid] + 1)
            if a > b or (a == b and votes[lid] > votes[best]):
                best = lid
        result[best] += 1
    return result


def national_votes(d: dict | None = None) -> dict:
    d = d or data()
    lists = d["lists"]
    return {lid: sum(v[lid] for v in d["votes"].values()) for lid in lists}


def national_eligible(d: dict | None = None) -> set:
    d = d or data()
    return eligible(national_votes(d), d["coalitions"], d["minorities"])


def district_seats(district: int, votes: dict | None = None, d: dict | None = None) -> dict:
    d = d or data()
    return allocate(d["seats"][district], votes if votes is not None else d["votes"][district], national_eligible(d))


def apply_witness(district: int, witness: dict, d: dict | None = None) -> tuple:
    """Votes after adding (positive) or removing (negative) votes per list; returns (votes, cost)."""
    d = d or data()
    votes = dict(d["votes"][district])
    cost = 0
    for lid, delta in witness.items():
        if lid not in votes or not isinstance(delta, int) or isinstance(delta, bool):
            raise ValueError(f"witness entries must be list ids with integer changes: {lid!r}: {delta!r}")
        votes[lid] += delta
        if votes[lid] < 0:
            raise ValueError(f"{lid} would have negative votes")
        cost += abs(delta)
    return votes, cost


def flips(district: int, witness: dict, d: dict | None = None) -> bool:
    """Does this change of votes change the district's seat allocation (national eligibility unchanged)?"""
    d = d or data()
    votes, _ = apply_witness(district, witness, d)
    return district_seats(district, votes, d) != district_seats(district, None, d)


# ---- inconsistency volumes -----------------------------------------------------------------------------
def station_volume(s: dict) -> dict:
    """strict: ballots in the box beyond those issued in the station and envelopes put in the box, plus ballots
    taken from envelopes beyond the envelopes; conservative: strict plus the card balance gap |A - C - D|."""
    strict = max(0, s["O"] - s["D"] - s["N"]) + max(0, s["P"] - s["N"])
    return {"strict": strict, "conservative": strict + abs(s["A"] - s["C"] - s["D"])}


def district_volume(district: int, d: dict | None = None) -> dict:
    """Volumes summed over the district's stations, plus votes in the official totals that the station
    protocols do not account for (|official valid votes - sum of station valid votes|)."""
    d = d or data()
    st = [s for s in d["stations"] if s["district"] == district]
    unreconciled = abs(d["official_valid"][district] - sum(s["T"] for s in st))
    strict = sum(station_volume(s)["strict"] for s in st) + unreconciled
    conservative = sum(station_volume(s)["conservative"] for s in st) + unreconciled
    return {"strict": strict, "conservative": conservative, "unreconciled": unreconciled}


def top_stations(district: int, n: int = 5, d: dict | None = None) -> list:
    """The n stations with the largest conservative volume (ties: by station id)."""
    d = d or data()
    st = [s for s in d["stations"] if s["district"] == district]
    ranked = sorted(st, key=lambda s: (-station_volume(s)["conservative"], s["id"]))[:n]
    return [{"station": s["id"], **station_volume(s)} for s in ranked]


def load_result(path: Path = WORK / "result.json") -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
