"""Generate a different incoming CSV for the n8n duplicate check every run.

Real people are drawn from the three source files and rewritten into the
messy shapes those files actually use -- `+919000000254`, `09000000287`,
`+91-9000000131`, uppercase emails, and so on -- then mixed with invented
people who are not in the database. The result is shuffled, so each
execution of the n8n flow sees different data in a different order.

The expected outcome for every row is **computed**, not assumed: each
generated row is run through the same `normalise_phone` and
`normalise_email` the merge used, and looked up against `person`. If the
generator ever produced a row whose outcome it could not predict, the log
would say so rather than quietly claiming a number.

Every run appends a record to `n8n/batch_log.md`.

    python n8n/make_batch.py
    python n8n/make_batch.py --known 12 --new 4
    python n8n/make_batch.py --seed 7        # reproducible
"""

from __future__ import annotations

import argparse
import csv
import random
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from normalise import normalise_email, normalise_phone  # noqa: E402

DATA_DIR = REPO_ROOT / "data"
DB_PATH = REPO_ROOT / "db" / "consultbae.db"
OUTPUT_PATH = REPO_ROOT / "n8n" / "incoming_batch.csv"
LOG_PATH = REPO_ROOT / "n8n" / "batch_log.md"

#: (filename, column count, name index, email index, phone index)
#: A -1 means the file does not carry that column.
SOURCES = [
    ("source1_naukri_applicants.csv", 8, 0, 1, 2),
    ("source2_gig_workers.csv", 6, 1, 0, -1),
    ("source3_cbnexus_contacts.csv", 5, 0, -1, 1),
]

FIRST_NAMES = ["Ananya", "Rohan", "Meghna", "Kabir", "Ishaan", "Nandini",
               "Aditya", "Sanya", "Vivaan", "Tara", "Dhruv", "Aarohi"]
LAST_NAMES = ["Rao", "Desai", "Iyer", "Sethi", "Bakshi", "Menon",
              "Chatterjee", "Kulkarni", "Pillai", "Ahuja"]


def load_real_people() -> list[dict]:
    """Read the three CSVs, keeping only rows that would reach staging.

    Applies the same three structural filters `ingest.py` uses: right field
    count, not blank, and a key column that holds what it claims to. So the
    generator never emits the rotated row or the repeated header.
    """
    people: list[dict] = []

    for filename, width, name_i, email_i, phone_i in SOURCES:
        path = DATA_DIR / filename
        lines = path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
        reader = csv.reader(lines)
        header = next(reader)

        for row in reader:
            if len(row) != width or not any(cell.strip() for cell in row):
                continue
            if row == header:
                continue

            email = row[email_i] if email_i >= 0 else ""
            phone = row[phone_i] if phone_i >= 0 else ""

            # The same key-shape check ingestion uses.
            if email_i >= 0 and "@" not in email:
                continue
            if phone_i >= 0 and not any(c.isdigit() for c in phone):
                continue

            people.append(
                {"name": row[name_i].strip(), "email": email.strip(),
                 "phone": phone.strip(), "source": filename}
            )

    return people


def rewrite_phone(raw: str, rng: random.Random) -> str:
    """Re-express a phone in one of the six shapes the files actually use."""
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) < 10:
        return raw
    last10 = digits[-10:]

    shape = rng.choice(["bare", "trunk", "plus91", "cc91", "plus91dash"])
    if shape == "bare":
        return last10
    if shape == "trunk":
        return "0" + last10
    if shape == "plus91":
        return "+91" + last10
    if shape == "cc91":
        return "91" + last10
    return "+91-" + last10


def rewrite_email(raw: str, rng: random.Random) -> str:
    """Vary the casing, the way source2 does."""
    if not raw:
        return ""
    return rng.choice([raw, raw.upper(), raw.lower()])


def invent_person(rng: random.Random, used_phones: set[str]) -> dict:
    """Make up someone who is definitely not in the database."""
    name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"

    # Keep well clear of the 90000001xx-90000002xx block the real data uses.
    while True:
        phone = "9" + str(rng.randint(100000000, 899999999)).zfill(9)
        if phone not in used_phones:
            break

    handle = name.lower().replace(" ", ".")
    return {"name": name, "email": f"{handle}@example.com", "phone": phone}


def drop_one_key(person: dict, rng: random.Random) -> dict:
    """Sometimes blank the email or the phone, never both.

    A row with only one key is the interesting case: it forces the match to
    happen on that key alone, which is what proves normalisation is doing
    the work rather than a lucky string comparison.
    """
    if not person["email"]:
        return person
    if not person["phone"]:
        return person

    choice = rng.choice(["keep", "keep", "drop_email", "drop_phone"])
    if choice == "drop_email":
        return {**person, "email": ""}
    if choice == "drop_phone":
        return {**person, "phone": ""}
    return person


def expected_outcome(connection, email: str, phone: str) -> tuple[str, str]:
    """Work out what the lookup endpoint will say about this row.

    Mirrors the endpoint exactly: normalise, try phone first, then email.
    """
    phone_key = normalise_phone(phone) if phone else None
    email_key = normalise_email(email) if email else None

    if phone_key:
        row = connection.execute(
            "SELECT id, full_name FROM person WHERE primary_phone = ? ORDER BY id",
            (phone_key,),
        ).fetchone()
        if row:
            return "duplicate", f"phone -> #{row[0]} {row[1]}"

    if email_key:
        row = connection.execute(
            "SELECT id, full_name FROM person WHERE primary_email = ? ORDER BY id",
            (email_key,),
        ).fetchone()
        if row:
            return "duplicate", f"email -> #{row[0]} {row[1]}"

    if phone_key is None and email_key is None:
        return "new", "no usable key"
    return "new", "not in database"


def write_log(rows: list[dict], outcomes: list[tuple[str, str]], seed: int) -> None:
    """Append a record of this run, so every generated batch is traceable."""
    duplicates = sum(1 for status, _ in outcomes if status == "duplicate")

    with LOG_PATH.open("a", encoding="utf-8") as handle:
        if LOG_PATH.stat().st_size == 0:
            handle.write("# Generated batch log\n\n")
            handle.write(
                "One entry per run of `make_batch.py`. Expected outcomes are\n"
                "computed against the `person` table, not assumed.\n\n"
            )

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        handle.write(f"## {stamp}  (seed {seed})\n\n")
        handle.write(
            f"{len(rows)} rows: **{duplicates} already in the database**, "
            f"{len(rows) - duplicates} new.\n\n"
        )
        handle.write("| # | name | email | phone | expected | matched by |\n")
        handle.write("|---|---|---|---|---|---|\n")
        for i, (row, (status, detail)) in enumerate(zip(rows, outcomes), start=1):
            handle.write(
                f"| {i} | {row['name']} | {row['email'] or '-'} | "
                f"{row['phone'] or '-'} | {status} | {detail} |\n"
            )
        handle.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--known", type=int, default=10,
                        help="how many real people to include")
    parser.add_argument("--new", type=int, default=4,
                        help="how many invented people to include")
    parser.add_argument("--seed", type=int, default=None,
                        help="fix the seed to reproduce a batch")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"No database at {DB_PATH}. Run ingest.py and merge.py first.")
        return 1

    seed = args.seed if args.seed is not None else random.randrange(1, 10_000)
    rng = random.Random(seed)

    real = load_real_people()
    if len(real) < args.known:
        print(f"Only {len(real)} usable rows in the source files.")
        return 1

    rows: list[dict] = []
    for person in rng.sample(real, args.known):
        varied = {
            "name": person["name"],
            "email": rewrite_email(person["email"], rng),
            "phone": rewrite_phone(person["phone"], rng) if person["phone"] else "",
        }
        rows.append(drop_one_key(varied, rng))

    used = {r["phone"] for r in rows}
    for _ in range(args.new):
        rows.append(drop_one_key(invent_person(rng, used), rng))

    rng.shuffle(rows)

    connection = sqlite3.connect(DB_PATH)
    outcomes = [expected_outcome(connection, r["email"], r["phone"]) for r in rows]

    with OUTPUT_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["name", "email", "phone"])
        writer.writeheader()
        writer.writerows(rows)

    write_log(rows, outcomes, seed)

    duplicates = sum(1 for status, _ in outcomes if status == "duplicate")
    print(f"Wrote {len(rows)} rows to {OUTPUT_PATH.relative_to(REPO_ROOT)}  (seed {seed})")
    print(f"  expected: {duplicates} already known, {len(rows) - duplicates} new")
    print(f"  recorded in {LOG_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
