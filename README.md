# ConsultBae — AI Automation Assignment

Three CSV exports of overlapping people, from three different systems, merged
into one clean database — plus an n8n automation and an audio collection app
built on top of it.

The source data is deliberately messy. Finding and handling the problems is as
much the deliverable as the pipeline, so the full write-up lives in
**[DATA_ISSUES.md](DATA_ISSUES.md) — 18 distinct problems found**, with what
was done about each and what would have broken had it been missed.

| | |
|---|---|
| **Data issues report** | **[DATA_ISSUES.md](DATA_ISSUES.md)** |
| **Stuck log** | **[STUCK_LOG.md](STUCK_LOG.md)** |
| **Video walkthrough** | _link to be added_ |

- [x] Phase 1 — repo, schema, raw ingestion
- [x] Phase 2 — normalisation, matching, merged person table
- [x] Phase 3 — audio collection app
- [x] Phase 4 — n8n duplicate-check flow
- [ ] Phase 5 — video

---

## What this is

Three files, no shared ID, overlapping people:

| File | Columns | Keys it carries |
|---|---|---|
| `source1_naukri_applicants.csv` | 8 | **email and phone** |
| `source2_gig_workers.csv` | 6 | email only |
| `source3_cbnexus_contacts.csv` | 5 | phone only |

**105 rows read → 102 staged → 60 people.**

Three deliverables sit on that database: a merge pipeline, an n8n flow that
checks an incoming CSV against it for duplicates, and a web app that collects
audio recordings and attaches them to the same people.

---

## Setup from a clean clone

**Prerequisites:** Python 3.14 and **ffmpeg** (which provides `ffprobe`). The
audio app shells out to both — there is no Python audio library in this
project.

```bash
# ffmpeg — must be on PATH
winget install Gyan.FFmpeg          # Windows
# brew install ffmpeg               # macOS
# sudo apt install ffmpeg           # Debian/Ubuntu

ffprobe -version                    # verify before continuing
```

```bash
git clone <repo-url>
cd consultbae-assignment

python -m venv .venv
.venv\Scripts\activate              # Windows
# source .venv/bin/activate         # macOS/Linux

pip install -r requirements.txt
```

---

## Running the three parts

### 1. The pipeline

```bash
python src/ingest.py     # load all 3 CSVs into staging, verbatim
python src/merge.py      # normalise, match, build the 60 canonical people
```

`ingest.py` prints rows read / loaded / skipped per file; `merge.py` prints
people, links, review candidates and logged issues. Both are **idempotent** —
they clear what they own first, so the database is a pure function of the CSVs
and can be rebuilt at any time.

```bash
python -m pytest tests/ -q          # 253 tests
```

### 2. The audio collection app

```bash
python -m uvicorn app:app --app-dir src --port 8000
```

- `http://127.0.0.1:8000` — enter name and phone, **record in the browser or
  upload a file**, submit
- `http://127.0.0.1:8000/submissions` — every submission with a play button,
  duration, sample rate in kHz, bitrate, loudness in dB and LUFS, and a rough
  noise estimate

Try phone `+91-9000000254` to attach to a person the merge already built, and
any unused number to watch the app create one.

### 3. The n8n duplicate-check flow

The flow calls a **read-only** lookup endpoint on the app, so the app must be
running and bound to `0.0.0.0` — n8n runs in a container and cannot reach a
localhost-only bind:

```bash
python -m uvicorn app:app --app-dir src --host 0.0.0.0 --port 8000
docker run -it --rm -p 5678:5678 -v n8n_data:/home/node/.n8n n8nio/n8n
```

Open `http://localhost:5678`, import [`n8n/workflow.json`](n8n/workflow.json),
click **Execute workflow**, then post the test CSV:

```bash
curl.exe -F "file=@n8n/test_incoming.csv" http://localhost:5678/webhook-test/duplicate-check
```

[`n8n/test_incoming.csv`](n8n/test_incoming.csv) contains a deliberate mix: two
exact duplicates, one row matching **only after phone normalisation**
(`+91-9000000287`, no email), and two genuinely new people. Expected response:
`3 of 5 incoming rows already exist in the database.`

The endpoint answers one question — *does this person exist?* The CSV parsing,
the per-row loop, the branch and the alert are all n8n nodes. Deliberately:
the brief scores a pure-code solution zero.

---

## Architecture

```
data/*.csv  ->  raw_source{1,2,3}_*  ->  person + person_skill
 (verbatim)      (TEXT, untouched)        (canonical, typed)
                        |                        |
                        +--> data_issues         +--> person_source_link
                             (every decision)         (traces back to rows)
                                                 +--> person_review_candidate
                                                 +--> audio_submission
```

**Staging is inviolate.** Every staging column is `TEXT` and holds the original
value — no strip, no casefold, no type conversion. Trailing whitespace in
`'Noida '` and the string `'4.2'` are *evidence*; if ingestion normalised them,
the analysis depending on them would not be defensible. A row either lands
verbatim or is skipped and logged.

**Raw values survive beside normalised ones** wherever producing the clean
value required an assumption: `ctc_raw` / `ctc_source_unit`, `rate_raw` /
`rate_period`, `city_raw`, `applied_date_raw`, `bitrate_is_derived`.

**`person_source_link` has `UNIQUE(source_file, source_row_number)`**, so each
source row belongs to exactly one person and double-counting is a constraint
violation rather than a silent bug.

### Why SQLite with SQLAlchemy

SQLite because the whole database is a single file that rebuilds from the CSVs
in under a second, with no server to install before a reviewer can run
anything. The dataset is 102 rows — a database server would be infrastructure
without a purpose.

SQLAlchemy because the schema is the argument. Declared models put the
`CHECK` constraints (`severity IN ('skipped','repaired','flagged')`), the
`UNIQUE` link constraint and the foreign keys in one readable file, and the
same models run the app and the tests. Nothing here depends on SQLite
specifically — the same models would move to Postgres by changing the URL.

### Why the stdlib `csv` module and not pandas

A forgiving parser dtype-guesses `Current CTC` into floats, hiding that the
column mixes absolute rupees (`417964`) with lakhs per annum (`4.2`). It also
accepts the rotated row at `source2_gig_workers.csv:20` as ordinary data,
because that row has a valid field count and is wrong only in its field
*contents*. Both are findings that had to surface, not be handled for us.

---

## Matching logic

No single ID is common to all three files. **Source 1 is the bridge** — it is
the only file carrying both an email and a phone.

```
source1  <--email-->  source2      15 of 30 rows match
source1  <--phone-->  source3      25 of 30 rows match
source2  <-- ??? -->  source3      no shared key at all
```

The pipeline runs in this order, and the order is load-bearing:

1. **Deduplicate source1 against itself first.** It is the bridge; if one
   person is still two rows when the other files attach, both copies collect
   links and the error propagates into everything downstream. 42 rows → 40
   people.
2. Seed `person` from those 40.
3. Attach source2 on normalised email, source3 on normalised phone.
4. Rows matching nothing become their own person (+15, +5 = **60**).
5. Name-only look-alikes go to `person_review_candidate`, never merged.

Every link records a `match_method` and `match_confidence`, so any merge can be
defended after the fact.

### Why phone, not email, deduplicates source1

Source 1 contains two duplicate pairs, and they are **caught by different
keys**:

```
line 25  R. Verma       rohit.verma13@mailtest.example.org   9000000294
line 31  Rohit Verma    rohit.verma13@mailtest.example.org   9000000294   <- same email AND phone

line 27  Nikhil Chopra  alt.nikhil.chopra70@example.com      09000000103
line 37  Nikhil Chopra      nikhil.chopra70@example.com      09000000103   <- same phone, DIFFERENT email
```

The Verma pair is found by either key. **The Chopra pair is found only by
phone** — the `alt.` prefix makes the two emails genuinely different strings,
so deduplicating on email leaves that pair as two people and every later join
inherits the split. Phone catches both pairs; email catches one.

### Why names are never a key

The data contains **three different Arjun Mehtas** and **two different Deepak
Nairs**. Matching on name similarity fuses distinct people into one record
carrying someone else's contact details. Name-only look-alikes are recorded in
`person_review_candidate` with `resolution='unresolved'` — not merged, not
dropped, and visible as a decision rather than an omission.

---

## Repo layout

```
data/       the three source CSVs, as received, never edited in place
src/        ingest.py  merge.py  normalise.py  audio_meta.py  app.py  models.py
tests/      253 tests
n8n/        workflow.json + test_incoming.csv
db/         SQLite file (generated, git-ignored)
uploads/    submitted audio (git-ignored)
```
