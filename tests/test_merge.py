"""Tests for the Phase 2 merge.

These build the whole database from scratch in memory: the three CSVs are
staged with `ingest_file`, then `merge` runs over that staging. Nothing
here reads `db/consultbae.db`, so the tests describe what the code does to
the data rather than what happens to be sitting on disk.

Where a test asserts a count, it also asserts how that count decomposes,
so a passing number cannot hide two errors cancelling out. `60 people` is
only meaningful if it is also `40 + 15 + 5`.

Line numbers in comments are physical lines in the CSV with the header as
line 1, matching `source_row_number` in staging.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from ingest import SOURCES, ingest_file
from merge import (
    MERGE_ISSUE_TYPES,
    choose_canonical_name,
    choose_primary_email,
    merge,
)
from models import (
    Base,
    DataIssue,
    Person,
    PersonReviewCandidate,
    PersonSkill,
    PersonSourceLink,
    RawSource1Naukri,
    RawSource2GigWorkers,
    RawSource3Cbnexus,
)

SOURCE1, SOURCE2, SOURCE3 = (spec.filename for spec in SOURCES)


@pytest.fixture(scope="module")
def session():
    """Stage all three files and merge them, in an in-memory database."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        for spec in SOURCES:
            ingest_file(db, spec)
        db.commit()
        merge(db)
        db.commit()
        yield db


def count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def people_named(session, name: str) -> list[Person]:
    return list(
        session.scalars(
            select(Person).where(func.lower(Person.full_name) == name.casefold())
        ).all()
    )


# ==========================================================================
# The headline counts
# ==========================================================================


def test_the_merge_produces_sixty_people(session):
    assert count(session, Person) == 60


def test_sixty_people_decompose_as_forty_plus_fifteen_plus_five(session):
    """60 is only correct if it is 40 source1 people + 15 + 5 orphans.

    Asserting the total alone would pass even if source1 under-deduplicated
    by one and an orphan were dropped by one.
    """
    seeded_from = {
        source: session.scalar(
            select(func.count())
            .select_from(PersonSourceLink)
            .where(
                PersonSourceLink.source_file == source,
                PersonSourceLink.match_method == "seed",
            )
        )
        for source in (SOURCE1, SOURCE2, SOURCE3)
    }
    assert seeded_from == {SOURCE1: 40, SOURCE2: 15, SOURCE3: 5}
    assert sum(seeded_from.values()) == count(session, Person) == 60


def test_every_staged_row_links_to_exactly_one_person(session):
    """102 links, and 102 is the number of rows that reached staging."""
    staged = (
        count(session, RawSource1Naukri)
        + count(session, RawSource2GigWorkers)
        + count(session, RawSource3Cbnexus)
    )
    assert staged == 42 + 30 + 30 == 102
    assert count(session, PersonSourceLink) == staged


def test_no_source_row_is_linked_twice(session):
    """The UNIQUE constraint should make double-counting impossible."""
    rows = session.execute(
        select(PersonSourceLink.source_file, PersonSourceLink.source_row_number)
    ).all()
    assert len(rows) == len(set(rows))


def test_there_are_five_review_candidates(session):
    assert count(session, PersonReviewCandidate) == 5


def test_review_candidates_all_start_unresolved(session):
    resolutions = set(
        session.scalars(select(PersonReviewCandidate.resolution)).all()
    )
    assert resolutions == {"unresolved"}


# ==========================================================================
# The name traps -- the whole reason names are never used as a key
# ==========================================================================


def test_three_distinct_arjun_mehtas_survive_the_merge(session):
    """Three rows, three people. Merging on name would collapse them to one.

    source1 line 20 (email + phone), source2 line 18 (email only, no phone),
    source3 line 28 (phone only, no email). Nothing links the three.
    """
    people = people_named(session, "Arjun Mehta")
    assert len(people) == 3
    assert len({p.id for p in people}) == 3

    # Each is identified by a different key, which is why they stay apart.
    assert {(p.primary_email, p.primary_phone) for p in people} == {
        ("arjun.mehta9@example.in", "9000000131"),
        ("arjun.mehta77@mailtest.example.org", None),
        (None, "9000000272"),
    }


def test_two_distinct_deepak_nairs_survive_the_merge(session):
    """source2 carries two different Deepak Nair addresses; only one bridges."""
    people = people_named(session, "Deepak Nair")
    assert len(people) == 2
    assert {(p.primary_email, p.primary_phone) for p in people} == {
        ("deepak.nair44@example.com", "9000000296"),
        ("deepak.nair57@example.in", None),
    }


def test_name_only_pairs_are_recorded_but_never_merged(session):
    """Each of the 5 pairs stays two people AND appears in the review table."""
    candidates = list(session.scalars(select(PersonReviewCandidate)).all())
    assert {c.name.casefold() for c in candidates} == {
        "arjun mehta",
        "divya chopra",
        "karan chopra",
        "manish bhatia",
        "vikram mehta",
    }

    for candidate in candidates:
        # The pair is source2 <-> source3, and neither side is in source1.
        assert candidate.source_a_file == SOURCE2
        assert candidate.source_b_file == SOURCE3
        # Both rows still exist as separate people.
        matches = people_named(session, candidate.name)
        assert len(matches) >= 2


def test_no_link_is_ever_made_on_a_name(session):
    """Only email and phone are keys. `seed` is the row that made the person."""
    methods = set(session.scalars(select(PersonSourceLink.match_method)).all())
    assert methods == {"seed", "exact_email", "exact_phone"}


def test_every_link_is_a_full_confidence_key_match(session):
    confidences = set(session.scalars(select(PersonSourceLink.match_confidence)).all())
    assert confidences == {1.0}


# ==========================================================================
# source1 deduplication
# ==========================================================================


def test_source1_collapses_from_42_rows_to_40_people(session):
    assert count(session, RawSource1Naukri) == 42
    seeded = session.scalar(
        select(func.count())
        .select_from(PersonSourceLink)
        .where(
            PersonSourceLink.source_file == SOURCE1,
            PersonSourceLink.match_method == "seed",
        )
    )
    assert seeded == 40


def test_r_verma_and_rohit_verma_become_one_person_named_rohit_verma(session):
    """Lines 25 and 31: identical except the name. The longer form wins."""
    assert people_named(session, "R. Verma") == []
    people = people_named(session, "Rohit Verma")
    assert len(people) == 1
    assert people[0].primary_phone == "9000000294"


def test_nikhil_chopra_pair_merges_on_the_unprefixed_email(session):
    """Lines 27 and 37: same phone, emails differ only by an `alt.` prefix."""
    people = people_named(session, "Nikhil Chopra")
    assert len(people) == 1
    assert people[0].primary_email == "nikhil.chopra70@example.com"
    assert people[0].primary_phone == "9000000103"


def test_the_nikhil_chopra_pair_is_why_phone_is_the_bridge_key(session):
    """Email-only matching misses this pair; the issue log must say so."""
    issues = list(
        session.scalars(
            select(DataIssue).where(
                DataIssue.issue_type == "duplicate_person_merged"
            )
        ).all()
    )
    assert len(issues) == 2

    chopra = [i for i in issues if "9000000103" in i.action_taken]
    assert len(chopra) == 1
    text = chopra[0].action_taken
    assert "email-only matching would have missed this pair" in text
    assert "Phone is what caught it" in text


def test_choose_canonical_name_prefers_the_longer_form():
    assert choose_canonical_name(["R. Verma", "Rohit Verma"]) == "Rohit Verma"
    # Ties break alphabetically, so the answer never depends on row order.
    assert choose_canonical_name(["Bravo", "Alpha"]) == "Alpha"


def test_choose_primary_email_prefers_the_unprefixed_address():
    assert (
        choose_primary_email(
            ["alt.nikhil.chopra70@example.com", "nikhil.chopra70@example.com"]
        )
        == "nikhil.chopra70@example.com"
    )


# ==========================================================================
# City precedence
# ==========================================================================


def test_six_city_conflicts_are_logged(session):
    conflicts = session.scalar(
        select(func.count())
        .select_from(DataIssue)
        .where(DataIssue.issue_type == "city_conflict_across_sources")
    )
    assert conflicts == 6


def test_every_city_conflict_is_in_the_delhi_cluster(session):
    """The six survivors are all Delhi / New Delhi / Delhi NCR disagreements.

    Everything else was a spelling or casing difference that normalisation
    resolved. These six are real disagreements between sources.
    """
    issues = session.scalars(
        select(DataIssue).where(
            DataIssue.issue_type == "city_conflict_across_sources"
        )
    ).all()
    for issue in issues:
        assert "Delhi" in issue.action_taken


def test_source1_wins_a_city_conflict(session):
    """Meera Bhatia: source1 says Delhi NCR, source2 New Delhi, source3 Delhi."""
    people = people_named(session, "Meera Bhatia")
    assert len(people) == 1
    assert people[0].city == "Delhi NCR"
    assert people[0].city_raw == "Delhi NCR"


def test_city_conflicts_quote_the_raw_value(session):
    issues = session.scalars(
        select(DataIssue).where(
            DataIssue.issue_type == "city_conflict_across_sources"
        )
    ).all()
    for issue in issues:
        assert issue.raw_value
        assert issue.column_name in {"location", "City"}


# ==========================================================================
# Field population
# ==========================================================================


def test_units_are_carried_with_their_values_never_converted(session):
    """CTC keeps its source unit; rate keeps its period. No conversion."""
    units = set(
        session.scalars(
            select(Person.ctc_source_unit).where(Person.ctc_source_unit.is_not(None))
        ).all()
    )
    assert units == {"absolute", "lpa"}

    periods = set(
        session.scalars(
            select(Person.rate_period).where(Person.rate_period.is_not(None))
        ).all()
    )
    assert periods == {"hourly", "monthly"}


def test_raw_values_survive_beside_the_parsed_ones(session):
    """Tanvi Gupta: absolute rupees. Amit Agarwal: the same 4.2 as his years."""
    tanvi = people_named(session, "Tanvi Gupta")[0]
    assert tanvi.ctc_raw == "417964"
    assert tanvi.ctc_annual_inr == 417964
    assert tanvi.ctc_source_unit == "absolute"

    amit = people_named(session, "Amit Agarwal")[0]
    assert amit.ctc_raw == "4.2"
    assert amit.ctc_annual_inr == 420000
    assert amit.ctc_source_unit == "lpa"
    # The raw CTC and the experience figure are the same string, which is
    # exactly why the raw value is kept.
    assert amit.experience_years == 1.8


def test_skills_are_merged_across_sources_without_duplication(session):
    """Varun Jain is in source1 and source2; the sets agree after casefolding."""
    varun = people_named(session, "Varun Jain")[0]
    skills = list(
        session.scalars(
            select(PersonSkill.skill).where(PersonSkill.person_id == varun.id)
        ).all()
    )
    assert len(skills) == len(set(skills))
    assert set(skills) == {
        "n8n",
        "Web Scraping",
        "FastAPI",
        "MySQL",
        "Pandas",
        "MongoDB",
    }


def test_everything_that_could_be_parsed_was_parsed(session):
    """No value in any of the three files defeated the normalisation rules."""
    unparsed = session.scalar(
        select(func.count())
        .select_from(DataIssue)
        .where(DataIssue.issue_type == "unparsed_value")
    )
    assert unparsed == 0


# ==========================================================================
# The issue log
# ==========================================================================


def test_the_merge_logs_thirteen_judgment_calls(session):
    issues = session.scalars(
        select(DataIssue).where(DataIssue.issue_type.in_(MERGE_ISSUE_TYPES))
    ).all()
    breakdown: dict[str, int] = {}
    for issue in issues:
        breakdown[issue.issue_type] = breakdown.get(issue.issue_type, 0) + 1

    assert breakdown == {
        "duplicate_person_merged": 2,
        "city_conflict_across_sources": 6,
        "name_only_match_not_merged": 5,
    }
    assert sum(breakdown.values()) == 13


def test_every_merge_issue_is_flagged_not_skipped_or_repaired(session):
    """Merging makes judgment calls; it never drops a row or rewrites one.

    Under the project's definitions that makes every one of them `flagged`,
    with the raw value surviving so the call can be reviewed.
    """
    severities = set(
        session.scalars(
            select(DataIssue.severity).where(
                DataIssue.issue_type.in_(MERGE_ISSUE_TYPES)
            )
        ).all()
    )
    assert severities == {"flagged"}


def test_ingestion_issues_are_left_alone_by_the_merge(session):
    """The 3 structural rows from Phase 1 are still there and still theirs."""
    ingestion = session.scalars(
        select(DataIssue).where(DataIssue.issue_type.not_in(MERGE_ISSUE_TYPES))
    ).all()
    assert len(ingestion) == 3
    assert {i.issue_type for i in ingestion} == {
        "blank_row",
        "repeated_header",
        "column_shift",
    }
