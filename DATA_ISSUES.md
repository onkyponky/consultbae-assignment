# Data issues report

Every data quality problem found across the three source files, what was done
about it, and what would have broken had it been missed.

Every value quoted here is copied from the files. Line numbers are physical
lines with the header as line 1, matching `source_row_number` in the staging
tables, so each one can be opened and checked.

## Count

**18 distinct problems found.**

| Section | Problems | Where the evidence lives |
|---|---|---|
| **A.** Structural — rows that could not be loaded | **3** | `data_issues`, severity `skipped` / `flagged` |
| **B.** Judgment calls — a decision was required | **4** kinds, 13 logged instances | `data_issues`, severity `flagged` |
| **C.** Resolved cleanly by normalisation | **11** | `src/normalise.py` + its 201 tests |

Sections A and B are the 16 rows in the `data_issues` table. Section C is the
larger half of the work and logs nothing, because none of it required a
judgment call — but it is where most of the planted problems live, and a
report built only from the issue table would omit all of it.

---

# A. Structural problems — rows that could not be loaded

Three rows never reached staging. Ingestion detects these by structure alone
and makes no attempt to interpret them.

### A1. Blank row — `source2_gig_workers.csv` line 12

```
,,,,,
```

Six empty fields. The row has the **correct field count**, so an arity check
passes it.

**Done:** skipped, logged `blank_row` / `skipped`.

**Would have broken:** a person with every column NULL enters staging, and the
merge turns it into a 61st person with no name, no email and no phone — a
phantom record that no key can ever match and that inflates every count.

### A2. Repeated header inside the data — `source3_cbnexus_contacts.csv` line 16

```
Name,Phone Number,City,Verified,Projects Completed
```

The header appears a second time in the middle of the file — the signature of
two exports concatenated.

**Done:** skipped, logged `repeated_header` / `skipped`.

**Would have broken:** a person named `Name` with the phone number
`Phone Number`. `normalise_phone` returns `None` for that, so it would have
become an unmatchable orphan person carrying column titles as personal data.

### A3. Column-rotated row — `source2_gig_workers.csv` line 20

```
"react, javascript, mysql",ISHA.CHOPRA95@MAILTEST.EXAMPLE.ORG,Isha Chopra,1406/hr,Pune,active
```

The nastiest row in the three files. Every field is present and the row has
**exactly 6 fields**, so it passes every field-count validation. It is wrong
only in its field *contents* — the values are rotated one position right, so
`skill_tags` has landed in `email_id`, the email in `worker_name`, and the
name in `rate`.

Detecting it needs a **per-column shape check** — "does `email_id` contain an
`@`?" — not an arity check.

**Done:** skipped, logged `column_shift` / `flagged`. Not repaired: rotating
left by one recovers a well-formed row, but repairing is a judgment call and
ingestion makes none. The recovered key is
`ISHA.CHOPRA95@MAILTEST.EXAMPLE.ORG`, which already exists at line 7, so the
repair would add no new person in any case.

**Would have broken:** a pandas-style forgiving parser accepts this row as
ordinary data. `email_id` would hold `react, javascript, mysql`, so the
source1↔source2 email join fails silently for it, and `rate` would hold the
string `Isha Chopra`. Because it duplicates line 7, Isha Chopra would also be
counted twice.

---

# B. Judgment calls — logged with severity `flagged`

Each of these had a defensible alternative. The raw value survives in
`data_issues` so the call can be reviewed rather than taken on trust.

### B1. Same person, name abbreviated — `source1` lines 25 and 31

```
line 25: R. Verma,     rohit.verma13@mailtest.example.org, 9000000294, Bangalore, 2.4, 6.1, 08/13/2026, "Python, React, MongoDB"
line 31: Rohit Verma,  rohit.verma13@mailtest.example.org, 9000000294, Bangalore, 2.4, 6.1, 08/13/2026, "Python, React, MongoDB"
```

Byte-identical except the name.

**Chosen:** merged into one person, canonical name `Rohit Verma`.

**Alternative rejected:** keeping `R. Verma`, or first-seen-wins. The rule
adopted is *prefer the longer form* — abbreviation loses information and
expansion invents none, so the longer string is the one that can be trusted to
contain what the shorter one had.

**Would have broken:** Rohit Verma counted as two people, and any per-person
aggregate — submissions, rates, skill counts — double-counting him.

### B2. Same person, alias email prefix — `source1` lines 27 and 37

```
line 27: Nikhil Chopra, alt.nikhil.chopra70@example.com, 09000000103, NOIDA, 0.8, 7.8, ...
line 37: Nikhil Chopra,     nikhil.chopra70@example.com, 09000000103, NOIDA, 0.8, 7.8, ...
```

Identical except an `alt.` prefix on the email.

**Chosen:** merged on **phone**; `primary_email` is the unprefixed
`nikhil.chopra70@example.com`.

**Alternative rejected:** deduplicating source1 on email. This pair is the
concrete reason that would be wrong — **the two emails genuinely differ, so
email-only matching misses this pair entirely and records two people.** Phone
is what catches it, and that is why phone is the key used to deduplicate
source1.

**Would have broken:** two Nikhil Chopras, and the `alt.` address treated as a
separate human rather than a second address for one.

### B3. Six city conflicts across sources

Real disagreements that survive normalisation — all in the Delhi cluster:

| Person | source1 | other source |
|---|---|---|
| Isha Kapoor | `new delhi` | `Delhi` (source2 line 6) |
| Meera Bhatia | `Delhi NCR` | `New Delhi` (source2 line 8) |
| Meera Bhatia | `Delhi NCR` | `Delhi` (source3 line 19) |
| Priya Saxena | `Delhi` | `New Delhi` (source3 line 4) |
| Rahul Malhotra | `new delhi` | `Delhi NCR` (source3 line 7) |
| Arjun Mishra | `Delhi` | `new delhi` (source3 line 15) |

**Chosen:** source1 wins, because it is the bridge file and the most complete
record. `person.city_raw` keeps the original text and all six disagreements
are logged with both readings.

**Alternatives rejected:** (a) collapsing `Delhi`, `New Delhi` and `Delhi NCR`
into one city — they are three different geographic scopes, not three
spellings, and merging them manufactures agreement the sources do not have;
(b) majority vote across sources, which lets two thin records outvote the
authoritative one.

**Would have broken:** collapsing them destroys the finding that the sources
disagree about six people. Keeping them apart *without* a precedence rule
would split six real people into duplicates.

### B4. Five name-only look-alikes, deliberately not merged

Five people appear in source2 and source3 but in **neither case in source1**,
so no email or phone bridges them. Only the name links them:

| Name | source2 | source3 |
|---|---|---|
| Arjun Mehta | line 18 | line 28 |
| Manish Bhatia | line 19 | line 29 |
| Divya Chopra | line 21 | line 30 |
| Karan Chopra | line 22 | line 31 |
| Vikram Mehta | line 23 | line 32 |

**Chosen:** each stays its own person; the pair is recorded in
`person_review_candidate` with `resolution='unresolved'`. Not merged, not
dropped.

**Alternative rejected:** merging on name similarity. This data contains
**three different Arjun Mehtas** (source1 line 20, source2 line 18, source3
line 28) and **two different Deepak Nairs** (`deepak.nair44@example.com` and
`deepak.nair57@example.in`). A shared name is not evidence of a shared person
here — it is the trap.

**Would have broken:** three distinct Arjun Mehtas fused into one record
carrying another person's phone and email, and the same for Deepak Nair.
Silently dropping them instead would have lost five real people.

---

# C. Problems normalisation resolved cleanly

No judgment was required for these — a rule resolves them, and the rule is
tested. Nothing is logged to `data_issues` because nothing was decided. This
is the larger half of the planted problems.

### C1. Four different date formats in one column

`source1.Applied Date` — 42 rows, four layouts:

| Layout | Count | Example |
|---|---|---|
| `DD-MM-YYYY` | 12 | `24-07-2026` |
| `YYYY-MM-DD` | 9 | `2026-08-08` |
| `MM/DD/YYYY` | 11 | `07/13/2026` |
| `D Mon YYYY` | 10 | `7 Jul 2026` |

**Would have broken:** any single `strptime` format fails on three quarters of
the column.

### C2. Eight genuinely ambiguous dates

Eight rows carry six distinct values where **both components are 12 or less**,
so swapping day and month also yields a valid but different date:

```
01-08-2026   02-06-2026   03-07-2026   07/03/2026 (3 rows)   07/12/2026   08/11/2026
```

Resolved by **separator**, and each convention is proved by a value that can
only be read one way: `24-07-2026` proves dash is `DD-MM` (there is no month
24), and `07/13/2026` proves slash is `MM/DD` (there is no month 13).

That is still an inference about upstream systems, so `parse_applied_date`
reports `ambiguous=True` and the raw string is kept in
`person.applied_date_raw`.

**Would have broken:** `07/03/2026` silently becoming 7 March instead of 3
July — an error invisible in the output, because both are valid dates.

### C3. Six phone formats across two files

| File | Shapes |
|---|---|
| source1 | `+919000000254` (12), `9000000237` (12), `09000000287` (18) |
| source3 | `9000000268` (13), `919000000231` (11), `+91-9000000131` (6) |

Normalised to the last 10 digits, which is identical across all six shapes.

**Would have broken:** the source1↔source3 join is **phone-only**. Without
normalisation the same subscriber written three ways is three people, and
almost none of the 25 phone matches are found.

### C4. Seventeen city spellings for seven cities

Raw values across all three files include `Noida`, `NOIDA`, `Noida `, `pune`,
`PUNE`, `Pune`, `Bengaluru`, `bangalore`, `Bangalore`, `GURGAON`, `Gurgaon`,
`gurugram `, `Gurugram`, `Delhi`, `new delhi`, `New Delhi`, `Delhi NCR`.

Only two genuine renames are collapsed — Bangalore/Bengaluru and
Gurgaon/Gurugram are the same municipality under former and current official
names. The Delhi cluster is deliberately **not** collapsed (see B3).

**Would have broken:** 17 city groups in any report instead of 7, and `Noida`
and `NOIDA` treated as different places.

### C5. Trailing whitespace inside values

`'Noida '` and `'gurugram '` appear with a trailing space in **all three
files**. Staging preserves them exactly; normalisation collapses whitespace.

**Would have broken:** `'Noida '` and `'Noida'` grouping separately in every
aggregate — an invisible bug, because the two look identical when printed.

### C6. Nine uppercase email addresses

`source2` carries nine fully-uppercase addresses, e.g.
`ISHA.CHOPRA95@MAILTEST.EXAMPLE.ORG`, `DEEPAK.NAIR44@EXAMPLE.COM`, against
lowercase in source1.

**Would have broken:** the source1↔source2 join is **email-only**. Without
casefolding, **9 of the 15 email matches disappear** — the single highest-cost
miss in the dataset.

### C7. Five spellings of a yes/no flag

`source3.Verified`: `Y` (5), `yes` (6), `Yes` (3), `N` (7), `No` (9).

**Would have broken:** five categories for a two-state field, or a truthiness
test treating the string `"No"` as true.

### C8. Five spellings of status, one of which is a real third state

`source2.status`: `Active` (8), `active` (5), `ACTIVE` (8), `Inactive` (6),
`paused` (3).

Four are casing variants of two states. **`paused` is not** — it is a third
state in its own right, held by three workers. It appears only in lower case,
which makes it easy to mistake for a stray spelling of `active`.

**Would have broken:** folding `paused` into `active` reports three
unavailable workers as available.

### C9. Skills in two different casings

Fifteen distinct skills; **fourteen** differ only by case between the files —
source1 writes `Web Scraping`, `REST APIs`, `FastAPI`; source2 writes
`web scraping`, `rest apis`, `fastapi`.

Matching is on the casefolded form. The display spelling is source1's, because
a blanket `.title()` would produce `Fastapi`, `Mysql` and `Rest Apis`. `n8n`
stays lowercase — that is how the product spells its name.

**Would have broken:** 29 skills instead of 15, and every skill-based grouping
split down the middle. (Once casefolded, all 15 matched people have
**identical** skill sets across the two files — there are no content conflicts
here, only casing.)

### C10. Mixed units in one salary column

`source1.Current CTC` mixes two units with **no marker distinguishing them** —
21 rows are absolute rupees, 21 are lakhs per annum:

```
absolute:  417964   332456   775670   1195422        (range 327287 .. 1195422)
LPA:       4.2      8.3      11.2     2.4            (range 2.4 .. 11.9)
```

The rule is structural, not a threshold guess: **a decimal point means LPA, a
bare integer means rupees.** The split is exactly clean — no integer below
1000, no decimal above 1000 — and reading the decimals as LPA puts them at
240,000–1,190,000, the same salary band as the integers. Reading them as
rupees instead would mean 21 people earn under 12 rupees a year.

Both `ctc_raw` and `ctc_source_unit` are stored beside the parsed number.

**Would have broken:** a five-orders-of-magnitude error in half the column. A
forgiving parser turns the whole column into floats and hides the problem
entirely — `4.2` and `417964` both become valid numbers, and the mean becomes
meaningless. Note that `4.2` also appears in the *experience* column as a
plain number of years, which is exactly why the raw value is preserved.

### C11. Mixed units in the rate column — never converted

`source2.rate` mixes two periods: 16 rows hourly (`1415/hr`, `403/hr`) and 14
monthly (`15k/month`, `72k/month`), with a `k` suffix meaning thousands that
appears only on the monthly rows.

**No conversion between hourly and monthly is performed anywhere in this
project.** Converting needs an hours-per-month figure the data does not
contain, and the obvious assumption does not survive contact with the numbers:
at 160 hours a month the monthly rows land at **94–494 per hour** while the
hourly rows sit at **330–1483 per hour**. Those two populations do not
reconcile. Either the monthly workers are part-time or the two groups are not
comparable, and the data cannot tell us which.

So `rate_amount` and `rate_period` are stored separately and any cross-period
comparison is left to whoever can supply the missing assumption.

**Would have broken:** dividing every monthly figure by a guessed 160 produces
a column that looks comparable and is not — Isha Kapoor's `15k/month` becomes
₹94/hr against peers at ₹1400/hr, an artefact of the assumption rather than a
fact about her rate.

---

## Where to see this in the database

```sql
-- the 16 logged rows, by kind and severity
SELECT severity, issue_type, COUNT(*) FROM data_issues GROUP BY severity, issue_type;

-- the five look-alikes held back from the merge
SELECT * FROM person_review_candidate;

-- raw values preserved beside their parsed forms
SELECT full_name, ctc_raw, ctc_annual_inr, ctc_source_unit,
       city, city_raw, applied_date, applied_date_raw
FROM person WHERE ctc_raw IS NOT NULL LIMIT 10;
```
