"""
Trip options end to end: passengers / cabin / return date pulled out of
free text, and the schedule engine honouring them (round trips, cabin
pricing, per-passenger totals, the fare calendar), plus the airport
explorer helpers behind /airports.
"""
import datetime as dt

import pytest

from agent.airports import browse_airports, continents, countries, popular_airports
from agent.flights_provider import fare_calendar, normalize_cabin, normalize_passengers, search_schedule, top_destinations
from agent.travel_parse import extract_trip_options

TODAY = dt.date(2026, 10, 4)  # a Sunday


@pytest.mark.parametrize("text,expected", [
    ("book a flight from Delhi to Paris on the 5th", {"date": "5th"}),
    ("chicago to miami next friday, back on the 20th, 2 adults",
     {"date": "next friday", "return_date": "20th", "passengers": 2}),
    ("mumbai to chennai on dec 12 returning on dec 19 for three people in business class",
     {"date": "dec 12", "return_date": "dec 19", "passengers": 3, "cabin": "business"}),
    ("london to new york tomorrow one way, premium economy", {"date": "tomorrow", "return_date": "", "cabin": "premium_economy"}),
    ("first class from dubai to london on friday", {"date": "friday", "cabin": "first"}),
    ("the first flight out of delhi tomorrow", {"date": "tomorrow"}),
])
def test_extract_trip_options(text, expected):
    assert extract_trip_options(text) == expected


def test_option_normalisers_are_forgiving():
    assert normalize_cabin("Business") == "business"
    assert normalize_cabin("premium economy") == "premium_economy"
    assert normalize_cabin("nonsense") == "economy"
    assert normalize_passengers("3") == 3
    assert normalize_passengers(0) == 1 and normalize_passengers(42) == 9


def test_round_trip_has_a_return_leg():
    r = search_schedule("Mumbai", "Chennai", "2026-11-05", today=TODAY, return_date="2026-11-12")
    assert r["status"] == "ok" and r["trip_type"] == "round_trip"
    out, back = r["offers"][0], r["return"]["offers"][0]
    assert out["from"] == "BOM" and back["to"] == "BOM"
    assert back["depart_local"].startswith("2026-11-12")


def test_return_before_departure_is_explained():
    r = search_schedule("Mumbai", "Chennai", "2026-11-05", today=TODAY, return_date="2026-11-01")
    assert r["status"] != "ok" or r.get("return", {}).get("status") != "ok"


def test_cabins_cost_more_and_totals_cover_every_passenger():
    eco = search_schedule("DEL", "BOM", "2026-11-05", today=TODAY)["offers"][0]
    biz = search_schedule("DEL", "BOM", "2026-11-05", today=TODAY, cabin="business", passengers=3)
    biz_same = next(o for o in biz["offers"] if o["flight_numbers"] == eco["flight_numbers"])
    assert biz_same["price"] > eco["price"] * 2
    assert biz_same["total_price"] == biz_same["price"] * 3 and biz_same["cabin"] == "Business"


def test_fare_calendar_brackets_the_chosen_day():
    cal = fare_calendar("DEL", "BOM", "2026-11-05", today=TODAY, days=3)
    assert cal["status"] == "ok" and len(cal["days"]) == 7
    assert [d["date"] for d in cal["days"] if d["selected"]] == ["2026-11-05"]
    prices = [d["min_price"] for d in cal["days"] if d["min_price"]]
    assert [d["min_price"] for d in cal["days"] if d["cheapest"]] == [min(prices)]


def test_renamed_cities_show_their_current_name():
    from agent.airports import get_airport, resolve_place
    assert get_airport("MAA").city == "Chennai" and "Madras" in get_airport("MAA").aliases
    assert get_airport("BLR").city == "Bengaluru"
    assert resolve_place("madras")[0].iata == "MAA"
    assert resolve_place("bangalore")[0].iata == "BLR"


def test_explorer_helpers():
    assert set(continents()) >= {"AF", "AS", "EU", "NA", "OC", "SA"}
    assert countries("AS")[0]["code"] in ("CN", "IN", "JP", "US")
    assert all(a["continent"] == "EU" for a in popular_airports(8, "EU"))
    page = browse_airports(country="IN", query="mumbai")
    assert page["airports"][0]["iata"] == "BOM"
    every = browse_airports(page_size=1)["total"]
    served = browse_airports(page_size=1, routes_only=True)["total"]
    assert 2000 < served < every
    last = browse_airports(page=10_000, page_size=50)
    assert last["page"] == last["pages"] and last["airports"]
    dests = top_destinations("BOM", 5)
    assert len(dests) == 5 and dests[0]["weekly_flights"] >= dests[-1]["weekly_flights"]
