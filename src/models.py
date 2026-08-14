"""SQLAlchemy schema for the merged ConsultBae database.

Three rules the rest of the pipeline depends on:

1. Staging tables store every original value as TEXT, untouched. Ingestion
   must never fail on bad data and must never guess at a type. `4.2` and
   `417964` both land verbatim so the CTC unit question stays open until
   Phase 2 decides it, rather than being silently resolved by a parser.

2. `source_row_number` is the physical line number in the source file with
   the header as line 1, so every logged issue points at a line you can
   open and look at.

3. Where producing a clean value required an assumption, the raw form is
   stored beside the normalised one -- see `Person.ctc_*`, `Person.rate_*`
   and `Person.city_raw`. The reviewer should be able to see the
   assumption, not have to infer it from a single derived number.
"""

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------
# Staging: one table per source. Every column TEXT, no conversion.
# --------------------------------------------------------------------------
# Separate tables rather than one wide union table: the three sources'
# columns genuinely do not correspond to each other (source2 has no phone,
# source3 has no email), so a union schema would be mostly NULL and would
# imply a mapping between columns that does not exist.


class RawSource1Naukri(Base):
    """source1_naukri_applicants.csv -- the bridge file (has email AND phone)."""

    __tablename__ = "raw_source1_naukri"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_row_number: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    full_name: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    experience_years: Mapped[str | None] = mapped_column(Text)
    current_ctc: Mapped[str | None] = mapped_column(Text)
    applied_date: Mapped[str | None] = mapped_column(Text)
    skills: Mapped[str | None] = mapped_column(Text)

    ingested_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class RawSource2GigWorkers(Base):
    """source2_gig_workers.csv -- email only, no phone. Joins source1 on email."""

    __tablename__ = "raw_source2_gig_workers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_row_number: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    email_id: Mapped[str | None] = mapped_column(Text)
    worker_name: Mapped[str | None] = mapped_column(Text)
    rate: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    skill_tags: Mapped[str | None] = mapped_column(Text)

    ingested_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class RawSource3Cbnexus(Base):
    """source3_cbnexus_contacts.csv -- phone only, no email. Joins source1 on phone."""

    __tablename__ = "raw_source3_cbnexus"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_row_number: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    name: Mapped[str | None] = mapped_column(Text)
    phone_number: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    verified: Mapped[str | None] = mapped_column(Text)
    projects_completed: Mapped[str | None] = mapped_column(Text)

    ingested_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# --------------------------------------------------------------------------
# Canonical
# --------------------------------------------------------------------------


class Person(Base):
    """One row per real human. Populated in Phase 2, not by ingestion."""

    __tablename__ = "person"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    full_name: Mapped[str | None] = mapped_column(Text)
    primary_email: Mapped[str | None] = mapped_column(Text, index=True)
    primary_phone: Mapped[str | None] = mapped_column(Text, index=True)

    # City: canonical form plus the raw form it was derived from, so the six
    # Delhi-cluster conflicts (Delhi / New Delhi / Delhi NCR) stay visible
    # instead of being flattened by the alias map. Precedence rule lands in
    # Phase 2.
    city: Mapped[str | None] = mapped_column(Text)
    city_raw: Mapped[str | None] = mapped_column(Text)

    experience_years: Mapped[float | None] = mapped_column(Float)

    # CTC: source1 mixes two units in one column -- 21 rows are absolute
    # rupees (`417964`), 21 are lakhs per annum (`4.2`). Storing only the
    # normalised number would bury which unit each row arrived in.
    ctc_raw: Mapped[str | None] = mapped_column(Text)
    ctc_annual_inr: Mapped[int | None] = mapped_column(Integer)
    ctc_source_unit: Mapped[str | None] = mapped_column(Text)

    # Rate: source2 mixes `1415/hr` and `15k/month`. Converting monthly to
    # hourly needs an hours-per-month assumption that the data does not
    # support -- at 160h/month the two populations do not reconcile
    # (hourly rows land at 330-1483/hr, monthly rows at 94-494/hr). So the
    # period is preserved and no cross-period conversion is stored.
    rate_raw: Mapped[str | None] = mapped_column(Text)
    rate_amount: Mapped[float | None] = mapped_column(Float)
    rate_period: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str | None] = mapped_column(Text)
    verified: Mapped[bool | None] = mapped_column(Boolean)
    projects_completed: Mapped[int | None] = mapped_column(Integer)
    applied_date: Mapped[date | None] = mapped_column(Date)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=func.now())

    __table_args__ = (
        CheckConstraint(
            "ctc_source_unit IS NULL OR ctc_source_unit IN ('absolute', 'lpa')",
            name="ck_person_ctc_source_unit",
        ),
        CheckConstraint(
            "rate_period IS NULL OR rate_period IN ('hourly', 'monthly')",
            name="ck_person_rate_period",
        ),
    )


class PersonSkill(Base):
    """Skills are multi-valued; a comma-joined string is not a schema.

    Canonical casing lives here -- source1 writes `Web Scraping`, source2
    writes `web scraping`, and for all 15 matched people the skill sets are
    identical once casefolded.
    """

    __tablename__ = "person_skill"

    person_id: Mapped[int] = mapped_column(
        ForeignKey("person.id", ondelete="CASCADE"), primary_key=True
    )
    skill: Mapped[str] = mapped_column(Text, primary_key=True)


class PersonSourceLink(Base):
    """Maps each merged person back to the exact source rows that built it."""

    __tablename__ = "person_source_link"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("person.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_file: Mapped[str] = mapped_column(Text, nullable=False)
    source_row_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # exact_email | exact_phone | alias_email | name_city | manual
    match_method: Mapped[str] = mapped_column(Text, nullable=False)
    # 1.0 for an exact key match, down to 0.5 for a name-only match.
    match_confidence: Mapped[float] = mapped_column(Float, nullable=False)

    linked_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        # A source row belongs to exactly one person. This makes
        # double-counting a constraint violation rather than a silent bug.
        UniqueConstraint(
            "source_file", "source_row_number", name="uq_person_source_link_row"
        ),
    )


class PersonReviewCandidate(Base):
    """Name-only pairs that were deliberately NOT merged.

    Five people appear in source2 and source3 but not in source1, so no
    email/phone bridge exists and only the name links them. Three distinct
    "Arjun Mehta" records prove name matching is unsafe here. These pairs
    are recorded rather than merged or dropped, so the decision is evidence
    rather than an omission.
    """

    __tablename__ = "person_review_candidate"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    name: Mapped[str] = mapped_column(Text, nullable=False)
    source_a_file: Mapped[str] = mapped_column(Text, nullable=False)
    source_a_row: Mapped[int] = mapped_column(Integer, nullable=False)
    source_b_file: Mapped[str] = mapped_column(Text, nullable=False)
    source_b_row: Mapped[int] = mapped_column(Integer, nullable=False)

    reason: Mapped[str] = mapped_column(Text, nullable=False)
    resolution: Mapped[str] = mapped_column(
        Text, nullable=False, default="unresolved", server_default="unresolved"
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DataIssue(Base):
    """Every data quality problem found, and what was done about it."""

    __tablename__ = "data_issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    source_file: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    row_number: Mapped[int | None] = mapped_column(Integer)
    issue_type: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    # NULL for row-level issues (blank row, repeated header) that do not
    # belong to any single column.
    column_name: Mapped[str | None] = mapped_column(Text)
    raw_value: Mapped[str | None] = mapped_column(Text)
    action_taken: Mapped[str] = mapped_column(Text, nullable=False)

    # skipped  -- the row never reached staging
    # repaired -- a value was changed
    # flagged  -- a judgment call was made and the raw value survives
    severity: Mapped[str] = mapped_column(Text, nullable=False, index=True)

    detected_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "severity IN ('skipped', 'repaired', 'flagged')",
            name="ck_data_issues_severity",
        ),
    )