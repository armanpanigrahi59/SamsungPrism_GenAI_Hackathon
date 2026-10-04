"""
agent/airports.py: the world airport gazetteer compiled into
agent/data/flight_model.json.gz (scripts/build_flight_model.py) -- search
ranking, place resolution (cities, old names, metro codes, typos) and the
token matcher the NLU uses.
"""
from agent.airports import (
    get_airport,
    load_model,
    match_place_at,
    model_meta,
    primary_airports,
    resolve_place,
    search_airports,
)


def test_model_covers_the_worlds_major_airports():
    counts = model_meta()["counts"]
    assert counts["airports"] > 3000
    assert counts["countries"] > 200
    assert counts["airport_pairs"] > 20000
    assert counts["carriers"] > 300
    # sources and licences travel with the data
    names = {s["name"] for s in model_meta()["sources"]}
    assert names == {"OurAirports", "OpenFlights"}


def test_frequency_model_is_calibrated_to_real_world_daily_flights():
    cal = model_meta()["calibration"]
    assert abs(cal["fitted_daily_departures"] - cal["target_daily_departures"]) < 0.02 * cal["target_daily_departures"]


def test_exact_iata_ranks_first():
    results = search_airports("DEL")
    assert results[0]["iata"] == "DEL"
    assert results[0]["country_name"] == "India"


def test_city_search_ranks_hubs_before_small_airports():
    codes = [r["iata"] for r in search_airports("new york", 5)]
    assert codes[:3] == ["JFK", "EWR", "LGA"]
    london = [r["iata"] for r in search_airports("london", 5)]
    assert london[0] == "LHR"
    assert {"LGW", "STN"} <= set(london)


def test_search_is_accent_and_case_insensitive():
    assert search_airports("sao paulo")[0]["iata"] in ("GRU", "CGH")
    assert search_airports("ZURICH")[0]["iata"] == "ZRH"


def test_search_short_query_returns_nothing():
    assert search_airports("d") == []
    assert search_airports("") == []


def test_search_respects_limit():
    assert len(search_airports("san", limit=3)) == 3


def test_resolve_historic_names_and_metro_codes():
    assert [a.iata for a in resolve_place("Bombay")] == ["BOM"]
    assert [a.iata for a in resolve_place("Madras")] == ["MAA"]
    assert resolve_place("Peking")[0].iata == "PEK"
    assert {"JFK", "EWR", "LGA"} <= {a.iata for a in resolve_place("NYC")}


def test_resolve_served_city_not_runway_town():
    # OurAirports lists KUL under "Sepang" -- travellers say Kuala Lumpur
    assert get_airport("KUL").city == "Kuala Lumpur"
    assert resolve_place("Kuala Lumpur")[0].iata == "KUL"
    assert resolve_place("Sepang")[0].iata == "KUL"


def test_resolve_tolerates_typos():
    assert resolve_place("chicgo")[0].iata == "ORD"


def test_primary_airports_is_the_metro_not_every_alias():
    assert [a.iata for a in primary_airports("Chicago")] == ["ORD", "MDW"]
    assert primary_airports("Gotham") == []


def test_replaced_airports_inherit_their_routes():
    # Berlin Tegel/Schoenefeld closed in 2020; their routes now belong to BER
    assert get_airport("BER").has_routes
    assert get_airport("TXL") is None


def test_match_place_at_prefers_longest_name():
    tokens = "fly from New York to Los Angeles".split()
    n, airports = match_place_at(tokens, 2, after_cue=True)
    assert n == 2 and airports[0].iata == "JFK"
    n, airports = match_place_at(tokens, 5, after_cue=True)
    assert n == 2 and airports[0].iata == "LAX"


def test_common_words_are_not_places_without_a_cue():
    assert match_place_at("a nice day".split(), 1, after_cue=False) is None
    assert match_place_at("to Nice".split(), 1, after_cue=True)[1][0].iata == "NCE"
    # lowercase 3-letter English words are never read as IATA codes
    assert match_place_at("to the beach".split(), 1, after_cue=True) is None


def test_model_file_is_plain_data():
    model = load_model()
    assert model["version"] == 1
    assert len(model["airports"][0]) == 11
