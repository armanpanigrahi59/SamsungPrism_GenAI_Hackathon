"""
agent/travel_parse.py: pulling origin / destination / date out of free
text using the world airport gazetteer, and turning date phrases into
real dates.
"""
import datetime as dt

import pytest

from agent.travel_parse import extract_date_phrase, extract_route, resolve_travel_date

TODAY = dt.date(2026, 10, 4)  # Sunday


@pytest.mark.parametrize("text,origin,dest", [
    ("book a flight from Delhi to Paris on the 5th", "Delhi", "Paris"),
    ("yo i need a flight outta chicago headed to miami next friday", "chicago", "miami"),
    ("I want to fly from New York to London tomorrow", "New York", "London"),
    ("chicago to miami on dec 12", "chicago", "miami"),
    ("Mumbai → Frankfurt 12 december", "Mumbai", "Frankfurt"),
    ("flights from bombay to madras in 3 days", "bombay", "madras"),
    ("fly me to tokyo from sydney this weekend", "sydney", "tokyo"),
    ("flying out of sao paulo to buenos aires on friday", "sao paulo", "buenos aires"),
    ("book a flight from DEL to CDG on 2026-11-05", "DEL", "CDG"),
])
def test_extract_route(text, origin, dest):
    o, d, info = extract_route(text)
    assert (o, d) == (origin, dest)
    assert info == {"origin": "known", "destination": "known"}


def test_last_mention_wins_on_a_correction():
    o, d, _ = extract_route("from Delhi to Paris, no wait, to Tokyo")
    assert (o, d) == ("Delhi", "Tokyo")


def test_unknown_places_are_captured_but_flagged():
    o, d, info = extract_route("book a flight from Gotham to Metropolis on the 5th")
    assert (o, d) == ("Gotham", "Metropolis")
    assert info == {"origin": "guessed", "destination": "guessed"}


def test_verbs_after_to_are_not_destinations():
    o, d, _ = extract_route("i want to go to the beach")
    assert d is None


@pytest.mark.parametrize("text,phrase", [
    ("on the 5th", "5th"),
    ("on the 5th of November", "5th of November"),
    ("leaving on 2026-11-05 please", "2026-11-05"),
    ("next friday", "next friday"),
    ("on friday", "friday"),
    ("tomorrow morning", "tomorrow"),
    ("around dec 12", "dec 12"),
    ("12 december", "12 december"),
    ("in 3 days", "in 3 days"),
    ("this weekend", "this weekend"),
    ("on the 5th please", "5th"),
])
def test_extract_date_phrase(text, phrase):
    assert extract_date_phrase(text) == phrase


@pytest.mark.parametrize("phrase,expected", [
    ("today", "2026-10-04"),
    ("tomorrow", "2026-10-05"),
    ("next friday", "2026-10-09"),
    ("friday", "2026-10-09"),
    ("this weekend", "2026-10-10"),
    ("in 3 days", "2026-10-07"),
    ("5th", "2026-10-05"),
    ("3rd", "2026-11-03"),           # already past this month -> next month
    ("5th of November", "2026-11-05"),
    ("dec 12", "2026-12-12"),
    ("march 2", "2027-03-02"),       # no year -> next occurrence
    ("2026-11-05", "2026-11-05"),
])
def test_resolve_travel_date(phrase, expected):
    assert resolve_travel_date(phrase, TODAY).isoformat() == expected


def test_resolve_travel_date_rejects_nonsense():
    assert resolve_travel_date("sometime", TODAY) is None
    assert resolve_travel_date("31st of february", TODAY) is None
    assert resolve_travel_date("", TODAY) is None
