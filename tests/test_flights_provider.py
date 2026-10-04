"""
agent/flights_provider.py: the offline day-to-day flight engine behind
search_flights / book_flight. No network, no API key -- every test runs
against the bundled model with a fixed "today" so results are stable.
"""
import datetime as dt

import pytest

from agent.flights_provider import (
    FlightModelError,
    OfflineFlightProvider,
    default_flight_env,
    describe_flight_backend,
    search_schedule,
)
from agent.mock_env import MockConfig, MockToolEnvironment

TODAY = dt.date(2026, 10, 4)  # a Sunday


def search(o, d, date):
    return search_schedule(o, d, date, today=TODAY)


def test_direct_flights_between_major_cities():
    r = search("New York", "London", "next friday")
    assert r["status"] == "ok"
    assert r["date"] == "2026-10-09"
    assert r["origin"]["city"] == "New York" and r["destination"]["city"] == "London"
    assert r["offers"] and all(o["stops"] == 0 for o in r["offers"][:3])
    o = r["offers"][0]
    for key in ("offer_id", "price", "currency", "airline_name", "flight_numbers", "depart_local",
                "arrive_local", "duration_min", "seats_left", "segments"):
        assert key in o
    assert r["modeled"] is True


def test_times_are_local_and_block_time_is_physical():
    r = search("JFK", "LHR", "2026-10-09")
    seg = r["offers"][0]["segments"][0]
    assert 5 * 60 < seg["duration_min"] < 9 * 60      # ~5,550 km eastbound
    dep = dt.datetime.fromisoformat(seg["depart_local"])
    arr = dt.datetime.fromisoformat(seg["arrive_local"])
    # New York is 5h behind London in October (EDT vs BST)
    assert (arr - dep) == dt.timedelta(minutes=seg["duration_min"] + 5 * 60)


def test_same_search_same_day_is_deterministic():
    assert search("Delhi", "Paris", "2026-11-05") == search("Delhi", "Paris", "2026-11-05")


def test_schedule_and_fares_change_day_to_day():
    a = search("Delhi", "Dubai", "2026-11-05")
    b = search("Delhi", "Dubai", "2026-11-06")
    assert a["offers"] and b["offers"]
    assert [o["price"] for o in a["offers"]] != [o["price"] for o in b["offers"]]


def test_last_minute_fares_cost_more_than_advance_fares():
    soon = search("Mumbai", "Delhi", "tomorrow")
    later = search("Mumbai", "Delhi", "in 45 days")
    cheapest = lambda r: min(o["price"] for o in r["offers"])
    assert cheapest(soon) > cheapest(later)


def test_connections_when_there_is_no_direct_flight():
    r = search("Kathmandu", "Lima", "2026-11-20")
    assert r["status"] == "ok"
    assert all(o["stops"] >= 1 for o in r["offers"])
    o = r["offers"][0]
    legs = o["segments"]
    assert legs[0]["from"] == "KTM" and legs[-1]["to"] == "LIM"
    for first, second in zip(legs, legs[1:]):
        assert first["to"] == second["from"]


def test_metro_search_covers_all_city_airports():
    r = search("London", "Paris", "2026-11-05")
    assert set(r["origin_airports"]) >= {"LHR", "LGW"}
    assert set(r["destination_airports"]) == {"CDG", "ORY"}


@pytest.mark.parametrize("origin,dest,date,status", [
    ("Gotham", "Paris", "tomorrow", "unknown_origin"),
    ("Paris", "Metropolis", "tomorrow", "unknown_destination"),
    ("Delhi", "Delhi", "tomorrow", "same_place"),
    ("Delhi", "Paris", "2025-01-01", "past_date"),
    ("Delhi", "Paris", "sometime", "invalid_date"),
    ("Delhi", "Paris", "2028-01-01", "too_far"),
])
def test_explains_searches_it_cannot_run(origin, dest, date, status):
    r = search(origin, dest, date)
    assert r["status"] == status
    assert r["message"]
    assert r["offers"] == []


def test_unknown_place_comes_with_suggestions():
    r = search("Bangalor", "Delhi", "tomorrow")  # typo resolves via fuzzy match
    assert r["status"] == "ok" and r["origin"]["city"] == "Bengaluru"


async def test_provider_search_then_book_is_idempotent():
    provider = OfflineFlightProvider(latency_ms=(0, 0), today_fn=lambda: TODAY)
    result = await provider.search_flights({"origin": "DEL", "destination": "BOM", "date": "2026-10-20"})
    offer = result["offers"][0]
    first = await provider.book_flight({"offer_id": offer["offer_id"], "passenger_name": "Asha Rao"})
    again = await provider.book_flight({"offer_id": offer["offer_id"], "passenger_name": "asha  rao"})
    assert first["status"] == "booked"
    assert len(first["confirmation_id"]) == 6
    assert again["confirmation_id"] == first["confirmation_id"]  # no double booking
    assert first["summary"]["route"][0] == "DEL" and first["summary"]["route"][-1] == "BOM"


async def test_book_rejects_unknown_offer_and_missing_name():
    provider = OfflineFlightProvider(latency_ms=(0, 0), today_fn=lambda: TODAY)
    with pytest.raises(FlightModelError):
        await provider.book_flight({"offer_id": "OF-nope", "passenger_name": "Asha Rao"})
    result = await provider.search_flights({"origin": "DEL", "destination": "BOM", "date": "2026-10-20"})
    with pytest.raises(FlightModelError):
        await provider.book_flight({"offer_id": result["offers"][0]["offer_id"], "passenger_name": "  "})


def test_default_env_is_the_offline_model(monkeypatch):
    monkeypatch.delenv("PRISM_FLIGHTS_BACKEND", raising=False)
    mock = MockToolEnvironment(MockConfig(latency_ms=(10.0, 20.0)))
    env = default_flight_env(mock)
    assert isinstance(env, OfflineFlightProvider)
    assert env.latency_ms == (10.0, 20.0)  # keeps the demo's latency window
    assert "OfflineFlightModel" in describe_flight_backend(env)
    assert set(env.as_registry()) == {"search_flights", "book_flight"}


def test_mock_backend_still_selectable(monkeypatch):
    monkeypatch.setenv("PRISM_FLIGHTS_BACKEND", "mock")
    mock = MockToolEnvironment()
    assert default_flight_env(mock) is mock
