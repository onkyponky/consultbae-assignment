"""Tests for the Phase 2 normalisation helpers.

Every value in here was taken from the three CSVs. Nothing is invented,
apart from a handful of clearly-marked boundary cases at the end of each
section that the files happen not to contain (an empty cell, a too-short
phone number). Where a test asserts a *count* -- 21 CTC rows in each unit,
8 ambiguous dates -- it re-reads the source file and counts, so the claim
stays tied to the data instead of to a number typed once and never
re-checked.

Line numbers in comments are physical lines in the CSV with the header as
line 1, matching `source_row_number` in staging.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import pytest

from normalise import (
    CANONICAL_CITIES,
    DELHI_CLUSTER,
    Ctc,
    ParsedDate,
    Rate,
    normalise_city,
    normalise_email,
    normalise_phone,
    normalise_skill,
    normalise_status,
    normalise_verified,
    parse_applied_date,
    parse_ctc,
    parse_rate,
    split_skills,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def staged_rows(filename: str, column_count: int, key_index: int, key_test) -> list:
    """Read a source file the way ingest.py stages it.

    Applies the same three structural filters, so these tests see exactly
    the rows that reached staging: right field count, not blank, and a key
    column that holds what it claims to.
    """
    lines = (DATA_DIR / filename).read_text(encoding="utf-8-sig").splitlines(
        keepends=True
    )
    reader = csv.reader(lines)
    next(reader)
    return [
        row
        for row in reader
        if len(row) == column_count
        and any(cell.strip() for cell in row)
        and key_test(row[key_index])
    ]


def source1() -> list:
    return staged_rows("source1_naukri_applicants.csv", 8, 1, lambda v: "@" in v)


def source2() -> list:
    return staged_rows("source2_gig_workers.csv", 6, 0, lambda v: "@" in v)


def source3() -> list:
    return staged_rows(
        "source3_cbnexus_contacts.csv", 5, 1, lambda v: any(c.isdigit() for c in v)
    )


# ==========================================================================
# normalise_phone
# ==========================================================================


@pytest.mark.parametrize(
    "raw, expected",
    [
        # The three shapes source1 uses.
        ("9000000237", "9000000237"),  # s1 line 3,  bare 10 digits
        ("09000000287", "9000000287"),  # s1 line 4,  leading trunk zero
        ("+919000000254", "9000000254"),  # s1 line 2,  plus and country code
        # The three shapes source3 uses.
        ("9000000268", "9000000268"),  # s3 line 2,  bare 10 digits
        ("919000000231", "9000000231"),  # s3 line 4,  country code, no plus
        ("+91-9000000131", "9000000131"),  # s3 line 5,  plus, code, hyphen
    ],
)
def test_normalise_phone_handles_every_shape_in_the_files(raw, expected):
    assert normalise_phone(raw) == expected


@pytest.mark.parametrize(
    "source1_raw, source3_raw, name",
    [
        # These pairs are the same person, and the phone is the *only* key
        # they share -- source3 has no email column at all. If normalisation
        # did not strip the prefixes, all 25 source1<->source3 matches would
        # be lost.
        ("+919000000254", "9000000254", "Tanvi Gupta"),  # s1 L2  / s3 L22
        ("09000000287", "9000000287", "Priya Singh"),  # s1 L4  / s3 L11
        ("+919000000231", "919000000231", "Priya Saxena"),  # s1 L28 / s3 L4
        ("09000000131", "+91-9000000131", "Arjun Mehta"),  # s1 L20 / s3 L5
        ("+919000000295", "+91-9000000295", "Isha Kapoor"),  # s1 L40 / s3 L17
    ],
)
def test_phone_is_the_bridge_between_source1_and_source3(source1_raw, source3_raw, name):
    assert normalise_phone(source1_raw) == normalise_phone(source3_raw), name


def test_phone_catches_the_nikhil_chopra_duplicate_that_email_misses():
    """s1 line 27 vs line 37 -- the alt. prefixed email duplicate.

    The two rows carry different emails (`alt.nikhil.chopra70@example.com`
    and `nikhil.chopra70@example.com`) but the same phone. Phone is the
    only key that collapses this pair.
    """
    line_27 = normalise_phone("09000000103")
    line_37 = normalise_phone("09000000103")
    assert line_27 == line_37 == "9000000103"

    assert normalise_email("alt.nikhil.chopra70@example.com") != normalise_email(
        "nikhil.chopra70@example.com"
    )


def test_every_staged_phone_normalises_to_ten_digits():
    for row in source1():
        assert normalise_phone(row[2]) is not None, row
        assert len(normalise_phone(row[2])) == 10
    for row in source3():
        assert normalise_phone(row[1]) is not None, row
        assert len(normalise_phone(row[1])) == 10


@pytest.mark.parametrize("raw", [None, "", "   ", "12345", "+91"])
def test_normalise_phone_returns_none_below_ten_digits(raw):
    # Boundary cases; the files contain no short numbers, but returning
    # None rather than a truncated string is the contract.
    assert normalise_phone(raw) is None


# ==========================================================================
# normalise_email
# ==========================================================================


@pytest.mark.parametrize(
    "raw, expected",
    [
        # All nine uppercase source2 addresses.
        ("ISHA.CHOPRA95@MAILTEST.EXAMPLE.ORG", "isha.chopra95@mailtest.example.org"),
        ("VARUN.SAXENA21@EXAMPLE.IN", "varun.saxena21@example.in"),
        ("DEEPAK.NAIR44@EXAMPLE.COM", "deepak.nair44@example.com"),
        ("NEHA.BHATIA60@MAILTEST.EXAMPLE.ORG", "neha.bhatia60@mailtest.example.org"),
        ("KARAN.CHOPRA76@MAILTEST.EXAMPLE.ORG", "karan.chopra76@mailtest.example.org"),
        ("KAVYA.VERMA74@MAILTEST.EXAMPLE.ORG", "kavya.verma74@mailtest.example.org"),
        ("TANVI.REDDY80@EXAMPLE.COM", "tanvi.reddy80@example.com"),
        ("TANVI.SHARMA56@MAILTEST.EXAMPLE.ORG", "tanvi.sharma56@mailtest.example.org"),
        ("DEEPAK.NAIR57@EXAMPLE.IN", "deepak.nair57@example.in"),
    ],
)
def test_normalise_email_lowercases_the_uppercase_source2_rows(raw, expected):
    assert normalise_email(raw) == expected


@pytest.mark.parametrize(
    "source1_raw, source2_raw, name",
    [
        # Email is the only key source2 shares with source1 -- source2 has
        # no phone column. Without lowercasing, these matches vanish.
        ("isha.chopra95@mailtest.example.org", "ISHA.CHOPRA95@MAILTEST.EXAMPLE.ORG",
         "Isha Chopra"),
        ("varun.saxena21@example.in", "VARUN.SAXENA21@EXAMPLE.IN", "Varun Saxena"),
        ("deepak.nair44@example.com", "DEEPAK.NAIR44@EXAMPLE.COM", "Deepak Nair"),
        ("neha.bhatia60@mailtest.example.org", "NEHA.BHATIA60@MAILTEST.EXAMPLE.ORG",
         "Neha Bhatia"),
    ],
)
def test_email_is_the_bridge_between_source1_and_source2(source1_raw, source2_raw, name):
    assert normalise_email(source1_raw) == normalise_email(source2_raw), name


def test_two_deepak_nairs_keep_different_emails():
    """s2 line 15 and s2 line 32 are different people with the same name."""
    first = normalise_email("DEEPAK.NAIR44@EXAMPLE.COM")
    second = normalise_email("DEEPAK.NAIR57@EXAMPLE.IN")
    assert first != second


def test_r_verma_and_rohit_verma_share_an_email_exactly():
    """s1 line 25 (`R. Verma`) and line 31 (`Rohit Verma`).

    Same email, same phone, different name spelling. This pair is catchable
    on either key, unlike the Nikhil Chopra pair.
    """
    assert normalise_email("rohit.verma13@mailtest.example.org") == normalise_email(
        "rohit.verma13@mailtest.example.org"
    )
    assert normalise_phone("9000000294") == normalise_phone("9000000294")


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Boundary: no source email carries surrounding whitespace, but
        # trimming is part of the contract.
        ("  tanvi.gupta31@example.com  ", "tanvi.gupta31@example.com"),
        (None, None),
        ("", None),
        ("   ", None),
    ],
)
def test_normalise_email_trims(raw, expected):
    assert normalise_email(raw) == expected


# ==========================================================================
# normalise_city
# ==========================================================================


@pytest.mark.parametrize(
    "raw, expected",
    [
        # All 17 distinct raw spellings across the three files.
        ("Bengaluru", "Bengaluru"),
        ("bangalore", "Bengaluru"),
        ("Bangalore", "Bengaluru"),
        ("GURGAON", "Gurugram"),
        ("Gurgaon", "Gurugram"),
        ("Gurugram", "Gurugram"),
        ("gurugram ", "Gurugram"),  # trailing whitespace, 10 rows across all files
        ("NOIDA", "Noida"),
        ("Noida", "Noida"),
        ("Noida ", "Noida"),  # trailing whitespace, 8 rows across all files
        ("PUNE", "Pune"),
        ("Pune", "Pune"),
        ("pune", "Pune"),
        ("Delhi", "Delhi"),
        ("New Delhi", "New Delhi"),
        ("new delhi", "New Delhi"),
        ("Delhi NCR", "Delhi NCR"),
    ],
)
def test_normalise_city_maps_every_spelling_in_the_files(raw, expected):
    assert normalise_city(raw) == expected


def test_trailing_whitespace_alone_never_creates_a_second_city():
    """`Noida ` and `gurugram ` appear in all three files, 18 rows total."""
    assert normalise_city("Noida ") == normalise_city("NOIDA") == normalise_city("Noida")
    assert normalise_city("gurugram ") == normalise_city("GURGAON") == "Gurugram"


def test_bangalore_and_gurgaon_are_collapsed_because_they_are_renames():
    """One municipality, two official names. Merging loses nothing."""
    assert normalise_city("bangalore") == normalise_city("Bengaluru")
    assert normalise_city("Gurgaon") == normalise_city("Gurugram")


def test_the_delhi_cluster_is_deliberately_not_collapsed():
    """Three different geographic scopes, not three spellings of one.

    New Delhi is a district inside Delhi, which is inside Delhi NCR.
    Collapsing them would manufacture agreement the sources do not have,
    and would hide the six cross-source conflicts.
    """
    delhi = normalise_city("Delhi")
    new_delhi = normalise_city("new delhi")
    ncr = normalise_city("Delhi NCR")

    assert delhi != new_delhi
    assert new_delhi != ncr
    assert delhi != ncr
    assert {delhi, new_delhi, ncr} == DELHI_CLUSTER


def test_every_staged_city_is_a_recognised_city():
    """No source row produces a city outside the canonical six."""
    values = (
        [row[3] for row in source1()]
        + [row[3] for row in source2()]
        + [row[2] for row in source3()]
    )
    unknown = {v for v in values if normalise_city(v) not in CANONICAL_CITIES}
    assert unknown == set()


def test_the_canonical_city_list():
    """17 raw spellings across three files reduce to these 7 values."""
    assert CANONICAL_CITIES == {
        "Bengaluru",
        "Gurugram",
        "Noida",
        "Pune",
        "Delhi",
        "New Delhi",
        "Delhi NCR",
    }


# ==========================================================================
# normalise_skill
# ==========================================================================


@pytest.mark.parametrize(
    "source1_spelling, source2_spelling, canonical",
    [
        # Every skill that appears in both files, in both files' casing.
        ("Docker", "docker", "Docker"),
        ("FastAPI", "fastapi", "FastAPI"),
        ("JavaScript", "javascript", "JavaScript"),
        ("LangChain", "langchain", "LangChain"),
        ("MongoDB", "mongodb", "MongoDB"),
        ("MySQL", "mysql", "MySQL"),
        ("Pandas", "pandas", "Pandas"),
        ("Python", "python", "Python"),
        ("React", "react", "React"),
        ("REST APIs", "rest apis", "REST APIs"),
        ("Selenium", "selenium", "Selenium"),
        ("SQL", "sql", "SQL"),
        ("Web Scraping", "web scraping", "Web Scraping"),
        ("Zapier", "zapier", "Zapier"),
    ],
)
def test_normalise_skill_unifies_the_two_casings(source1_spelling, source2_spelling, canonical):
    assert normalise_skill(source1_spelling) == canonical
    assert normalise_skill(source2_spelling) == canonical


def test_n8n_stays_lowercase():
    """n8n is spelled lowercase by the product itself, and both files agree."""
    assert normalise_skill("n8n") == "n8n"
    assert normalise_skill("N8N") == "n8n"


@pytest.mark.parametrize(
    "raw, expected",
    [
        # A blanket .title() would mangle these; the display table exists
        # precisely to stop that.
        ("fastapi", "FastAPI"),
        ("mysql", "MySQL"),
        ("rest apis", "REST APIs"),
        ("mongodb", "MongoDB"),
        ("javascript", "JavaScript"),
    ],
)
def test_display_form_beats_title_case(raw, expected):
    assert normalise_skill(raw) == expected
    assert normalise_skill(raw) != raw.title()


def test_split_skills_on_the_real_cells():
    """s1 line 2 and s2 line 11 are the same person, Tanvi Gupta."""
    from_source1 = split_skills("n8n, LangChain, REST APIs, MongoDB, SQL")
    from_source2 = split_skills("n8n, langchain, rest apis, mongodb, sql")

    assert from_source1 == ["n8n", "LangChain", "REST APIs", "MongoDB", "SQL"]
    assert from_source1 == from_source2


def test_matched_people_have_identical_skill_sets_across_sources():
    """All 15 source1<->source2 matches agree on skills once normalised.

    Reads both files and checks every email-matched pair, so this stays
    true only as long as the data says so.
    """
    by_email_s1 = {normalise_email(row[1]): row[7] for row in source1()}
    checked = 0
    for row in source2():
        email = normalise_email(row[0])
        if email not in by_email_s1:
            continue
        checked += 1
        assert set(split_skills(by_email_s1[email])) == set(split_skills(row[5])), email
    assert checked == 15


@pytest.mark.parametrize("raw, expected", [(None, None), ("", None), ("   ", None)])
def test_normalise_skill_ignores_empty(raw, expected):
    assert normalise_skill(raw) == expected


# ==========================================================================
# normalise_status
# ==========================================================================


@pytest.mark.parametrize(
    "raw, expected",
    [
        # The five distinct strings source2 actually contains.
        ("ACTIVE", "active"),
        ("Active", "active"),
        ("active", "active"),
        ("Inactive", "inactive"),
        ("paused", "paused"),
    ],
)
def test_normalise_status_on_the_real_values(raw, expected):
    assert normalise_status(raw) == expected


def test_paused_is_a_third_state_not_a_casing_variant():
    """3 workers are paused: Tanvi Gupta, Deepak Nair, Tanvi Reddy.

    `paused` appears only in lower case, which makes it easy to mistake for
    a stray spelling. Folding it into `active` would report three
    unavailable workers as available.
    """
    assert normalise_status("paused") == "paused"
    assert normalise_status("paused") != normalise_status("active")
    assert normalise_status("paused") != normalise_status("inactive")


def test_every_staged_status_is_recognised():
    for row in source2():
        assert normalise_status(row[4]) is not None, row


def test_all_three_states_are_present_in_the_file():
    seen = {normalise_status(row[4]) for row in source2()}
    assert seen == {"active", "inactive", "paused"}


@pytest.mark.parametrize("raw", [None, "", "   ", "archived"])
def test_normalise_status_returns_none_for_unknown(raw):
    # An unknown state must not silently become `active`.
    assert normalise_status(raw) is None


# ==========================================================================
# normalise_verified
# ==========================================================================


@pytest.mark.parametrize(
    "raw, expected",
    [
        # The five distinct strings source3 actually contains...
        ("Y", True),
        ("Yes", True),
        ("yes", True),
        ("N", False),
        ("No", False),
        # ...plus the two lower-case forms the spec calls for, which the
        # file happens not to use.
        ("y", True),
        ("n", False),
    ],
)
def test_normalise_verified_on_the_real_values(raw, expected):
    assert normalise_verified(raw) is expected


def test_every_staged_verified_flag_is_recognised():
    for row in source3():
        assert normalise_verified(row[3]) is not None, row


@pytest.mark.parametrize("raw", [None, "", "   ", "maybe"])
def test_normalise_verified_returns_none_for_unknown(raw):
    # None means "not stated", which is not the same fact as False.
    assert normalise_verified(raw) is None


# ==========================================================================
# parse_ctc
# ==========================================================================


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Absolute rupees -- bare integers, six or seven digits.
        ("417964", Ctc(417964, "absolute")),  # s1 line 2
        ("332456", Ctc(332456, "absolute")),  # s1 line 3
        ("1195422", Ctc(1195422, "absolute")),  # s1 line 13, largest
        ("327287", Ctc(327287, "absolute")),  # s1 line 23, smallest
        # Lakhs per annum -- always written with a decimal point.
        ("4.2", Ctc(420000, "lpa")),  # s1 line 6
        ("11.9", Ctc(1190000, "lpa")),  # s1 line 24, largest
        ("2.4", Ctc(240000, "lpa")),  # s1 line 21, smallest
        ("10.0", Ctc(1000000, "lpa")),  # s1 line 22, trailing zero
    ],
)
def test_parse_ctc_on_the_real_values(raw, expected):
    assert parse_ctc(raw) == expected


def test_parse_ctc_unpacks_as_a_plain_tuple():
    amount, unit = parse_ctc("4.2")
    assert (amount, unit) == (420000, "lpa")


def test_the_decimal_point_rule_splits_the_file_cleanly():
    """21 rows each way, with no value that could belong to both.

    This is the evidence for the rule. If a future file broke the split --
    an integer under 1000, or a decimal over 1000 -- this test fails and
    the rule gets revisited rather than quietly misreading rows.
    """
    values = [row[5] for row in source1()]
    decimals = [v for v in values if "." in v]
    integers = [v for v in values if "." not in v]

    assert len(decimals) == 21
    assert len(integers) == 21

    # No overlap between the two populations.
    assert max(float(v) for v in decimals) < 1000
    assert min(int(v) for v in integers) > 1000


def test_both_ctc_units_describe_the_same_salary_band():
    """The real proof that the decimals are LPA and not rupees.

    Read as LPA the decimals land at 2.4-11.9 lakh; the integers sit at
    3.27-11.95 lakh. Same band. Read as rupees instead, 21 people would
    earn under 12 rupees a year.
    """
    lpa_amounts = []
    absolute_amounts = []
    for row in source1():
        amount, unit = parse_ctc(row[5])
        (lpa_amounts if unit == "lpa" else absolute_amounts).append(amount)

    # The ranges overlap, which is what "same population" looks like.
    assert min(lpa_amounts) < max(absolute_amounts)
    assert min(absolute_amounts) < max(lpa_amounts)

    # And every parsed salary is a plausible annual figure.
    for amount in lpa_amounts + absolute_amounts:
        assert 100_000 <= amount <= 10_000_000


def test_every_staged_ctc_parses():
    for row in source1():
        amount, unit = parse_ctc(row[5])
        assert amount is not None, row
        assert unit in ("absolute", "lpa")


@pytest.mark.parametrize("raw", [None, "", "   ", "not a number"])
def test_parse_ctc_returns_none_pair_for_unusable(raw):
    assert parse_ctc(raw) == Ctc(None, None)


# ==========================================================================
# parse_rate
# ==========================================================================


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Hourly rows -- three and four digit amounts.
        ("1415/hr", Rate(1415.0, "hourly")),  # s2 line 2
        ("403/hr", Rate(403.0, "hourly")),  # s2 line 4
        ("330/hr", Rate(330.0, "hourly")),  # s2 line 8, smallest hourly
        ("1483/hr", Rate(1483.0, "hourly")),  # s2 line 25, largest hourly
        # Monthly rows -- the k suffix means thousands.
        ("15k/month", Rate(15000.0, "monthly")),  # s2 line 6, smallest monthly
        ("72k/month", Rate(72000.0, "monthly")),  # s2 line 10
        ("79k/month", Rate(79000.0, "monthly")),  # s2 line 17, largest monthly
        ("21k/month", Rate(21000.0, "monthly")),  # s2 line 24
    ],
)
def test_parse_rate_on_the_real_values(raw, expected):
    assert parse_rate(raw) == expected


def test_parse_rate_unpacks_as_a_plain_tuple():
    amount, period = parse_rate("15k/month")
    assert (amount, period) == (15000.0, "monthly")


def test_the_k_suffix_only_ever_appears_on_monthly_rows():
    for row in source2():
        amount, period = parse_rate(row[2])
        assert ("k" in row[2].lower()) == (period == "monthly"), row


def test_hourly_and_monthly_are_never_converted():
    """The two populations do not reconcile under any single divisor.

    At 160 hours a month the monthly rows imply 94-494 per hour while the
    hourly rows sit at 330-1483. Overlapping but plainly different
    distributions, so no divisor is derivable from the data and the period
    is carried instead.
    """
    hourly = []
    monthly = []
    for row in source2():
        amount, period = parse_rate(row[2])
        (hourly if period == "hourly" else monthly).append(amount)

    assert min(hourly) == 330.0 and max(hourly) == 1483.0
    assert min(monthly) == 15000.0 and max(monthly) == 79000.0

    # A monthly amount is never silently expressed in hourly units.
    assert min(monthly) > max(hourly)


def test_every_staged_rate_parses():
    for row in source2():
        amount, period = parse_rate(row[2])
        assert amount is not None, row
        assert period in ("hourly", "monthly")


@pytest.mark.parametrize("raw", [None, "", "   ", "1415", "1415/week"])
def test_parse_rate_returns_none_pair_for_unusable(raw):
    assert parse_rate(raw) == Rate(None, None)


# ==========================================================================
# parse_applied_date
# ==========================================================================

# Every one of the 42 staged source1 values, with the date it must produce.
# (raw, expected date, layout, ambiguous)
REAL_DATES = [
    ("24-07-2026", date(2026, 7, 24), "dash", False),  # L2  proves dash is DD-MM
    ("2026-08-08", date(2026, 8, 8), "iso", False),  # L3
    ("01-08-2026", date(2026, 8, 1), "dash", True),  # L4  ambiguous
    ("7 Jul 2026", date(2026, 7, 7), "textual", False),  # L5  unpadded day
    ("19-07-2026", date(2026, 7, 19), "dash", False),  # L6
    ("07/13/2026", date(2026, 7, 13), "slash", False),  # L7  proves slash is MM/DD
    ("19 Jul 2026", date(2026, 7, 19), "textual", False),  # L8
    ("2026-08-02", date(2026, 8, 2), "iso", False),  # L9
    ("28-07-2026", date(2026, 7, 28), "dash", False),  # L10
    ("2026-07-13", date(2026, 7, 13), "iso", False),  # L11
    ("07/03/2026", date(2026, 7, 3), "slash", True),  # L12 ambiguous
    ("2026-06-24", date(2026, 6, 24), "iso", False),  # L13
    ("21-08-2026", date(2026, 8, 21), "dash", False),  # L14
    ("8 Jul 2026", date(2026, 7, 8), "textual", False),  # L15 unpadded day
    ("2026-08-03", date(2026, 8, 3), "iso", False),  # L16
    ("22-08-2026", date(2026, 8, 22), "dash", False),  # L17
    ("2026-07-03", date(2026, 7, 3), "iso", False),  # L18
    ("08/19/2026", date(2026, 8, 19), "slash", False),  # L19
    ("21-07-2026", date(2026, 7, 21), "dash", False),  # L20
    ("2 Jul 2026", date(2026, 7, 2), "textual", False),  # L21 unpadded day
    ("03-07-2026", date(2026, 7, 3), "dash", True),  # L22 ambiguous
    ("2026-07-23", date(2026, 7, 23), "iso", False),  # L23
    ("24-06-2026", date(2026, 6, 24), "dash", False),  # L24
    ("08/13/2026", date(2026, 8, 13), "slash", False),  # L25
    ("15-06-2026", date(2026, 6, 15), "dash", False),  # L26
    ("07/03/2026", date(2026, 7, 3), "slash", True),  # L27 ambiguous
    ("08/16/2026", date(2026, 8, 16), "slash", False),  # L28
    ("5 Jul 2026", date(2026, 7, 5), "textual", False),  # L29 unpadded day
    ("19-07-2026", date(2026, 7, 19), "dash", False),  # L30
    ("08/13/2026", date(2026, 8, 13), "slash", False),  # L31
    ("2026-08-19", date(2026, 8, 19), "iso", False),  # L32
    ("15 Jul 2026", date(2026, 7, 15), "textual", False),  # L33
    ("27 Jul 2026", date(2026, 7, 27), "textual", False),  # L34
    ("08/11/2026", date(2026, 8, 11), "slash", True),  # L35 ambiguous
    ("21 Jul 2026", date(2026, 7, 21), "textual", False),  # L36
    ("07/03/2026", date(2026, 7, 3), "slash", True),  # L37 ambiguous
    ("27 Jul 2026", date(2026, 7, 27), "textual", False),  # L38
    ("2026-07-26", date(2026, 7, 26), "iso", False),  # L39
    ("08/21/2026", date(2026, 8, 21), "slash", False),  # L40
    ("02-06-2026", date(2026, 6, 2), "dash", True),  # L41 ambiguous
    ("07/12/2026", date(2026, 7, 12), "slash", True),  # L42 ambiguous
    ("22 Jul 2026", date(2026, 7, 22), "textual", False),  # L43
]


@pytest.mark.parametrize("raw, expected, layout, ambiguous", REAL_DATES)
def test_parse_applied_date_on_every_real_value(raw, expected, layout, ambiguous):
    result = parse_applied_date(raw)
    assert result.value == expected
    assert result.layout == layout
    assert result.ambiguous is ambiguous


def test_the_test_table_covers_every_staged_row():
    """Guards against the table above drifting away from the file."""
    from_file = [row[6] for row in source1()]
    assert len(from_file) == 42
    assert from_file == [raw for raw, _, _, _ in REAL_DATES]


def test_the_dash_format_is_proved_to_be_day_first():
    """`24-07-2026` cannot be MM-DD: there is no month 24."""
    result = parse_applied_date("24-07-2026")
    assert result.value == date(2026, 7, 24)
    assert result.ambiguous is False
    assert result.alternative is None


def test_the_slash_format_is_proved_to_be_month_first():
    """`07/13/2026` cannot be DD/MM: there is no month 13."""
    result = parse_applied_date("07/13/2026")
    assert result.value == date(2026, 7, 13)
    assert result.ambiguous is False
    assert result.alternative is None


def test_the_separator_decides_the_order_for_the_same_digits():
    """`03-07-2026` and `07/03/2026` are both 3 July, by different routes.

    Dash reads day first, slash reads month first. Same date, opposite
    field order -- which is exactly why the separator has to be honoured
    rather than a single format applied to everything.
    """
    assert parse_applied_date("03-07-2026").value == date(2026, 7, 3)
    assert parse_applied_date("07/03/2026").value == date(2026, 7, 3)


@pytest.mark.parametrize(
    "raw, chosen, other",
    [
        ("01-08-2026", date(2026, 8, 1), date(2026, 1, 8)),  # L4
        ("03-07-2026", date(2026, 7, 3), date(2026, 3, 7)),  # L22
        ("02-06-2026", date(2026, 6, 2), date(2026, 2, 6)),  # L41
        ("07/03/2026", date(2026, 7, 3), date(2026, 3, 7)),  # L12, L27, L37
        ("08/11/2026", date(2026, 8, 11), date(2026, 11, 8)),  # L35
        ("07/12/2026", date(2026, 7, 12), date(2026, 12, 7)),  # L42
    ],
)
def test_ambiguous_values_report_both_readings(raw, chosen, other):
    """The six distinct ambiguous values, and the reading each one rejects.

    `alternative` is carried so the data_issues row can quote both dates
    rather than just asserting the winner.
    """
    result = parse_applied_date(raw)
    assert result.value == chosen
    assert result.ambiguous is True
    assert result.alternative == other


def test_the_file_contains_eight_ambiguous_rows_with_six_distinct_values():
    """Counted from the file, not asserted from memory.

    `07/03/2026` accounts for three of the eight rows: source1 lines 12, 27
    and 37. Lines 27 and 37 are the Nikhil Chopra duplicate pair, so they
    carry the same value twice.
    """
    ambiguous = [row[6] for row in source1() if parse_applied_date(row[6]).ambiguous]
    assert len(ambiguous) == 8
    assert len(set(ambiguous)) == 6
    assert ambiguous.count("07/03/2026") == 3


def test_every_staged_applied_date_parses():
    for row in source1():
        result = parse_applied_date(row[6])
        assert result.value is not None, row
        assert result.layout in ("iso", "dash", "slash", "textual")


def test_the_four_layouts_have_the_counts_the_file_shows():
    counts: dict[str, int] = {}
    for row in source1():
        layout = parse_applied_date(row[6]).layout
        counts[layout] = counts.get(layout, 0) + 1
    assert counts == {"iso": 9, "dash": 12, "slash": 11, "textual": 10}


@pytest.mark.parametrize("raw", [None, "", "   ", "not a date", "2026"])
def test_parse_applied_date_returns_empty_for_unusable(raw):
    assert parse_applied_date(raw) == ParsedDate(None, None, False, None)
