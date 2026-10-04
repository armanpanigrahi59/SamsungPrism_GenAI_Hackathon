"""
Offline flight engine: search_flights / book_flight for every major
airport in the world, with day-to-day schedules -- no airline API, no API
key, no network.

It runs on the model compiled by scripts/build_flight_model.py
(agent/data/flight_model.json.gz): ~3,200 airports, ~31,000 real airport
pairs and which of ~500 airlines fly them (OurAirports + OpenFlights), with
a weekly frequency per airline-route fitted so the whole network operates
~106,000 departures a day. For a given date this module derives:

  * which flights operate that day (weekly frequencies are spread across
    the week, so a 3x-weekly long-haul only shows on its days),
  * departure times (stable per flight number, like a real timetable),
    block times from great-circle distance with an eastbound/westbound
    wind adjustment, arrival times in the destination's local time zone,
  * one-stop connections through real hubs when there's no or little
    direct service, two-stop as a last resort (minimum connection times
    enforced),
  * fares from a distance-based model with advance-purchase, weekday,
    season, time-of-day, competition and low-cost-carrier factors,
  * seats left, aircraft type, flight numbers, and simulated bookings
    with a 6-character confirmation code.

Everything is deterministic for a given (route, date): search the same
trip twice and you get the same flights and prices; search a different
day and the schedule and fares change the way they would day to day.

What it is NOT: live data. Real delays, cancellations, sold-out flights
and today's actual fares can't be known offline -- the route network is
OpenFlights' 2014 snapshot (cleaned for later mergers/closures/new
airports), and fares/seats are modeled. Every result carries
`"modeled": true` and the UI labels it.

Set PRISM_FLIGHTS_BACKEND=mock to fall back to mock_env.py's original
fake generator.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import math
import os
import string
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Optional

import trio

from .airports import Airport, get_airport, load_model, model_meta, primary_airports, search_airports
from .travel_parse import resolve_travel_date

try:  # Windows has no system tz database; `pip install tzdata` provides one.
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

UTC = _dt.timezone.utc
MAX_DAYS_AHEAD = 330          # airlines publish schedules ~11 months out
MAX_RESULTS = 30

# Widely known low-cost carriers (IATA codes) -- cheaper base fares.
LOW_COST_CARRIERS = {
    "FR", "U2", "W6", "WN", "NK", "F9", "G4", "6E", "SG", "AK", "FD", "D7",
    "TR", "JQ", "3K", "VY", "DY", "PC", "HV", "TO", "5J", "IX", "QZ", "JT",
    "MM", "GK", "9C", "G9", "FZ", "J9", "XY", "LS", "EW", "VB", "Y4",
}


class FlightModelError(RuntimeError):
    """Raised for requests the model can't satisfy (unknown offer, bad
    booking input). Search problems are returned as a `status`, not raised,
    so the UI can explain them."""


# ---------------------------------------------------------------------------
# Network + helpers
# ---------------------------------------------------------------------------


def _h(key: str) -> int:
    return int.from_bytes(hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest(), "big")


def _unit(key: str) -> float:
    return (_h(key) % 1_000_000) / 1_000_000.0


def haversine_km(a: Airport, b: Airport) -> float:
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp, dl = p2 - p1, math.radians(b.lon - a.lon)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(x))


@dataclass(frozen=True)
class _Service:
    carrier: str
    weekly: int
    equipment: tuple


class _Network:
    def __init__(self, model: dict) -> None:
        codes = [row[0] for row in model["airports"]]
        self.carriers = {c: name for c, name in model["carriers"]}
        carrier_codes = [c for c, _ in model["carriers"]]
        self.aircraft = dict(model.get("aircraft", {}))
        self.out: dict[str, dict[str, list[_Service]]] = {}
        self.inbound: dict[str, set[str]] = {}
        for src_i, dst_i, services in model["routes"]:
            src, dst = codes[src_i], codes[dst_i]
            self.out.setdefault(src, {})[dst] = [
                _Service(carrier_codes[ci], int(weekly), tuple(eq.split())) for ci, weekly, eq in services
            ]
            self.inbound.setdefault(dst, set()).add(src)
        self.pair_count = len(model["routes"])


@lru_cache(maxsize=1)
def network() -> _Network:
    return _Network(load_model())


@lru_cache(maxsize=1024)
def _zone(name: Optional[str]):
    if not name or ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 -- missing tzdata on Windows, unknown name
        return None


def _to_utc(airport: Airport, local: _dt.datetime) -> _dt.datetime:
    zone = _zone(airport.tz)
    if zone is not None:
        return local.replace(tzinfo=zone).astimezone(UTC)
    return (local - _dt.timedelta(hours=airport.utc_offset)).replace(tzinfo=UTC)


def _to_local(airport: Airport, utc: _dt.datetime) -> _dt.datetime:
    zone = _zone(airport.tz)
    if zone is not None:
        return utc.astimezone(zone).replace(tzinfo=None)
    return (utc + _dt.timedelta(hours=airport.utc_offset)).replace(tzinfo=None)


def _block_minutes(a: Airport, b: Airport, km: float) -> int:
    minutes = 32 + km / 13.2  # ~790 km/h average incl. climb/descent + taxi
    if km > 1500:  # jet stream: eastbound faster, westbound slower
        dlon = ((b.lon - a.lon + 540) % 360) - 180
        minutes *= 0.96 if dlon > 0 else 1.04
    return int(5 * round(minutes / 5))


def _fmt_duration(minutes: int) -> str:
    return f"PT{minutes // 60}H{minutes % 60}M"


# ---------------------------------------------------------------------------
# Day-to-day schedule
# ---------------------------------------------------------------------------


@dataclass
class _Leg:
    origin: Airport
    dest: Airport
    carrier: str
    flight_no: str
    dep_local: _dt.datetime
    arr_local: _dt.datetime
    dep_utc: _dt.datetime
    arr_utc: _dt.datetime
    minutes: int
    km: float
    aircraft: Optional[str]
    competitors: int

    def to_dict(self, net: _Network) -> dict:
        return {
            "from": self.origin.iata,
            "from_city": self.origin.city,
            "to": self.dest.iata,
            "to_city": self.dest.city,
            "flight": self.flight_no,
            "carrier": self.carrier,
            "carrier_name": net.carriers.get(self.carrier, self.carrier),
            "depart_local": self.dep_local.isoformat(timespec="minutes"),
            "arrive_local": self.arr_local.isoformat(timespec="minutes"),
            "duration_min": self.minutes,
            "distance_km": round(self.km),
            "aircraft": self.aircraft,
        }


def _legs_on(src: Airport, dst: Airport, day: _dt.date, net: _Network) -> list[_Leg]:
    services = net.out.get(src.iata, {}).get(dst.iata, [])
    if not services:
        return []
    km = haversine_km(src, dst)
    minutes = _block_minutes(src, dst, km)
    legs: list[_Leg] = []
    for svc in services:
        key = f"{src.iata}{dst.iata}{svc.carrier}"
        per_day, extra = divmod(svc.weekly, 7)
        weekday_order = sorted(range(7), key=lambda d: _h(f"{key}:wd{d}"))
        count = per_day + (1 if day.weekday() in weekday_order[:extra] else 0)
        if count == 0:
            continue
        long_haul = km > 5000
        start, end = (8 * 60, 23 * 60 + 50) if long_haul else (6 * 60, 22 * 60 + 30)
        for k in range(count):
            if count == 1:
                base = start + _unit(f"{key}:solo") * (end - start)
            else:
                base = start + (k + 0.5) / count * (end - start) + (_unit(f"{key}:j{k}") - 0.5) * 50
            dep_min = int(5 * round(max(start, min(end, base)) / 5))
            dep_local = _dt.datetime.combine(day, _dt.time()) + _dt.timedelta(minutes=dep_min)
            dep_utc = _to_utc(src, dep_local)
            arr_utc = dep_utc + _dt.timedelta(minutes=minutes)
            number = 100 + _h(f"{key}:fn{k}") % 8900
            equip = svc.equipment[_h(f"{key}:eq{k}") % len(svc.equipment)] if svc.equipment else None
            legs.append(_Leg(
                origin=src, dest=dst, carrier=svc.carrier, flight_no=f"{svc.carrier} {number}",
                dep_local=dep_local, arr_local=_to_local(dst, arr_utc), dep_utc=dep_utc, arr_utc=arr_utc,
                minutes=minutes, km=km, aircraft=net.aircraft.get(equip, equip) if equip else None,
                competitors=len(services),
            ))
    return legs


# ---------------------------------------------------------------------------
# Fares + seats (modeled)
# ---------------------------------------------------------------------------


def _base_fare(km: float) -> float:
    if km < 500:
        return 45 + 0.16 * km
    if km < 1500:
        return 70 + 0.11 * km
    if km < 4000:
        return 120 + 0.075 * km
    return 200 + 0.058 * km


_WEEKDAY_FACTOR = [1.04, 0.92, 0.94, 1.0, 1.12, 0.96, 1.10]
_MONTH_FACTOR = {1: 0.9, 2: 0.9, 6: 1.18, 7: 1.18, 8: 1.18, 11: 0.95, 12: 1.22}


def _leg_fare(leg: _Leg, today: _dt.date) -> float:
    day = leg.dep_local.date()
    ahead = (day - today).days
    if ahead <= 2:
        adv = 1.65
    elif ahead <= 6:
        adv = 1.35
    elif ahead <= 13:
        adv = 1.15
    elif ahead <= 29:
        adv = 1.0
    elif ahead <= 59:
        adv = 0.92
    else:
        adv = 0.88
    hour = leg.dep_local.hour
    tod = 1.08 if (6 <= hour < 9 or 17 <= hour < 20) else (0.9 if hour >= 22 or hour < 6 else 1.0)
    competition = 1.12 - 0.04 * min(leg.competitors, 5)
    lcc = 0.72 if leg.carrier in LOW_COST_CARRIERS else 1.0
    jitter = 0.9 + 0.2 * _unit(f"{leg.flight_no}:{day.isoformat()}:fare")
    fare = (_base_fare(leg.km) * adv * _WEEKDAY_FACTOR[day.weekday()] * _MONTH_FACTOR.get(day.month, 1.0)
            * tod * competition * lcc * jitter)
    return max(29.0, fare)


def _seats_left(leg: _Leg, today: _dt.date) -> int:
    ahead = (leg.dep_local.date() - today).days
    seats = 1 + _h(f"{leg.flight_no}:{leg.dep_local.date()}:seats") % 9
    if ahead <= 3:
        seats = max(1, seats - 4)
    return seats


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def _chain(path: list[Airport], day: _dt.date, net: _Network, max_starts: int = 6) -> list[list[_Leg]]:
    """Itineraries along a fixed airport path (o -> hub [-> hub2] -> d):
    each first-leg departure is extended greedily with the first onward
    flight that respects the minimum connection time (preferring the same
    airline), within 8 hours."""
    firsts = _legs_on(path[0], path[1], day, net)
    if not firsts:
        return []
    onward: list[list[_Leg]] = []
    for i in range(1, len(path) - 1):
        a, b = path[i], path[i + 1]
        onward.append([leg for k in range(3) for leg in _legs_on(a, b, day + _dt.timedelta(days=k), net)])
        if not onward[-1]:
            return []
    out: list[list[_Leg]] = []
    for first in sorted(firsts, key=lambda l: l.dep_utc)[:max_starts]:
        legs = [first]
        for options in onward:
            prev = legs[-1]
            hub = prev.dest
            same_country = prev.origin.country == hub.country
            mct = _dt.timedelta(minutes=45 if same_country and options[0].dest.country == hub.country else 75)
            window = [o for o in options if prev.arr_utc + mct <= o.dep_utc <= prev.arr_utc + _dt.timedelta(hours=8)]
            if not window:
                legs = []
                break
            window.sort(key=lambda o: (o.carrier != first.carrier, o.dep_utc))
            legs.append(window[0])
        if legs:
            out.append(legs)
    return out


def _detour(path: list[Airport]) -> float:
    flown = sum(haversine_km(a, b) for a, b in zip(path, path[1:]))
    return flown / max(haversine_km(path[0], path[-1]), 1.0)


def _connections(o: Airport, d: Airport, day: _dt.date, net: _Network) -> list[list[_Leg]]:
    """One-stop itineraries through real hubs (both legs exist in the
    network), best-placed hubs first."""
    hubs = []
    for hub_code in set(net.out.get(o.iata, {})) & net.inbound.get(d.iata, set()):
        hub = get_airport(hub_code)
        if hub is None or hub_code in (o.iata, d.iata):
            continue
        detour = _detour([o, hub, d])
        if detour <= 1.7:
            hubs.append((detour - hub.weight / 5000.0, hub))
    hubs.sort(key=lambda t: t[0])
    itineraries: list[list[_Leg]] = []
    for _, hub in hubs[:5]:
        itineraries.extend(_chain([o, hub, d], day, net))
    return itineraries


def _two_stop(o: Airport, d: Airport, day: _dt.date, net: _Network) -> list[list[_Leg]]:
    """Fallback for pairs with no one-stop path (e.g. a regional airport to
    another continent): origin -> big hub -> big hub -> destination."""
    def by_weight(codes):
        airports = [get_airport(c) for c in codes]
        return sorted((a for a in airports if a), key=lambda a: a.weight, reverse=True)

    firsts = [h for h in by_weight(net.out.get(o.iata, {})) if h.iata != d.iata][:10]
    lasts = [h for h in by_weight(net.inbound.get(d.iata, set())) if h.iata != o.iata][:10]
    paths = []
    for h1 in firsts:
        for h2 in lasts:
            if h1.iata == h2.iata or h2.iata not in net.out.get(h1.iata, {}):
                continue
            detour = _detour([o, h1, h2, d])
            if detour <= 2.2:
                paths.append((detour, [o, h1, h2, d]))
    paths.sort(key=lambda t: t[0])
    itineraries: list[list[_Leg]] = []
    for _, path in paths[:4]:
        itineraries.extend(_chain(path, day, net, max_starts=3))
    return itineraries


CABINS = {
    # cabin -> (fare multiplier vs economy, share of seats sold in that cabin)
    "economy": (1.0, 1.0),
    "premium_economy": (1.65, 0.35),
    "business": (3.4, 0.25),
    "first": (5.6, 0.12),
}
CABIN_LABELS = {"economy": "Economy", "premium_economy": "Premium Economy", "business": "Business", "first": "First"}
MAX_PASSENGERS = 9


def normalize_cabin(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", " ").replace("_", " ")
    if "first" in text:
        return "first"
    if "business" in text or text in ("j", "c"):
        return "business"
    if "premium" in text:
        return "premium_economy"
    return "economy"


def normalize_passengers(value: Any) -> int:
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
    text = str(value if value is not None else "1").strip().lower()
    if text in words:
        n = words[text]
    else:
        digits = "".join(ch for ch in text if ch.isdigit())
        n = int(digits) if digits else 1
    return max(1, min(MAX_PASSENGERS, n))


def _offer(legs: list[_Leg], today: _dt.date, net: _Network, *, cabin: str = "economy", passengers: int = 1) -> dict:
    total_min = int((legs[-1].arr_utc - legs[0].dep_utc).total_seconds() // 60)
    fare_mult, seat_share = CABINS[cabin]
    fare = sum(_leg_fare(l, today) for l in legs) * (0.85 if len(legs) > 1 else 1.0) * fare_mult
    first, last = legs[0], legs[-1]
    day_offset = (last.arr_local.date() - first.dep_local.date()).days
    offer_id = "OF-" + "-".join(
        f"{l.flight_no.replace(' ', '')}{l.dep_local.strftime('%Y%m%d%H%M')}" for l in legs
    ) + ("" if cabin == "economy" else f"-{cabin.upper()}")
    seats = min(_seats_left(l, today) for l in legs)
    if cabin != "economy":
        seats = max(0, round(seats * seat_share * 2))
    price = float(round(fare))
    return {
        "offer_id": offer_id,
        "price": price,
        "total_price": float(round(price * passengers)),
        "passengers": passengers,
        "currency": "USD",
        "date": first.dep_local.date().isoformat(),
        "airline": first.carrier,
        "airline_name": net.carriers.get(first.carrier, first.carrier),
        "carriers": sorted({l.carrier for l in legs}),
        "carrier_names": sorted({net.carriers.get(l.carrier, l.carrier) for l in legs}),
        "flight_numbers": [l.flight_no for l in legs],
        "stops": len(legs) - 1,
        "via": [l.dest.iata for l in legs[:-1]],
        "from": first.origin.iata,
        "to": last.dest.iata,
        "depart_local": first.dep_local.isoformat(timespec="minutes"),
        "arrive_local": last.arr_local.isoformat(timespec="minutes"),
        "depart_time": first.dep_local.strftime("%H:%M"),
        "arrive_time": last.arr_local.strftime("%H:%M"),
        "depart_hour": first.dep_local.hour,
        "arrive_day_offset": day_offset,
        "duration_min": total_min,
        "duration": _fmt_duration(total_min),
        "seats_left": seats,
        "cabin": CABIN_LABELS[cabin],
        "cabin_code": cabin,
        "segments": [l.to_dict(net) for l in legs],
    }


def _place_summary(a: Airport) -> dict:
    return {"iata": a.iata, "name": a.name, "city": a.city, "country": a.country_name}


# Offers recently generated by any search, so a booking can still find its
# offer after a reconnect or when the search came from the REST API rather
# than this session's agent. Bounded; oldest entries fall out first.
_RECENT_OFFERS: "OrderedDict[str, dict]" = OrderedDict()
_RECENT_LIMIT = 20000


def _remember(offers: list[dict]) -> None:
    for o in offers:
        _RECENT_OFFERS[o["offer_id"]] = o
        _RECENT_OFFERS.move_to_end(o["offer_id"])
    while len(_RECENT_OFFERS) > _RECENT_LIMIT:
        _RECENT_OFFERS.popitem(last=False)


def _search_leg(origins: list[Airport], dests: list[Airport], day: _dt.date, today: _dt.date,
                net: _Network, *, cabin: str, passengers: int) -> list[dict]:
    itineraries: list[list[_Leg]] = []
    for o in origins:
        for d in dests:
            itineraries.extend([leg] for leg in _legs_on(o, d, day, net))
    if len(itineraries) < 4:
        for o in origins[:2]:
            for d in dests[:2]:
                itineraries.extend(_connections(o, d, day, net))
    if not itineraries:
        itineraries.extend(_two_stop(origins[0], dests[0], day, net))

    offers = [_offer(legs, today, net, cabin=cabin, passengers=passengers) for legs in itineraries]
    # de-duplicate (two hubs can yield the same pair of flights); drop
    # flights without enough seats left in the requested cabin
    offers = [o for o in {o["offer_id"]: o for o in offers}.values() if o["seats_left"] >= passengers]
    if not offers:
        return []
    min_price = min(o["price"] for o in offers)
    min_dur = min(o["duration_min"] for o in offers)
    for o in offers:
        o["score"] = round(o["price"] / min_price + o["duration_min"] / min_dur + 0.15 * o["stops"], 4)
    offers.sort(key=lambda o: o["score"])
    offers = offers[:MAX_RESULTS]
    cheapest = min(offers, key=lambda o: o["price"])["offer_id"]
    fastest = min(offers, key=lambda o: o["duration_min"])["offer_id"]
    for o in offers:
        tags = []
        if o["offer_id"] == offers[0]["offer_id"]:
            tags.append("best")
        if o["offer_id"] == cheapest:
            tags.append("cheapest")
        if o["offer_id"] == fastest:
            tags.append("fastest")
        o["tags"] = tags
    _remember(offers)
    return offers


def _check_date(text: str, today: _dt.date, *, what: str) -> tuple[Optional[_dt.date], Optional[dict]]:
    day = resolve_travel_date(text, today)
    if day is None:
        return None, {"status": "invalid_date", "message": f"I couldn't understand the {what} date '{text}'."}
    if day < today:
        return None, {"status": "past_date", "message": f"The {what} date {day.isoformat()} is in the past."}
    if (day - today).days > MAX_DAYS_AHEAD:
        return None, {"status": "too_far", "message": "Schedules are only modeled about 11 months ahead."}
    return day, None


def _resolve_route(origin: str, destination: str, base: dict):
    origins = primary_airports(origin or "")
    dests = primary_airports(destination or "")
    if not origins:
        return None, None, {**base, "status": "unknown_origin",
                            "message": f"I couldn't match '{origin}' to an airport with scheduled flights.",
                            "suggestions": search_airports(origin or "", 5)}
    if not dests:
        return None, None, {**base, "status": "unknown_destination",
                            "message": f"I couldn't match '{destination}' to an airport with scheduled flights.",
                            "suggestions": search_airports(destination or "", 5)}
    if {a.iata for a in origins} & {a.iata for a in dests}:
        return None, None, {**base, "status": "same_place", "message": "Origin and destination are the same place."}
    return origins, dests, None


def search_schedule(origin: str, destination: str, date_text: str, *, today: Optional[_dt.date] = None,
                    passengers: Any = 1, cabin: Any = "economy", return_date: Optional[str] = None) -> dict:
    """The whole search, synchronous and pure (given `today`). With
    `return_date`, the result also carries a "return" leg (destination ->
    origin) with its own offers."""
    today = today or _dt.date.today()
    net = network()
    pax = normalize_passengers(passengers)
    cabin_code = normalize_cabin(cabin)
    base = {"origin_query": origin, "destination_query": destination, "date_query": date_text,
            "return_query": return_date or None, "passengers": pax, "cabin": CABIN_LABELS[cabin_code],
            "cabin_code": cabin_code, "modeled": True, "offers": []}

    origins, dests, problem = _resolve_route(origin, destination, base)
    if problem:
        return problem
    day, problem = _check_date(date_text, today, what="departure")
    if problem:
        return {**base, **problem}
    ret_day = None
    if return_date:
        ret_day, problem = _check_date(return_date, today, what="return")
        if problem:
            return {**base, **problem}
        if ret_day < day:
            return {**base, "status": "invalid_date", "message": "The return date is before the departure date."}

    offers = _search_leg(origins, dests, day, today, net, cabin=cabin_code, passengers=pax)
    result = {
        **base,
        "status": "ok" if offers else "no_service",
        "trip_type": "round_trip" if ret_day else "one_way",
        "origin": _place_summary(origins[0]),
        "destination": _place_summary(dests[0]),
        "origin_airports": [a.iata for a in origins],
        "destination_airports": [a.iata for a in dests],
        "date": day.isoformat(),
        "weekday": day.strftime("%A"),
        "currency": "USD",
        "offers": offers,
    }
    if not offers:
        result["message"] = (
            f"No {CABIN_LABELS[cabin_code].lower()} seats for {pax} with up to two connections between "
            f"{origins[0].city} and {dests[0].city} on {day.strftime('%a %d %b')} in the route model."
        )
    if ret_day:
        ret_offers = _search_leg(dests, origins, ret_day, today, net, cabin=cabin_code, passengers=pax)
        result["return"] = {
            "date": ret_day.isoformat(),
            "weekday": ret_day.strftime("%A"),
            "offers": ret_offers,
            "status": "ok" if ret_offers else "no_service",
        }
        if offers and not ret_offers:
            result["status"] = "no_service"
            result["message"] = f"No return flights found on {ret_day.strftime('%a %d %b')}."
    return result


def fare_calendar(origin: str, destination: str, date_text: str, *, today: Optional[_dt.date] = None,
                  days: int = 3, passengers: Any = 1, cabin: Any = "economy") -> dict:
    """Lowest fare per day around a date (the results page's date strip) --
    the same engine, run for each neighbouring day."""
    today = today or _dt.date.today()
    net = network()
    pax = normalize_passengers(passengers)
    cabin_code = normalize_cabin(cabin)
    base = {"origin_query": origin, "destination_query": destination, "days": []}
    origins, dests, problem = _resolve_route(origin, destination, base)
    if problem:
        return problem
    center, problem = _check_date(date_text, today, what="departure")
    if problem:
        return {**base, **problem}
    out = []
    for delta in range(-days, days + 1):
        day = center + _dt.timedelta(days=delta)
        if day < today or (day - today).days > MAX_DAYS_AHEAD:
            continue
        offers = _search_leg(origins, dests, day, today, net, cabin=cabin_code, passengers=pax)
        out.append({
            "date": day.isoformat(),
            "weekday": day.strftime("%a"),
            "min_price": min((o["price"] for o in offers), default=None),
            "flights": len(offers),
            "selected": delta == 0,
        })
    priced = [d for d in out if d["min_price"] is not None]
    if priced:
        low = min(d["min_price"] for d in priced)
        for d in out:
            d["cheapest"] = d["min_price"] == low
    return {**base, "status": "ok", "currency": "USD", "date": center.isoformat(), "days": out}


def top_destinations(iata: str, n: int = 12) -> list[dict]:
    """Busiest routes out of an airport in the model (airport explorer)."""
    net = network()
    out = []
    for dst, services in net.out.get((iata or "").upper(), {}).items():
        a = get_airport(dst)
        if a is None:
            continue
        out.append({
            "iata": a.iata, "city": a.city, "country": a.country_name, "name": a.name,
            "weekly_flights": sum(s.weekly for s in services),
            "airlines": sorted({net.carriers.get(s.carrier, s.carrier) for s in services}),
        })
    out.sort(key=lambda d: (-d["weekly_flights"], d["city"]))
    return out[:n]


# ---------------------------------------------------------------------------
# Tool environment
# ---------------------------------------------------------------------------


class OfflineFlightProvider:
    """search_flights / book_flight backed by the offline world model.
    One instance per WebSocket session (see server/app.py), which is also
    the lifetime of the offers a booking can refer to."""

    def __init__(self, latency_ms: tuple[float, float] = (400.0, 900.0), *, today_fn=None) -> None:
        self.latency_ms = latency_ms
        self._today_fn = today_fn or _dt.date.today
        self._offers: dict[str, dict] = {}
        self._bookings: dict[tuple[str, str], dict] = {}
        self.call_log: list[dict] = []

    async def _simulate_latency(self, name: str, args: dict) -> None:
        lo, hi = self.latency_ms
        delay = lo + (hi - lo) * _unit(f"{name}:{sorted(args.items())}") if hi > 0 else 0.0
        self.call_log.append({"tool": name, "args": args, "delay_ms": delay})
        if delay > 0:
            await trio.sleep(delay / 1000.0)

    async def search_flights(self, args: dict) -> dict:
        await self._simulate_latency("search_flights", args)
        result = search_schedule(
            str(args.get("origin", "")), str(args.get("destination", "")), str(args.get("date", "")),
            today=self._today_fn(),
            passengers=args.get("passengers", 1),
            cabin=args.get("cabin", "economy"),
            return_date=(str(args["return_date"]) if args.get("return_date") else None),
        )
        for offer in result.get("offers", []) + (result.get("return") or {}).get("offers", []):
            self._offers[offer["offer_id"]] = offer
        return result

    async def book_flight(self, args: dict) -> dict:
        await self._simulate_latency("book_flight", args)
        return self.book(str(args.get("offer_id", "")), str(args.get("passenger_name", "")))

    def book(self, offer_id: str, passenger_name: str) -> dict:
        name = " ".join(passenger_name.split())
        if not name:
            raise FlightModelError("A passenger name is required to book.")
        offer = self._offers.get(offer_id) or _RECENT_OFFERS.get(offer_id)
        if offer is None:
            raise FlightModelError(
                f"Unknown offer '{offer_id}' -- search first, then book one of the returned offers."
            )
        key = (offer_id, name.lower())
        if key not in self._bookings:  # idempotent: same offer + name -> same booking
            alphabet = string.ascii_uppercase.replace("I", "").replace("O", "") + "23456789"
            n = _h(f"{offer_id}:{name.lower()}")
            pnr = "".join(alphabet[(n >> (5 * i)) % len(alphabet)] for i in range(6))
            self._bookings[key] = {
                "confirmation_id": pnr,
                "passenger_name": name,
                "status": "booked",
                "offer_id": offer_id,
                "summary": {
                    "flights": offer["flight_numbers"],
                    "depart_local": offer["depart_local"],
                    "arrive_local": offer["arrive_local"],
                    "route": [offer["segments"][0]["from"]] + [s["to"] for s in offer["segments"]],
                    "price": offer["price"],
                    "passengers": offer.get("passengers", 1),
                    "total_price": offer.get("total_price", offer["price"]),
                    "cabin": offer.get("cabin", "Economy"),
                    "currency": offer["currency"],
                },
                "modeled": True,
                "note": "Simulated booking against the offline flight model -- no ticket is issued.",
            }
        return self._bookings[key]

    def as_registry(self) -> dict:
        return {"search_flights": self.search_flights, "book_flight": self.book_flight}


def describe_flight_backend(env: Any) -> str:
    if isinstance(env, OfflineFlightProvider):
        counts = model_meta().get("counts", {})
        return (f"OfflineFlightModel ({counts.get('airports', 0):,} airports · "
                f"{counts.get('airport_pairs', 0):,} routes)")
    return type(env).__name__


def default_flight_env(mock_env: Any) -> Any:
    """Offline world model by default; PRISM_FLIGHTS_BACKEND=mock returns
    `mock_env` (mock_env.py's original fake generator) instead. The mock's
    latency window is reused so the barge-in demo keeps the same feel."""
    if os.environ.get("PRISM_FLIGHTS_BACKEND", "").strip().lower() == "mock":
        return mock_env
    latency = getattr(getattr(mock_env, "config", None), "latency_ms", (400.0, 900.0))
    return OfflineFlightProvider(latency_ms=latency)


__all__ = [
    "CABIN_LABELS",
    "FlightModelError",
    "fare_calendar",
    "normalize_cabin",
    "normalize_passengers",
    "top_destinations",
    "OfflineFlightProvider",
    "default_flight_env",
    "describe_flight_backend",
    "network",
    "search_schedule",
]
