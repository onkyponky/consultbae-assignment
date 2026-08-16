"""Show where one merged person came from.

Prints the raw source rows that built a person, straight out of the staging
tables, and then the single canonical record they became. This is the
Task 1 requirement made visible: the same person appearing in multiple
files becomes ONE record.

    python src/show_person.py                 # person 1
    python src/show_person.py 17              # by id
    python src/show_person.py --name "Arjun Mehta"
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "db" / "consultbae.db"

LINE = "-" * 74

#: Which staging table and columns to show for each source file.
STAGING = {
    "source1_naukri_applicants.csv": (
        "raw_source1_naukri",
        ["full_name", "email", "phone", "city", "experience_years",
         "current_ctc", "applied_date", "skills"],
    ),
    "source2_gig_workers.csv": (
        "raw_source2_gig_workers",
        ["email_id", "worker_name", "rate", "location", "status", "skill_tags"],
    ),
    "source3_cbnexus_contacts.csv": (
        "raw_source3_cbnexus",
        ["name", "phone_number", "city", "verified", "projects_completed"],
    ),
}


def find_person(connection, person_id: int | None, name: str | None) -> list[tuple]:
    """Return the person rows to display."""
    if name:
        return connection.execute(
            "SELECT id, full_name FROM person WHERE lower(full_name) = ? ORDER BY id",
            (name.strip().lower(),),
        ).fetchall()

    return connection.execute(
        "SELECT id, full_name FROM person WHERE id = ?", (person_id,)
    ).fetchall()


def show_sources(connection, person_id: int) -> int:
    """Print every staged row that fed this person. Returns how many."""
    links = connection.execute(
        "SELECT source_file, source_row_number, match_method "
        "FROM person_source_link WHERE person_id = ? ORDER BY source_file",
        (person_id,),
    ).fetchall()

    for source_file, row_number, method in links:
        table, columns = STAGING[source_file]
        row = connection.execute(
            f"SELECT {', '.join(columns)} FROM {table} WHERE source_row_number = ?",
            (row_number,),
        ).fetchone()

        print(f"  {source_file}  line {row_number}   [matched by {method}]")
        if row is None:
            print("      (row not found in staging)")
            continue
        for column, value in zip(columns, row):
            shown = value if value not in (None, "") else "-"
            print(f"      {column:<18} {shown!r}")
        print()

    return len(links)


def show_merged(connection, person_id: int) -> None:
    """Print the single canonical record, raw values beside parsed ones."""
    row = connection.execute(
        "SELECT full_name, primary_email, primary_phone, city, city_raw, "
        "experience_years, ctc_raw, ctc_annual_inr, ctc_source_unit, "
        "rate_raw, rate_amount, rate_period, status, verified, "
        "projects_completed, applied_date, applied_date_raw, source_origin "
        "FROM person WHERE id = ?",
        (person_id,),
    ).fetchone()

    (name, email, phone, city, city_raw, years, ctc_raw, ctc, ctc_unit,
     rate_raw, rate_amount, rate_period, status, verified, projects,
     applied, applied_raw, origin) = row

    skills = connection.execute(
        "SELECT skill FROM person_skill WHERE person_id = ? ORDER BY skill",
        (person_id,),
    ).fetchall()

    print(f"  full_name          {name}")
    print(f"  primary_email      {email or '-'}")
    print(f"  primary_phone      {phone or '-'}")
    print(f"  city               {city or '-':<24} raw {city_raw!r}")
    print(f"  experience_years   {years if years is not None else '-'}")
    print(f"  ctc_annual_inr     {str(ctc or '-'):<24} raw {ctc_raw!r}  unit {ctc_unit}")
    print(f"  rate               {str(rate_amount or '-'):<24} raw {rate_raw!r}  period {rate_period}")
    print(f"  status             {status or '-'}")
    print(f"  verified           {verified if verified is not None else '-'}")
    print(f"  projects_completed {projects if projects is not None else '-'}")
    print(f"  applied_date       {str(applied or '-'):<24} raw {applied_raw!r}")
    print(f"  source_origin      {origin}")
    print(f"  skills             {' | '.join(s[0] for s in skills) or '-'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("person_id", nargs="?", type=int, default=1)
    parser.add_argument("--name", help="show everyone with this exact name")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"No database at {DB_PATH}. Run ingest.py and merge.py first.")
        return 1

    connection = sqlite3.connect(DB_PATH)
    people = find_person(connection, args.person_id, args.name)

    if not people:
        print("No such person.")
        return 1

    if len(people) > 1:
        print()
        print(f"  {len(people)} DIFFERENT people share the name {people[0][1]!r}.")
        print("  They were never merged, because a name is not a key.")

    for person_id, name in people:
        print()
        print("=" * 74)
        print(f"  PERSON #{person_id}   {name}")
        print("=" * 74)
        print()
        print("  SOURCE ROWS THAT BUILT THIS RECORD")
        print(LINE)
        count = show_sources(connection, person_id)
        print(f"  MERGED FROM {count} SOURCE ROW(S) INTO ONE RECORD")
        print(LINE)
        show_merged(connection, person_id)
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
