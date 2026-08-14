"""Phase 1 ingestion: load all three CSVs into staging exactly as-is.

This script makes no judgment calls. It does not clean, convert, match or
deduplicate. Its only decisions are structural: a row either reaches
staging verbatim, or it is skipped and logged to `data_issues` with the
raw line that caused it.

Read with the stdlib `csv` module on purpose. A forgiving parser (pandas)
would dtype-guess the mixed-unit CTC column into floats and would accept
the rotated source2 row as valid data, which is precisely what we need to
see rather than have handled for us.

Re-running is safe: staging tables and `data_issues` are cleared first, so
the result is a function of the CSVs alone.

    python src/ingest.py
"""

from __future__ import annotations

import csv
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from models import (
    Base,
    DataIssue,
    RawSource1Naukri,
    RawSource2GigWorkers,
    RawSource3Cbnexus,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DB_PATH = REPO_ROOT / "db" / "consultbae.db"


@dataclass
class SourceSpec:
    """How to read one source file into one staging table."""

    filename: str
    model: type
    #: (header as it appears in the file, staging column name)
    columns: list[tuple[str, str]]
    #: Index of the column whose *shape* identifies a well-formed row.
    key_index: int
    #: Returns True if the key column looks like what it claims to be.
    key_check: Callable[[str], bool]
    #: Human-readable form of `key_check`, used in `action_taken`.
    key_expectation: str

    @property
    def headers(self) -> list[str]:
        return [h for h, _ in self.columns]

    @property
    def attrs(self) -> list[str]:
        return [a for _, a in self.columns]


def _looks_like_email(value: str) -> bool:
    return "@" in value


def _looks_like_phone(value: str) -> bool:
    return any(ch.isdigit() for ch in value)


SOURCES: list[SourceSpec] = [
    SourceSpec(
        filename="source1_naukri_applicants.csv",
        model=RawSource1Naukri,
        columns=[
            ("Full Name", "full_name"),
            ("Email", "email"),
            ("Phone", "phone"),
            ("City", "city"),
            ("Experience (Years)", "experience_years"),
            ("Current CTC", "current_ctc"),
            ("Applied Date", "applied_date"),
            ("Skills", "skills"),
        ],
        key_index=1,
        key_check=_looks_like_email,
        key_expectation="the Email column to contain '@'",
    ),
    SourceSpec(
        filename="source2_gig_workers.csv",
        model=RawSource2GigWorkers,
        columns=[
            ("email_id", "email_id"),
            ("worker_name", "worker_name"),
            ("rate", "rate"),
            ("location", "location"),
            ("status", "status"),
            ("skill_tags", "skill_tags"),
        ],
        key_index=0,
        key_check=_looks_like_email,
        key_expectation="the email_id column to contain '@'",
    ),
    SourceSpec(
        filename="source3_cbnexus_contacts.csv",
        model=RawSource3Cbnexus,
        columns=[
            ("Name", "name"),
            ("Phone Number", "phone_number"),
            ("City", "city"),
            ("Verified", "verified"),
            ("Projects Completed", "projects_completed"),
        ],
        key_index=1,
        key_check=_looks_like_phone,
        key_expectation="the Phone Number column to contain a digit",
    ),
]


@dataclass
class FileResult:
    filename: str
    read: int = 0
    loaded: int = 0
    skipped: int = 0
    issues: list[tuple[str, str]] = field(default_factory=list)  # (type, severity)


def _rotate_left(row: list[str]) -> list[str]:
    """Undo a one-position right rotation."""
    return row[1:] + row[:1]


def ingest_file(session: Session, spec: SourceSpec) -> FileResult:
    path = DATA_DIR / spec.filename
    result = FileResult(filename=spec.filename)

    if not path.exists():
        raise FileNotFoundError(f"Missing source file: {path}")

    def log(
        line_no: int | None,
        issue_type: str,
        column_name: str | None,
        raw_value: str,
        action: str,
        severity: str,
    ) -> None:
        session.add(
            DataIssue(
                source_file=spec.filename,
                row_number=line_no,
                issue_type=issue_type,
                column_name=column_name,
                raw_value=raw_value,
                action_taken=action,
                severity=severity,
            )
        )
        result.issues.append((issue_type, severity))

    # Key value -> physical line, for rows already staged from this file.
    # Used to report that a malformed row duplicates an existing one.
    staged_keys: dict[str, int] = {}

    # utf-8-sig so a BOM from an Excel export does not end up inside the
    # first header name.
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)

        try:
            header = next(reader)
        except StopIteration:
            raise ValueError(f"{spec.filename} is empty")

        if header != spec.headers:
            raise ValueError(
                f"{spec.filename} header does not match the expected schema.\n"
                f"  expected: {spec.headers}\n"
                f"  found:    {header}"
            )

        for row in reader:
            # reader.line_num is the physical line in the file, header = 1.
            line_no = reader.line_num
            result.read += 1
            raw = ",".join(row)

            # 1. Blank row -- structurally valid CSV, semantically empty.
            if not any(cell.strip() for cell in row):
                result.skipped += 1
                log(
                    line_no,
                    "blank_row",
                    None,
                    raw,
                    "Row skipped. Every field is empty, so there is nothing "
                    "to stage.",
                    "skipped",
                )
                continue

            # 2. A second copy of the header pasted into the data. Two
            #    exports concatenated.
            if row == header:
                result.skipped += 1
                log(
                    line_no,
                    "repeated_header",
                    None,
                    raw,
                    "Row skipped. This is a duplicate of the header row "
                    "appearing inside the data, not a person.",
                    "skipped",
                )
                continue

            # 3. Wrong number of fields -- genuinely unparseable.
            if len(row) != len(header):
                result.skipped += 1
                log(
                    line_no,
                    "wrong_column_count",
                    None,
                    raw,
                    f"Row skipped. Expected {len(header)} fields, found "
                    f"{len(row)}. Cannot align fields to columns without "
                    "guessing.",
                    "skipped",
                )
                continue

            # 4. Right field count, wrong field contents. A rotated row has
            #    a valid arity, so only a per-column shape check finds it.
            key_value = row[spec.key_index]
            if not spec.key_check(key_value):
                result.skipped += 1
                key_header = spec.headers[spec.key_index]

                action = (
                    f"Row skipped, not repaired. Expected {spec.key_expectation}, "
                    f"found {key_value!r}. Fields appear rotated one position "
                    "right."
                )

                rotated = _rotate_left(row)
                if spec.key_check(rotated[spec.key_index]):
                    recovered = rotated[spec.key_index]
                    action += (
                        " Rotating left by one recovers a well-formed row, so "
                        "this is repairable."
                    )
                    duplicate_of = staged_keys.get(recovered.strip().lower())
                    if duplicate_of is not None:
                        action += (
                            f" The recovered key {recovered!r} already exists at "
                            f"line {duplicate_of}, so repairing this row would "
                            "add no new person."
                        )
                action += (
                    " Deferred to Phase 2: Phase 1 makes no judgment calls."
                )

                log(
                    line_no,
                    "column_shift",
                    key_header,
                    raw,
                    action,
                    "flagged",
                )
                continue

            # Row is structurally sound. Stage it verbatim -- no strip(), no
            # case folding, no type conversion. Trailing whitespace in
            # 'Noida ' is a real finding and must survive to Phase 2.
            session.add(
                spec.model(
                    source_row_number=line_no,
                    **dict(zip(spec.attrs, row)),
                )
            )
            staged_keys.setdefault(key_value.strip().lower(), line_no)
            result.loaded += 1

    return result


def main() -> int:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{DB_PATH}")
    Base.metadata.create_all(engine)

    results: list[FileResult] = []

    with Session(engine) as session:
        # Idempotency: clear staging and the issue log so a re-run produces
        # the same database. Canonical tables are left alone -- ingestion
        # does not own them.
        for spec in SOURCES:
            session.execute(delete(spec.model))
        session.execute(delete(DataIssue))
        session.commit()

        for spec in SOURCES:
            results.append(ingest_file(session, spec))
        session.commit()

    # ---------------- summary ----------------
    width = max(len(r.filename) for r in results)
    print()
    print("=" * (width + 34))
    print(f"INGESTION SUMMARY  ->  {DB_PATH.relative_to(REPO_ROOT)}")
    print("=" * (width + 34))
    print(f"{'file':<{width}}  {'read':>6} {'loaded':>7} {'skipped':>8}")
    print("-" * (width + 34))
    for r in results:
        print(f"{r.filename:<{width}}  {r.read:>6} {r.loaded:>7} {r.skipped:>8}")
    print("-" * (width + 34))
    totals = (
        sum(r.read for r in results),
        sum(r.loaded for r in results),
        sum(r.skipped for r in results),
    )
    print(f"{'TOTAL':<{width}}  {totals[0]:>6} {totals[1]:>7} {totals[2]:>8}")

    all_issues = [i for r in results for i in r.issues]
    if all_issues:
        print()
        print(f"data_issues logged: {len(all_issues)}")
        by_kind: dict[tuple[str, str], int] = {}
        for kind in all_issues:
            by_kind[kind] = by_kind.get(kind, 0) + 1
        for (issue_type, severity), count in sorted(by_kind.items()):
            print(f"  {severity:<9} {issue_type:<20} {count}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())