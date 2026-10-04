"""
World airport knowledge for the agent: search, place-name resolution and
the shared loader for the offline flight model.

All data comes from agent/data/flight_model.json.gz, compiled offline by
scripts/build_flight_model.py from OurAirports (public domain) and
OpenFlights (ODbL) -- see that script's docstring. Nothing here calls a
network service or needs an API key.

What lives here:
  * load_model()          -- the compiled model, parsed once per process.
  * search_airports()     -- ranked autocomplete for the /flights From/To
                             fields (backs GET /api/airports).
  * resolve_place()       -- "delhi" / "Bombay" / "heathrow" / "JFK" /
                             "NYC" -> the airport(s) a traveller means.
  * match_place_at()      -- longest place-name match starting at a token,
                             used by the NLU (agent/nlu.py) to pull origin
                             and destination out of free text.

Ranking uses each airport's connectivity score from the model (how many
airline-routes touch it), so "new york" lists JFK/EWR/LGA before Islip and
a bare "London" resolves to Heathrow, not London, Ontario.
"""
from __future__ import annotations

import difflib
import gzip
import json
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

MODEL_PATH = Path(__file__).resolve().parent / "data" / "flight_model.json.gz"

# Common English words that are also city names / aliases somewhere in the
# world. They only count as a place right after an explicit cue ("from",
# "to", ...) or when written with a capital letter -- so "a nice flight"
# never resolves to Nice, but "to Nice" does.
AMBIGUOUS_WORDS = {
    "nice", "split", "male", "mobile", "normal", "page", "hope", "eagle",
    "price", "rock", "sale", "union", "victoria", "university", "bath",
    "reading", "mission", "orange", "spring", "liberal", "independence",
    "commerce", "friendship", "grand", "college", "lake", "valley", "city",
    "station", "airport", "international", "national", "regional", "central",
}

# Lowercase 3-letter words that are also IATA codes. Only treated as a code
# when typed in capitals ("THE" would be Teresina, Brazil).
_COMMON_THREE_LETTER = {
    "the", "and", "for", "you", "any", "can", "get", "see", "may", "new", "now",
    "one", "two", "out", "how", "all", "its", "our", "but", "not", "are", "was",
    "day", "way", "buy", "fly", "via", "who", "why", "yes", "use", "her", "his",
    "him", "she", "too", "off", "own", "per", "set", "top", "try", "far", "big",
    "low", "end", "few", "let", "put", "say", "sun", "mon", "tue", "wed", "thu",
    "fri", "sat", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "oct",
    "nov", "dec", "pls", "asap", "eta",
}


def fold(text: str) -> str:
    """Lowercase + strip accents, so 'São Paulo' == 'sao paulo' and
    'Zürich' == 'zurich'."""
    norm = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in norm if not unicodedata.combining(ch)).lower().strip()


@dataclass(frozen=True)
class Airport:
    iata: str
    name: str
    city: str
    country: str
    country_name: str
    lat: float
    lon: float
    kind: str               # "large" | "medium"
    tz: Optional[str]
    utc_offset: float       # standard-time offset, fallback when tzdata is missing
    aliases: tuple = field(default_factory=tuple)
    weight: int = 0         # airline-routes touching this airport
    continent: str = ""     # AF AS EU NA OC SA

    @property
    def has_routes(self) -> bool:
        return self.weight > 0

    def to_dict(self) -> dict:
        return {
            "iata": self.iata,
            "name": self.name,
            "city": self.city,
            "country": self.country,
            "country_name": self.country_name,
            "lat": self.lat,
            "lon": self.lon,
            "kind": self.kind,
            "continent": self.continent,
            "has_routes": self.has_routes,
            "routes": self.weight,
            "label": f"{self.iata} — {self.city}",
        }


class _Index:
    def __init__(self, model: dict) -> None:
        countries = model.get("countries", {})
        continent_of = model.get("country_continent", {})
        self.continents = dict(model.get("continents", {}))
        self.countries = dict(countries)
        self.country_continent = dict(continent_of)
        self.meta = model.get("meta", {})
        self.airports: list[Airport] = []
        for row in model["airports"]:
            code, name, city, cc, lat, lon, kind, tz, off, aliases, weight = row
            self.airports.append(Airport(
                iata=code, name=name, city=city, country=cc,
                country_name=countries.get(cc, cc), lat=lat, lon=lon,
                kind="large" if kind == "L" else "medium", tz=tz,
                utc_offset=float(off if off is not None else 0.0),
                aliases=tuple(aliases), weight=int(weight), continent=continent_of.get(cc, ""),
            ))
        self.by_iata = {a.iata: a for a in self.airports}

        # name key (folded) -> airports, best-connected first
        keys: dict[str, list[Airport]] = {}
        self.city_keys: set[str] = set()

        def add(key: str, airport: Airport, *, city: bool = False) -> None:
            k = fold(key)
            if len(k) < 2:
                return
            bucket = keys.setdefault(k, [])
            if airport not in bucket:
                bucket.append(airport)
            if city:
                self.city_keys.add(k)

        for a in self.airports:
            add(a.city, a, city=True)
            add(a.name, a)
            for alias in a.aliases:
                add(alias, a, city=True)
        # "New York City" (an alias of EWR) also means "New York"
        for k in list(keys):
            if k.endswith(" city") and k[:-5] in self.city_keys:
                for a in keys[k]:
                    if a not in keys[k[:-5]]:
                        keys[k[:-5]].append(a)
        for bucket in keys.values():
            bucket.sort(key=lambda a: (a.has_routes, a.weight), reverse=True)
        self.keys = keys
        self.max_key_tokens = max((len(k.split()) for k in keys), default=1)
        self._key_list = list(keys)

    def ranked(self, airports: Iterable[Airport]) -> list[Airport]:
        return sorted(airports, key=lambda a: (a.has_routes, a.weight), reverse=True)


@lru_cache(maxsize=1)
def load_model() -> dict:
    """The raw compiled model (see scripts/build_flight_model.py)."""
    with gzip.open(MODEL_PATH, "rb") as fh:
        return json.loads(fh.read().decode("utf-8"))


@lru_cache(maxsize=1)
def _index() -> _Index:
    return _Index(load_model())


def all_airports() -> list[Airport]:
    return list(_index().airports)


def model_meta() -> dict:
    return dict(_index().meta)


def continents() -> dict:
    """{"AS": "Asia", ...} for continents that have airports in the model."""
    idx = _index()
    used = {a.continent for a in idx.airports}
    return {k: v for k, v in idx.continents.items() if k in used}


def countries(continent: str = "") -> list[dict]:
    """Countries with at least one airport, optionally within a continent,
    biggest aviation markets first."""
    idx = _index()
    totals: dict[str, list] = {}
    for a in idx.airports:
        if continent and a.continent != continent:
            continue
        entry = totals.setdefault(a.country, [0, 0])
        entry[0] += 1
        entry[1] += a.weight
    out = [
        {"code": cc, "name": idx.countries.get(cc, cc), "continent": idx.country_continent.get(cc, ""),
         "airports": n, "routes": w}
        for cc, (n, w) in totals.items()
    ]
    out.sort(key=lambda c: (-c["routes"], c["name"]))
    return out


def popular_airports(n: int = 12, continent: str = "") -> list[dict]:
    """Best-connected airports (optionally in one continent) -- what the
    From/To fields offer before the user has typed anything."""
    idx = _index()
    pool = [a for a in idx.airports if a.has_routes and (not continent or a.continent == continent)]
    pool.sort(key=lambda a: a.weight, reverse=True)
    return [a.to_dict() for a in pool[:n]]


def browse_airports(*, continent: str = "", country: str = "", query: str = "", sort: str = "routes",
                    page: int = 1, page_size: int = 48, routes_only: bool = False) -> dict:
    """Filterable, paginated directory of every airport in the model (the
    /airports explorer page)."""
    idx = _index()
    q = fold(query)
    items = []
    for a in idx.airports:
        if continent and a.continent != continent:
            continue
        if country and a.country != country:
            continue
        if routes_only and not a.has_routes:
            continue
        if q and not (q == a.iata.lower() or q in fold(a.city) or q in fold(a.name)
                      or q in fold(a.country_name) or any(q in fold(x) for x in a.aliases)):
            continue
        items.append(a)
    if sort == "name":
        items.sort(key=lambda a: (fold(a.city), a.iata))
    elif sort == "code":
        items.sort(key=lambda a: a.iata)
    else:
        items.sort(key=lambda a: (-a.weight, a.iata))
    page_size = max(1, min(int(page_size), 200))
    pages = max(1, -(-len(items) // page_size))
    page = max(1, min(int(page), pages))
    start = (page - 1) * page_size
    return {
        "total": len(items), "page": page, "pages": pages, "page_size": page_size,
        "airports": [a.to_dict() for a in items[start:start + page_size]],
    }


def get_airport(iata: str) -> Optional[Airport]:
    """Exact IATA lookup, case-insensitive."""
    return _index().by_iata.get((iata or "").strip().upper())


def search_airports(query: str, limit: int = 8) -> list[dict]:
    """Ranked autocomplete. Tiers, best first:
      1. exact IATA code              ("del" -> DEL)
      2. exact city / alias / name    ("bombay" -> BOM, "nyc" -> JFK, EWR...)
      3. city / alias starts with it  ("lond" -> LHR, LGW, STN, LTN, LCY)
      4. IATA code starts with it     ("lh" -> LHR ...)
      5. word in name/city/country starts with it, then substring
    Inside each tier, airports with scheduled routes in the model come
    first, then by connectivity -- so hubs outrank regional strips.
    """
    idx = _index()
    q = fold(query)
    if len(q) < 2:
        return []
    qu = q.upper()
    seen: set[str] = set()
    out: list[Airport] = []

    def take(cands: Iterable[Airport]) -> None:
        for a in idx.ranked(cands):
            if a.iata not in seen:
                seen.add(a.iata)
                out.append(a)

    if qu in idx.by_iata:
        take([idx.by_iata[qu]])
    take(idx.keys.get(q, []))
    take(a for k in idx._key_list if k.startswith(q) for a in idx.keys[k])
    if len(q) <= 3:
        take(a for a in idx.airports if a.iata.startswith(qu))
    if len(out) < limit:
        def word_prefix(a: Airport) -> bool:
            hay = f"{fold(a.name)} {fold(a.city)} {fold(a.country_name)}"
            return any(w.startswith(q) for w in hay.split()) or (len(q) >= 4 and q in hay)
        take(a for a in idx.airports if word_prefix(a))
    return [a.to_dict() for a in out[:limit]]


def resolve_place(text: str, *, allow_fuzzy: bool = True) -> list[Airport]:
    """All airports a place string plausibly means, best first. An IATA
    code returns just that airport; a city ("London", "NYC") returns its
    airports with scheduled service (so a search can cover the whole
    metro); a typo ("chicgo") falls back to the closest known name."""
    idx = _index()
    raw = (text or "").strip()
    if not raw:
        return []
    if len(raw) == 3 and raw.upper() in idx.by_iata and (raw.isupper() or fold(raw) not in idx.keys):
        return [idx.by_iata[raw.upper()]]
    key = fold(raw)
    for prefix in ("the ", "city of "):
        if key.startswith(prefix) and key[len(prefix):] in idx.keys:
            key = key[len(prefix):]
    if key in idx.keys:
        return list(idx.keys[key])
    if len(raw) == 3 and raw.upper() in idx.by_iata:
        return [idx.by_iata[raw.upper()]]
    if allow_fuzzy and len(key) >= 4:
        close = difflib.get_close_matches(key, idx._key_list, n=3, cutoff=0.86)
        for k in close:
            if k in idx.city_keys:
                return list(idx.keys[k])
    return []


def primary_airports(text: str, limit: int = 4) -> list[Airport]:
    """resolve_place() narrowed to what a flight search should cover:
    airports with routes in the model, at most `limit` (biggest first)."""
    found = [a for a in resolve_place(text) if a.has_routes]
    if len(found) > 1:
        # a metro search covers the city's real airports, not every strip
        # that lists the city as an alias (Chicago -> ORD, MDW; not Rockford)
        top = found[0].weight
        found = [a for a in found if a.weight >= 0.05 * top or a is found[0]]
    return found[:limit]


def match_place_at(tokens: list[str], start: int, *, after_cue: bool) -> Optional[tuple[int, list[Airport]]]:
    """Longest known place name beginning at tokens[start]. Returns
    (number_of_tokens_matched, airports) or None. `tokens` keep their
    original casing so capitalisation can disambiguate."""
    idx = _index()
    if start >= len(tokens):
        return None
    for n in range(min(idx.max_key_tokens, len(tokens) - start), 0, -1):
        span = tokens[start:start + n]
        key = fold(" ".join(span))
        if key in idx.keys:
            if n == 1:
                word = span[0]
                if key in AMBIGUOUS_WORDS and not after_cue and not word[:1].isupper():
                    continue
                if len(key) <= 2:
                    continue
            if key in idx.city_keys or n > 1 or after_cue or span[0][:1].isupper():
                return n, list(idx.keys[key])
    word = tokens[start]
    if len(word) == 3 and word.isalpha() and word.upper() in idx.by_iata:
        if word.isupper() or (after_cue and word.lower() not in _COMMON_THREE_LETTER):
            return 1, [idx.by_iata[word.upper()]]
    return None
