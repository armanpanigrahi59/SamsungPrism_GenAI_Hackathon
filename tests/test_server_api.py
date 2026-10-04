"""
server/app.py: every HTTP endpoint the pages call, plus one full WebSocket
session (search -> streamed results -> booking), through Quart-Trio's test
client -- the same frontend <-> backend contract the browser uses.
Skipped when the web extras aren't installed.
"""
import datetime as dt
import json

import pytest
import trio

pytest.importorskip("quart_trio")

from server.app import API_VERSION, app  # noqa: E402

IN_A_MONTH = (dt.date.today() + dt.timedelta(days=30)).isoformat()
RETURN_DAY = (dt.date.today() + dt.timedelta(days=37)).isoformat()


async def get_json(path):
    client = app.test_client()
    resp = await client.get(path)
    return resp.status_code, await resp.get_json()


@pytest.mark.parametrize("page", ["/", "/flights", "/support", "/airports", "/how-it-works"])
async def test_pages_render(page):
    client = app.test_client()
    resp = await client.get(page)
    assert resp.status_code == 200
    assert "<html" in (await resp.get_data(as_text=True))


@pytest.mark.parametrize("asset", ["style.css", "assistant.js", "flights.js", "airports.js"])
async def test_assets_served(asset):
    client = app.test_client()
    resp = await client.get(f"/assets/{asset}")
    assert resp.status_code == 200


async def test_version_lets_pages_detect_a_stale_server():
    status, body = await get_json("/api/version")
    assert status == 200 and body["api"] == API_VERSION and body["airports"] > 3000


async def test_airport_autocomplete():
    status, body = await get_json("/api/airports?q=Mumbai")
    assert status == 200 and body["airports"][0]["iata"] == "BOM"
    _, body = await get_json("/api/airports?q=chennai")
    assert body["airports"][0]["iata"] == "MAA"


async def test_popular_airports_and_explorer():
    _, body = await get_json("/api/airports/popular?n=10")
    assert len(body["airports"]) == 10
    _, body = await get_json("/api/airports/popular?n=5&continent=AS")
    assert all(a["continent"] == "AS" for a in body["airports"])
    _, body = await get_json("/api/airports/browse?continent=EU&country=FR&page_size=10")
    assert body["total"] > 10 and body["airports"][0]["country"] == "FR"
    _, body = await get_json("/api/countries?continent=SA")
    assert any(c["code"] == "BR" for c in body["countries"]) and "SA" in body["continents"]
    status, body = await get_json("/api/airport/BOM")
    assert status == 200 and body["city"] == "Mumbai" and body["destinations"]
    status, _ = await get_json("/api/airport/ZZZ")
    assert status == 404


async def test_direct_search_round_trip_with_options():
    status, body = await get_json(
        f"/api/search?from=Mumbai&to=Chennai&date={IN_A_MONTH}&return={RETURN_DAY}&pax=2&cabin=business")
    assert status == 200 and body["status"] == "ok"
    assert body["trip_type"] == "round_trip" and body["return"]["offers"]
    offer = body["offers"][0]
    assert offer["cabin"] == "Business" and offer["passengers"] == 2
    assert offer["total_price"] == offer["price"] * 2


async def test_fare_calendar():
    _, body = await get_json(f"/api/fares?from=DEL&to=BOM&date={IN_A_MONTH}")
    assert body["status"] == "ok" and len(body["days"]) == 7
    assert sum(d["selected"] for d in body["days"]) == 1


async def test_rest_booking_after_rest_search():
    _, search = await get_json(f"/api/search?from=DEL&to=BOM&date={IN_A_MONTH}")
    offer_id = search["offers"][0]["offer_id"]
    client = app.test_client()
    resp = await client.post("/api/book", json={"offer_id": offer_id, "passenger_name": "Asha Rao"})
    body = await resp.get_json()
    assert resp.status_code == 200 and body["ok"] and len(body["booking"]["confirmation_id"]) == 6
    resp = await client.post("/api/book", json={"offer_id": "OF-nope", "passenger_name": "Asha Rao"})
    assert resp.status_code == 400


async def test_websocket_search_streams_results_then_books():
    client = app.test_client()
    async with client.websocket("/ws") as ws:
        await ws.send(json.dumps({"type": "init", "domain": "flights"}))
        await ws.send(json.dumps({
            "type": "text_chunk", "end_of_turn": True,
            "text": f"book a flight from BOM to MAA on {IN_A_MONTH} for 2 passengers in business class",
        }))
        result = None
        backends = {}
        with trio.fail_after(10):
            while result is None:
                msg = json.loads(await ws.receive())
                if msg["type"] in ("nlu_backend", "flights_backend"):
                    backends[msg["type"]] = msg["chain"]
                if msg["type"] == "tool_result" and msg["tool"] == "search_flights":
                    result = msg["result"]
        assert "OfflineFlightModel" in backends["flights_backend"]
        assert result["status"] == "ok" and result["passengers"] == 2 and result["cabin"] == "Business"
        offer_id = result["offers"][0]["offer_id"]

        await ws.send(json.dumps({"type": "book", "offer_id": offer_id, "passenger_name": "Asha Rao"}))
        with trio.fail_after(10):
            while True:
                msg = json.loads(await ws.receive())
                if msg["type"] == "booking":
                    break
        assert msg["ok"] and msg["booking"]["summary"]["passengers"] == 2
