"""
Build ("train") the offline world flight model shipped in
agent/data/flight_model.json.gz.

    python scripts/build_flight_model.py            # downloads raw data, ~15MB
    python scripts/build_flight_model.py --raw-dir path/to/cached/raw

Nothing in the running app calls an airline API or needs an API key. This
script runs once, offline from the app, and compiles three public datasets
into one compact file the server loads at startup:

  OurAirports  (public domain)   airports.csv, countries.csv
      Every airport in the world with its type (large/medium/small...),
      scheduled-service flag, IATA code, city, coordinates and alternate
      names ("Bombay", "Madras", "Peking", ...).
  OpenFlights  (ODbL 1.0)        routes.dat, airlines.dat, planes.dat, airports.dat
      ~67,000 airline routes (who flies what between which airports, with
      which aircraft) plus IANA time zones per airport. NOTE: OpenFlights'
      route data was last updated in June 2014 -- it is a real network, but
      a historical one. The cleaning step below applies well-known carrier
      mergers/shutdowns since then; it does not add routes launched after
      2014.

What "training" means here (all of it is data-driven, none of it is a
hand-written list of demo cities):

  1. Airport set: large + medium airports with scheduled service and an
     IATA code (~3,200), each scored by how many airline-routes touch it.
     The score drives autocomplete ranking ("new york" -> JFK, EWR, LGA
     before Islip) and which airport a bare city name resolves to.
  2. Carrier resolution: OpenFlights' airlines.dat reuses IATA codes (VY is
     both Vueling and Formosa Airlines). Each code is resolved to the
     airline whose home country best matches where that code's routes
     actually fly from -- learned from the route data itself. Codes whose
     routes never touch the named airline's country are flagged; the big
     ones are corrected (CARRIER_NAME_FIXES), the rest shown neutrally.
  3. Network cleaning: codeshares and multi-stop entries dropped, plus
     codeshare-like entries OpenFlights doesn't flag (a carrier on a pair
     touching neither its home country nor one of its bases); carriers
     that have since merged are folded into their successor, ceased
     carriers removed (CARRIER_UPDATES, each with its year); routes of
     replaced airports move to the new airport (TXL/SXF -> BER, ...) and
     re-coded airports are matched through their ICAO code.
  4. Frequency model: each carrier-route gets a weekly frequency from a
     gravity model (geometric mean of both airports' connectivity, damped
     by distance band). One global scale factor is then fitted by
     bisection so the whole network operates ~TARGET_DAILY_DEPARTURES a
     day -- the real-world aggregate (ICAO/ATAG report ~38.9M commercial
     flights in 2019, i.e. ~106k/day).

agent/flights_provider.py turns this into day-to-day data at query time:
which flights operate on a given date, departure/arrival times in local
time, block times, aircraft, fares and seats -- deterministic per date, so
the same search on the same day always returns the same schedule. It is a
model of the network, not a live feed: real-time delays, sold-out flights
or today's actual fares are not knowable without one.
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as _dt
import gzip
import io
import json
import math
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = ROOT / "agent" / "data" / "flight_model.json.gz"

SOURCES = {
    "airports.csv": "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/airports.csv",
    "countries.csv": "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/countries.csv",
    "routes.dat": "https://raw.githubusercontent.com/jpatokal/openflights/master/data/routes.dat",
    "airlines.dat": "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airlines.dat",
    "planes.dat": "https://raw.githubusercontent.com/jpatokal/openflights/master/data/planes.dat",
    "openflights_airports.dat": "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airports.dat",
}

TARGET_DAILY_DEPARTURES = 106_000

# Well-known changes since OpenFlights' 2014 snapshot, matched on the
# resolved airline name. Conservative on purpose: only carriers whose
# fate is unambiguous public record.
CARRIER_UPDATES = {
    "US Airways": ("merge", "AA", "American Airlines", 2015),
    "Virgin America": ("merge", "AS", "Alaska Airlines", 2018),
    "AirTran Airways": ("merge", "WN", "Southwest Airlines", 2014),
    "Germanwings": ("merge", "EW", "Eurowings", 2020),
    "Dragonair": ("merge", "CX", "Cathay Pacific", 2020),
    "SilkAir": ("merge", "SQ", "Singapore Airlines", 2021),
    "Alitalia": ("rename", "AZ", "ITA Airways", 2021),
    "TAM Brazilian Airlines": ("rename", "JJ", "LATAM Airlines Brasil", 2016),
    "LAN Airlines": ("rename", "LA", "LATAM Airlines", 2016),
    "Air Berlin": ("ceased", None, None, 2017),
    "Jet Airways": ("ceased", None, None, 2019),
    "Monarch Airlines": ("ceased", None, None, 2017),
    "Transaero Airlines": ("ceased", None, None, 2015),
    "bmibaby": ("ceased", None, None, 2012),
    "Thomas Cook Airlines": ("ceased", None, None, 2019),
    "Germania": ("ceased", None, None, 2019),
    "Kingfisher Airlines": ("ceased", None, None, 2012),
    "Flybe": ("ceased", None, None, 2023),
    "Niki": ("ceased", None, None, 2017),
    "Air One": ("ceased", None, None, 2014),
    "TransAsia Airways": ("ceased", None, None, 2016),
    "Meridiana": ("ceased", None, None, 2020),
    "Interjet (ABC Aerolineas)": ("ceased", None, None, 2020),
    "Oceanair": ("ceased", None, None, 2019),
    "Go Air": ("ceased", None, None, 2023),
    "Intersky": ("ceased", None, None, 2015),
}

# Same idea, for codes whose airlines.dat name is wrong (see below), so
# the name can't be matched: Thomas Cook Airlines Belgium (HQ), bmi
# regional (BM).
CARRIER_CODE_UPDATES = {
    "HQ": ("ceased", 2017),
    "BM": ("ceased", 2019),
}

# airlines.dat attaches some route codes to the wrong (often long-defunct)
# airline: OpenFlights' "JD" routes are all inside China, i.e. Beijing
# Capital Airlines, not Japan Air System (absorbed by JAL in 2004). The
# build flags every code whose routes never touch the named airline's home
# country; these are the corrections for the ones that matter. Any other
# flagged code is shown neutrally as "Airline <code>" rather than with a
# name the data can't support.
CARRIER_NAME_FIXES = {
    "GS": "Tianjin Airlines", "JD": "Beijing Capital Airlines", "8L": "Lucky Air",
    "BK": "Okay Airways", "EU": "Chengdu Airlines", "KY": "Kunming Airlines",
    "NS": "Hebei Airlines", "TV": "Tibet Airlines", "VB": "VivaAerobus",
    "YC": "Yamal Airlines", "VJ": "VietJet Air", "GK": "Jetstar Japan",
    "9N": "Tropic Air", "SZ": "Somon Air", "B9": "Iran Airtour", "2Z": "Passaredo",
    "GQ": "Sky Express", "9V": "Avior Airlines", "OB": "Boliviana de Aviación",
    "7P": "Air Panama", "HF": "Air Côte d'Ivoire", "BU": "Compagnie Africaine d'Aviation",
    "8R": "Sol Líneas Aéreas",
}

# Airports replaced by a new airport (new code) since 2014: routes are
# carried over to the successor. Applied only when the successor exists in
# the current OurAirports data.
# Cities OpenFlights still lists under their pre-rename names (2014 data).
# The current name is shown; the old one stays searchable as an alias.
CITY_RENAMES = {
    "Madras": "Chennai", "Bangalore": "Bengaluru", "Trivandrum": "Thiruvananthapuram",
    "Calicut": "Kozhikode", "Mangalore": "Mangaluru", "Mysore": "Mysuru", "Baroda": "Vadodara",
    "Hubli": "Hubballi", "Belgaum": "Belagavi", "Allahabad": "Prayagraj",
    "Vishakhapatnam": "Visakhapatnam", "Alma-ata": "Almaty", "Ujung Pandang": "Makassar",
    "Gurgaon": "Gurugram", "Kiev": "Kyiv", "Nur-Sultan": "Astana",
}

AIRPORT_RELOCATIONS = {
    "TXL": "BER", "SXF": "BER",   # Berlin Brandenburg, 2020
    "DKR": "DSS",                 # Dakar Blaise Diagne, 2017
    "NAY": "PKX",                 # Beijing Nanyuan -> Daxing, 2019
    "ULN": "UBN",                 # Ulaanbaatar Chinggis Khaan, 2021
    "REP": "SAI",                 # Siem Reap-Angkor, 2023
    "PNH": "KTI",                 # Phnom Penh Techo, 2025
    "MJV": "RMU",                 # Murcia San Javier -> Corvera, 2019
    "ADA": "COV",                 # Adana -> Cukurova, 2024
}

# airlines.dat spells some countries differently from OurAirports
AIRLINE_COUNTRY_ALIASES = {
    "Republic of Korea": "KR", "Democratic People's Republic of Korea": "KP",
    "Hong Kong SAR of China": "HK", "Macao": "MO", "Burma": "MM", "Reunion": "RE",
    "Russian Federation": "RU", "Macedonia": "MK", "Lao Peoples Democratic Republic": "LA",
    "Ivory Coast": "CI", "Congo (Kinshasa)": "CD", "Netherlands Antilles": "SX",
    "ALASKA": "US", "AVIANCA": "CO", "DRAGON": "HK",
}

NAME_CLEANUPS = {
    "Air India Limited": "Air India",
    "Scandinavian Airlines System": "SAS Scandinavian Airlines",
    "Avianca - Aerovias Nacionales de Colombia": "Avianca",
    "Nas Air": "flynas",
    "Fly Dubai": "flydubai",
    "Condor Flugdienst": "Condor",
    "Lion Mentari Airlines": "Lion Air",
    "Aeroflot Russian Airlines": "Aeroflot",
    "IndiGo Airlines": "IndiGo",
    "Spicejet": "SpiceJet",
}

# Common IATA equipment codes that planes.dat doesn't name (winglet /
# sharklet sub-variants and generic family codes).
AIRCRAFT_SUPPLEMENT = {
    "73H": "Boeing 737-800", "73W": "Boeing 737-700", "73C": "Boeing 737-300",
    "73J": "Boeing 737-900", "75W": "Boeing 757-200", "76W": "Boeing 767-300ER",
    "32S": "Airbus A320 family", "32A": "Airbus A320", "32B": "Airbus A321",
    "CRJ": "Bombardier CRJ", "ERJ": "Embraer ERJ", "EMJ": "Embraer E-Jet",
    "M80": "McDonnell Douglas MD-80", "DH8": "De Havilland Dash 8",
    "BE1": "Beechcraft 1900", "313": "Airbus A310-300", "DC9": "Douglas DC-9",
    "CNA": "Cessna", "CNC": "Cessna",
}

_GENERIC_NAME_WORDS = {
    "international", "intl", "airport", "aeropuerto", "aeroporto", "aéroport",
    "aeroport", "flughafen", "regional", "municipal", "domestic", "air", "base",
    "field", "airfield", "national", "county",
}


def fetch_raw(raw_dir: Path) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    for name, url in SOURCES.items():
        target = raw_dir / name
        if target.exists() and target.stat().st_size > 0:
            continue
        print(f"  downloading {name} ...", flush=True)
        with urllib.request.urlopen(url, timeout=120) as resp:
            target.write_bytes(resp.read())


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def short_airport_name(name: str, city: str) -> str:
    words = [w for w in name.replace("-", " ").split() if w.lower().strip(".,") not in _GENERIC_NAME_WORDS]
    short = " ".join(words).strip()
    return "" if short.lower() == city.lower() else short


def split_city(raw: str) -> tuple[str, list[str]]:
    """'Paris (Roissy-en-France, Val-d'Oise)' -> ('Paris', ['Roissy-en-France', ...]),
    'Kerkyra (Corfu)' -> ('Kerkyra', ['Corfu']), 'Allentown/Bethlehem' ->
    ('Allentown/Bethlehem', ['Allentown', 'Bethlehem']),
    'Birmingham, West Midlands' -> ('Birmingham', [])."""
    raw = (raw or "").strip()
    extra: list[str] = []
    base = raw
    if "(" in raw:
        base, _, rest = raw.partition("(")
        inner = rest.rstrip(")")
        for part in inner.split(","):
            part = part.strip()
            if len(part) > 2:  # skip province codes like "(BG)"
                extra.append(part)
    base = base.split(",")[0].strip()
    if "/" in base:
        extra.extend(p.strip() for p in base.split("/") if len(p.strip()) > 2)
    return base, extra


def clean_aliases(raw: str, city: str, iata: str) -> list[str]:
    out = []
    seen = {city.lower(), iata.lower()}
    for alias in (raw or "").split(","):
        alias = alias.strip()
        low = alias.lower()
        if len(alias) < 3 or low in seen or alias.isdigit():
            continue
        if any(bad in low for bad in ("air force", "afb", "naval", "army", "http")):
            continue
        seen.add(low)
        out.append(alias)
    return out[:12]


def build(raw_dir: Path) -> dict:
    # ---- countries -------------------------------------------------------
    countries = {}
    continent_of = {}
    with open(raw_dir / "countries.csv", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            countries[row["code"]] = row["name"]
            continent_of[row["code"]] = row["continent"]

    # ---- time zones (OpenFlights airports.dat) ---------------------------
    tz_by_iata = {}
    of_city = {}
    of_icao = {}
    with open(raw_dir / "openflights_airports.dat", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if len(row) < 12:
                continue
            iata, offset, tzname = row[4], row[9], row[11]
            if len(iata) != 3 or iata == "\\N":
                continue
            try:
                off = float(offset)
            except ValueError:
                off = None
            tz_by_iata[iata] = (None if tzname in ("\\N", "") else tzname, off)
            if row[2] and row[2] != "\\N":
                of_city[iata] = row[2].strip()
            if row[5] and row[5] != "\\N":
                of_icao[iata] = row[5].strip()

    # Optional build-time fallback for time zones OpenFlights lacks (new
    # airports like IST/PKX). Not needed at runtime.
    try:
        import airportsdata  # type: ignore

        _ad = airportsdata.load("IATA")
    except Exception:  # noqa: BLE001
        _ad = {}

    # ---- airports (OurAirports) ------------------------------------------
    candidates = {}
    oa_by_icao = {}
    rank = {"large_airport": 2, "medium_airport": 1}
    with open(raw_dir / "airports.csv", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            for ident in {row["ident"], row["icao_code"], row["gps_code"]}:
                if ident:
                    oa_by_icao.setdefault(ident, row)
            iata = (row["iata_code"] or "").strip().upper()
            if (
                len(iata) != 3
                or not iata.isalpha()
                or row["scheduled_service"] != "yes"
                or row["type"] not in rank
            ):
                continue
            prev = candidates.get(iata)
            if prev is None or rank[row["type"]] > rank[prev["type"]]:
                candidates[iata] = row

    # ---- airlines + carrier resolution -----------------------------------
    airlines_by_id = {}
    airlines_by_code = collections.defaultdict(list)
    with open(raw_dir / "airlines.dat", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if len(row) < 8:
                continue
            aid, name, code, country, active = row[0], row[1], row[3], row[6], row[7]
            airlines_by_id[aid] = (name, code, country, active)
            if len(code) == 2 and code != "\\N":
                airlines_by_code[code].append((aid, name, country, active))

    aircraft = {}
    with open(raw_dir / "planes.dat", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if len(row) >= 2 and row[1] and row[1] != "\\N":
                aircraft[row[1]] = row[0]
    for code, name in AIRCRAFT_SUPPLEMENT.items():
        aircraft.setdefault(code, name)

    # OpenFlights codes -> today's codes. Same physical airport with a new
    # IATA code is matched through its ICAO identifier (e.g. Chisinau
    # KIV -> RMO); a replaced airport goes through AIRPORT_RELOCATIONS.
    remap_stats = collections.Counter()

    def remap(code: str):
        if code in candidates:
            return code
        new = AIRPORT_RELOCATIONS.get(code)
        if new in candidates:
            remap_stats["relocated"] += 1
            return new
        row = oa_by_icao.get(of_icao.get(code, ""))
        if row is not None and (row["iata_code"] or "") in candidates:
            remap_stats["recoded_via_icao"] += 1
            return row["iata_code"]
        return None

    raw_routes = []
    with open(raw_dir / "routes.dat", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if len(row) < 9:
                continue
            code, aid, src, dst, codeshare, stops, equip = row[0], row[1], row[2], row[4], row[6], row[7], row[8]
            if codeshare == "Y" or stops != "0":
                continue
            src, dst = remap(src), remap(dst)
            if src is None or dst is None or src == dst:
                continue
            if len(code) != 2:
                continue
            raw_routes.append((code, src, dst, equip.split()))

    # Learn each code's home country from where its routes depart.
    country_by_cc_name = {v: k for k, v in countries.items()}
    country_by_cc_name.update(AIRLINE_COUNTRY_ALIASES)
    route_countries = collections.defaultdict(collections.Counter)
    for code, src, dst, _ in raw_routes:
        route_countries[code][candidates[src]["iso_country"]] += 1

    def resolve_airline(code: str):
        options = airlines_by_code.get(code) or []
        if not options:
            return None
        if len(options) == 1:
            return options[0][1], country_by_cc_name.get(options[0][2], "")
        home = route_countries[code]

        def score(opt):
            # where the code's routes actually fly from, discounted for
            # airlines airlines.dat marks inactive (Ozark, 1986, also "OZ")
            cc = country_by_cc_name.get(opt[2], "")
            active = opt[3] == "Y"
            return (home.get(cc, 0) * (1.0 if active else 0.25), active)

        best = max(options, key=score)
        return best[1], country_by_cc_name.get(best[2], "")

    carriers = {}  # code -> name, after updates
    home_cc = {}   # code -> ISO country of the airline
    merged_into = {}
    stats = collections.Counter()
    for code in sorted({r[0] for r in raw_routes}):
        resolved = resolve_airline(code)
        if not resolved:
            stats["unresolved_code"] += 1
            continue
        name, cc = resolved
        if code in CARRIER_CODE_UPDATES:
            stats["ceased_carrier"] += 1
            continue
        if code in CARRIER_NAME_FIXES:
            name, cc = CARRIER_NAME_FIXES[code], ""
            stats["corrected_carrier_name"] += 1
        elif route_countries[code] and route_countries[code].get(cc, 0) == 0:
            # routes never touch the named airline's country: name unreliable
            name, cc = f"Airline {code}", ""
            stats["unverified_carrier_name"] += 1
        home_cc[code] = cc or (route_countries[code].most_common(1)[0][0] if route_countries[code] else "")
        update = CARRIER_UPDATES.get(name)
        if update:
            action, new_code, new_name, _year = update
            if action == "ceased":
                stats["ceased_carrier"] += 1
                continue
            if action == "merge":
                merged_into[code] = new_code
                carriers[new_code] = new_name
                stats["merged_carrier"] += 1
                continue
            if action == "rename":
                name = new_name
        carriers[code] = NAME_CLEANUPS.get(name, name)

    # ---- aggregate routes -------------------------------------------------
    # OpenFlights doesn't flag every codeshare: e.g. American Airlines
    # "operating" Sydney-Melbourne. A carrier-route is kept only if one end
    # is in the airline's home country or is one of the airline's own
    # bases (>= 8 routes and >= 4% of its network) -- which keeps genuine
    # foreign-base operations (Ryanair at Bergamo, easyJet at Geneva) and
    # drops marketing-only entries.
    per_carrier_airports = collections.defaultdict(collections.Counter)
    for code, src, dst, _ in raw_routes:
        code = merged_into.get(code, code)
        per_carrier_airports[code][src] += 1
        per_carrier_airports[code][dst] += 1
    bases = {}
    for code, counter in per_carrier_airports.items():
        total = sum(counter.values())
        bases[code] = {a for a, n in counter.items() if n >= 8 and n >= 0.04 * total}

    pairs = collections.defaultdict(dict)  # (src, dst) -> code -> set(equip)
    for code, src, dst, equip in raw_routes:
        code = merged_into.get(code, code)
        if code not in carriers:
            continue
        home = home_cc.get(code) or home_cc.get(next((k for k, v in merged_into.items() if v == code), ""), "")
        in_home = home and home in (candidates[src]["iso_country"], candidates[dst]["iso_country"])
        if not in_home and src not in bases.get(code, ()) and dst not in bases.get(code, ()):
            stats["dropped_codeshare_like"] += 1
            continue
        pairs[(src, dst)].setdefault(code, set()).update(e for e in equip if e)

    touch = collections.Counter()
    for (src, dst), by_code in pairs.items():
        touch[src] += len(by_code)
        touch[dst] += len(by_code)

    # Keep airports that have at least one route; the rest stay searchable
    # but flagged so the UI can say "no scheduled service in the model".
    airport_codes = sorted(candidates)
    index = {code: i for i, code in enumerate(airport_codes)}

    # ---- frequency model + calibration -----------------------------------
    def distance_factor(km: float) -> float:
        if km < 800:
            return 1.8
        if km < 1500:
            return 1.3
        if km < 3000:
            return 1.0
        if km < 5000:
            return 0.5
        if km < 9000:
            return 0.3
        return 0.22

    def weekly_cap(km: float) -> int:
        # a single airline rarely flies a long-haul pair more than a few
        # times a day; ultra-long-haul is at most daily
        if km < 1500:
            return 70
        if km < 3000:
            return 42
        if km < 6000:
            return 28
        if km < 9000:
            return 14
        return 7

    entries = []  # (src, dst, code, raw_weight, cap)
    for (src, dst), by_code in pairs.items():
        a, b = candidates[src], candidates[dst]
        km = haversine_km(float(a["latitude_deg"]), float(a["longitude_deg"]),
                          float(b["latitude_deg"]), float(b["longitude_deg"]))
        competition = 1.0 / math.sqrt(len(by_code))  # share of a fixed-ish market
        for code in by_code:
            w = math.sqrt(touch[src] * touch[dst]) * distance_factor(km) * competition
            entries.append((src, dst, code, w, weekly_cap(km)))

    def weekly(w: float, scale: float, cap: int = 70) -> int:
        return int(min(cap, max(2, round(w * scale))))

    lo, hi = 1e-4, 10.0
    for _ in range(60):
        mid = (lo + hi) / 2
        daily = sum(weekly(w, mid, cap) for *_, w, cap in entries) / 7.0
        if daily < TARGET_DAILY_DEPARTURES:
            lo = mid
        else:
            hi = mid
    scale = (lo + hi) / 2
    fitted_daily = sum(weekly(w, scale, cap) for *_, w, cap in entries) / 7.0

    route_out = collections.defaultdict(list)
    for src, dst, code, w, cap in entries:
        route_out[(src, dst)].append([code, weekly(w, scale, cap), " ".join(sorted(pairs[(src, dst)][code]))])

    # ---- assemble ----------------------------------------------------------
    airports_out = []
    for code in airport_codes:
        row = candidates[code]
        # Display city = the city the airport SERVES (OpenFlights: "Kuala
        # Lumpur" for KUL, "Tokyo" for NRT, "Taipei" for TPE), falling back
        # to OurAirports' municipality, which is often the town the runway
        # sits in ("Sepang", "Narita", "Taoyuan"); that one becomes an alias.
        municipality, city_extra = split_city(row["municipality"])
        served = of_city.get(code, "")
        city = served or municipality or (_ad.get(code) or {}).get("city", "") \
            or short_airport_name(row["name"], "") or row["name"]
        if city in CITY_RENAMES:
            city_extra = city_extra + [city]
            city = CITY_RENAMES[city]
        alt_city = municipality if municipality and municipality.lower() != city.lower() else \
            (_ad.get(code) or {}).get("city", "")
        tzname, off = tz_by_iata.get(code, (None, None))
        if not tzname and _ad.get(code, {}).get("tz"):
            tzname = _ad[code]["tz"]
        if off is None:
            off = round(float(row["longitude_deg"]) / 15.0)
        aliases = clean_aliases(row["keywords"], city, code)
        known = {a.lower() for a in aliases} | {city.lower()}
        for extra in city_extra + [alt_city]:
            if extra and extra.lower() not in known:
                aliases.append(extra)
                known.add(extra.lower())
        short = short_airport_name(row["name"], city)
        if short and short.lower() not in known:
            aliases.insert(0, short)
        airports_out.append([
            code,
            row["name"],
            city,
            row["iso_country"],
            round(float(row["latitude_deg"]), 4),
            round(float(row["longitude_deg"]), 4),
            "L" if row["type"] == "large_airport" else "M",
            tzname,
            off,
            aliases,
            touch.get(code, 0),
        ])

    used_equipment = {e for lst in route_out.values() for _, _, eq in lst for e in eq.split()}
    carrier_codes = sorted(carriers)
    carrier_index = {c: i for i, c in enumerate(carrier_codes)}
    routes_out = []
    for (src, dst), lst in sorted(route_out.items()):
        routes_out.append([
            index[src],
            index[dst],
            [[carrier_index[c], f, eq] for c, f, eq in sorted(lst)],
        ])

    model = {
        "version": 1,
        "meta": {
            "built_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sources": [
                {"name": "OurAirports", "license": "Public Domain", "url": "https://ourairports.com/data/",
                 "files": ["airports.csv", "countries.csv"]},
                {"name": "OpenFlights", "license": "Open Database License (ODbL) 1.0",
                 "url": "https://openflights.org/data.html", "note": "route network last updated June 2014",
                 "files": ["routes.dat", "airlines.dat", "planes.dat", "airports.dat"]},
            ],
            "counts": {
                "airports": len(airports_out),
                "airports_with_routes": sum(1 for a in airports_out if a[10] > 0),
                "airport_pairs": len(routes_out),
                "carrier_routes": len(entries),
                "carriers": len(carrier_codes),
                "countries": len({a[3] for a in airports_out}),
            },
            "calibration": {
                "target_daily_departures": TARGET_DAILY_DEPARTURES,
                "fitted_daily_departures": round(fitted_daily),
                "gravity_scale": scale,
            },
            "cleaning": {**dict(stats), **dict(remap_stats)},
            "airport_relocations": AIRPORT_RELOCATIONS,
            "carrier_updates": {k: {"action": v[0], "successor": v[2], "year": v[3]} for k, v in CARRIER_UPDATES.items()},
        },
        "countries": {cc: countries.get(cc, cc) for cc in sorted({a[3] for a in airports_out})},
        "country_continent": {cc: continent_of.get(cc, "") for cc in sorted({a[3] for a in airports_out})},
        "continents": {"AF": "Africa", "AN": "Antarctica", "AS": "Asia", "EU": "Europe",
                       "NA": "North America", "OC": "Oceania", "SA": "South America"},
        "carriers": [[c, carriers[c]] for c in carrier_codes],
        "aircraft": {k: v for k, v in aircraft.items() if k in used_equipment},
        "airports": airports_out,
        "routes": routes_out,
    }
    return model


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--raw-dir", type=Path, default=ROOT / "build" / "raw-data",
                        help="where raw downloads are cached (default: build/raw-data)")
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    args = parser.parse_args(argv)

    print("1/3 fetching raw datasets (skipped if cached)")
    fetch_raw(args.raw_dir)
    print("2/3 building model")
    model = build(args.raw_dir)
    print("3/3 writing", args.out)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(model, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    buf = io.BytesIO()
    # mtime=0 so rebuilding from the same inputs yields byte-identical output
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as gz:
        gz.write(payload)
    args.out.write_bytes(buf.getvalue())
    counts = model["meta"]["counts"]
    cal = model["meta"]["calibration"]
    print(f"   airports={counts['airports']} (with routes: {counts['airports_with_routes']}) "
          f"pairs={counts['airport_pairs']} carriers={counts['carriers']} "
          f"daily departures fitted={cal['fitted_daily_departures']} (target {cal['target_daily_departures']})")
    print(f"   {args.out.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
