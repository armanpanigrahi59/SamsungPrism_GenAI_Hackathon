"""
Travel-language parsing shared by the NLU (agent/nlu.py) and the flight
engine (agent/flights_provider.py):

  extract_route(text)        -> (origin_span, destination_span) as typed,
                                using the world airport gazetteer
                                (agent/airports.py) so any of ~3,200 airports
                                / their cities / old names ("Bombay") work,
                                including informal phrasing:
                                "outta chicago headed to miami",
                                "Delhi to Paris tomorrow", "from NYC to LON".
  extract_date_phrase(text)  -> the date as said ("5th", "next friday",
                                "2026-11-05", "12 december", "tomorrow").
  resolve_travel_date(p, today) -> a concrete datetime.date for that phrase.
  extract_trip_options(text) -> outbound date, return date, passengers and
                                cabin ("returning on the 25th", "for 2
                                adults", "in business class").

Slot values stay exactly as the user typed them (so the trip card shows
"chicago", not an airport code); the flight engine resolves them to
airports and dates at search time.
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Optional

from .airports import match_place_at

# ---------------------------------------------------------------------------
# Tokenising
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[^\W_]+(?:['’.\-][^\W_]+)*|[→>]", re.UNICODE)

ORIGIN_CUES = [
    ("flying", "out", "of"), ("leaving", "from"), ("departing", "from"),
    ("out", "of"), ("from",), ("outta",), ("leaving",), ("departing",), ("ex",),
]
DEST_CUES = [
    ("headed", "to"), ("heading", "to"), ("bound", "for"), ("going", "to"),
    ("arriving", "in"), ("arriving", "at"), ("landing", "in"), ("flying", "to"),
    ("fly", "to"), ("into",), ("towards",), ("toward",), ("to",), ("→",), (">",),
]
_CUE_ONLY_STRONG = {("ex",)}  # weak cue: only accepted when followed by a known place

# Words that end a free-text place capture (used only when the gazetteer
# doesn't recognise the place).
_BOUNDARY = {
    "on", "next", "this", "tomorrow", "today", "tonight", "in", "at", "by",
    "for", "with", "and", "please", "pls", "asap", "around", "departing",
    "leaving", "returning", "return", "via", "then", "but", "actually", "no",
    "wait", "sorry", "i", "we", "my", "the", "a", "an", "from", "to", "headed",
    "heading", "outta", "out", "flight", "flights", "ticket", "tickets",
    "fly", "go", "travel", "book", "get", "buy", "visit", "see", "be", "have",
    "make", "find", "check", "know", "leave", "come", "change", "it", "me", "us",
}

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2,
    "wed": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4,
    "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
_DATE_WORDS = set(_MONTHS) | set(_WEEKDAYS) | {"today", "tomorrow", "tonight", "weekend"}


def _tokens(text: str) -> list[tuple[str, int, int]]:
    return [(m.group(0), m.start(), m.end()) for m in _TOKEN_RE.finditer(text)]


def _cue_at(words_l: list[str], i: int, cues) -> Optional[int]:
    for cue in cues:
        n = len(cue)
        if tuple(words_l[i:i + n]) == cue:
            return n
    return None


def _free_capture(words: list[str], start: int, max_words: int = 3) -> Optional[tuple[int, str]]:
    """Fallback for places the gazetteer doesn't know: up to 3 words that
    look like a name, stopping at boundary/date words."""
    taken = []
    for w in words[start:start + max_words]:
        wl = w.lower()
        if wl in _BOUNDARY or wl in _DATE_WORDS or not w[:1].isalpha():
            break
        taken.append(w)
    if not taken:
        return None
    return len(taken), " ".join(taken)


def extract_route(text: str) -> tuple[Optional[str], Optional[str], dict]:
    """Returns (origin, destination, info). `info` reports per-slot whether
    the value was recognised as a real place ("known") or only captured
    from phrasing ("guessed"), which the NLU turns into confidence. When a
    slot is mentioned more than once ("to Paris -- no, to Tokyo") the last
    mention wins."""
    toks = _tokens(text)
    words = [t[0] for t in toks]
    words_l = [w.lower() for w in words]
    origin = dest = None
    info: dict[str, str] = {}
    i = 0
    found_dest_spans: list[tuple[int, int]] = []
    while i < len(words):
        for kind, cues in (("origin", ORIGIN_CUES), ("destination", DEST_CUES)):
            n = _cue_at(words_l, i, cues)
            if n is None:
                continue
            j = i + n
            match = match_place_at(words, j, after_cue=True)
            if match:
                k, _ = match
                value = text[toks[j][1]:toks[j + k - 1][2]]
                quality = "known"
            else:
                if tuple(words_l[i:i + n]) in _CUE_ONLY_STRONG:
                    continue
                cap = _free_capture(words, j)
                if not cap:
                    continue
                k, value = cap
                quality = "guessed"
                # a free capture must not overwrite a recognised place
                if kind == "origin" and info.get("origin") == "known":
                    continue
                if kind == "destination" and info.get("destination") == "known":
                    continue
            if kind == "origin":
                origin, info["origin"] = value, quality
            else:
                dest, info["destination"] = value, quality
                found_dest_spans.append((i, j))
            i = j + k - 1
            break
        i += 1

    # "Delhi to Paris" / "chicago -> miami": a known place right before a
    # destination cue is the origin when no explicit origin was given.
    if origin is None:
        for cue_start, _ in found_dest_spans:
            for back in range(1, 6):
                s = cue_start - back
                if s < 0:
                    break
                m = match_place_at(words, s, after_cue=False)
                if m and s + m[0] == cue_start:
                    origin = text[toks[s][1]:toks[cue_start - 1][2]]
                    info["origin"] = "known"
                    break
            if origin:
                break
    return origin, dest, info


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_WD_ALT = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))

_ISO_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
# "on the 5th", "on 5th of November", "on the 12th december" -- kept
# returning just the date part ("5th", "5th of November") as before.
_ON_ORDINAL_RE = re.compile(
    rf"\bon (?:the )?(\d{{1,2}}(?:st|nd|rd|th)?(?: of)?(?: (?:{_MONTH_ALT})\b)?)",
    re.IGNORECASE,
)
_DAY_MONTH_RE = re.compile(
    rf"\b(?:the )?(\d{{1,2}}(?:st|nd|rd|th)?(?: of)? (?:{_MONTH_ALT}))\b", re.IGNORECASE
)
_MONTH_DAY_RE = re.compile(rf"\b((?:{_MONTH_ALT}) \d{{1,2}}(?:st|nd|rd|th)?)\b", re.IGNORECASE)
_RELATIVE_RE = re.compile(
    rf"\b(day after tomorrow|tomorrow|today|tonight|this weekend|next weekend|next week|"
    rf"(?:next|this|coming|on) (?:{_WD_ALT})|in \d{{1,3}} days?|\d{{1,3}} days? from now)\b",
    re.IGNORECASE,
)
_BARE_WD_RE = re.compile(rf"\b({_WD_ALT})\b", re.IGNORECASE)
_THE_ORDINAL_RE = re.compile(r"\bthe (\d{1,2}(?:st|nd|rd|th))\b", re.IGNORECASE)


def extract_date_phrase(text: str) -> Optional[str]:
    """The travel date as phrased, or None. Most specific pattern wins."""
    for rx in (_ISO_RE, _ON_ORDINAL_RE, _DAY_MONTH_RE, _MONTH_DAY_RE, _RELATIVE_RE):
        m = rx.search(text)
        if m:
            phrase = m.group(1).strip()
            return phrase[3:] if phrase.lower().startswith("on ") else phrase
    m = _BARE_WD_RE.search(text)
    if m and m.group(1).lower() in {k for k in _WEEKDAYS if len(k) > 3}:
        return m.group(1)
    m = _THE_ORDINAL_RE.search(text)
    if m:
        return m.group(1)
    return None


def _next_weekday(today: _dt.date, wd: int, *, include_today: bool) -> _dt.date:
    delta = (wd - today.weekday()) % 7
    if delta == 0 and not include_today:
        delta = 7
    return today + _dt.timedelta(days=delta)


def _month_day(today: _dt.date, month: int, day: int) -> Optional[_dt.date]:
    for year in (today.year, today.year + 1):
        try:
            d = _dt.date(year, month, day)
        except ValueError:
            return None
        if d >= today:
            return d
    return None


def resolve_travel_date(phrase: Optional[str], today: Optional[_dt.date] = None) -> Optional[_dt.date]:
    """Concrete date for a phrase from extract_date_phrase (or an ISO
    string). Dates without a year roll forward to the next occurrence, so
    "the 5th" on the 20th means the 5th of next month."""
    if not phrase:
        return None
    today = today or _dt.date.today()
    p = re.sub(r"\s+", " ", str(phrase).strip().lower())
    p = re.sub(r"^(on|the) ", "", p)

    m = _ISO_RE.fullmatch(p)
    if m:
        try:
            return _dt.date.fromisoformat(p)
        except ValueError:
            return None
    if p in ("today", "tonight"):
        return today
    if p == "tomorrow":
        return today + _dt.timedelta(days=1)
    if p == "day after tomorrow":
        return today + _dt.timedelta(days=2)
    if p == "next week":
        return today + _dt.timedelta(days=7)
    if p in ("this weekend", "next weekend"):
        sat = _next_weekday(today, 5, include_today=True)
        return sat + (_dt.timedelta(days=7) if p == "next weekend" else _dt.timedelta())
    m = re.fullmatch(r"in (\d{1,3}) days?|(\d{1,3}) days? from now", p)
    if m:
        return today + _dt.timedelta(days=int(m.group(1) or m.group(2)))
    m = re.fullmatch(rf"(next|this|coming|on)? ?({_WD_ALT})", p)
    if m:
        return _next_weekday(today, _WEEKDAYS[m.group(2)], include_today=(m.group(1) == "this"))
    m = re.fullmatch(rf"(\d{{1,2}})(?:st|nd|rd|th)?(?: of)? ({_MONTH_ALT})", p)
    if m:
        return _month_day(today, _MONTHS[m.group(2)], int(m.group(1)))
    m = re.fullmatch(rf"({_MONTH_ALT}) (\d{{1,2}})(?:st|nd|rd|th)?", p)
    if m:
        return _month_day(today, _MONTHS[m.group(1)], int(m.group(2)))
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?(?: of)?", p)
    if m:
        day = int(m.group(1))
        y, mo = today.year, today.month
        for _ in range(3):
            try:
                d = _dt.date(y, mo, day)
                if d >= today:
                    return d
            except ValueError:
                pass
            mo += 1
            if mo == 13:
                y, mo = y + 1, 1
        return None
    return None


# ---------------------------------------------------------------------------
# Trip options: return date, passengers, cabin
# ---------------------------------------------------------------------------

_RETURN_RE = re.compile(
    r"\b(returning|return(?:ing)? on|return|coming back|come back|back on|round[- ]trip back)\b", re.IGNORECASE
)
_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
              "a couple of": 2, "couple of": 2}
_PAX_RE = re.compile(
    r"\b(\d{1,2}|one|two|three|four|five|six|seven|eight|nine)\s+(?:adults?|passengers?|people|persons|"
    r"travell?ers|pax|tickets|seats)\b|\bfamily of (\d{1,2}|three|four|five|six)\b",
    re.IGNORECASE,
)
_CABIN_RE = re.compile(
    r"\b(premium economy|premium|business(?: class)?|first class|first|economy(?: class)?|coach)\b", re.IGNORECASE
)


def extract_trip_options(text: str) -> dict:
    """{"date", "return_date", "passengers", "cabin"} -- only the keys
    actually mentioned. Splits on the return keyword so "on the 19th
    returning on the 25th" yields two different dates."""
    out: dict = {}
    m = _RETURN_RE.search(text)
    head, tail = (text[:m.start()], text[m.end():]) if m else (text, "")
    date = extract_date_phrase(head)
    if date:
        out["date"] = date
    if tail:
        ret = extract_date_phrase(tail) or extract_date_phrase(" on " + tail.strip())
        if ret:
            out["return_date"] = ret
    elif re.search(r"\bone[- ]way\b", text, re.IGNORECASE):
        out["return_date"] = ""  # explicit one-way clears an earlier return date
    pm = _PAX_RE.search(text)
    if pm:
        raw = (pm.group(1) or pm.group(2) or "1").lower()
        n = _NUM_WORDS.get(raw) or (int(raw) if raw.isdigit() else 1)
        if 1 <= n <= 9:
            out["passengers"] = n
    cm = _CABIN_RE.search(text)
    if cm:
        word = cm.group(1).lower()
        # "first" alone only counts as a cabin next to "class" -- "first flight
        # out" is not a first-class request
        if word != "first":
            out["cabin"] = ("premium_economy" if word.startswith("premium") else
                            "business" if word.startswith("business") else
                            "first" if word.startswith("first") else "economy")
    return out
