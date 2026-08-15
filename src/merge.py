"""Phase 2 merge: turn the three staging tables into canonical person records.

The order of the steps is load-bearing, not stylistic:

  1. Deduplicate source1 against itself FIRST. It is the bridge file, so if
     one human is still two rows when the other files are attached, both
     copies collect links and the error is inherited by everything
     downstream.
  2. Seed `person` from the deduplicated source1 groups.
  3. Attach source2 on email, source3 on phone.
  4. Rows that matched nothing become their own person.
  5. Record name-only look-alikes for review. Never merge on a name.

Matching only ever uses a key that identifies a person: a normalised email
or a normalised phone. Names are evidence, not keys -- this data holds
three different Arjun Mehtas and two different Deepak Nairs, and a name
match would silently fuse them.

Every decision that required judgment writes a `flagged` row into
`data_issues` with the raw value attached, so the call can be reviewed
rather than taken on trust.

Re-running is safe: the canonical tables and this phase's issue rows are
cleared first, so the result is a function of staging alone.

    python src/merge.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session

from ingest import DB_PATH, SOURCES
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
from normalise import (
    normalise_city,
    normalise_email,
    normalise_phone,
    normalise_status,
    normalise_verified,
    parse_applied_date,
    parse_ctc,
    parse_rate,
    split_skills,
)

SOURCE1, SOURCE2, SOURCE3 = (spec.filename for spec in SOURCES)

#: Issue types this module owns. Cleared on re-run so ingestion's rows,
#: which describe a different phase, are left untouched.
MERGE_ISSUE_TYPES = (
    "duplicate_person_merged",
    "city_conflict_across_sources",
    "name_only_match_not_merged",
    "unparsed_value",
)


# --------------------------------------------------------------------------
# Small value helpers
# --------------------------------------------------------------------------


def choose_canonical_name(names: list[str]) -> str:
    """Pick the fuller of several names recorded for one person.

    The rule: when two rows for one person disagree on the name, prefer the
    longer form. Abbreviating loses information and expanding invents none,
    so the longer string is the one that can be trusted to contain whatever
    the shorter one had. `R. Verma` and `Rohit Verma` are one person, and
    `Rohit Verma` is the safe canonical form.

    Ties break alphabetically so the answer never depends on row order.
    """
    return sorted(names, key=lambda name: (-len(name), name))[0]


def choose_primary_email(emails: list[str]) -> str:
    """Pick the base address when one is a prefixed variant of another.

    source1 line 27 carries `alt.nikhil.chopra70@example.com` and line 37
    carries `nikhil.chopra70@example.com` for the same phone. The `alt.` is
    a prefix marking an alternate address, not a different person, so the
    shorter unprefixed form is the real one.
    """
    return sorted(set(emails), key=lambda email: (len(email), email))[0]


def _parse_float(raw: str | None) -> float | None:
    """Read a plain decimal. No units, no ambiguity, so no judgment here."""
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw.strip())
    except ValueError:
        return None


def _parse_int(raw: str | None) -> int | None:
    """Read a plain whole number."""
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Issue logging
# --------------------------------------------------------------------------


@dataclass
class MergeResult:
    """What the merge produced, for the summary and the tests."""

    people: int = 0
    links: int = 0
    review_candidates: int = 0
    issues: list[DataIssue] = field(default_factory=list)


def _log(
    result: MergeResult,
    source_file: str,
    row_number: int | None,
    issue_type: str,
    column_name: str | None,
    raw_value: str | None,
    action_taken: str,
) -> None:
    """Record a judgment call. Everything this module logs is `flagged`."""
    result.issues.append(
        DataIssue(
            source_file=source_file,
            row_number=row_number,
            issue_type=issue_type,
            column_name=column_name,
            raw_value=raw_value,
            action_taken=action_taken,
            severity="flagged",
        )
    )


def _log_unparsed(
    result: MergeResult,
    source_file: str,
    row_number: int,
    column_name: str,
    raw: str | None,
    expected: str,
) -> None:
    """Log a value that was present but could not be read.

    A blank cell is absence, not a problem, so it is not logged.
    """
    if raw is None or not raw.strip():
        return
    _log(
        result,
        source_file,
        row_number,
        "unparsed_value",
        column_name,
        raw,
        f"Could not read as {expected}. Left null on person; the raw text "
        "is preserved here so the value is not lost.",
    )


# --------------------------------------------------------------------------
# Step 1 -- deduplicate source1 against itself
# --------------------------------------------------------------------------


def group_source1_by_phone(rows: list[RawSource1Naukri]) -> dict[str, list]:
    """Group source1 rows by normalised phone, preserving file order.

    Phone rather than email on purpose. Both of source1's duplicate pairs
    share a phone, but only one of them shares an email -- the Nikhil Chopra
    pair differs by an `alt.` prefix. Grouping on email would leave that
    pair as two people.
    """
    groups: dict[str, list] = {}
    for row in rows:
        key = normalise_phone(row.phone)
        if key is None:
            # No usable phone: the row cannot be grouped, so it stands
            # alone under a key that cannot collide with a real number.
            key = f"row-{row.source_row_number}"
        groups.setdefault(key, []).append(row)
    return groups


def _log_merged_duplicate(result: MergeResult, rows: list, person: Person) -> None:
    """Explain a source1 group that held more than one row for one person."""
    lines = [row.source_row_number for row in rows]
    names = {row.full_name for row in rows}
    emails = {normalise_email(row.email) for row in rows}

    detail = (
        f"Lines {lines} share phone {person.primary_phone} and are one "
        f"person. Kept name {person.full_name!r} and email "
        f"{person.primary_email!r}."
    )
    if len(names) > 1:
        detail += (
            f" The rows disagree on the name ({sorted(names)}); the longer "
            "form was kept because abbreviation loses information and "
            "expansion invents none."
        )
    if len(emails) > 1:
        # This is the Nikhil Chopra pair. Worth stating plainly: it is the
        # concrete evidence for why phone is the bridge key.
        detail += (
            f" The rows also disagree on the email ({sorted(e for e in emails if e)}), "
            "so email-only matching would have missed this pair entirely and "
            "recorded two people. Phone is what caught it, which is why phone "
            "and not email is the key used to deduplicate source1."
        )

    _log(
        result,
        SOURCE1,
        lines[0],
        "duplicate_person_merged",
        None,
        " | ".join(f"line {row.source_row_number}: {row.full_name}" for row in rows),
        detail,
    )


def person_from_source1(rows: list, result: MergeResult) -> tuple[Person, list[str]]:
    """Build one canonical person from one deduplicated source1 group."""
    first = rows[0]

    names = [row.full_name for row in rows if row.full_name]
    emails = [e for e in (normalise_email(row.email) for row in rows) if e]

    ctc = parse_ctc(first.current_ctc)
    applied = parse_applied_date(first.applied_date)

    person = Person(
        full_name=choose_canonical_name(names) if names else None,
        primary_email=choose_primary_email(emails) if emails else None,
        primary_phone=normalise_phone(first.phone),
        city=normalise_city(first.city),
        city_raw=first.city,
        experience_years=_parse_float(first.experience_years),
        ctc_raw=first.current_ctc,
        ctc_annual_inr=ctc.amount_inr,
        ctc_source_unit=ctc.unit,
        applied_date=applied.value,
        applied_date_raw=first.applied_date,
    )

    if ctc.amount_inr is None:
        _log_unparsed(
            result, SOURCE1, first.source_row_number, "Current CTC",
            first.current_ctc, "a CTC in rupees or lakhs per annum",
        )
    if applied.value is None:
        _log_unparsed(
            result, SOURCE1, first.source_row_number, "Applied Date",
            first.applied_date, "a date",
        )

    if len(rows) > 1:
        _log_merged_duplicate(result, rows, person)

    skills = split_skills(first.skills)
    return person, skills


# --------------------------------------------------------------------------
# Steps 3 and 4 -- attach the other two sources
# --------------------------------------------------------------------------


def _check_city(
    person: Person,
    raw_city: str | None,
    source_file: str,
    row_number: int,
    column_name: str,
    result: MergeResult,
) -> None:
    """Compare another source's city against the one source1 already gave.

    source1 wins: it is the bridge file and the most complete record, and
    letting two thinner records outvote it would be a worse rule. The
    disagreement is logged with both values so the six Delhi-cluster
    conflicts stay visible instead of being flattened.
    """
    incoming = normalise_city(raw_city)
    if incoming is None or person.city is None or incoming == person.city:
        return

    _log(
        result,
        source_file,
        row_number,
        "city_conflict_across_sources",
        column_name,
        raw_city,
        f"{source_file} says {incoming!r}, {SOURCE1} says {person.city!r} "
        f"(raw {person.city_raw!r}). Kept the {SOURCE1} value because it is "
        "the bridge file and the most complete record. Both readings are "
        "recorded here and person.city_raw keeps the original text.",
    )


def apply_source2(person: Person, row: RawSource2GigWorkers, result: MergeResult) -> list[str]:
    """Fold a source2 row into an existing person. Returns its skills."""
    rate = parse_rate(row.rate)
    person.rate_raw = row.rate
    person.rate_amount = rate.amount
    person.rate_period = rate.period
    person.status = normalise_status(row.status)

    if rate.amount is None:
        _log_unparsed(result, SOURCE2, row.source_row_number, "rate", row.rate,
                      "an amount with a period, like 1415/hr or 15k/month")
    if person.status is None:
        _log_unparsed(result, SOURCE2, row.source_row_number, "status", row.status,
                      "one of active, inactive or paused")

    _check_city(person, row.location, SOURCE2, row.source_row_number, "location", result)
    return split_skills(row.skill_tags)


def apply_source3(person: Person, row: RawSource3Cbnexus, result: MergeResult) -> None:
    """Fold a source3 row into an existing person."""
    person.verified = normalise_verified(row.verified)
    person.projects_completed = _parse_int(row.projects_completed)

    if person.verified is None:
        _log_unparsed(result, SOURCE3, row.source_row_number, "Verified",
                      row.verified, "a yes or no answer")

    _check_city(person, row.city, SOURCE3, row.source_row_number, "City", result)


# --------------------------------------------------------------------------
# The merge itself
# --------------------------------------------------------------------------


def merge(session: Session) -> MergeResult:
    """Build `person` and its satellites from the staging tables."""
    result = MergeResult()

    s1 = session.scalars(
        select(RawSource1Naukri).order_by(RawSource1Naukri.source_row_number)
    ).all()
    s2 = session.scalars(
        select(RawSource2GigWorkers).order_by(RawSource2GigWorkers.source_row_number)
    ).all()
    s3 = session.scalars(
        select(RawSource3Cbnexus).order_by(RawSource3Cbnexus.source_row_number)
    ).all()

    # Skills are collected per person and written once at the end, because a
    # person can pick up skills from both source1 and source2.
    skills_by_person: dict[int, list[str]] = {}

    def remember_skills(person_id: int, skills: list[str]) -> None:
        bucket = skills_by_person.setdefault(person_id, [])
        for skill in skills:
            if skill not in bucket:
                bucket.append(skill)

    # ---- steps 1 and 2: dedupe source1, seed person ----
    by_email: dict[str, Person] = {}
    by_phone: dict[str, Person] = {}

    for rows in group_source1_by_phone(s1).values():
        person, skills = person_from_source1(rows, result)
        session.add(person)
        session.flush()  # assigns person.id, needed by the link rows

        remember_skills(person.id, skills)

        # The lowest-numbered row seeded the person; any others in the group
        # joined it because their phone matched.
        for index, row in enumerate(rows):
            session.add(
                PersonSourceLink(
                    person_id=person.id,
                    source_file=SOURCE1,
                    source_row_number=row.source_row_number,
                    match_method="seed" if index == 0 else "exact_phone",
                    match_confidence=1.0,
                )
            )
            result.links += 1

        if person.primary_email:
            by_email[person.primary_email] = person
        # Index every email in the group, not just the canonical one, so the
        # `alt.` address still resolves if source2 uses it.
        for row in rows:
            email = normalise_email(row.email)
            if email:
                by_email.setdefault(email, person)
        if person.primary_phone:
            by_phone[person.primary_phone] = person

    # ---- step 3: source2 by email ----
    s2_orphans: list[RawSource2GigWorkers] = []
    for row in s2:
        email = normalise_email(row.email_id)
        person = by_email.get(email) if email else None
        if person is None:
            s2_orphans.append(row)
            continue

        remember_skills(person.id, apply_source2(person, row, result))
        session.add(
            PersonSourceLink(
                person_id=person.id,
                source_file=SOURCE2,
                source_row_number=row.source_row_number,
                match_method="exact_email",
                match_confidence=1.0,
            )
        )
        result.links += 1

    # ---- step 4: source3 by phone ----
    s3_orphans: list[RawSource3Cbnexus] = []
    for row in s3:
        phone = normalise_phone(row.phone_number)
        person = by_phone.get(phone) if phone else None
        if person is None:
            s3_orphans.append(row)
            continue

        apply_source3(person, row, result)
        session.add(
            PersonSourceLink(
                person_id=person.id,
                source_file=SOURCE3,
                source_row_number=row.source_row_number,
                match_method="exact_phone",
                match_confidence=1.0,
            )
        )
        result.links += 1

    # ---- step 5: orphans become their own people ----
    # These rows describe someone source1 never saw. They are still real
    # people, so they get a person record; they simply have fewer fields.
    #
    # The name is stored with the source's own casing, which is why
    # `MANISH BHATIA` and `DIVYA CHOPRA` appear in caps in `person` while
    # everyone else is title case. That is deliberate. Title casing here
    # would be inventing a canonical form for a name we have only seen
    # once, with no second source to arbitrate against -- and `.title()`
    # actively corrupts real names: `McDonald` becomes `Mcdonald`,
    # `van der Berg` becomes `Van Der Berg`. Preserving what the source
    # actually wrote is the honest choice. Matched people do not have this
    # problem because source1 supplies the name.
    s2_people: dict[str, Person] = {}
    for row in s2_orphans:
        person = Person(
            full_name=row.worker_name,
            primary_email=normalise_email(row.email_id),
            city=normalise_city(row.location),
            city_raw=row.location,
        )
        session.add(person)
        session.flush()
        remember_skills(person.id, apply_source2(person, row, result))
        session.add(
            PersonSourceLink(
                person_id=person.id,
                source_file=SOURCE2,
                source_row_number=row.source_row_number,
                match_method="seed",
                match_confidence=1.0,
            )
        )
        result.links += 1
        if row.worker_name:
            s2_people[row.worker_name.strip().casefold()] = person

    s3_people: dict[str, Person] = {}
    for row in s3_orphans:
        person = Person(
            full_name=row.name,
            primary_phone=normalise_phone(row.phone_number),
            city=normalise_city(row.city),
            city_raw=row.city,
        )
        session.add(person)
        session.flush()
        apply_source3(person, row, result)
        session.add(
            PersonSourceLink(
                person_id=person.id,
                source_file=SOURCE3,
                source_row_number=row.source_row_number,
                match_method="seed",
                match_confidence=1.0,
            )
        )
        result.links += 1
        if row.name:
            s3_people[row.name.strip().casefold()] = person

    # ---- step 6: name-only look-alikes, recorded but never merged ----
    # An orphan in source2 and an orphan in source3 sharing a name might be
    # one human, but nothing links them except that name. Since the data
    # contains three Arjun Mehtas and two Deepak Nairs, a name is not
    # evidence enough. Both stay separate people and the pair is recorded.
    for row2 in s2_orphans:
        if not row2.worker_name:
            continue
        key = row2.worker_name.strip().casefold()
        row3 = next(
            (r for r in s3_orphans if r.name and r.name.strip().casefold() == key),
            None,
        )
        if row3 is None:
            continue

        session.add(
            PersonReviewCandidate(
                name=row2.worker_name,
                source_a_file=SOURCE2,
                source_a_row=row2.source_row_number,
                source_b_file=SOURCE3,
                source_b_row=row3.source_row_number,
                reason=(
                    "Same name in both files, but neither row appears in "
                    f"{SOURCE1}, so there is no email or phone linking them. "
                    "Kept as two separate people pending review."
                ),
                resolution="unresolved",
            )
        )
        result.review_candidates += 1

        _log(
            result,
            SOURCE2,
            row2.source_row_number,
            "name_only_match_not_merged",
            "worker_name",
            row2.worker_name,
            f"Matches {SOURCE3} line {row3.source_row_number} on name alone. "
            "Not merged: this data holds three different Arjun Mehtas and two "
            "different Deepak Nairs, so a shared name is not evidence of a "
            "shared person. Recorded in person_review_candidate instead.",
        )

    # ---- write the collected skills ----
    for person_id, skills in skills_by_person.items():
        for skill in skills:
            session.add(PersonSkill(person_id=person_id, skill=skill))

    for issue in result.issues:
        session.add(issue)

    session.flush()
    result.people = session.scalar(select(func.count()).select_from(Person))
    return result


def main() -> int:
    engine = create_engine(f"sqlite:///{DB_PATH}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        # Idempotency: clear what this phase owns, leave staging and the
        # ingestion issue rows alone.
        session.execute(delete(PersonSkill))
        session.execute(delete(PersonSourceLink))
        session.execute(delete(PersonReviewCandidate))
        session.execute(delete(Person))
        session.execute(
            delete(DataIssue).where(DataIssue.issue_type.in_(MERGE_ISSUE_TYPES))
        )
        session.commit()

        result = merge(session)
        session.commit()

        # ---------------- summary ----------------
        print()
        print("=" * 55)
        print(f"MERGE SUMMARY  ->  {DB_PATH.name}")
        print("=" * 55)
        print(f"  people                 {result.people:>4}")
        print(f"  source links           {result.links:>4}")
        print(f"  review candidates      {result.review_candidates:>4}")
        skill_rows = session.scalar(select(func.count()).select_from(PersonSkill))
        print(f"  skill rows             {skill_rows:>4}")

        by_type: dict[str, int] = {}
        for issue in result.issues:
            by_type[issue.issue_type] = by_type.get(issue.issue_type, 0) + 1
        print()
        print(f"  data_issues from merge {sum(by_type.values()):>4}")
        for issue_type, count in sorted(by_type.items()):
            print(f"    flagged  {issue_type:<32} {count}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
