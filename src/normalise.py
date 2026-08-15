"""Phase 2 normalisation helpers.

Every function here is pure: same input, same output, no database, no
logging, no clock. That is deliberate. `merge.py` decides what to *record*
about a value; this module only decides what the value *is*. Keeping the
two apart means each rule can be tested on its own and explained on its
own.

Three rules in here are judgment calls rather than mechanical clean-ups,
so each one carries its evidence in the docstring:

  * `normalise_city`   -- which spellings are the same place, and which
                          look similar but are not.
  * `parse_ctc`        -- which unit a bare number is written in.
  * `parse_applied_date` -- which component is the day.

Functions that cannot make sense of a value return `None` rather than
guessing or raising. The caller still holds the raw string, so it can log
the failure with the original text attached.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import NamedTuple

# --------------------------------------------------------------------------
# Phone
# --------------------------------------------------------------------------


def normalise_phone(raw: str | None) -> str | None:
    """Reduce a phone number to its last 10 digits.

    The three files write the same subscriber number six different ways:
    `9000000254`, `09000000287`, `+919000000254` (source1) and
    `9000000268`, `919000000231`, `+91-9000000131` (source3). What varies
    is only the prefix -- a leading trunk `0`, a country code `91`, a `+`,
    a hyphen. The last 10 digits are the part that actually identifies the
    subscriber, and they are identical across all six shapes.

    Returns None if fewer than 10 digits are present, because then there is
    no subscriber number to speak of and a shorter string would be a
    different kind of value, not a worse-formatted phone number.
    """
    if raw is None:
        return None

    digits = re.sub(r"\D", "", raw)
    if len(digits) < 10:
        return None
    return digits[-10:]


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------


def normalise_email(raw: str | None) -> str | None:
    """Trim surrounding whitespace and lowercase.

    Nine source2 rows are fully uppercase, e.g.
    `ISHA.CHOPRA95@MAILTEST.EXAMPLE.ORG`. The local part of an address is
    formally case-sensitive, but no real mail system treats it that way,
    and here the uppercase rows are plainly the same people as their
    lowercase source1 counterparts. Lowercasing is what makes source1 join
    source2 at all -- without it, 9 of the 15 email matches disappear.

    `.casefold()` rather than `.lower()`: for ASCII they are identical, and
    casefold is the method meant for case-insensitive comparison.
    """
    if raw is None:
        return None

    cleaned = raw.strip().casefold()
    return cleaned or None


# --------------------------------------------------------------------------
# City
# --------------------------------------------------------------------------

# Only two genuine renames are collapsed here. Bangalore and Bengaluru are
# the same municipality under its former and current official names, as are
# Gurgaon and Gurugram. Merging those loses nothing.
#
# Delhi, New Delhi and Delhi NCR are deliberately NOT merged. They are three
# different geographic scopes, not three spellings of one:
#     New Delhi  is a district inside
#     Delhi      (the NCT), which is inside
#     Delhi NCR  (the wider capital region, including Noida and Gurugram)
# Collapsing them would manufacture agreement that the sources do not
# actually have. Keeping them apart is what leaves six cross-source
# conflicts standing, which is the honest outcome -- those six are a
# finding for the report, not noise to be flattened.
CITY_ALIASES: dict[str, str] = {
    "bangalore": "Bengaluru",
    "bengaluru": "Bengaluru",
    "gurgaon": "Gurugram",
    "gurugram": "Gurugram",
    "noida": "Noida",
    "pune": "Pune",
    "delhi": "Delhi",
    "new delhi": "New Delhi",
    "delhi ncr": "Delhi NCR",
}

#: The values `normalise_city` is allowed to produce for a known city.
#: `merge.py` tests membership to spot a city it has never seen.
CANONICAL_CITIES: frozenset[str] = frozenset(CITY_ALIASES.values())

#: The three Delhi-cluster values that are near neighbours but not equal.
#: Named so the conflict report can say *why* a disagreement is interesting.
DELHI_CLUSTER: frozenset[str] = frozenset({"Delhi", "New Delhi", "Delhi NCR"})


def normalise_city(raw: str | None) -> str | None:
    """Trim, collapse case and internal spacing, then map through aliases.

    The 17 distinct raw spellings across the three files reduce to 7
    canonical cities. `Noida `, `NOIDA` and `Noida` differ only by trailing
    space and case; `gurugram ` and `GURGAON` are the same place under two
    names.

    An unrecognised city is returned title-cased rather than dropped, so a
    new city still reaches `person`. `merge.py` compares the result against
    `CANONICAL_CITIES` to notice it and log it.
    """
    if raw is None:
        return None

    # Collapse any run of whitespace to one space. This handles the
    # trailing space in 'Noida ' and would also handle 'New  Delhi'.
    cleaned = " ".join(raw.split())
    if not cleaned:
        return None

    key = cleaned.casefold()
    if key in CITY_ALIASES:
        return CITY_ALIASES[key]

    # Unknown city: keep it, in a predictable shape.
    return cleaned.title()


# --------------------------------------------------------------------------
# Skill
# --------------------------------------------------------------------------

# source1 writes skills in title case (`Web Scraping`), source2 in lower
# case (`web scraping`). They are the same 15 skills. The display form
# chosen here is source1's, because it preserves the capitalisation these
# technologies actually use -- `FastAPI`, `MySQL`, `REST APIs` -- which a
# blanket `.title()` would mangle into `Fastapi`, `Mysql`, `Rest Apis`.
# `n8n` is lowercase on purpose: that is how the product spells its name.
SKILL_DISPLAY: dict[str, str] = {
    "docker": "Docker",
    "fastapi": "FastAPI",
    "javascript": "JavaScript",
    "langchain": "LangChain",
    "mongodb": "MongoDB",
    "mysql": "MySQL",
    "n8n": "n8n",
    "pandas": "Pandas",
    "python": "Python",
    "react": "React",
    "rest apis": "REST APIs",
    "selenium": "Selenium",
    "sql": "SQL",
    "web scraping": "Web Scraping",
    "zapier": "Zapier",
}

#: Skills `normalise_skill` recognises. `merge.py` tests membership of the
#: casefolded key to spot a skill the display table does not cover.
KNOWN_SKILL_KEYS: frozenset[str] = frozenset(SKILL_DISPLAY)


def normalise_skill(raw: str | None) -> str | None:
    """Trim, collapse case, return the canonical display spelling.

    Matching is done on the casefolded form so `Web Scraping` and
    `web scraping` are one skill. An unrecognised skill is returned trimmed
    but otherwise untouched -- inventing a capitalisation for a technology
    nobody has seen yet would be a guess, and the source's own spelling is
    the better default.
    """
    if raw is None:
        return None

    cleaned = " ".join(raw.split())
    if not cleaned:
        return None

    return SKILL_DISPLAY.get(cleaned.casefold(), cleaned)


def split_skills(raw: str | None) -> list[str]:
    """Split a comma-joined skills cell into canonical skills, in order.

    Both files store skills as one comma-separated string. Duplicates
    within a cell are dropped; order is kept so the first-listed skill
    stays first, which makes the result easy to eyeball against the CSV.
    """
    if raw is None:
        return []

    skills: list[str] = []
    for token in raw.split(","):
        skill = normalise_skill(token)
        if skill is not None and skill not in skills:
            skills.append(skill)
    return skills


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------

# source2 writes five distinct strings: ACTIVE, Active, active, Inactive,
# paused. Four of them are casing variants of two states. `paused` is not:
# it is a third state in its own right, held by 3 workers (Tanvi Gupta,
# Deepak Nair, Tanvi Reddy). It happens to appear only in lower case, which
# makes it easy to mistake for a stray spelling of `active` -- it is not,
# and folding it into `active` would report three paused workers as
# available.
VALID_STATUSES: frozenset[str] = frozenset({"active", "inactive", "paused"})


def normalise_status(raw: str | None) -> str | None:
    """Collapse case to one of active / inactive / paused.

    Returns None for anything else, leaving the caller to log the raw
    string rather than have an unknown state silently become `active`.
    """
    if raw is None:
        return None

    cleaned = " ".join(raw.split()).casefold()
    if cleaned in VALID_STATUSES:
        return cleaned
    return None


# --------------------------------------------------------------------------
# Verified
# --------------------------------------------------------------------------

_TRUE_TOKENS = frozenset({"y", "yes"})
_FALSE_TOKENS = frozenset({"n", "no"})


def normalise_verified(raw: str | None) -> bool | None:
    """Map Y / y / yes / Yes to True and N / n / No / no to False.

    source3 uses five spellings across 30 rows: Y, Yes, yes, N, No. All are
    plain yes/no answers once case is collapsed.

    Returns None for an unrecognised value. Note that None means "not
    stated", which is a different fact from False ("stated as not
    verified"), so the two must not be conflated by the caller.
    """
    if raw is None:
        return None

    cleaned = " ".join(raw.split()).casefold()
    if cleaned in _TRUE_TOKENS:
        return True
    if cleaned in _FALSE_TOKENS:
        return False
    return None


# --------------------------------------------------------------------------
# CTC
# --------------------------------------------------------------------------

#: One lakh, the multiplier that turns an LPA figure into rupees.
RUPEES_PER_LAKH = 100_000


class Ctc(NamedTuple):
    """(amount_in_rupees, unit) -- unpacks as a plain 2-tuple."""

    amount_inr: int | None
    unit: str | None  # 'absolute' | 'lpa'


def parse_ctc(raw: str | None) -> Ctc:
    """Read source1's `Current CTC`, which mixes two units in one column.

    The rule is: **a decimal point means lakhs per annum, a bare integer
    means absolute rupees.** That is a structural test, not a threshold
    guess. The evidence, counted over all 42 staged source1 rows:

        values containing '.'  21 rows   range   2.4 ..      11.9
        values with no '.'     21 rows   range 327287 .. 1195422

    Three things make this conclusive rather than convenient:

    1. The split is exactly clean. No integer is below 1000 and no decimal
       is above 1000, so the two rules never compete for the same value.
       There is nothing in the five-orders-of-magnitude gap between 11.9
       and 327287 to argue about.

    2. The two populations describe the same salaries once the unit is
       applied. Reading the decimals as LPA puts them at 240000..1190000;
       the integers sit at 327287..1195422. Those are the same salary band.
       Reading the decimals as rupees instead would mean 21 people earn
       under 12 rupees a year, which is not a salary.

    3. `4.2` in the CTC column and `4.2` in the experience column are the
       same string, which is exactly why the raw value is kept alongside
       the parsed one on `Person`.

    The limit worth naming out loud: a future file writing `417964.0` would
    be misread as LPA by this rule. That is why `merge.py` range-checks the
    parsed amount and logs anything implausible, instead of this function
    silently widening its own rule.
    """
    if raw is None:
        return Ctc(None, None)

    cleaned = raw.strip()
    if not cleaned:
        return Ctc(None, None)

    if "." in cleaned:
        try:
            lakhs = float(cleaned)
        except ValueError:
            return Ctc(None, None)
        return Ctc(round(lakhs * RUPEES_PER_LAKH), "lpa")

    try:
        rupees = int(cleaned)
    except ValueError:
        return Ctc(None, None)
    return Ctc(rupees, "absolute")


# --------------------------------------------------------------------------
# Rate
# --------------------------------------------------------------------------

# `1415/hr`, `403/hr`, `15k/month`, `72k/month`. A `k` suffix means
# thousands and only ever appears on the monthly rows.
_RATE_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)\s*(k?)\s*/\s*(hr|month)$", re.IGNORECASE)

_PERIOD_NAMES = {"hr": "hourly", "month": "monthly"}


class Rate(NamedTuple):
    """(amount, period) -- unpacks as a plain 2-tuple."""

    amount: float | None
    period: str | None  # 'hourly' | 'monthly'


def parse_rate(raw: str | None) -> Rate:
    """Read source2's `rate` into an amount and the period it applies to.

    **No conversion between hourly and monthly happens here, or anywhere.**
    Converting needs an hours-per-month figure the data does not contain,
    and the obvious guess does not hold up: at 160 hours a month the
    monthly rows land at 94..494 per hour while the hourly rows sit at
    330..1483 per hour. Those two populations do not reconcile, so any
    single divisor would invent a number rather than derive one.

    Both quantities are therefore carried separately, and a comparison
    across periods is left to whoever can supply the missing assumption.
    """
    if raw is None:
        return Rate(None, None)

    cleaned = raw.strip()
    match = _RATE_PATTERN.match(cleaned)
    if match is None:
        return Rate(None, None)

    amount = float(match.group(1))
    if match.group(2).lower() == "k":
        amount *= 1000

    return Rate(amount, _PERIOD_NAMES[match.group(3).lower()])


# --------------------------------------------------------------------------
# Applied date
# --------------------------------------------------------------------------


class ParsedDate(NamedTuple):
    """A parsed date plus how it was read and whether the reading was safe.

    `ambiguous` is True when the *other* day/month order would also have
    produced a valid, different date. `alternative` is that other date, so
    a log line can quote both readings instead of just asserting one.
    """

    value: date | None
    layout: str | None  # 'iso' | 'dash' | 'slash' | 'textual'
    ambiguous: bool
    alternative: date | None


# (layout name, format used, format for the opposite day/month order)
#
# The separator is what decides the order, and each is proved by a value in
# the file that only reads one way:
#     dash  is DD-MM  -- proved by `24-07-2026`, since there is no month 24
#     slash is MM/DD  -- proved by `07/13/2026`, since there is no month 13
# The other two layouts cannot be ambiguous at all: ISO puts the 4-digit
# year first, and the textual form spells the month out.
#
# Order matters only for correctness of the *first* match, and the layouts
# are mutually exclusive by separator and shape, so no format can steal
# another's values. The tests assert this over all 42 real values.
_DATE_LAYOUTS: list[tuple[str, str, str | None]] = [
    ("iso", "%Y-%m-%d", None),
    ("dash", "%d-%m-%Y", "%m-%d-%Y"),
    ("slash", "%m/%d/%Y", "%d/%m/%Y"),
    ("textual", "%d %b %Y", None),
]


def _try(value: str, fmt: str) -> date | None:
    """Parse with one format, returning None instead of raising."""
    try:
        return datetime.strptime(value, fmt).date()
    except ValueError:
        return None


def parse_applied_date(raw: str | None) -> ParsedDate:
    """Read source1's `Applied Date`, which arrives in four formats.

    The 42 staged rows break down as 9 ISO (`2026-08-08`), 12 dash
    (`24-07-2026`), 11 slash (`07/13/2026`) and 10 textual (`7 Jul 2026`).

    Ambiguity is *derived*, not hardcoded to "both components are 12 or
    less": a value is ambiguous when swapping day and month also yields a
    valid date that differs from the first reading. That comes out at 8
    rows carrying 6 distinct values -- `01-08-2026`, `03-07-2026`,
    `02-06-2026`, `07/03/2026` (3 rows), `08/11/2026`, `07/12/2026`.

    Those 8 are resolved by separator, not by content, which is an
    inference about how each upstream system formats dates rather than
    something the value itself proves. So `ambiguous` is reported back and
    the raw string is kept on `Person.applied_date_raw`, letting the
    decision be reviewed instead of buried.
    """
    if raw is None:
        return ParsedDate(None, None, False, None)

    cleaned = " ".join(raw.split())
    if not cleaned:
        return ParsedDate(None, None, False, None)

    for layout, fmt, swapped_fmt in _DATE_LAYOUTS:
        parsed = _try(cleaned, fmt)
        if parsed is None:
            continue

        # Would reading day and month the other way round also work, and
        # give a different answer? If so, only the separator convention is
        # holding this value in place.
        alternative = _try(cleaned, swapped_fmt) if swapped_fmt else None
        ambiguous = alternative is not None and alternative != parsed

        return ParsedDate(parsed, layout, ambiguous, alternative if ambiguous else None)

    return ParsedDate(None, None, False, None)
